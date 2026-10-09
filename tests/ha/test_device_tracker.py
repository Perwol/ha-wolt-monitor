"""Courier position on real HA libraries with synthetic HTTP only."""

from unittest.mock import patch

import pytest
from homeassistant.components.device_tracker import SourceType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_data_path import QueueSession, token_response, tracked_order, tracking_response
from test_transport import Response

from custom_components.wolt_monitor.diagnostics import async_get_config_entry_diagnostics

KEY = "latest_order_courier_position"
ENTITY_ID = f"device_tracker.wolt_monitor_{KEY}"


def test_tracker_inherits_supported_ha_state_attributes():
    from homeassistant.components.device_tracker.config_entry import TrackerEntity

    from custom_components.wolt_monitor.device_tracker import WoltCourierPosition

    assert "state_attributes" not in WoltCourierPosition.__dict__
    assert WoltCourierPosition.state_attributes is TrackerEntity.state_attributes
    assert "location_accuracy" not in WoltCourierPosition.__dict__
    assert "_attr_location_accuracy" not in WoltCourierPosition.__dict__


def completion():
    return Response(
        body={"result": {"order_view": {"order_pdrn": "4ujWWng.chosen", "status": "ORDER_PLACED"}}}
    )


async def test_position_always_registered_and_same_http_courier_reaches_ha(hass):
    data = QueueSession(
        [
            Response(body={"order_details": []}),
            Response(body={"order_details": [tracked_order()]}),
            completion(),
            tracking_response(),
        ]
    )
    auth = QueueSession([token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-secret"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wolt_monitor.transport.aiohttp.ClientSession", side_effect=[data, auth]
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            coordinator = entry.runtime_data
            assert len(coordinator.entities) == 8
            tracker = coordinator.entities[KEY]
            assert tracker.unique_id == f"{entry.entry_id}_{KEY}"
            assert tracker.source_type == SourceType.GPS
            assert tracker.location_accuracy == 0
            assert tracker.state_attributes == {
                "in_zones": [],
                "source_type": SourceType.GPS,
            }
            assert tracker.device_info["identifiers"] == {("wolt_monitor", entry.entry_id)}
            assert len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) == 8
            assert len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 1
            assert hass.states.get(ENTITY_ID).state == "unavailable"
            assert "latitude" not in hass.states.get(ENTITY_ID).attributes
            assert len(data.calls) == 1
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()
            state = hass.states.get(ENTITY_ID)
            assert state.state == "not_home"
            assert state.attributes["latitude"] == 52
            assert state.attributes["longitude"] == 21
            assert state.attributes["gps_accuracy"] == 0
            assert tracker.latitude == 52 and tracker.longitude == 21
            assert hass.states.get("sensor.wolt_monitor_latest_order_courier_distance").state == "0"
            assert len(data.calls) == 4
            assert sum("purchase_tracking" in str(url) for url, _ in data.calls) == 1
            report = await async_get_config_entry_diagnostics(hass, entry)
            assert report["entities"][KEY] == {"created": True, "available": True}
            assert "latitude" not in str(report) and "longitude" not in str(report)
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
        assert data.close_count == auth.close_count == 1


@pytest.mark.parametrize(
    "language,name",
    [("en", "Latest Order Courier Position"), ("pl", "Ostatnie Zamówienie Pozycja Kuriera")],
)
async def test_tracker_translation_and_delivery_moped_icon(hass, language, name):
    from homeassistant.helpers.translation import async_get_translations

    from custom_components.wolt_monitor.binary_sensor import DESCRIPTIONS

    translated = await async_get_translations(hass, language, "entity", {"wolt_monitor"})
    assert translated[f"component.wolt_monitor.entity.device_tracker.{KEY}.name"] == name
    assert DESCRIPTIONS[0].icon == "mdi:moped"
    assert DESCRIPTIONS[1].icon == "mdi:check-circle-outline"


@pytest.mark.parametrize("terminal", ["delivered", "rejected"])
async def test_http_position_attributes_clear_on_data_loss_terminal_expiry_and_reload(
    hass, caplog, terminal
):
    import logging

    caplog.set_level(logging.DEBUG, logger="custom_components.wolt_monitor")
    order = tracked_order()
    gps = {"delivering_your_order": True, "location": [13.24681, 45.13579]}

    def tracking(drivers, destination=True):
        details = order if destination else {**order, "delivery_location": None}
        return Response(body={"order_details": details, "drivers": drivers})

    replies = [Response(body={"order_details": [order]}), completion(), tracking([gps])]
    stages = [
        (tracking([{**gps, "delivering_your_order": False}]), False),
        (tracking([gps], destination=False), True),
        (tracking([gps, {"delivering_your_order": True}]), False),
        (tracking([gps]), True),
    ]
    for response, _ in stages:
        replies.extend([Response(body={"order_details": [order]}), completion(), response])
    replies.append(Response(body={"order_details": [{**order, "status": terminal}]}))
    if terminal == "rejected":
        replies.append(completion())
    data, auth = QueueSession(replies), QueueSession([token_response()])
    reload_data, reload_auth = (
        QueueSession([Response(body={"order_details": []})]),
        QueueSession([token_response()]),
    )
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-secret"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wolt_monitor.transport.aiohttp.ClientSession",
        side_effect=[data, auth, reload_data, reload_auth],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            coordinator = entry.runtime_data
            ids = {
                e.entity_id: e.id
                for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
            }
            tracker = coordinator.entities[KEY]
            assert hass.states.get(ENTITY_ID).attributes["latitude"] == 45.13579
            # Effective delivery enables GPS; destination is irrelevant to GPS.
            assert (
                hass.states.get(
                    "binary_sensor.wolt_monitor_latest_order_delivery_in_progress"
                ).state
                == "on"
            )
            for index, (_, available) in enumerate(stages):
                await coordinator.async_request_refresh()
                await hass.async_block_till_done()
                state = hass.states.get(ENTITY_ID)
                assert (state.state != "unavailable") is available
                if available:
                    assert state.attributes["latitude"] == 45.13579
                    assert state.attributes["longitude"] == 13.24681
                    assert state.attributes["gps_accuracy"] == 0
                    if index == 1:
                        assert (
                            hass.states.get(
                                "sensor.wolt_monitor_latest_order_courier_distance"
                            ).state
                            == "unavailable"
                        )
                else:
                    assert (
                        "latitude" not in state.attributes and "longitude" not in state.attributes
                    )
                    assert tracker.latitude is None and tracker.longitude is None
                report = await async_get_config_entry_diagnostics(hass, entry)
                assert report["entities"][KEY] == {"created": True, "available": available}
                assert "45.13579" not in str(report) and "13.24681" not in str(report)
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(ENTITY_ID).state == "unavailable"
            assert "latitude" not in hass.states.get(ENTITY_ID).attributes
            coordinator.runtime.set_retention(0)
            coordinator.async_set_updated_data(coordinator.runtime.values())
            await hass.async_block_till_done()
            assert hass.states.get(ENTITY_ID).state == "unavailable"
            assert tracker.latitude is None and tracker.longitude is None
            assert len(coordinator.entities) == 8
            assert sum("purchase_tracking" in str(url) for url, _ in data.calls) == 5
            assert not data.replies
            assert "45.13579" not in caplog.text and "13.24681" not in caplog.text
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert hass.states.get(ENTITY_ID).state == "unavailable"
            assert {
                e.entity_id: e.id
                for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
            } == ids
            assert len(entry.runtime_data.entities) == 8
            assert data.close_count == auth.close_count == 1
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
        assert reload_data.close_count == reload_auth.close_count == 1


async def test_standard_ha_zone_state_and_coordinate_update_without_scalar_change(hass):
    hass.states.async_set(
        "zone.home",
        "Home",
        {
            "latitude": 52,
            "longitude": 21,
            "radius": 100,
            "passive": False,
        },
    )
    order = tracked_order()
    destination = {
        **order,
        "delivery_location": {"coordinates": {"type": "Point", "coordinates": [22, 53]}},
    }
    data = QueueSession(
        [
            Response(body={"order_details": [order]}),
            completion(),
            tracking_response(),
            Response(body={"order_details": [order]}),
            completion(),
            Response(
                body={
                    "order_details": destination,
                    "drivers": [{"delivering_your_order": True, "location": [22, 53]}],
                }
            ),
        ]
    )
    auth = QueueSession([token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-secret"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wolt_monitor.transport.aiohttp.ClientSession", side_effect=[data, auth]
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            coordinator = entry.runtime_data
            assert hass.states.get(ENTITY_ID).state == "home"
            assert (
                hass.states.get(
                    "binary_sensor.wolt_monitor_latest_order_delivery_in_progress"
                ).state
                == "on"
            )
            before = coordinator.data.copy()
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()
            assert (
                coordinator.data == before
            )  # GPS updates independently of unchanged scalar values.
            state = hass.states.get(ENTITY_ID)
            assert state.state == "not_home"
            assert state.attributes["latitude"] == 53 and state.attributes["longitude"] == 22
            assert state.attributes["gps_accuracy"] == 0
            assert len(data.calls) == 6
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("source", ["order", "courier"])
async def test_tracker_ha_state_drops_gps_on_third_error_with_suppressed_backoff(hass, source):
    order = tracked_order()
    replies = [Response(body={"order_details": [order]}), completion(), tracking_response()]
    for _ in range(3):
        if source == "courier":
            replies.extend(
                [Response(body={"order_details": [order]}), completion(), Response(503, {})]
            )
        else:
            replies.extend([Response(503, {}), completion(), tracking_response()])
    data, auth = QueueSession(replies), QueueSession([token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-secret"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wolt_monitor.transport.aiohttp.ClientSession", side_effect=[data, auth]
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            coordinator = entry.runtime_data
            now = [coordinator.clock()]
            coordinator.clock = coordinator.scheduler._clock = coordinator.tokens._clock = lambda: (
                now[0]
            )
            coordinator.runtime._clock = lambda: now[0]
            for n in range(1, 4):
                if n > 1:
                    now[0] += 300
                await coordinator.async_request_refresh()
                await hass.async_block_till_done()
                assert coordinator.runtime.sources[source].consecutive_errors == n
                assert coordinator.runtime.sources[source].last_http_status == 503
                state = hass.states.get(ENTITY_ID)
                assert hass.states.get(
                    "binary_sensor.wolt_monitor_latest_order_delivery_in_progress"
                ).state == ("on" if n < 3 else "unavailable")
                assert hass.states.get(
                    "sensor.wolt_monitor_latest_order_courier_distance"
                ).state == ("0" if n < 3 else "unavailable")
                if n < 3:
                    assert state.state != "unavailable" and state.attributes["latitude"] == 52
                else:
                    assert state.state == "unavailable"
                    assert (
                        "latitude" not in state.attributes and "longitude" not in state.attributes
                    )
                if n < 3:
                    before = sum(
                        ("subscriptions" if source == "order" else "purchase_tracking") in str(url)
                        for url, _ in data.calls
                    )
                    # No failed-source request is introduced by the new tracker.
                    if source == "order":
                        # Healthy courier is independently due on manual refresh.
                        data.replies[0:0] = [tracking_response()]
                    else:
                        data.replies[0:0] = [
                            Response(body={"order_details": [order]}),
                            completion(),
                        ]
                    await coordinator.async_request_refresh()
                    await hass.async_block_till_done()
                    after = sum(
                        ("subscriptions" if source == "order" else "purchase_tracking") in str(url)
                        for url, _ in data.calls
                    )
                    assert before == after
                    assert coordinator.runtime.sources[source].consecutive_errors == n
            assert len(coordinator.entities) == 8
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
