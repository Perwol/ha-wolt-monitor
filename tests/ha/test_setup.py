from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.api import OrderResult
from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.state import OrderSnapshot


async def test_real_platform_states_live_options_rotation_and_unload(hass):
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(
        OrderSnapshot("fake-key", "unknown", "fake-private-restaurant"),
        True,
        True,
    )
    api.async_courier.return_value = 50
    client.refresh.return_value = TokenReply("fake-access", "fake-rotated", 1000)
    entry = MockConfigEntry(
        domain="wolt_monitor", title="HA Wolt Monitor", data={"refresh_token": "fake-input"}
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch("custom_components.wolt_monitor.ClassicAuthClient", return_value=client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    assert entry.title == "Wolt Monitor"
    coordinator = entry.runtime_data
    assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == "unknown"
    assert (
        hass.states.get("sensor.wolt_monitor_latest_order_courier_distance").state == "unavailable"
    )
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            entry, options={"completed_order_retention_minutes": 0}
        )
        await hass.async_block_till_done()
        assert entry.runtime_data is coordinator
        assert coordinator.runtime.retention_minutes == 0
        coordinator._persist("fake-second-rotation")
        await hass.async_block_till_done()
        reload.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    client.close.assert_awaited_once()
    assert coordinator._poll_cancel is None
    assert coordinator._retention_cancel is None


async def test_synthetic_auth_failure_is_not_ready_and_closes_both_sessions(hass):
    from test_data_path import QueueSession
    from test_transport import Response

    data, auth = QueueSession([]), QueueSession([Response(503, {})])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wolt_monitor.transport.aiohttp.ClientSession", side_effect=[data, auth]
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state == ConfigEntryState.SETUP_RETRY
    assert entry.reason == "Unable to connect to Wolt"
    assert data.closed and auth.closed
    assert not data.calls
