"""Approved entity contract exercised on real HA platforms."""

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.api import OrderResult, normalize_order
from custom_components.wolt_monitor.auth import TokenReply


async def test_new_entity_set_completion_refund_and_identity(hass):
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(
        normalize_order(
            {
                "order_id": "synthetic",
                "status": "delivered",
                "venue_name": "Synthetic venue",
                "delivery_eta": "2026-10-07T12:01:00Z",
            }
        ),
        courier_supported=True,
    )
    client.refresh.return_value = TokenReply("fake-access", "fake-refresh", 3600)
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch("custom_components.wolt_monitor.ClassicAuthClient", return_value=client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    expected = {
        "latest_order_status",
        "latest_order_restaurant",
        "latest_order_courier_distance",
        "latest_order_courier_position",
        "latest_order_delivery_time",
        "latest_order_eta",
        "latest_order_delivery_in_progress",
        "latest_order_delivered",
    }
    assert set(coordinator.entities) == expected
    from homeassistant.components.sensor import SensorDeviceClass

    status_description = coordinator.entities["latest_order_status"].entity_description
    assert status_description.device_class == SensorDeviceClass.ENUM
    assert set(status_description.options) == {
        "received",
        "acknowledged",
        "scheduled",
        "preparing",
        "ready",
        "on_the_way",
        "delivered",
        "cancelled",
        "unknown",
        "no_active_order",
    }
    for key, entity in coordinator.entities.items():
        assert entity.unique_id == f"{entry.entry_id}_{key}"
        assert entity.entity_description.translation_key == key
        assert entity.entity_description.entity_category is None
        assert entity.entity_description.entity_registry_enabled_default
    assert len(coordinator.entities) == 8
    assert hass.states.get("sensor.wolt_monitor_latest_order_eta").state == "0"
    assert hass.states.get("sensor.wolt_monitor_latest_order_delivery_time").state == "unavailable"
    forecast = coordinator.entities["latest_order_delivery_time"].entity_description
    eta = coordinator.entities["latest_order_eta"].entity_description
    assert forecast.device_class == SensorDeviceClass.TIMESTAMP
    assert forecast.icon == "mdi:clock-outline"
    assert eta.device_class == SensorDeviceClass.DURATION
    assert eta.native_unit_of_measurement == "min"
    assert eta.icon == "mdi:timer-outline"
    import json
    from pathlib import Path

    component = Path(__file__).parents[2] / "custom_components" / "wolt_monitor"
    for path in (component / "strings.json", component / "translations" / "en.json"):
        translations = json.loads(path.read_text())["entity"]["sensor"]
        assert set(translations) == expected - {
            "latest_order_delivery_in_progress",
            "latest_order_delivered",
            "latest_order_courier_position",
        }
        assert (
            translations["latest_order_delivery_time"]["name"]
            == "Latest Order Estimated Delivery Time"
        )
        assert translations["latest_order_eta"]["name"] == "Latest Order ETA"
    delivered_id = "binary_sensor.wolt_monitor_latest_order_delivered"
    assert hass.states.get(delivered_id).state == "on"
    deadline = coordinator.runtime.retention_deadline
    coordinator.runtime.update_order(
        normalize_order({"order_id": "synthetic", "status": "refunded"}), "refund"
    )
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    assert hass.states.get(delivered_id).state == "on"
    assert coordinator.runtime.retention_deadline == deadline
    coordinator.runtime.set_retention(0)
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    assert hass.states.get(delivered_id).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)
