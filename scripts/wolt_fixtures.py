"""Offline allowlisted sanitization/replay and sanitized-only persistence. No HTTP/CLI."""

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from custom_components.wolt_monitor.api import coordinates, timestamp


def invalid(value):
    """Type witness without contents; never traverse unknown containers."""
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        return "invalid"
    if type(value) is list:
        return []
    if type(value) is dict:
        return {}
    if type(value) is int:
        return 0
    if type(value) is float:
        return 0.0
    return None


def position(value, *, destination=False):
    # Synthetic geometry; no original distance or motion retained.
    return ([0.0, 0.0] if destination else [0.001, 0.0]) if coordinates(value) else invalid(value)


def subset(raw, key, transform):
    return {key: transform(raw[key])} if key in raw else {}


STATUSES = frozenset(
    {
        "received",
        "acknowledged",
        "fetched",
        "production",
        "ready",
        "delivered",
        "rejected",
        "preorder-received",
        "preorder-confirmed",
        "deferred_payment_failed",
        "process_payment_failed",
        "payment_method_not_valid_error",
        "invalid",
        "estimated",
        "pending_transaction",
        "pending_revenue_transaction",
        "picked_up",
        "refunded",
    }
)
DATES = frozenset(
    {"payment_time", "delivery_eta", "delivery_eta_min", "delivery_eta_max", "delivery_time"}
)


FIXTURE_BASE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "wolt"
MAX_FIXTURE_BYTES = 1024 * 1024


class CaptureSession:
    """Transient aliases only; discard the session after a recording."""

    def __init__(self, *, offset, provenance):
        if (
            type(offset) is not timedelta
            or type(provenance) is not str
            or provenance not in {"synthetic", "observed"}
            or offset.microseconds % 1000
            or (provenance == "observed" and offset == timedelta(0))
        ):
            raise ValueError("Invalid capture configuration")
        self._offset = offset
        self._provenance = provenance
        self._aliases = {}

    def reference_time(self, value):
        result = None
        try:
            require(type(value) is datetime and timestamp(value.isoformat()) is not None)
            shifted = (value + self._offset).isoformat()
            require(timestamp(shifted) is not None)
            result = shifted
        except Exception:
            pass
        if result is None:
            raise ValueError("Invalid reference time")
        return result

    def _order(self, raw):
        if type(raw) is not dict:
            return None
        result = {}
        for key, value in raw.items():
            if key == "order_id":
                if type(value) is str and value:
                    if value not in self._aliases:
                        self._aliases[value] = f"order-{len(self._aliases) + 1}"
                    result[key] = self._aliases[value]
                else:
                    result[key] = "" if type(value) is str else invalid(value)
            elif key == "status":
                result[key] = (
                    (value if value in STATUSES else "unknown")
                    if type(value) is str
                    else invalid(value)
                )
            elif key == "venue_name":
                result[key] = (
                    ("Fixture venue" if value else "") if type(value) is str else invalid(value)
                )
            elif key == "delivery_method":
                result[key] = (
                    (value if value in ("homedelivery", "takeaway") else "unknown")
                    if type(value) is str
                    else invalid(value)
                )
            elif key == "is_marketplace_v2":
                result[key] = value if type(value) is bool else invalid(value)
            elif key == "preorder_status":
                result[key] = value if value in ("received", "confirmed") else invalid(value)
            elif key in (
                "preorder",
                "is_preorder",
                "is_scheduled",
                "scheduled_time",
                "scheduled_delivery_time",
                "planned_delivery_time",
            ):
                # Only presence/type/false matters to the conservative assumption gate.
                result[key] = invalid(value)
            elif key == "client_pre_estimate_unit":
                result[key] = (
                    value if value in ("minutes", "MINUTES", "days", "DAYS") else invalid(value)
                )
            elif key == "time_slot_order":
                result[key] = (
                    subset(
                        value,
                        "type",
                        lambda v: (
                            v if v in ("DELIVERY_WITHIN_TIME_RANGE", "PRIORITY") else invalid(v)
                        ),
                    )
                    if type(value) is dict
                    else invalid(value)
                )
            elif key == "client_pre_estimate":
                result[key] = (
                    value
                    # bounded_json already limits string length; preserve numeric evidence,
                    # including reversed/overflowing ranges the production parser rejects.
                    if type(value) is str and re.fullmatch(r"\d+-\d+", value)
                    else invalid(value)
                )
            elif key == "self_delivery":
                result[key] = (
                    subset(value, "is_tracking_enabled", invalid)
                    if type(value) is dict
                    else invalid(value)
                )
            elif key == "delivery_location":

                def point(v):
                    if type(v) is not dict:
                        return invalid(v)
                    return {
                        **subset(v, "type", lambda t: "Point" if t == "Point" else invalid(t)),
                        **subset(v, "coordinates", lambda p: position(p, destination=True)),
                    }

                result[key] = (
                    subset(value, "coordinates", point) if type(value) is dict else invalid(value)
                )
            elif key in DATES:
                parsed = timestamp(value)
                shifted = parsed + self._offset if parsed else None
                if shifted is not None:
                    require(timestamp(shifted.isoformat()) is not None)
                result[key] = (
                    {"$date": value["$date"] + self._offset // timedelta(milliseconds=1)}
                    if shifted and type(value) is dict
                    else shifted.isoformat()
                    if shifted
                    else (
                        subset(
                            value,
                            "$date",
                            lambda v: 1e30 if type(v) in (int, float) else invalid(v),
                        )
                        if type(value) is dict
                        else invalid(value)
                    )
                )
        return result

    def dataset(self, stages):
        result = {
            "metadata": {
                "version": 1,
                "provenance": self._provenance,
                "coordinates": "synthetic-replacement",
                "timestamps": "common-shift",
            },
            "stages": stages,
        }
        validate_dataset(result)
        return result

    def save(self, stages, *, slug):
        """Save a session-labelled sanitized snapshot; no raw input or metadata API."""
        valid = False
        try:
            snapshot = json.loads(json.dumps(self.dataset(stages), allow_nan=False))
            validate_dataset(snapshot)
            data = json.dumps(
                snapshot, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            require(type(slug) is str and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug))
            require(len(data) <= MAX_FIXTURE_BYTES)
            _publish(data, slug)
            valid = True
        except Exception:
            pass
        if not valid:
            raise ValueError("Unable to save sanitized fixture")
        return FIXTURE_BASE / f"{slug}.json"

    def capture(self, resource, payload):
        valid = False
        try:
            bounded_json(payload)
            result = self._capture(resource, payload)
            valid = True
        except Exception:
            pass
        if not valid:
            raise ValueError("Invalid capture input")
        return result

    def _capture(self, resource, payload):
        if resource not in {"subscriptions", "details", "tracking"} or type(payload) is not dict:
            raise ValueError("Invalid capture input")
        result = {}
        if "order_details" in payload:
            value = payload["order_details"]
            result["order_details"] = (
                [self._order(item) for item in value] if type(value) is list else self._order(value)
            )
        if "drivers" in payload:
            drivers = payload["drivers"]
            result["drivers"] = (
                [
                    {
                        **subset(d, "delivering_your_order", invalid),
                        **subset(d, "location", position),
                    }
                    if type(d) is dict
                    else invalid(d)
                    for d in drivers
                ]
                if type(drivers) is list
                else invalid(drivers)
            )
        return result


def _open_fixture_directory():
    """Walk the fixed base by descriptors; never follow a directory symlink."""
    require(FIXTURE_BASE.is_absolute() and ".." not in FIXTURE_BASE.parts)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(FIXTURE_BASE.anchor, flags)
    try:
        for component in FIXTURE_BASE.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=directory)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory
    except Exception:
        os.close(directory)
        raise


def _publish(data, slug):
    """Install complete bytes with an atomic no-replace link."""
    directory = _open_fixture_directory()
    temporary = f".fixture-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(
            temporary,
            f"{slug}.json",
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory)
        created = False
        os.fsync(directory)
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)


def require(condition):
    if not condition:
        raise ValueError("Invalid fixture structure")


def bounded_json(value, depth=0, budget=None):
    """Validate types/size only, not a generic replacement traversal."""
    budget = [10000] if budget is None else budget
    budget[0] -= 1
    require(depth <= 12 and budget[0] >= 0)
    kind = type(value)
    require(kind in (str, int, float, bool, list, dict, type(None)))
    if kind is int:
        require(value.bit_length() <= 128)
    elif kind is str:
        require(len(value) <= 4096)
    elif kind in (list, dict):
        require(len(value) <= 1000)
        if kind is dict:
            for key in value:
                require(type(key) is str and len(key) <= 256)
        for item in value.values() if kind is dict else value:
            bounded_json(item, depth + 1, budget)


STAGES = STATUSES | {"courier", "retention", "unknown", "refund"}


def validate_dataset(dataset):
    """Fail closed for raw/modified structures. Does not certify real observation."""
    valid = False
    try:
        bounded_json(dataset)
        metadata = dataset["metadata"]
        require(set(dataset) == {"metadata", "stages"})
        require(
            metadata
            == {
                "version": 1,
                "provenance": metadata["provenance"],
                "coordinates": "synthetic-replacement",
                "timestamps": "common-shift",
            }
        )
        require(metadata["provenance"] in {"observed", "synthetic"})
        require(type(dataset["stages"]) is list and 0 < len(dataset["stages"]) <= 100)
        checker = CaptureSession(offset=timedelta(0), provenance="synthetic")
        last = None
        for stage in dataset["stages"]:
            require(set(stage) == {"stage", "at", "responses"})
            require(stage["stage"] in STAGES)
            at = timestamp(stage["at"])
            require(type(stage["at"]) is str and at is not None)
            require(last is None or at >= last)
            last = at
            responses = stage["responses"]
            require(type(responses) is dict and "subscriptions" in responses)
            require(set(responses) <= {"subscriptions", "details", "tracking"})
            for resource, payload in responses.items():
                require(type(payload) is dict)
                orders = payload.get("order_details")
                for order in orders if type(orders) is list else [orders]:
                    if type(order) is dict and type(order.get("order_id")) is str:
                        key = order["order_id"]
                        require(re.fullmatch(r"order-[1-9][0-9]{0,3}", key))
                        checker._aliases[key] = key
                require(checker.capture(resource, payload) == payload)
        valid = True
    except Exception:
        pass
    if not valid:
        raise ValueError("Invalid sanitized dataset")


class ReplayClient:
    """Synthetic in-memory transport; cannot perform HTTP or refresh auth."""

    def __init__(self, responses):
        self.responses = responses

    async def get(self, resource, _token, _key=None):
        if resource not in self.responses:
            raise ValueError("Missing replay response")
        return self.responses[resource]


async def replay(dataset):
    """Exercise actual adapter, selector and runtime with controlled clocks."""
    from custom_components.wolt_monitor.api import WoltDataAPI, resolve_order_list
    from custom_components.wolt_monitor.state import Runtime

    validate_dataset(dataset)
    start = timestamp(dataset["stages"][0]["at"])
    now = [0.0]
    wall = [start]
    runtime = Runtime(clock=lambda: now[0])
    runtime.wall_clock = lambda: wall[0]
    results = []
    for index, stage in enumerate(dataset["stages"]):
        wall[0] = timestamp(stage["at"])
        now[0] = (wall[0] - start).total_seconds()
        adapter = WoltDataAPI(ReplayClient(stage["responses"]))
        listed = await adapter.async_order("synthetic-unused")
        runtime.expire()
        metadata = await resolve_order_list(
            listed,
            previous=runtime.order,
            completed=runtime.completed,
            excluded=runtime.excluded_keys,
            fetch_details=lambda key, adapter=adapter: adapter.async_order_details(
                "synthetic-unused", key
            ),
        )
        snapshot = metadata.snapshot
        if metadata.retired_key is not None:
            runtime.retire_key(metadata.retired_key)
        runtime.update_order(snapshot, str(index))
        eligible = metadata.courier_eligible and metadata.courier_supported
        if not eligible:
            runtime.clear_distance()
        if (
            snapshot is not None
            and not runtime.completed
            and eligible
            and "tracking" in stage["responses"]
        ):
            courier = await adapter.async_courier("synthetic-unused", snapshot.key)
            runtime.update_courier(courier, str(index), order_key=snapshot.key)
        results.append(
            {
                **runtime.values(),
                "delivery_in_progress": runtime.delivery_in_progress(),
                "is_delivered": runtime.is_delivered(),
            }
        )
    return results
