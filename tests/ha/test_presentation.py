"""Native HA presentation metadata without a server or live Wolt reads."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers import entity_registry
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.api import OrderResult
from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.sensor import DESCRIPTIONS

STATUS_LABELS = {
    "received": ("Received", "Otrzymane"),
    "acknowledged": ("Accepted", "Przyjęte"),
    "scheduled": ("Scheduled", "Zaplanowane"),
    "preparing": ("Preparing", "W przygotowaniu"),
    "ready": ("Ready", "Gotowe"),
    "on_the_way": ("On the Way", "W drodze"),
    "delivered": ("Delivered", "Dostarczone"),
    "cancelled": ("Cancelled", "Anulowane"),
    "unknown": ("Unknown", "Nieznany"),
    "no_active_order": ("No Active Order", "Brak aktywnego zamówienia"),
}


@pytest.mark.parametrize("language,index", [("en", 0), ("pl", 1)])
async def test_enum_state_labels_resolve_through_native_ha_translation(hass, language, index):
    translated = await async_get_translations(hass, language, "entity", {"wolt_monitor"})
    root = "component.wolt_monitor.entity.sensor.latest_order_status.state."
    assert {
        key.removeprefix(root): value for key, value in translated.items() if key.startswith(root)
    } == {state: labels[index] for state, labels in STATUS_LABELS.items()}


def test_source_and_english_status_labels_are_byte_identical():
    component = Path(__file__).resolve().parents[2] / "custom_components" / "wolt_monitor"
    strings = component / "strings.json"
    english = component / "translations" / "en.json"
    assert strings.read_bytes() == english.read_bytes()
    assert json.loads(strings.read_text())["entity"]["sensor"]["latest_order_status"]["state"] == {
        state: labels[0] for state, labels in STATUS_LABELS.items()
    }
    description = next(desc for desc in DESCRIPTIONS if desc.key == "latest_order_status")
    assert description.device_class == SensorDeviceClass.ENUM
    assert description.options == list(STATUS_LABELS)
    assert description.translation_key == "latest_order_status"


async def test_translated_enum_keeps_raw_native_and_published_automation_states(hass):
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(None, courier_supported=True)
    client.refresh.return_value = TokenReply("fake-access", "fake-refresh", 3600)
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch("custom_components.wolt_monitor.ClassicAuthClient", return_value=client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    try:
        entity = entry.runtime_data.entities["latest_order_status"]
        assert entity.entity_id == "sensor.wolt_monitor_latest_order_status"
        assert entity.unique_id == f"{entry.entry_id}_latest_order_status"
        for raw in STATUS_LABELS:
            entry.runtime_data.data["status"] = raw
            entity.async_write_ha_state()
            assert entity.native_value == raw
            assert hass.states.get(entity.entity_id).state == raw
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_distance_display_precision_is_suggested_by_ha_without_changing_values(hass):
    description = next(desc for desc in DESCRIPTIONS if desc.key == "latest_order_courier_distance")
    assert description.suggested_display_precision == 0
    assert all(
        desc.suggested_display_precision is None for desc in DESCRIPTIONS if desc != description
    )
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(None, courier_supported=True)
    client.refresh.return_value = TokenReply("fake-access", "fake-refresh", 3600)
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch("custom_components.wolt_monitor.ClassicAuthClient", return_value=client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    try:
        entity = entry.runtime_data.entities["latest_order_courier_distance"]
        assert entity.suggested_display_precision == 0
        assert entity.native_unit_of_measurement == "m"
        assert entity.device_class == SensorDeviceClass.DISTANCE
        assert entity.entity_id == "sensor.wolt_monitor_latest_order_courier_distance"
        assert entity.unique_id == f"{entry.entry_id}_latest_order_courier_distance"
        registry = entity_registry.async_get(hass)
        registered = registry.async_get(entity.entity_id)
        assert registered.options["sensor"]["suggested_display_precision"] == 0
        assert "display_precision" not in registered.options["sensor"]
        # Exercise existing integer native values, not a new runtime rounding policy.
        for distance in (0, 111, 1975):
            entry.runtime_data.data["distance"] = distance
            entity.async_write_ha_state()
            assert entity.native_value == distance
            state = hass.states.get(entity.entity_id)
            assert state.state == str(distance)
            assert state.attributes["unit_of_measurement"] == "m"
            assert state.attributes["device_class"] == "distance"
        # A user preference remains distinct from, and is not overwritten by, the suggestion.
        registry.async_update_entity_options(
            entity.entity_id, "sensor", {"suggested_display_precision": 0, "display_precision": 2}
        )
        await hass.async_block_till_done()
        assert registry.async_get(entity.entity_id).options["sensor"] == {
            "suggested_display_precision": 0,
            "display_precision": 2,
        }
        assert entity.suggested_display_precision == 0
        assert entity.native_value == 1975
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
