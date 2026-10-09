"""Token lifecycle tests; all credentials are fictional."""

import asyncio
import importlib

import pytest

from custom_components.wolt_monitor.auth import TokenManager, TokenReply


@pytest.mark.asyncio
async def test_concurrent_consumers_share_refresh_and_cached_token():
    calls = []
    ready = asyncio.Event()

    async def refresh(token):
        calls.append(token)
        await ready.wait()
        return TokenReply("fake-access", "fake-rotated", 100)

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: 0)
    tasks = [asyncio.create_task(manager.acquire("cycle")) for _ in range(5)]
    await asyncio.sleep(0)
    ready.set()
    assert await asyncio.gather(*tasks) == ["fake-access"] * 5
    assert await manager.acquire("next-cycle") == "fake-access"
    assert calls == ["fake-input"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("expires_in", 0),
        ("expires_in", -1),
        ("expires_in", True),
        ("expires_in", "100"),
        ("expires_in", float("inf")),
        ("expires_in", float("nan")),
        ("access_token", ""),
        ("access_token", None),
        ("refresh_token", ""),
        ("refresh_token", 42),
    ],
)
async def test_invalid_reply_does_not_replace_tokens(field, value):
    values = {"access_token": "fake-access", "refresh_token": "fake-rotated", "expires_in": 100}
    values[field] = value

    async def refresh(_):
        return TokenReply(**values)

    saved = []
    manager = TokenManager("fake-original", refresh, saved.append, clock=lambda: 0)
    with pytest.raises(Exception) as caught:
        await manager.acquire("cycle")
    assert caught.value.category == "invalid_data"
    assert manager.refresh_token == "fake-original"
    assert manager.expires_at == 0
    assert saved == []


@pytest.mark.asyncio
async def test_initial_refresh_uses_rotated_token_and_monotonic_expiry():
    module = importlib.import_module("custom_components.wolt_monitor.auth")
    calls = []
    saved = []

    async def refresh(token):
        calls.append(token)
        return module.TokenReply("fake-access", "fake-rotated", 100)

    manager = module.TokenManager("fake-input", refresh, saved.append, clock=lambda: 10)
    assert await manager.acquire("cycle-1") == "fake-access"
    assert calls == ["fake-input"]
    assert saved == ["fake-rotated"]
    assert manager.expires_at == 110
    assert manager.margin == 20
    assert manager.refresh_token == "fake-rotated"


async def test_backoff_counts_from_end_respects_retry_after_and_wait_is_not_failure():
    from custom_components.wolt_monitor.errors import Failure

    now = [0.0]
    calls = []

    async def refresh(_):
        calls.append(now[0])
        now[0] += 10
        raise Failure("rate_limit", 429, retry_after=45)

    manager = TokenManager("fake-original", refresh, lambda _: None, clock=lambda: now[0])
    for index, delay in enumerate([45, 60, 120, 240, 480, 900, 900]):
        with pytest.raises(Failure):
            await manager.acquire(str(index))
        assert manager.not_before == now[0] + delay
        assert manager.failures == index + 1
        with pytest.raises(Failure):
            await manager.acquire("waiting")
        assert len(calls) == index + 1
        assert manager.failures == index + 1
        now[0] = manager.not_before


async def test_refresh_failure_allows_valid_old_access_without_clearing_auth_backoff():
    from custom_components.wolt_monitor.errors import Failure

    now = [0.0]
    calls = []

    async def refresh(_):
        calls.append(1)
        if len(calls) == 1:
            return TokenReply("fake-access", "fake-rotated", 100)
        raise Failure("connection")

    manager = TokenManager("fake-original", refresh, lambda _: None, clock=lambda: now[0])
    await manager.acquire("first")
    now[0] = 80
    assert await manager.acquire("second") == "fake-access"
    assert manager.failures == 1
    assert manager.not_before == 110
    assert await manager.acquire("third") == "fake-access"
    assert len(calls) == 2
    now[0] = 100
    with pytest.raises(Failure):
        await manager.acquire("expired")
    assert manager.failures == 1


@pytest.mark.parametrize("received", [80, 100, 120])
async def test_delayed_reply_uses_request_start_and_no_repeat_for_margin(received):
    from custom_components.wolt_monitor.errors import Failure

    now = [0.0]
    saved = []
    calls = []

    async def refresh(_):
        calls.append(1)
        now[0] = received
        return TokenReply("fake-access", "fake-rotated", 100)

    manager = TokenManager("fake-original", refresh, saved.append, clock=lambda: now[0])
    if received >= 100:
        with pytest.raises(Failure) as caught:
            await manager.acquire("first")
        assert caught.value.category == "timeout"
        assert caught.value.http_status is None
        assert manager.not_before == received + 30
        assert manager.failures == 1
    else:
        assert await manager.acquire("first") == "fake-access"
        assert await manager.acquire("first") == "fake-access"
        now[0] = 100
        with pytest.raises(Failure) as caught:
            await manager.acquire("first")
        assert caught.value.category == "timeout"
    assert saved == ["fake-rotated"]
    assert manager.expires_at == 100
    assert calls == [1]


async def test_persistence_failure_does_not_undo_rotation_and_retries_latest_on_next_cycle():
    now = [0.0]
    saved = []
    replies = iter([TokenReply("fake-a1", "fake-r1", 100), TokenReply("fake-a2", "fake-r2", 100)])

    async def refresh(_):
        return next(replies)

    def persist(token):
        saved.append(token)
        if len(saved) <= 2:
            raise OSError("fake-secret must never appear in logs")
        return False

    manager = TokenManager("fake-input", refresh, persist, clock=lambda: now[0])
    assert await manager.acquire("first") == "fake-a1"
    assert manager.persistence_pending
    assert manager.failures == 0
    assert await manager.acquire("first") == "fake-a1"
    assert saved == ["fake-r1"]
    now[0] = 80
    assert await manager.acquire("second") == "fake-a2"
    assert saved[-1] == "fake-r2"
    assert not manager.persistence_pending


async def test_invalid_refresh_stops_future_attempts_even_with_old_valid_access():
    from custom_components.wolt_monitor.errors import Failure

    now = [0.0]
    calls = []

    async def refresh(_):
        calls.append(1)
        if len(calls) == 1:
            return TokenReply("fake-a", "fake-r", 100)
        raise Failure("authentication", 401, invalid_refresh=True)

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: now[0])
    await manager.acquire("first")
    now[0] = 80
    with pytest.raises(Failure):
        await manager.acquire("second")
    assert manager.reauth_required
    now[0] = 10000
    with pytest.raises(Failure):
        await manager.acquire("third")
    assert calls == [1, 1]


async def test_rejected_access_is_never_reused_and_repair_budget_is_shared():
    from custom_components.wolt_monitor.errors import Failure

    calls = []

    async def refresh(_):
        calls.append(1)
        return TokenReply("fake-a" + str(len(calls)), "fake-r" + str(len(calls)), 100)

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: 0)
    first = await manager.acquire("cycle")
    manager.reject(first)
    repaired = await manager.acquire("cycle", repair=True)
    assert repaired == "fake-a2"
    manager.reject(first)  # Stale 401 from concurrent consumer cannot reject the new token.
    assert manager.usable()
    manager.reject(repaired)
    with pytest.raises(Failure) as caught:
        await manager.acquire("cycle", repair=True)
    assert caught.value.http_status == 401
    assert not manager.usable()
    assert calls == [1, 1]
    assert await manager.acquire("next") == "fake-a3"


@pytest.mark.parametrize("lifetime,start", [(10**400, 0.0), (1e308, 1e308)])
async def test_overflowing_lifetime_or_expiry_is_invalid_without_rotating(lifetime, start):
    from custom_components.wolt_monitor.errors import Failure

    calls, saved = [], []

    async def refresh(_):
        calls.append(1)
        return TokenReply("fake-a", "fake-replacement", lifetime)

    manager = TokenManager("fake-original", refresh, saved.append, clock=lambda: start)
    for cycle in ["first", "second"] if start == 0 else ["first", "first"]:
        with pytest.raises(Failure) as caught:
            await manager.acquire(cycle)
        assert caught.value.category == "invalid_data"
        assert caught.value.__context__ is None
    assert calls == [1]
    assert saved == []
    assert manager.refresh_token == "fake-original"
    assert manager.failures == 1
    assert not manager.usable()


async def test_shared_failed_refresh_keeps_final_cause_for_each_consumer():
    from custom_components.wolt_monitor.errors import Failure

    async def refresh(_):
        raise Failure("connection")

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: 0)
    for _ in range(2):
        with pytest.raises(Failure) as caught:
            await manager.acquire("same-cycle")
        assert caught.value.category == "connection"
    assert manager.failures == 1


@pytest.mark.parametrize(
    "error,category",
    [
        (OSError("fictional-private-refresh-value"), "connection"),
        (TimeoutError("fictional-private-refresh-value"), "timeout"),
        (ValueError("fictional-private-refresh-value"), "unexpected"),
    ],
)
async def test_unexpected_refresh_exception_is_sanitized_and_backs_off(error, category):
    from custom_components.wolt_monitor.errors import Failure

    now = [0.0]
    calls = []

    async def refresh(_):
        calls.append(1)
        now[0] = 5
        raise error

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: now[0])
    for cycle in ["first", "second"]:
        with pytest.raises(Failure) as caught:
            await manager.acquire(cycle)
        assert caught.value.category == category
        assert caught.value.__context__ is None
        assert "fictional-private" not in str(caught.value)
    assert manager.failures == 1
    assert manager.not_before == 35
    assert calls == [1]


async def test_sole_cancelled_waiter_does_not_leave_unobserved_refresh_failure():
    import gc

    from custom_components.wolt_monitor.errors import Failure

    loop = asyncio.get_running_loop()
    events = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: events.append(context["message"]))
    entered, ready, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def refresh(_):
        entered.set()
        await ready.wait()
        finished.set()
        raise Failure("connection")

    try:
        manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: 0)
        waiter = asyncio.create_task(manager.acquire("first"))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        ready.set()
        await finished.wait()
        await asyncio.sleep(0)
        gc.collect()
        assert events == []
        assert manager.failures == 1
    finally:
        loop.set_exception_handler(old_handler)


async def test_cancelled_waiter_does_not_cancel_shared_refresh():
    calls = []
    ready = asyncio.Event()
    entered = asyncio.Event()

    async def refresh(_):
        calls.append(1)
        entered.set()
        await ready.wait()
        return TokenReply("fake-a", "fake-r", 100)

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: 0)
    first = asyncio.create_task(manager.acquire("cycle"))
    await entered.wait()
    second = asyncio.create_task(manager.acquire("cycle"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    ready.set()
    assert await second == "fake-a"
    assert calls == [1]
