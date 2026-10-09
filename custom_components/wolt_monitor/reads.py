"""Authenticated reads with one access-401 retry, without Wolt payload mapping."""

from collections.abc import Awaitable, Callable

import aiohttp

from .auth import TokenManager
from .errors import Failure


class ReadSuppressed(Exception):
    """A local stop, not a failed request or an authentication failure."""


def _require_allowed(can_read: Callable[[], bool]) -> None:
    if not can_read():
        raise ReadSuppressed()


async def _read[T](fetch: Callable[[str], Awaitable[T]], token: str) -> T:
    failure = None
    try:
        return await fetch(token)
    except Failure, ReadSuppressed:
        raise
    except TimeoutError:
        failure = Failure("timeout")
    except OSError, aiohttp.ClientError:
        failure = Failure("connection")
    except Exception:
        failure = Failure("unexpected")
    raise failure


async def authenticated_read[T](
    manager: TokenManager,
    cycle: str,
    fetch: Callable[[str], Awaitable[T]],
    *,
    can_read: Callable[[], bool] = lambda: True,
) -> T:
    """Caller records only the final outcome and shares data Retry-After globally."""
    _require_allowed(can_read)
    token = await manager.acquire(cycle)
    _require_allowed(can_read)
    try:
        return await _read(fetch, token)
    except Failure as error:
        if error.http_status != 401:
            raise
        manager.reject(token)
        if error.retry_after is not None and error.retry_after > 0:
            raise
    _require_allowed(can_read)
    token = await manager.acquire(cycle, repair=True)
    _require_allowed(can_read)
    try:
        return await _read(fetch, token)
    except Failure as error:
        if error.http_status == 401:
            manager.reject(token)
        raise
