"""Memory-only order policy consumes normalized snapshots, not Wolt payloads."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import ceil, isfinite

from .errors import Failure
from .safe_logging import CATEGORIES
from .scheduler import Options


def coordinates(value):
    """Validate [longitude, latitude] before conversion, including oversized integers."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(
        type(item) not in (int, float) or (type(item) is float and not isfinite(item))
        for item in value
    ):
        return None
    lon, lat = value
    return (lon, lat) if -180 <= lon <= 180 and -90 <= lat <= 90 else None


@dataclass(frozen=True)
class OrderSnapshot:
    """Private normalized input; identities and forecasts never enter diagnostics."""

    key: str = field(repr=False)
    status: str
    restaurant: str | None = field(default=None, repr=False)
    estimated_delivery_time: datetime | None = field(default=None, repr=False, kw_only=True)
    terminal: bool = False
    delivery_in_progress: bool | None = None


@dataclass(frozen=True)
class CourierSnapshot:
    """Memory-only tracking evidence; coordinates never enter repr or diagnostics."""

    distance: int | None = None
    delivery_in_progress: bool | None = None
    latitude: float | None = field(default=None, repr=False, kw_only=True)
    longitude: float | None = field(default=None, repr=False, kw_only=True)

    def __post_init__(self):
        if coordinates((self.longitude, self.latitude)) is None:
            object.__setattr__(self, "latitude", None)
            object.__setattr__(self, "longitude", None)


@dataclass(frozen=True)
class CompletionResult:
    """Only validated same-order evidence; no response or private URL retention."""

    key: str = field(repr=False)
    complete: bool = False


@dataclass
class SourceState:
    """Only allowlisted, non-identifying diagnostics."""

    last_successful_update: str | None = None
    consecutive_errors: int = 0
    last_error_category: str | None = None
    last_http_status: int | None = None
    _failed_cycle: str | None = field(default=None, repr=False)

    def success(self) -> None:
        self.last_successful_update = datetime.now(UTC).isoformat()
        self.consecutive_errors = 0
        self.last_error_category = None
        self.last_http_status = None
        self._failed_cycle = None

    def failure(self, error: Failure, cycle: str) -> None:
        if self._failed_cycle != cycle:
            self.consecutive_errors += 1
            self._failed_cycle = cycle
        self.last_error_category = (
            error.category
            if isinstance(error.category, str) and error.category in CATEGORIES
            else "unexpected"
        )
        self.last_http_status = (
            error.http_status
            if type(error.http_status) is int and 100 <= error.http_status <= 599
            else None
        )


class Runtime:
    """Retained terminal data expires independently of requests and backoff."""

    def __init__(self, *, clock: Callable[[], float], retention_minutes: int = 10) -> None:
        self._clock = clock
        self.wall_clock = lambda: datetime.now(UTC)
        Options(retention_minutes=retention_minutes)
        self.retention_minutes = retention_minutes
        self._order: OrderSnapshot | None = None
        self._terminal_at: float | None = None
        self._expired: set[str] = set()
        self._distance: int | None = None
        self._position: tuple[float, float] | None = None
        self._delivered = False
        self._tracking: bool | None = None
        self.sources = {"order": SourceState(), "courier": SourceState(), "unified": SourceState()}
        self.reauth_required = False

    @property
    def completed(self):
        self.expire()
        return self._terminal_at is not None

    @property
    def delivery_confirmed(self):
        self.expire()
        return self._delivered

    @property
    def excluded_keys(self):
        self.expire()
        return frozenset(self._expired)

    def retire_key(self, key):
        self._expired.add(key)

    @property
    def order(self) -> OrderSnapshot | None:
        return self._order

    def update_order(self, order: OrderSnapshot | None, cycle: str) -> None:
        self.expire()
        self.sources["order"].success()
        if order is None and self._terminal_at is not None:
            return
        if order is None:
            self._order = None
            self._terminal_at = None
            self.clear_distance()
            self._delivered = False
            return
        if order.key in self._expired:
            return
        if self._order is None or self._order.key != order.key:
            if self._order is not None and self._terminal_at is not None:
                self._expired.add(self._order.key)
            self._terminal_at = None
            self.clear_distance()
            self._delivered = False
        self._order = order
        if order.status == "delivered":
            self._delivered = True
        if order.terminal:
            self._position = None
            if self._terminal_at is None:
                self._terminal_at = self._clock()
        self.expire()

    def confirm_delivery(self, result: CompletionResult) -> None:
        self.expire()
        if self._order is None or self._order.key != result.key or result.complete is not True:
            return
        self._delivered = True
        if self._terminal_at is None:
            self._terminal_at = self._clock()
        self.clear_distance()
        self.expire()

    def clear_distance(self) -> None:
        self._distance = None
        self._position = None
        self._tracking = None

    def update_courier(
        self, distance: CourierSnapshot | int | None, cycle: str, *, order_key: str
    ) -> None:
        self.expire()
        if self._order is None or self._order.key != order_key or self._terminal_at is not None:
            return
        self.sources["courier"].success()
        self._distance = distance.distance if isinstance(distance, CourierSnapshot) else distance
        self._position = None
        if isinstance(distance, CourierSnapshot):
            self._tracking = distance.delivery_in_progress
            if (
                distance.delivery_in_progress is True
                and distance.latitude is not None
                and distance.longitude is not None
            ):
                self._position = (distance.latitude, distance.longitude)

    def courier_position(self) -> tuple[float, float] | None:
        """Publish GPS only during effective delivery, with independent geometry."""
        if self.values()["status"] is None or self._order is None or not self._direct_leg():
            return None
        return self._position

    def fail(self, source: str, error: Failure, cycle: str) -> None:
        self.sources[source].failure(error, cycle)
        if source in {"order", "courier"} and self.sources[source].consecutive_errors >= 3:
            self._position = None
        if error.invalid_refresh:
            self._position = None
            self.reauth_required = True

    @property
    def retention_deadline(self) -> float | None:
        if self._terminal_at is None:
            return None
        return self._terminal_at + self.retention_minutes * 60

    def set_retention(self, minutes: int) -> None:
        self.expire()
        Options(retention_minutes=minutes)
        self.retention_minutes = minutes
        self.expire()

    def expire(self) -> None:
        if self._terminal_at is not None and (
            self._clock() >= self._terminal_at + self.retention_minutes * 60
        ):
            if self._order is not None:
                self._expired.add(self._order.key)
            self._order = None
            self._terminal_at = None
            self.clear_distance()
            self._delivered = False

    @property
    def eta_update_delay(self):
        if self.completed or self.values()["status"] is None or self._order is None:
            return None
        estimate = self._order.estimated_delivery_time
        if estimate is not None:
            remaining = (estimate - self.wall_clock()).total_seconds()
            if remaining > 0:
                return remaining - (ceil(remaining / 60) - 1) * 60
        return None

    def is_delivered(self) -> bool | None:
        if self.values()["status"] is None:
            return None
        if self._order is None:
            return None
        if self._delivered:
            return True
        return None if self._order.status == "unknown" else False

    def _direct_leg(self) -> bool:
        """Ready eligibility plus current tracking, independent of coordinates."""
        return (
            self._order is not None
            and self._order.status == "ready"
            and self._terminal_at is None
            and self._tracking is True
            and self.sources["courier"].consecutive_errors < 3
        )

    def delivery_in_progress(self) -> bool | None:
        """Use the same direct-leg condition as Status; preserve source availability."""
        if self.values()["status"] is None:
            return None
        if self._order is None:
            return None
        if self._terminal_at is not None:
            return False
        if self.sources["courier"].consecutive_errors >= 3:
            return None
        if self._tracking is not None:
            return self._direct_leg()
        value = self._order.delivery_in_progress
        return False if type(value) is bool else None

    @staticmethod
    def _empty_values(status):
        return dict(
            status=status,
            restaurant=None,
            distance=None,
            estimated_delivery_time=None,
            eta=None,
        )

    def values(self) -> dict[str, object]:
        self.expire()
        order = self._order
        if (
            self.reauth_required
            or self.sources["order"].last_successful_update is None
            or self.sources["order"].consecutive_errors >= 3
        ):
            return self._empty_values(None)
        if order is None:
            return self._empty_values("no_active_order")
        result = dict(
            status="delivered"
            if self._delivered
            else "on_the_way"
            if self._direct_leg()
            else order.status,
            restaurant=order.restaurant,
            distance=None if not self._direct_leg() else self._distance,
        )
        estimate = order.estimated_delivery_time
        result["estimated_delivery_time"] = (
            estimate.isoformat() if estimate is not None and self._terminal_at is None else None
        )
        result["eta"] = (
            0
            if self._delivered
            else (
                max(0, ceil((estimate - self.wall_clock()).total_seconds() / 60))
                if estimate is not None and self._terminal_at is None
                else None
            )
        )
        return result
