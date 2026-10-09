"""Refresh-token lifecycle independent of Home Assistant."""

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .errors import Failure


@dataclass(frozen=True)
class TokenReply:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: float


class TokenManager:
    """Keep tokens in memory; persistence is an injected config-entry update."""

    def __init__(
        self,
        refresh_token: str,
        refresh: Callable[[str], Awaitable[TokenReply]],
        persist: Callable[[str], object],
        *,
        clock: Callable[[], float],
    ) -> None:
        self.refresh_token = refresh_token
        self._refresh = refresh
        self._persist = persist
        self._clock = clock
        self.expires_at = 0.0
        self.margin = 0.0
        self._access_token: str | None = None
        self._inflight: asyncio.Task[str | Failure] | None = None
        self.failures = 0
        self.not_before = 0.0
        self.last_error: Failure | None = None
        self._renewed_cycle: str | None = None
        self.reauth_required = False
        self.persistence_pending = False
        self._persistence_cycle: str | None = None
        self._rejected = False
        self._repair_cycle: str | None = None

    def usable(self) -> bool:
        return (
            self._access_token is not None
            and not self._rejected
            and self._clock() < self.expires_at
        )

    def reject(self, token: str) -> None:
        if token == self._access_token:
            self._rejected = True

    async def acquire(self, cycle: str, *, repair: bool = False) -> str:
        if self.reauth_required:
            raise Failure("authentication", 401, invalid_refresh=True)
        if self.persistence_pending and self._persistence_cycle != cycle:
            self._persistence_cycle = cycle
            self._try_persist()
        if self._inflight is not None:
            return await self._wait_refresh(self._inflight)
        if repair and self.usable():
            return self._access_token  # type: ignore[return-value]
        if repair and self._repair_cycle == cycle:
            raise Failure("authentication", 401)
        if not repair and self._renewed_cycle == cycle:
            if self.usable():
                return self._access_token  # type: ignore[return-value]
            raise self.last_error or Failure("timeout")
        if self._clock() < self.not_before:
            if self.usable():
                return self._access_token  # type: ignore[return-value]
            raise self.last_error  # type: ignore[misc]
        if self.usable() and self.expires_at - self._clock() > self.margin:
            return self._access_token
        if repair:
            self._repair_cycle = cycle
        self._renewed_cycle = cycle
        self._persistence_cycle = cycle
        self._inflight = asyncio.create_task(self._shared_refresh())
        return await self._wait_refresh(self._inflight)

    async def _wait_refresh(self, task: asyncio.Task[str | Failure]) -> str:
        result = await asyncio.shield(task)
        if isinstance(result, Failure):
            raise result
        return result

    async def _shared_refresh(self) -> str | Failure:
        # A cancelled sole waiter must not leave an exceptional detached task.
        try:
            return await self._renew()
        except Failure as error:
            return error

    async def _renew(self) -> str:
        try:
            token = await self._perform_refresh()
            self.failures = 0
            self.not_before = 0.0
            self.last_error = None
            return token
        except Failure as error:
            error.auth_failure = True
            if error.invalid_refresh:
                self.reauth_required = True
                self._access_token = None
                self.last_error = error
                raise
            self.failures += 1
            delays = (30, 60, 120, 240, 480, 900)
            self.not_before = self._clock() + max(
                delays[min(self.failures - 1, len(delays) - 1)], error.retry_after
            )
            self.last_error = error
            if self.usable():
                return self._access_token  # type: ignore[return-value]
            raise
        finally:
            self._inflight = None

    async def _safe_refresh(self) -> TokenReply:
        failure = None
        try:
            return await self._refresh(self.refresh_token)
        except Failure:
            raise
        except TimeoutError:
            failure = Failure("timeout")
        except OSError:
            failure = Failure("connection")
        except Exception:
            failure = Failure("unexpected")
        raise failure

    async def _perform_refresh(self) -> str:
        started = self._clock()
        reply = await self._safe_refresh()
        if (
            not isinstance(reply, TokenReply)
            or not isinstance(reply.access_token, str)
            or not reply.access_token
            or not isinstance(reply.refresh_token, str)
            or not reply.refresh_token
            or type(reply.expires_in) not in (int, float)
            or reply.expires_in <= 0
        ):
            raise Failure("invalid_data")
        lifetime = expires_at = None
        try:
            lifetime = float(reply.expires_in)
            expires_at = started + lifetime
        except OverflowError, ValueError:
            pass
        if (
            lifetime is None
            or expires_at is None
            or not (math.isfinite(lifetime) and math.isfinite(expires_at))
        ):
            raise Failure("invalid_data")
        self.refresh_token = reply.refresh_token
        self._access_token = reply.access_token
        self._rejected = False
        self.expires_at = expires_at
        self.margin = min(60.0, lifetime * 0.2)
        self._try_persist()
        if not self.usable():
            raise Failure("timeout")
        return reply.access_token

    def _try_persist(self) -> None:
        try:
            self._persist(self.refresh_token)
        except Exception:
            self.persistence_pending = True
        else:
            self.persistence_pending = False
