"""No selected order is unavailable, not a negative delivery assertion."""

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.api import OrderResult
from custom_components.wolt_monitor.auth import TokenReply


async def test_initial_empty_order_has_unavailable_binary_entities(hass):
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
    coordinator = entry.runtime_data
    assert coordinator.runtime.order is None
    assert coordinator.runtime.sources["order"].consecutive_errors == 0
    assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == "no_active_order"
    for key in ("latest_order_delivery_in_progress", "latest_order_delivered"):
        entity = coordinator.entities[key]
        assert entity.available is False
        assert entity.is_on is None
        assert hass.states.get(entity.entity_id).state == "unavailable"
    for key in ("latest_order_delivery_time", "latest_order_eta", "latest_order_restaurant"):
        assert hass.states.get(coordinator.entities[key].entity_id).state == "unavailable"
    from custom_components.wolt_monitor.diagnostics import async_get_config_entry_diagnostics

    report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["runtime"]["normalized_status"] == "no_active_order"
    for key in ("latest_order_delivery_in_progress", "latest_order_delivered"):
        assert report["entities"][key] == {"created": True, "available": False}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_retained_terminal_binary_states_expire_without_a_request(hass):
    from custom_components.wolt_monitor.state import CourierSnapshot, OrderSnapshot

    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(OrderSnapshot("synthetic", "ready"))
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
    now = [0.0]
    coordinator.runtime._clock = lambda: now[0]
    flags = [
        coordinator.entities[k]
        for k in ("latest_order_delivery_in_progress", "latest_order_delivered")
    ]

    async def published(expected):
        coordinator.async_set_updated_data(coordinator.runtime.values())
        await hass.async_block_till_done()
        for entity, state in zip(flags, expected, strict=True):
            assert entity.available is (state != "unavailable")
            assert hass.states.get(entity.entity_id).state == state

    coordinator.runtime.update_courier(
        CourierSnapshot(delivery_in_progress=True), "direct", order_key="synthetic"
    )
    await published(("on", "off"))
    identities = [(e.entity_id, e.unique_id) for e in flags]
    for key, status, expected in [
        ("synthetic", "delivered", ("off", "on")),
        ("cancelled", "cancelled", ("off", "off")),
    ]:
        if key == "cancelled":
            coordinator.runtime.update_order(OrderSnapshot(key, "preparing"), "new")
        coordinator.runtime.update_order(OrderSnapshot(key, status, terminal=True), "end")
        await published(expected)
        coordinator.runtime.update_order(None, "empty-during-retention")
        await published(expected)
        now[0] += 600
        await published(("unavailable", "unavailable"))
        assert coordinator.runtime.order is None
        assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == "no_active_order"
        assert hass.states.get("sensor.wolt_monitor_latest_order_eta").state == "unavailable"
    coordinator.runtime.update_order(OrderSnapshot("zero", "preparing"), "new")
    coordinator.runtime.set_retention(0)
    coordinator.runtime.update_order(OrderSnapshot("zero", "delivered", terminal=True), "end")
    await published(("unavailable", "unavailable"))
    assert identities == [(e.entity_id, e.unique_id) for e in flags]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unknown_selected_and_source_errors_keep_existing_policy(hass):
    from custom_components.wolt_monitor.api import normalize_order
    from custom_components.wolt_monitor.errors import Failure

    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(
        normalize_order({"order_id": "synthetic", "status": "not-mapped"})
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
    assert coordinator.runtime.order is not None
    assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == "unknown"
    assert (
        hass.states.get("binary_sensor.wolt_monitor_latest_order_delivered").state == "unavailable"
    )
    # Missing tracking is unavailable; valid tracking must not promote unknown.
    assert (
        hass.states.get("binary_sensor.wolt_monitor_latest_order_delivery_in_progress").state
        == "unavailable"
    )
    from custom_components.wolt_monitor.state import CourierSnapshot

    coordinator.runtime.update_courier(
        CourierSnapshot(delivery_in_progress=True), "tracking", order_key="synthetic"
    )
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    assert (
        hass.states.get("binary_sensor.wolt_monitor_latest_order_delivery_in_progress").state
        == "off"
    )
    for i in range(3):
        coordinator.runtime.fail("order", Failure("invalid_data"), f"missing-{i}")
        coordinator.async_set_updated_data(coordinator.runtime.values())
        await hass.async_block_till_done()
        assert coordinator.runtime.order is not None
        assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == (
            "unknown" if i < 2 else "unavailable"
        )
        assert hass.states.get(
            "binary_sensor.wolt_monitor_latest_order_delivery_in_progress"
        ).state == ("off" if i < 2 else "unavailable")
    coordinator.runtime.update_order(None, "empty-recovered")
    coordinator.async_set_updated_data(coordinator.runtime.values())
    await hass.async_block_till_done()
    for key in ("latest_order_delivery_in_progress", "latest_order_delivered"):
        assert not coordinator.entities[key].available
        assert hass.states.get(coordinator.entities[key].entity_id).state == "unavailable"
    assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == "no_active_order"
    assert await hass.config_entries.async_unload(entry.entry_id)
