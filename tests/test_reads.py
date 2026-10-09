"""End-to-end authenticated reads use fictional transports and no HA runtime."""

import importlib

import pytest

from custom_components.wolt_monitor.auth import TokenManager, TokenReply
from custom_components.wolt_monitor.errors import Failure


@pytest.mark.parametrize("final_category", [None, "authentication", "timeout"])
async def test_data_401_performs_one_shared_repair_and_one_retry(final_category):
    mod = importlib.import_module("custom_components.wolt_monitor.reads")
    refresh_calls = []
    read_calls = []

    async def refresh(_):
        refresh_calls.append(1)
        return TokenReply("fake-a" + str(len(refresh_calls)), "fake-r", 100)

    async def fetch(token):
        read_calls.append(token)
        if len(read_calls) == 1:
            raise Failure("authentication", 401)
        if final_category is not None:
            raise Failure(final_category, 401 if final_category == "authentication" else None)
        return "fake-value"

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: 0)
    if final_category is None:
        assert await mod.authenticated_read(manager, "cycle", fetch) == "fake-value"
    else:
        with pytest.raises(Failure) as caught:
            await mod.authenticated_read(manager, "cycle", fetch)
        assert caught.value.category == final_category
    assert read_calls == ["fake-a1", "fake-a2"]
    assert refresh_calls == [1, 1]
    assert not manager.reauth_required


async def test_data_retry_after_blocks_repair_and_keeps_authentication_401():
    from custom_components.wolt_monitor.reads import authenticated_read

    refresh_calls = []

    async def refresh(_):
        refresh_calls.append(1)
        return TokenReply("fake-a", "fake-r", 100)

    async def fetch(_):
        raise Failure("authentication", 401, retry_after=60)

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: 0)
    with pytest.raises(Failure) as caught:
        await authenticated_read(manager, "cycle", fetch)
    assert caught.value.http_status == 401
    assert refresh_calls == [1]
    assert not manager.usable()


@pytest.mark.parametrize(
    "error,category",
    [
        (TimeoutError("fake-private"), "timeout"),
        (OSError("fake-private"), "connection"),
        (ValueError("fake-private"), "unexpected"),
    ],
)
async def test_fetch_exceptions_are_sanitized(error, category):
    from custom_components.wolt_monitor.reads import authenticated_read

    async def refresh(_):
        return TokenReply("fake-a", "fake-r", 100)

    async def fetch(_):
        raise error

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: 0)
    with pytest.raises(Failure) as caught:
        await authenticated_read(manager, "cycle", fetch)
    assert caught.value.category == category
    assert caught.value.__context__ is None


async def test_real_refresh_failure_replaces_initial_access_401():
    from custom_components.wolt_monitor.reads import authenticated_read

    calls = []

    async def refresh(_):
        calls.append(1)
        if len(calls) == 1:
            return TokenReply("fake-a", "fake-r", 100)
        raise Failure("timeout")

    async def fetch(_):
        raise Failure("authentication", 401)

    manager = TokenManager("fake-input", refresh, lambda _: None, clock=lambda: 0)
    with pytest.raises(Failure) as caught:
        await authenticated_read(manager, "cycle", fetch)
    assert caught.value.category == "timeout"
    assert caught.value.http_status is None
