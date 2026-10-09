"""Monotonic scheduling policy, without an event loop timer or HA runtime."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Options:
    """Validated atomic user options; intervals expressed in seconds."""

    idle_seconds: int = 120
    active_seconds: int = 30
    courier_seconds: int = 30
    retention_minutes: int = 10

    def __post_init__(self) -> None:
        for value, low, high, step in (
            (self.idle_seconds, 60, 600, 60),
            (self.active_seconds, 30, 300, 30),
            (self.courier_seconds, 30, 300, 30),
            (self.retention_minutes, 0, 60, 1),
        ):
            if type(value) is not int or not low <= value <= high or value % step:
                raise ValueError("Invalid polling or retention option")


class Scheduler:
    """One active cycle, no accumulated queue; deadlines measured from completion."""

    def __init__(self, *, clock: Callable[[], float], options: Options | None = None) -> None:
        self._clock = clock
        self.options = options or Options()
        self._next = {"order": clock(), "courier": clock()}
        self._last_finish: dict[str, float] = {}
        self._errors = {"order": 0, "courier": 0}
        self._running: frozenset[str] = frozenset()
        self._active = False
        self._courier = False
        self._data_not_before = 0.0

    def _interval(self, source: str) -> int:
        if source == "courier":
            base = self.options.courier_seconds
        else:
            base = self.options.active_seconds if self._active else self.options.idle_seconds
        return min(900, base * (2 ** min(self._errors[source], 5)))

    def set_errors(self, source: str, count: int) -> None:
        """Mirror the final source outcome; waiting never changes the streak."""
        self._errors[source] = count
        if source in self._last_finish:
            self._next[source] = self._last_finish[source] + self._interval(source)

    def set_activity(self, *, active: bool, courier_eligible: bool) -> None:
        courier = active and courier_eligible
        if courier and not self._courier and not self._errors["courier"]:
            self._next["courier"] = self._clock()
        self._active = active
        self._courier = courier
        if "order" in self._last_finish:
            self._next["order"] = self._last_finish["order"] + self._interval("order")

    def apply_options(self, options: Options) -> None:
        self.options = options
        for source, finished in self._last_finish.items():
            self._next[source] = finished + self._interval(source)

    def defer_data(self, seconds: float) -> None:
        self._data_not_before = max(self._data_not_before, self._clock() + seconds)

    @property
    def courier_eligible(self) -> bool:
        return self._courier

    @property
    def courier_due(self) -> bool:
        return self._courier and self._clock() >= self._next["courier"]

    @property
    def courier_read_allowed(self) -> bool:
        """Allow an immediate read for a new order, but never bypass failure waits."""
        return (
            self._courier
            and self._clock() >= self._data_not_before
            and (not self._errors["courier"] or self.courier_due)
        )

    @property
    def next_deadline(self) -> float:
        sources = ["order", "courier"] if self._courier else ["order"]
        return max(self._data_not_before, min(self._next[s] for s in sources))

    def begin(self, *, manual: bool = False) -> frozenset[str]:
        if self._running or self._clock() < self._data_not_before:
            return frozenset()
        sources = ["order", "courier"] if self._courier else ["order"]
        self._running = frozenset(
            s for s in sources if (manual and not self._errors[s]) or self._clock() >= self._next[s]
        )
        return self._running

    def finish(self, sources: frozenset[str] | None = None) -> None:
        for source in self._running if sources is None else sources:
            self._last_finish[source] = self._clock()
            self._next[source] = self._clock() + self._interval(source)
        self._running = frozenset()
