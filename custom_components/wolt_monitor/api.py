"""Normalized Wolt data boundary and fixed-origin HTTP adapter."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import asin, cos, isfinite, radians, sin, sqrt
from typing import Protocol

from .errors import Failure
from .state import CompletionResult, CourierSnapshot, OrderSnapshot, coordinates
from .transport import UnifiedClient


@dataclass(frozen=True)
class OrderResult:
    snapshot: OrderSnapshot | None = field(repr=False)
    courier_eligible: bool = False
    courier_supported: bool = False
    retired_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OrderListResult:
    orders: tuple[OrderSnapshot, ...] = field(repr=False)
    courier_eligible: bool = False
    courier_supported: bool = False
    per_order: tuple[OrderResult, ...] = field(default=(), repr=False)

    def for_order(self, snapshot):
        if snapshot is None:
            return OrderResult(None)
        for result in self.per_order:
            if result.snapshot.key == snapshot.key:
                return result
        return OrderResult(snapshot, self.courier_eligible, self.courier_supported)


@dataclass(frozen=True)
class Selection:
    snapshot: OrderSnapshot | None = field(repr=False)
    details_required: bool = False


def select_order(orders, *, previous=None, completed=False, excluded=frozenset()) -> Selection:
    """Stable list fallback; no ID or payment-time chronology inference."""
    candidates = [
        order
        for order in orders
        if not order.terminal
        and order.key not in excluded
        and not (completed and previous is not None and order.key == previous.key)
    ]
    if previous is not None and not completed:
        for order in orders:
            if order.key == previous.key:
                return Selection(order)
        return Selection(previous, details_required=True)
    if candidates:
        return Selection(candidates[0])
    if previous is not None:
        return Selection(next((order for order in orders if order.key == previous.key), previous))
    return Selection(None)


async def resolve_order_list(result, *, previous, completed, excluded, fetch_details):
    """Shared list/details resolution; callers expire runtime before taking previous."""
    if not isinstance(result.orders, tuple) or any(
        not isinstance(order, OrderSnapshot) for order in result.orders
    ):
        raise Failure("invalid_data")
    selected = select_order(
        result.orders, previous=previous, completed=completed, excluded=excluded
    )
    snapshot = selected.snapshot
    metadata = result.for_order(snapshot)
    retired_key = None
    if selected.details_required:
        details = await fetch_details(snapshot.key)
        if (
            not isinstance(details, OrderResult)
            or not isinstance(details.snapshot, OrderSnapshot)
            or details.snapshot.key != snapshot.key
        ):
            raise Failure("invalid_data")
        snapshot = details.snapshot
        metadata = details
    if snapshot is not None and snapshot.terminal:
        replacement = select_order(result.orders, excluded=excluded)
        if replacement.snapshot is not None:
            retired_key = snapshot.key
            snapshot = replacement.snapshot
            metadata = result.for_order(snapshot)
    return OrderResult(snapshot, metadata.courier_eligible, metadata.courier_supported, retired_key)


class DataAPI(Protocol):
    @property
    def completion_supported(self) -> bool: ...
    async def async_completion(self, access_token: str, order_key: str) -> CompletionResult: ...
    async def async_order(self, access_token: str) -> OrderResult | OrderListResult: ...
    async def async_order_details(self, access_token: str, order_key: str) -> OrderResult: ...
    async def async_courier(
        self, access_token: str, order_key: str
    ) -> CourierSnapshot | int | None: ...


def normalize_courier(payload: dict, *, destination=None) -> CourierSnapshot:
    """Count explicit true flags before validating [longitude, latitude]."""
    drivers = payload.get("drivers")
    if not isinstance(drivers, list):
        return CourierSnapshot()
    flags = [
        driver.get("delivering_your_order") if isinstance(driver, dict) else None
        for driver in drivers
    ]
    selected = [driver for driver, flag in zip(drivers, flags, strict=True) if flag is True]
    flag = (
        True
        if len(selected) == 1
        else (False if flags and all(f is False for f in flags) else None)
    )
    distance = None
    location = None
    if len(selected) == 1:
        location = coordinates(selected[0].get("location"))
        target = coordinates(destination)
        if location is not None and target is not None:
            lon1, lat1, lon2, lat2 = map(radians, (*location, *target))
            haversine = (
                sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
            )
            distance = round(6371000 * 2 * asin(sqrt(min(1, max(0, haversine)))))
    return CourierSnapshot(
        distance,
        flag,
        latitude=location[1] if location is not None else None,
        longitude=location[0] if location is not None else None,
    )


def timestamp(value) -> datetime | None:
    """Only explicit timezone-aware ISO dates are reliable timestamps."""
    if isinstance(value, dict):
        milliseconds = value.get("$date")
        try:
            if type(milliseconds) not in (int, float) or not isfinite(milliseconds):
                return None
            return datetime.fromtimestamp(milliseconds / 1000, UTC)
        except ValueError, OverflowError, OSError:
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        # HA timestamp states convert to UTC; reject dates that overflow there.
        parsed.astimezone(UTC)
    except ValueError, OverflowError:
        return None
    return parsed


def forecast_time(payload: dict, *, minutes_verified=False) -> datetime | None:
    lower = timestamp(payload.get("delivery_eta_min"))
    upper = timestamp(payload.get("delivery_eta_max"))
    if lower is not None and upper is not None:
        return upper if lower <= upper else None
    if lower is not None or upper is not None:
        return lower or upper
    estimate = timestamp(payload.get("delivery_eta"))
    if estimate is not None:
        return estimate
    payment = timestamp(payload.get("payment_time"))
    initial = payload.get("client_pre_estimate")
    if minutes_verified is True and payment is not None and isinstance(initial, str):
        match = re.fullmatch(r"(\d+)-(\d+)", initial)
        if match:
            try:
                lower_minutes, upper_minutes = map(int, match.groups())
                if lower_minutes <= upper_minutes:
                    estimate = payment + timedelta(minutes=upper_minutes)
                    estimate.astimezone(UTC)  # HA must be able to serialize the result.
                    return estimate
            except ValueError, OverflowError:
                pass
    return None


def normalize_order(payload: dict, *, minutes_verified=False) -> OrderSnapshot:
    """Interpret one local details object; never perform a request."""
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("order_id"), str)
        or not payload["order_id"]
    ):
        raise Failure("invalid_data")
    raw = payload.get("status")
    mapping = {
        "received": "received",
        "acknowledged": "acknowledged",
        "fetched": "acknowledged",
        "production": "preparing",
        "ready": "ready",
        "delivered": "delivered",
        "rejected": "cancelled",
        "preorder-received": "scheduled",
        "preorder-confirmed": "scheduled",
    }
    status = mapping.get(raw, "unknown") if isinstance(raw, str) else "unknown"
    if raw in ("received", "acknowledged", "fetched") and payload.get("preorder_status") in (
        "received",
        "confirmed",
    ):
        status = "scheduled"
    estimate = forecast_time(payload, minutes_verified=minutes_verified)
    return OrderSnapshot(
        payload["order_id"],
        status,
        restaurant=payload.get("venue_name")
        if isinstance(payload.get("venue_name"), str) and payload["venue_name"]
        else None,
        terminal=status in {"delivered", "cancelled"},
        estimated_delivery_time=estimate,
        delivery_in_progress=False
        if status in {"received", "acknowledged", "scheduled", "preparing"}
        else None,
    )


def normalize_orders(payload: dict, *, minutes_verified=False) -> OrderListResult:
    """Interpret details in their returned order; capability is not inferred."""
    if not isinstance(payload, dict) or not isinstance(payload.get("order_details"), list):
        raise Failure("invalid_data")
    return OrderListResult(
        tuple(
            normalize_order(order, minutes_verified=minutes_verified)
            for order in payload["order_details"]
        )
    )


def assumes_initial_minutes(payload: dict) -> bool:
    """Approved ordinary-order assumption; absence of markers is not unit proof.

    Exclude all non-null slot/preorder/planned metadata, even malformed or
    historical markers. Unknown delivery methods/stages fail closed. No numeric
    magnitude or marketplace/tracking flag is used to infer units.
    """
    return (
        payload.get("delivery_method") == "homedelivery"
        and payload.get("status") in ("received", "acknowledged", "fetched", "production", "ready")
        and all(
            payload.get(key) is None
            for key in (
                "preorder_status",
                "time_slot_order",
                "scheduled_time",
                "scheduled_delivery_time",
                "planned_delivery_time",
            )
        )
        and all(
            payload.get(key) is None or payload.get(key) is False
            for key in ("preorder", "is_preorder", "is_scheduled")
        )
        and payload.get("client_pre_estimate_unit") in (None, "minutes", "MINUTES")
    )


class WoltDataAPI:
    """Unwrap verified HTTP payloads; no histories or private payload caches."""

    def __init__(self, client, *, unified_client=None):
        self._client = client
        self._unified_client = unified_client

    @property
    def completion_supported(self):
        return self._unified_client is not None

    async def async_completion(self, access_token, order_key):
        return await self._unified_client.get_completion(access_token, order_key)

    async def close(self):
        try:
            await self._client.close()
        finally:
            if self._unified_client is not None and self._unified_client is not self._client:
                await self._unified_client.close()

    async def async_order(self, access_token):
        payload = await self._client.get("subscriptions", access_token)
        results = self._order_results(payload)
        orders = tuple(result.snapshot for result in results)
        if len({order.key for order in orders}) != len(orders):
            raise Failure("invalid_data")
        return OrderListResult(orders, per_order=results)

    async def async_order_details(self, access_token, order_key):
        payload = await self._client.get("details", access_token, order_key)
        results = self._order_results(payload)
        if len(results) != 1 or results[0].snapshot.key != order_key:
            raise Failure("invalid_data")
        return results[0]

    async def async_courier(self, access_token, order_key):
        payload = await self._client.get("tracking", access_token, order_key)
        order = payload.get("order_details")
        snapshot = normalize_order(order)
        if snapshot.key != order_key or (
            "drivers" in payload and not isinstance(payload["drivers"], list)
        ):
            raise Failure("invalid_data")
        location = order.get("delivery_location")
        point = location.get("coordinates") if isinstance(location, dict) else None
        destination = (
            point.get("coordinates")
            if isinstance(point, dict) and point.get("type") == "Point"
            else None
        )
        return normalize_courier(payload, destination=destination)

    @classmethod
    def _order_results(cls, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("order_details"), list):
            raise Failure("invalid_data")
        if any(not isinstance(order, dict) for order in payload["order_details"]):
            raise Failure("invalid_data")
        # Normalize once: selection and per-order metadata share the same forecast.
        return tuple(cls._order_result(order) for order in payload["order_details"])

    @staticmethod
    def _order_result(payload):
        # Keep the explicit normalized override; this adapter passes an assumption.
        snapshot = normalize_order(payload, minutes_verified=assumes_initial_minutes(payload))
        self_delivery = payload.get("self_delivery")
        # Wolt's map/status consumers use homedelivery and marketplace/self tracking:
        # https://wolt-com-static-assets.wolt.com/56062-577f80a34e6be76f.chunk.js
        # Require explicit evidence instead of copying the UI's absent-field defaults.
        enabled = (
            isinstance(self_delivery, dict) and self_delivery.get("is_tracking_enabled") is True
        )
        supported = payload.get("delivery_method") == "homedelivery" and (
            payload.get("is_marketplace_v2") is False or enabled
        )
        return OrderResult(snapshot, supported and not snapshot.terminal, supported)


def create_data_api(hass):
    """Create a dedicated owned session; construction does not send requests."""
    client = UnifiedClient(timezone=hass.config.time_zone)
    # One owned cookie-free data session, shared by the two fixed-origin reads.
    return WoltDataAPI(client, unified_client=client)
