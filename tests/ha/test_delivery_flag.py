"""Delivery flag stays tri-state and independent of ETA/courier presence."""

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.api import OrderResult
from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.errors import Failure
from custom_components.wolt_monitor.state import CourierSnapshot, OrderSnapshot


async def test_delivery_flag_real_platform_tristate_and_cleanup(hass):
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(None)
    client.refresh.return_value = TokenReply("fake-access", "fake-refresh", 3600)
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch("custom_components.wolt_monitor.ClassicAuthClient", return_value=client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = "binary_sensor.wolt_monitor_latest_order_delivery_in_progress"
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unavailable"
    coordinator = entry.runtime_data
    flag = coordinator.entities["latest_order_delivery_in_progress"]
    assert flag.unique_id == f"{entry.entry_id}_latest_order_delivery_in_progress"
    assert flag.device_info["identifiers"] == {("wolt_monitor", entry.entry_id)}
    assert flag.entity_description.translation_key == "latest_order_delivery_in_progress"

    for value, expected in [
        (False, "off"),
        (True, "off"),
        (None, "unavailable"),
        (1, "unavailable"),
    ]:
        coordinator.runtime.update_order(
            OrderSnapshot("fake-order", "preparing", delivery_in_progress=value), "change"
        )
        coordinator.async_set_updated_data(coordinator.runtime.values())
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == expected
    coordinator.runtime.update_order(OrderSnapshot("fake-order", "ready"), "active")
    coordinator.runtime.update_courier(
        CourierSnapshot(delivery_in_progress=True), "active", order_key="fake-order"
    )
    for i in range(2):
        coordinator.runtime.fail("order", Failure("timeout"), f"cache-{i}")
        coordinator.async_set_updated_data(coordinator.runtime.values())
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == "on"
    coordinator.runtime.fail("order", Failure("timeout"), "cache-2")
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    coordinator.runtime.update_order(OrderSnapshot("fake-order", "ready"), "recovery")
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"
    coordinator.runtime.reauth_required = True
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    coordinator.runtime.reauth_required = False
    # Terminal wins over a contradictory flag; expiry leaves no selected order.
    coordinator.runtime.update_order(
        OrderSnapshot("fake-order", "delivered", terminal=True, delivery_in_progress=True), "end"
    )
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"
    coordinator.runtime.set_retention(0)
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    for i in range(3):
        coordinator.runtime.fail("order", Failure("connection"), str(i))
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)
    client.close.assert_awaited_once()
