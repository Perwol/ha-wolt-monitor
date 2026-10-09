"""Fixed messages and allowlisted metadata; never use exception tracebacks."""

import logging

CATEGORIES = {
    "connection",
    "timeout",
    "server",
    "rate_limit",
    "authentication",
    "invalid_data",
    "unexpected",
}
_LOGGER = logging.getLogger("custom_components.wolt_monitor")


class SafeLog:
    def __init__(self):
        self._warned = set()
        self._failed = set()

    def cycle(self, *, manual, due, auth_wait, data_wait):
        def safe_delay(value):
            return value if type(value) in (int, float) and 0 <= value <= 315360000 else None

        _LOGGER.debug(
            "Poll cycle: mode=%s order=%s courier=%s auth_backoff=%s data_backoff=%s",
            "manual" if manual else "scheduled",
            "order" in due,
            "courier" in due,
            safe_delay(auth_wait),
            safe_delay(data_wait),
        )

    def failure(self, source, error, counter):
        source = source if source in {"order", "courier", "unified"} else "order"
        self._failed.add(source)
        category = error.category if error.category in CATEGORIES else "unexpected"
        status = (
            error.http_status
            if type(error.http_status) is int and 100 <= error.http_status <= 599
            else None
        )
        _LOGGER.debug(
            "Source result: source=%s category=%s http_status=%s errors=%s",
            source,
            category,
            status,
            counter,
        )
        if category == "unexpected":
            _LOGGER.error("Unexpected integration error: source=%s", source)
        for event, condition in [
            ("rate_limit", category == "rate_limit"),
            ("unavailable", counter >= 3),
        ]:
            if condition and (source, event) not in self._warned:
                self._warned.add((source, event))
                _LOGGER.warning(
                    "Source condition: source=%s category=%s http_status=%s", source, event, status
                )

    def success(self, source):
        if source in self._failed:
            _LOGGER.info("Source recovered: source=%s", source)
            self._warned = {key for key in self._warned if key[0] != source}
            self._failed.discard(source)
