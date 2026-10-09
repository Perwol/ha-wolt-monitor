"""Fixed, non-identifying error metadata; never carry server messages."""


class Failure(Exception):
    """Sanitized error used by transport, auth and source counters."""

    def __init__(
        self,
        category: str,
        http_status: int | None = None,
        *,
        retry_after: float = 0,
        invalid_refresh: bool = False,
        auth_failure: bool = False,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.http_status = http_status
        self.retry_after = retry_after
        self.invalid_refresh = invalid_refresh
        self.auth_failure = auth_failure
