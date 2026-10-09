import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.api import OrderResult
from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.state import OrderSnapshot


async def test_sensor_identity_availability_and_allowlisted_diagnostics(hass):
    mod = importlib.import_module("custom_components.wolt_monitor.sensor")
    diag = importlib.import_module("custom_components.wolt_monitor.diagnostics")
    from custom_components.wolt_monitor.coordinator import WoltCoordinator

    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-private-secret"})
    entry.add_to_hass(hass)
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(
        OrderSnapshot(
            "fake-private-key",
            "unknown",
            "fake-private-restaurant",
            estimated_delivery_time=datetime(2026, 10, 6, 15, tzinfo=UTC),
        ),
        True,
        True,
    )
    api.async_courier.return_value = 120
    client.refresh.return_value = TokenReply("fake-private-access", "fake-private-refresh", 100)
    coordinator = WoltCoordinator(hass, entry, api, client, clock=lambda: 0)
    coordinator.runtime.wall_clock = lambda: datetime(2026, 10, 6, 14, 57, tzinfo=UTC)
    entry.runtime_data = coordinator
    await coordinator.async_refresh()
    entities = [mod.WoltSensor(coordinator, desc) for desc in mod.DESCRIPTIONS]
    assert [e.native_value for e in entities] == [
        "unknown",
        "fake-private-restaurant",
        datetime(2026, 10, 6, 15, tzinfo=UTC),
        3,
        None,
    ]
    assert all(e.available for e in entities[:-1])
    assert not entities[-1].available
    assert entities[0].unique_id == entry.entry_id + "_latest_order_status"
    assert entities[0].device_info["identifiers"] == {("wolt_monitor", entry.entry_id)}
    assert "manufacturer" not in entities[0].device_info
    report = await diag.async_get_config_entry_diagnostics(hass, entry)
    assert set(report) == {"integration", "configuration", "runtime", "entities"}
    assert set(report["runtime"]) == {
        "normalized_status",
        "reauthentication_required",
        "order",
        "courier",
        "unified",
    }
    assert set(report["entities"]) == {e.entity_description.key for e in entities} | {
        "latest_order_delivery_in_progress",
        "latest_order_delivered",
        "latest_order_courier_position",
    }
    assert report["entities"]["latest_order_delivery_in_progress"] == {
        "created": False,
        "available": False,
    }
    assert "fake-private" not in str(report)
    assert set(report["runtime"]["order"]) == {
        "last_successful_update",
        "consecutive_errors",
        "last_error_category",
        "last_http_status",
    }
    from custom_components.wolt_monitor.errors import Failure

    coordinator.runtime.fail("order", Failure("fake-private-error", 999), "bad")
    coordinator.data["status"] = "fake-private-raw-status"
    safe = await diag.async_get_config_entry_diagnostics(hass, entry)
    assert safe["runtime"]["normalized_status"] == "unknown"
    assert safe["runtime"]["order"]["last_error_category"] == "unexpected"
    assert safe["runtime"]["order"]["last_http_status"] is None
    assert "fake-private" not in str(safe)
    # Exercise HA's actual downloadable JSON envelope without opening an HTTP server.
    import json

    from homeassistant.components.diagnostics import _async_get_json_file_response

    response = await _async_get_json_file_response(
        hass, report, [], "wolt", "wolt_monitor", entry.entry_id
    )
    downloaded = json.loads(response.text)
    assert downloaded["data"] == report
    assert "fake-private" not in response.text
    for normalized in (
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
    ):
        coordinator.data["status"] = normalized
        state_report = await diag.async_get_config_entry_diagnostics(hass, entry)
        assert state_report["runtime"]["normalized_status"] == normalized
    coordinator.data["status"] = "arriving"
    state_report = await diag.async_get_config_entry_diagnostics(hass, entry)
    assert state_report["runtime"]["normalized_status"] == "unknown"
    await coordinator.async_shutdown()
