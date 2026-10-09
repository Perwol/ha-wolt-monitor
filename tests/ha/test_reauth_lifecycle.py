"""Reauthentication exercises HA reload and identity preservation end-to-end."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.api import OrderResult
from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.errors import Failure
from custom_components.wolt_monitor.state import OrderSnapshot


@pytest.mark.parametrize("source", ["reauth", "options"])
async def test_loaded_reauth_resets_runtime_preserves_identity_and_closes_old_client(
    hass, caplog, source
):
    api = AsyncMock()
    api.async_order.return_value = OrderResult(
        OrderSnapshot("fake-old-order", "delivered", terminal=True)
    )
    old_client, new_client = AsyncMock(), AsyncMock()
    old_client.refresh.return_value = TokenReply("fake-old-access", "fake-old-refresh", 1000)
    new_client.refresh.return_value = TokenReply("fake-new-access", "fake-new-refresh", 1000)
    entry = MockConfigEntry(
        domain="wolt_monitor", title="HA Wolt Monitor", data={"refresh_token": "fake-input"}
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch(
            "custom_components.wolt_monitor.ClassicAuthClient", side_effect=[old_client, new_client]
        ),
        patch(
            "custom_components.wolt_monitor.config_flow.validate_token",
            AsyncMock(return_value="fake-validated"),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        old = entry.runtime_data
        before = {
            key: (sensor.unique_id, sensor.entity_id, sensor.device_info["identifiers"])
            for key, sensor in old.entities.items()
        }
        assert old.runtime.retention_deadline is not None
        old.runtime.fail("order", Failure("connection"), "fake-failure")
        old.tokens.failures = 3
        api.async_order.return_value = OrderResult(None)
        try:
            if source == "reauth":
                result = await hass.config_entries.flow.async_init(
                    "wolt_monitor",
                    context={"source": "reauth", "entry_id": entry.entry_id},
                    data=entry.data,
                )
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"], {"refresh_token": "fake-input2"}
                )
                assert result["reason"] == "reauth_successful"
            else:
                result = await hass.config_entries.options.async_init(entry.entry_id)
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"],
                    {"refresh_token": "fake-input2", "no_active_order_polling_interval_seconds": 5},
                )
            await hass.async_block_till_done()
            current = entry.runtime_data
            assert current is not old
            assert current.runtime.retention_deadline is None
            assert current.tokens.failures == 0
            assert current.runtime.sources["order"].consecutive_errors == 0
            assert current.data["status"] == "no_active_order"
            after = {
                key: (sensor.unique_id, sensor.entity_id, sensor.device_info["identifiers"])
                for key, sensor in current.entities.items()
            }
            assert after == before
            old_client.close.assert_awaited_once()
            assert "2026.12.0" not in caplog.text
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
    new_client.close.assert_awaited_once()
