"""Real transport/adapter/coordinator/entities, with synthetic HTTP only."""

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_data_api import details
from test_data_transport import GetSession
from test_transport import Response

from custom_components.wolt_monitor import api, transport
from custom_components.wolt_monitor.diagnostics import async_get_config_entry_diagnostics


class QueueSession(GetSession):
    def __init__(self, replies):
        super().__init__(None)
        self.replies = list(replies)
        self.closed = False
        self.close_count = 0

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.replies.pop(0)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.replies.pop(0)

    async def close(self):
        self.close_count += 1
        self.closed = True


def token_response():
    return Response(
        body={
            "access_token": "synthetic-access-secret",
            "refresh_token": "synthetic-rotated-secret",
            "expires_in": 1000,
        }
    )


def tracked_order(key="chosen"):
    return details(
        key,
        delivery_method="homedelivery",
        is_marketplace_v2=False,
        delivery_location={"coordinates": {"type": "Point", "coordinates": [21, 52]}},
    )


def tracking_response(key="chosen"):
    return Response(
        body={
            "order_details": tracked_order(key),
            "drivers": [{"delivering_your_order": True, "location": [21, 52]}],
        }
    )


async def test_distance_exists_from_empty_http_setup_through_tracking_and_reload(hass):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    key = "latest_order_courier_distance"
    entity_id = "sensor.wolt_monitor_latest_order_courier_distance"
    order = tracked_order()

    def completion():
        return Response(
            body={
                "result": {"order_view": {"order_pdrn": "4ujWWng.chosen", "status": "ORDER_PLACED"}}
            }
        )

    data = QueueSession(
        [
            Response(body={"order_details": []}),
            Response(body={"order_details": [order]}),
            completion(),
            tracking_response(),
            Response(body={"order_details": [order]}),
            completion(),
            Response(body={"order_details": order, "drivers": []}),
            Response(body={"order_details": [details("chosen")]}),
            completion(),
            Response(body={"order_details": [{**order, "status": "delivered"}]}),
        ]
    )
    reloaded_data = QueueSession([Response(body={"order_details": []})])
    auth = QueueSession([token_response()])
    reloaded_auth = QueueSession([token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-secret"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wolt_monitor.transport.aiohttp.ClientSession",
        side_effect=[data, auth, reloaded_data, reloaded_auth],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            coordinator = entry.runtime_data
            assert len(coordinator.entities) == 8
            distance = coordinator.entities[key]
            assert not coordinator.courier_supported
            assert distance.native_value is None
            assert not distance.available
            assert hass.states.get(entity_id).state == "unavailable"
            assert distance.unique_id == f"{entry.entry_id}_{key}"
            assert (
                len(
                    {frozenset(e.device_info["identifiers"]) for e in coordinator.entities.values()}
                )
                == 1
            )
            assert len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 1
            report = await async_get_config_entry_diagnostics(hass, entry)
            assert report["entities"][key] == {"created": True, "available": False}
            assert len(data.calls) == 1
            assert str(data.calls[0][0]).endswith("/subscriptions")
            registry = er.async_get(hass)
            initial_entries = er.async_entries_for_config_entry(registry, entry.entry_id)
            assert len(initial_entries) == 8
            registry_ids = {e.entity_id: e.id for e in initial_entries}
            for expected_state, supported in [
                ("0", True),
                ("unavailable", True),
                ("unavailable", False),
                ("unavailable", True),
            ]:
                await coordinator.async_request_refresh()
                await hass.async_block_till_done()
                assert hass.states.get(entity_id).state == expected_state
                assert coordinator.entities[key] is distance
                assert len(coordinator.entities) == 8
                assert coordinator.courier_supported is supported
                report = await async_get_config_entry_diagnostics(hass, entry)
                assert report["entities"][key] == {
                    "created": True,
                    "available": expected_state != "unavailable",
                }
                assert {
                    e.entity_id: e.id
                    for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                } == registry_ids
            assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == "delivered"
            assert len(data.calls) == 10
            assert sum("purchase_tracking" in str(url) for url, _ in data.calls) == 2
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.runtime_data is not coordinator
            assert len(entry.runtime_data.entities) == 8
            assert hass.states.get(entity_id).state == "unavailable"
            report = await async_get_config_entry_diagnostics(hass, entry)
            assert report["entities"][key] == {"created": True, "available": False}
            assert {
                e.entity_id: e.id
                for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            } == registry_ids
            assert len(reloaded_data.calls) == 1
            assert str(reloaded_data.calls[0][0]).endswith("/subscriptions")
        finally:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_http_forecast_reaches_single_entities_and_updates_dynamically(hass):
    from datetime import timedelta

    from homeassistant.util.dt import utcnow

    now = utcnow()
    first = details(
        delivery_eta_min=(now + timedelta(seconds=1)).isoformat(),
        delivery_eta_max=(now + timedelta(seconds=121)).isoformat(),
        delivery_eta=(now + timedelta(seconds=301)).isoformat(),
    )
    later = details(delivery_eta=(now + timedelta(seconds=601)).isoformat())
    delivered = details(status="delivered", delivery_eta=later["delivery_eta"])
    data = QueueSession(
        [
            Response(body={"order_details": [first]}),
            Response(body={"order_details": [later]}),
            Response(body={"order_details": [delivered]}),
        ]
    )
    auth = QueueSession([token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-secret"})
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.wolt_monitor.create_data_api",
            return_value=api.WoltDataAPI(transport.WoltDataClient(data)),
        ),
        patch(
            "custom_components.wolt_monitor.ClassicAuthClient",
            return_value=transport.ClassicAuthClient(auth),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    try:
        coordinator = entry.runtime_data
        forecast = coordinator.entities["latest_order_delivery_time"]
        eta = coordinator.entities["latest_order_eta"]
        assert forecast.native_value == now + timedelta(seconds=121)
        assert eta.native_value == 3
        assert len(coordinator.entities) == 8  # All entities exist before tracking.
        for expected_eta, expected_time in [(11, now + timedelta(seconds=601)), (0, None)]:
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()
            assert eta.native_value == expected_eta
            assert forecast.native_value == expected_time
        assert hass.states.get("sensor.wolt_monitor_latest_order_eta").state == "0"
        assert (
            hass.states.get("sensor.wolt_monitor_latest_order_delivery_time").state == "unavailable"
        )
        assert hass.states.get("binary_sensor.wolt_monitor_latest_order_delivered").state == "on"
        assert len(data.calls) == 3
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_assumed_initial_minutes_reach_entities_from_fixed_payment(hass, freezer):
    from datetime import UTC, datetime, timedelta

    payment = datetime(2026, 10, 7, 12, tzinfo=UTC)
    freezer.move_to(payment)
    ordinary = details(
        status="received",
        delivery_method="homedelivery",
        payment_time=payment.isoformat(),
        client_pre_estimate="46-56",
    )
    updates = [
        ordinary,
        ordinary,
        {**ordinary, "client_pre_estimate": "50-60"},
        {**ordinary, "delivery_eta": (payment + timedelta(minutes=70)).isoformat()},
        {
            **ordinary,
            "delivery_eta_min": (payment + timedelta(minutes=65)).isoformat(),
            "delivery_eta_max": (payment + timedelta(minutes=75)).isoformat(),
            "delivery_eta": (payment + timedelta(minutes=80)).isoformat(),
        },
        {**ordinary, "status": "delivered"},
    ]
    data = QueueSession([Response(body={"order_details": [value]}) for value in updates])
    auth = QueueSession([token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-secret"})
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.wolt_monitor.create_data_api",
            return_value=api.WoltDataAPI(transport.WoltDataClient(data)),
        ),
        patch(
            "custom_components.wolt_monitor.ClassicAuthClient",
            return_value=transport.ClassicAuthClient(auth),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    try:
        coordinator = entry.runtime_data
        now = [payment]
        coordinator.runtime.wall_clock = lambda: now[0]
        forecast = coordinator.entities["latest_order_delivery_time"]
        eta = coordinator.entities["latest_order_eta"]
        assert forecast.native_value == payment + timedelta(minutes=56)
        assert eta.native_value == 56
        now[0] += timedelta(minutes=3, seconds=1)
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert forecast.native_value == payment + timedelta(minutes=56)
        assert eta.native_value == 53
        assert hass.states.get("sensor.wolt_monitor_latest_order_eta").state == "53"
        assert (
            hass.states.get("sensor.wolt_monitor_latest_order_delivery_time").state
            == (payment + timedelta(minutes=56)).isoformat()
        )
        for upper, remaining in [(60, 57), (70, 67), (75, 72), (None, 0)]:
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()
            assert forecast.native_value == (
                payment + timedelta(minutes=upper) if upper is not None else None
            )
            assert eta.native_value == remaining
        assert len(data.calls) == 6
        assert (
            hass.states.get("sensor.wolt_monitor_latest_order_delivery_time").state == "unavailable"
        )
        assert hass.states.get("binary_sensor.wolt_monitor_latest_order_delivered").state == "on"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_selected_capability_drives_real_entity_path(hass, caplog):
    order = tracked_order()
    other = details("other", delivery_method="takeaway", is_marketplace_v2=False)
    data = QueueSession([Response(body={"order_details": [order, other]}), tracking_response()])
    auth = QueueSession([token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-input-secret"})
    entry.add_to_hass(hass)
    adapter = api.WoltDataAPI(transport.WoltDataClient(data))
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=adapter),
        patch(
            "custom_components.wolt_monitor.ClassicAuthClient",
            return_value=transport.ClassicAuthClient(auth),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    try:
        assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == "on_the_way"
        assert hass.states.get("sensor.wolt_monitor_latest_order_courier_distance").state == "0"
        assert (
            hass.states.get("binary_sensor.wolt_monitor_latest_order_delivery_in_progress").state
            == "on"
        )
        assert len(entry.runtime_data.entities) == 8
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert "secret" not in str(diagnostics) + caplog.text
        assert "chosen" not in str(diagnostics)
        assert "synthetic venue" not in str(diagnostics)
        assert len(data.calls) == 2
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "shutdown",
    [
        "unload",
        "stop",
        "first_refresh_failure",
        "auth_creation_failure",
        "coordinator_creation_failure",
    ],
)
async def test_owned_sessions_close_on_all_lifecycle_exits(hass, shutdown, caplog):
    import custom_components.wolt_monitor as integration

    data = QueueSession([Response(body={"order_details": []})])
    auth = QueueSession(
        [Response(503, {}) if shutdown == "first_refresh_failure" else token_response()]
    )
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-input-secret"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wolt_monitor.transport.aiohttp.ClientSession", side_effect=[data, auth]
    ):
        if shutdown in ("auth_creation_failure", "coordinator_creation_failure"):
            target = (
                "ClassicAuthClient" if shutdown == "auth_creation_failure" else "WoltCoordinator"
            )
            with (
                patch.object(
                    integration, target, side_effect=RuntimeError("synthetic creation failure")
                ),
                pytest.raises(RuntimeError),
            ):
                await integration.async_setup_entry(hass, entry)
        elif shutdown == "first_refresh_failure":
            assert not await hass.config_entries.async_setup(entry.entry_id)
        else:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            if shutdown == "unload":
                assert await hass.config_entries.async_unload(entry.entry_id)
            else:
                await hass.async_stop()
                await hass.async_block_till_done()
    assert data.closed
    assert data.close_count == 1
    assert not [record for record in caplog.records if record.levelname == "ERROR"]
    if shutdown != "auth_creation_failure":
        assert auth.closed
        assert auth.close_count == 1


async def test_terminal_details_replacement_restores_only_replacement_metadata(hass):
    first = details("first", delivery_method="takeaway", is_marketplace_v2=False)
    data = QueueSession(
        [
            Response(body={"order_details": [first]}),
            Response(
                body={
                    "result": {
                        "order_view": {
                            "order_pdrn": "4ujWWng.first",
                            "status": "ORDER_PLACED",
                        }
                    }
                }
            ),
            Response(body={"order_details": [tracked_order("replacement")]}),
            Response(body={"order_details": [details("first", status="delivered")]}),
            Response(
                body={
                    "result": {
                        "order_view": {
                            "order_pdrn": "4ujWWng.replacement",
                            "status": "ORDER_PLACED",
                        }
                    }
                }
            ),
            tracking_response("replacement"),
        ]
    )
    auth = QueueSession([token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-input-secret"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wolt_monitor.transport.aiohttp.ClientSession", side_effect=[data, auth]
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            await entry.runtime_data.async_request_refresh()
            await hass.async_block_till_done()
            assert entry.runtime_data.runtime.order.key == "replacement"
            assert entry.runtime_data.courier_supported
            assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == "on_the_way"
            assert len(data.calls) == 6
        finally:
            await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("final_status", [200, 401, 429])
async def test_data_401_has_one_repair_and_one_final_order_outcome(hass, final_status, caplog):
    from custom_components.wolt_monitor.coordinator import WoltCoordinator

    data = QueueSession(
        [Response(401, {"error_code": 126}), Response(final_status, {"order_details": [details()]})]
    )
    auth = QueueSession([token_response(), token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-input-secret"})
    entry.add_to_hass(hass)
    coordinator = WoltCoordinator(
        hass,
        entry,
        api.WoltDataAPI(transport.WoltDataClient(data)),
        transport.ClassicAuthClient(auth),
        clock=lambda: 0,
    )
    try:
        await coordinator.async_request_refresh()
        assert len(data.calls) == 2
        assert len(auth.calls) == 2
        assert coordinator.runtime.sources["order"].consecutive_errors == (
            0 if final_status == 200 else 1
        )
        assert not coordinator.runtime.reauth_required
        assert coordinator.tokens.usable() is (final_status != 401)
        assert "secret" not in caplog.text
    finally:
        await coordinator.async_shutdown()


async def test_real_adapter_preserves_independent_backoff_and_shared_retry_after(hass):
    from custom_components.wolt_monitor.coordinator import WoltCoordinator

    def subscription():
        return Response(body={"order_details": [tracked_order()]})

    data = QueueSession(
        [
            subscription(),
            tracking_response(),
            subscription(),
            Response(429, {}, {"Retry-After": "120", "Content-Type": "application/json"}),
            subscription(),
            tracking_response(),
        ]
    )
    auth = QueueSession([token_response()])
    now = [0.0]
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "synthetic-input-secret"})
    entry.add_to_hass(hass)
    coordinator = WoltCoordinator(
        hass,
        entry,
        api.WoltDataAPI(transport.WoltDataClient(data)),
        transport.ClassicAuthClient(auth),
        clock=lambda: now[0],
    )
    try:
        await coordinator.async_request_refresh()
        await coordinator.async_request_refresh()
        assert coordinator.runtime.sources["order"].consecutive_errors == 0
        assert coordinator.runtime.sources["courier"].consecutive_errors == 1
        assert coordinator.tokens.not_before == 0
        assert coordinator._data_not_before == 120
        assert coordinator.data["distance"] == 0
        await coordinator.async_request_refresh()
        assert len(data.calls) == 4
        assert len(auth.calls) == 1
        now[0] = 120
        await coordinator.async_request_refresh()
        assert len(data.calls) == 6
        assert coordinator.runtime.sources["courier"].consecutive_errors == 0
    finally:
        await coordinator.async_shutdown()
