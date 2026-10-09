"""Cross-component scheduling and lifecycle regressions, without a server."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.api import OrderResult
from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.coordinator import WoltCoordinator
from custom_components.wolt_monitor.errors import Failure
from custom_components.wolt_monitor.state import OrderSnapshot


async def test_data_rate_limit_blocks_courier_and_manual_requests(hass):
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    now = [0.0]
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(OrderSnapshot("fake-key", "unknown"), True, True)
    api.async_courier.return_value = 10
    client.refresh.return_value = TokenReply("fake-access", "fake-refresh", 1000)
    coordinator = WoltCoordinator(hass, entry, api, client, clock=lambda: now[0])
    try:
        await coordinator.async_request_refresh()
        api.async_order.side_effect = Failure("rate_limit", 429, retry_after=120)
        await coordinator.async_request_refresh()
        assert api.async_courier.await_count == 1
        before = coordinator.runtime.sources["order"].consecutive_errors
        await coordinator.async_request_refresh()
        assert api.async_order.await_count == 2
        assert coordinator.runtime.sources["order"].consecutive_errors == before
        assert coordinator.tokens.not_before == 0
        now[0] = 120
        api.async_order.side_effect = None
        await coordinator.async_request_refresh()
        assert api.async_order.await_count == 3
        assert api.async_courier.await_count == 2
    finally:
        await coordinator.async_shutdown()


async def test_failed_platform_setup_closes_client_and_timers(hass):
    import custom_components.wolt_monitor as integration

    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(None)
    client.refresh.return_value = TokenReply("fake-access", "fake-refresh", 1000)
    with (
        patch.object(integration, "create_data_api", return_value=api),
        patch.object(integration, "ClassicAuthClient", return_value=client),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", side_effect=RuntimeError
        ) as forward,
    ):
        with pytest.raises(RuntimeError):
            await integration.async_setup_entry(hass, entry)
    from homeassistant.const import Platform

    forward.assert_awaited_once_with(
        entry, [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]
    )
    client.close.assert_awaited_once()
    api.close.assert_awaited_once()
    assert entry.runtime_data._poll_cancel is None
    assert entry.runtime_data._retention_cancel is None


@pytest.mark.parametrize("backoff", ["auth", "data"])
async def test_service_update_entity_uses_coordinator_and_disabled_polling(hass, backoff):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "homeassistant", {})
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(OrderSnapshot("fake-key", "unknown"))
    client.refresh.return_value = TokenReply("fake-access", "fake-refresh", 1000)
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch("custom_components.wolt_monitor.ClassicAuthClient", return_value=client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert all(not sensor.should_poll for sensor in coordinator.entities.values())
    try:
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": "sensor.wolt_monitor_latest_order_status"},
            blocking=True,
        )
        assert api.async_order.await_count == 2
        assert client.refresh.await_count == 1
        if backoff == "auth":
            coordinator.tokens.reject("fake-access")
            client.refresh.side_effect = Failure("connection")
        else:
            api.async_order.side_effect = Failure("rate_limit", 429, retry_after=120)
        action = {"entity_id": "sensor.wolt_monitor_latest_order_status"}
        await hass.services.async_call("homeassistant", "update_entity", action, blocking=True)
        before = (
            api.async_order.await_count,
            client.refresh.await_count,
            coordinator.runtime.sources["order"].consecutive_errors,
        )
        assert before[2] == 1
        await hass.services.async_call("homeassistant", "update_entity", action, blocking=True)
        assert (
            api.async_order.await_count,
            client.refresh.await_count,
            coordinator.runtime.sources["order"].consecutive_errors,
        ) == before
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
