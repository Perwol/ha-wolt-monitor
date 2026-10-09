"""Current direct-leg contract through real HTTP, coordinator and HA entity states."""

import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from time import monotonic
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_data_path import QueueSession, token_response, tracked_order
from test_transport import Response

from custom_components.wolt_monitor import api, transport
from custom_components.wolt_monitor.coordinator import WoltCoordinator

TRUE = {"delivering_your_order": True, "location": [21, 52]}
FALSE = {"delivering_your_order": False, "location": [21, 52]}


@asynccontextmanager
async def integration(hass, replies, *, clock=None):
    data = replies if isinstance(replies, QueueSession) else QueueSession(replies)
    token = Response(body={**token_response().body, "expires_in": 100000})
    auth = QueueSession([token])
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
        patch(
            "custom_components.wolt_monitor.WoltCoordinator",
            side_effect=lambda *args: WoltCoordinator(*args, clock=clock or monotonic),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    try:
        yield entry.runtime_data, data
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


def cycle(order, drivers, *, tracking_key=None):
    return [
        Response(body={"order_details": [order]}),
        Response(
            body={
                "order_details": {**order, "order_id": tracking_key or order["order_id"]},
                "drivers": drivers,
            }
        ),
    ]


def states(hass):
    return tuple(
        hass.states.get(entity).state
        for entity in (
            "sensor.wolt_monitor_latest_order_status",
            "binary_sensor.wolt_monitor_latest_order_delivery_in_progress",
            "sensor.wolt_monitor_latest_order_courier_distance",
        )
    )


@pytest.mark.parametrize(
    "raw,drivers,status,flag,distance",
    [
        ("production", [TRUE], "preparing", "off", "unavailable"),
        ("ready", [TRUE, TRUE], "ready", "unavailable", "unavailable"),
        ("ready", [TRUE], "on_the_way", "on", "0"),
        ("ready", [FALSE], "ready", "off", "unavailable"),
        ("ready", [], "ready", "unavailable", "unavailable"),
        ("ready", [{}], "ready", "unavailable", "unavailable"),
        ("ready", [{"delivering_your_order": 1}], "ready", "unavailable", "unavailable"),
        ("ready", [{"delivering_your_order": "true"}], "ready", "unavailable", "unavailable"),
        ("ready", [{**TRUE, "location": [181, 52]}], "on_the_way", "on", "unavailable"),
        ("ready", [{"delivering_your_order": True}], "on_the_way", "on", "unavailable"),
        ("ready", [TRUE, FALSE], "on_the_way", "on", "0"),
        ("ready", [FALSE, {}], "ready", "unavailable", "unavailable"),
        ("production", [], "preparing", "off", "unavailable"),
        ("picked_up", [TRUE], "unknown", "off", "unavailable"),
        ("refunded", [], "unknown", "unavailable", "unavailable"),
    ],
)
async def test_current_direct_leg_states(hass, raw, drivers, status, flag, distance):
    order = {**tracked_order(), "status": raw}
    async with integration(hass, cycle(order, drivers)) as (coordinator, data):
        assert states(hass) == (status, flag, distance)
        assert coordinator.runtime.sources["courier"].consecutive_errors == 0
        assert len(data.calls) == 2


@pytest.mark.parametrize(
    "raw,drivers,flag,available",
    [
        ("production", [TRUE], "off", False),
        ("picked_up", [TRUE], "off", False),
        ("ready", [FALSE], "off", False),
        ("ready", [], "unavailable", False),
        ("ready", [TRUE, TRUE], "unavailable", False),
        ("ready", [{"delivering_your_order": "true"}], "unavailable", False),
        ("ready", [{**TRUE, "location": [181, 52]}], "on", False),
        ("ready", [TRUE], "on", True),
    ],
)
async def test_shared_gate_clears_published_gps_after_eligible_delivery(
    hass, raw, drivers, flag, available
):
    order = tracked_order()
    replies = cycle(order, [TRUE]) + cycle({**order, "status": raw}, drivers)
    async with integration(hass, replies) as (coordinator, data):
        tracker_id = "device_tracker.wolt_monitor_latest_order_courier_position"
        assert hass.states.get(tracker_id).attributes["latitude"] == 52
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert states(hass)[1:] == (flag, "0" if available else "unavailable")
        position = hass.states.get(tracker_id)
        assert (position.state != "unavailable") is available
        if available:
            assert position.attributes["latitude"] == 52
            assert position.attributes["longitude"] == 21
            assert position.attributes["gps_accuracy"] == 0
        else:
            assert "latitude" not in position.attributes
            assert "longitude" not in position.attributes
            tracker = coordinator.entities["latest_order_courier_position"]
            assert tracker.latitude is None and tracker.longitude is None
        assert len(coordinator.entities) == 8
        assert len(data.calls) == 4


async def test_direct_leg_reverts_without_pickup_latch_and_clears_inconclusive_data(hass):
    order = tracked_order()
    replies = [
        response
        for drivers in ([TRUE], [FALSE], [TRUE], [], [TRUE, TRUE], [TRUE])
        for response in cycle(order, drivers)
    ]
    async with integration(hass, replies) as (coordinator, data):
        assert states(hass) == ("on_the_way", "on", "0")
        for expected in [
            ("ready", "off", "unavailable"),
            ("on_the_way", "on", "0"),
            ("ready", "unavailable", "unavailable"),
            ("ready", "unavailable", "unavailable"),
            ("on_the_way", "on", "0"),
        ]:
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()
            assert states(hass) == expected
            assert coordinator.runtime.sources["courier"].consecutive_errors == 0
        assert len(data.calls) == 12


async def test_missing_tracking_errors_keep_two_cached_reads_then_ready_unavailable(hass):
    order = tracked_order()
    replies = cycle(order, [TRUE])
    for _ in range(3):
        replies.extend([Response(body={"order_details": [order]}), Response(body={})])
    replies.extend(cycle(order, [FALSE]))
    now = [0.0]
    async with integration(hass, replies, clock=lambda: now[0]) as (coordinator, data):
        for count in range(1, 4):
            now[0] += 900
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()
            assert states(hass) == (
                ("on_the_way", "on", "0") if count < 3 else ("ready", "unavailable", "unavailable")
            )
            assert coordinator.runtime.sources["courier"].consecutive_errors == count
            before = len(data.calls)
            # Manual request reads healthy order only; suppressed courier is not a fourth error.
            data.replies.insert(0, Response(body={"order_details": [order]}))
            await coordinator.async_request_refresh()
            assert len(data.calls) == before + 1
            assert coordinator.runtime.sources["courier"].consecutive_errors == count
        now[0] += 900
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert states(hass) == ("ready", "off", "unavailable")
        assert coordinator.runtime.sources["courier"].consecutive_errors == 0
        assert len(data.calls) == 13


async def test_new_order_and_foreign_tracking_do_not_inherit_direct_leg(hass):
    old = tracked_order()
    new = {**tracked_order("replacement"), "status": "production"}
    replacement = cycle(new, [TRUE], tracking_key="chosen")
    replacement.insert(1, Response(body={"order_details": [{**old, "status": "delivered"}]}))
    replies = cycle(old, [TRUE]) + replacement + cycle(new, [TRUE])
    async with integration(hass, replies) as (coordinator, data):
        assert states(hass) == ("on_the_way", "on", "0")
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert states(hass) == ("preparing", "off", "unavailable")
        assert coordinator.runtime.sources["courier"].consecutive_errors == 1
        assert coordinator.runtime.order.key == "replacement"
        now = [coordinator.clock() + 900]
        coordinator.clock = coordinator.scheduler._clock = lambda: now[0]
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert states(hass) == ("preparing", "off", "unavailable")
        assert len(data.calls) == 7


async def test_replacement_during_courier_backoff_does_not_transfer_cached_true(hass):
    old, new = tracked_order(), tracked_order("replacement")
    replies = (
        cycle(old, [TRUE])
        + [
            Response(body={"order_details": [old]}),
            Response(body={}),
            Response(body={"order_details": [{**old, "status": "delivered"}, new]}),
            Response(body={"order_details": [new]}),
        ]
        + cycle(new, [FALSE])
    )
    now = [0.0]
    async with integration(hass, replies, clock=lambda: now[0]) as (coordinator, data):
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert states(hass) == ("on_the_way", "on", "0")
        assert coordinator.runtime.sources["courier"].consecutive_errors == 1
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert states(hass) == ("ready", "unavailable", "unavailable")
        assert coordinator.runtime.order.key == "replacement"
        assert coordinator.runtime.sources["courier"].consecutive_errors == 1
        assert len(data.calls) == 5
        await coordinator.async_request_refresh()
        assert len(data.calls) == 6
        assert coordinator.runtime.sources["courier"].consecutive_errors == 1
        now[0] = 120
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert states(hass) == ("ready", "off", "unavailable")
        assert coordinator.runtime.sources["courier"].consecutive_errors == 0
        assert len(data.calls) == 8


@pytest.mark.parametrize(
    "raw,expected,delivered,eta",
    [("delivered", "delivered", "on", "0"), ("rejected", "cancelled", "off", "unavailable")],
)
async def test_terminal_evidence_clears_current_leg(hass, raw, expected, delivered, eta):
    order = tracked_order()
    replies = cycle(order, [TRUE]) + [Response(body={"order_details": [{**order, "status": raw}]})]
    async with integration(hass, replies) as (coordinator, data):
        assert states(hass) == ("on_the_way", "on", "0")
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert states(hass) == (expected, "off", "unavailable")
        assert (
            hass.states.get("binary_sensor.wolt_monitor_latest_order_delivered").state == delivered
        )
        assert hass.states.get("sensor.wolt_monitor_latest_order_eta").state == eta
        assert (
            hass.states.get("sensor.wolt_monitor_latest_order_delivery_time").state == "unavailable"
        )
        assert len(data.calls) == 3


@pytest.mark.parametrize(
    "prefix,expected",
    [
        (
            "natural-order-stage-",
            [
                "preparing",
                "preparing",
                "preparing",
                "on_the_way",
                "ready",
                "on_the_way",
                "delivered",
            ],
        ),
        (
            "natural-order-two-stage-",
            [
                "acknowledged",
                "preparing",
                "preparing",
                "preparing",
                "preparing",
                "ready",
                "on_the_way",
                "on_the_way",
                "delivered",
            ],
        ),
    ],
)
async def test_both_immutable_orders_through_http_and_published_states(hass, prefix, expected):
    folder = Path(__file__).parents[1] / "fixtures" / "wolt"
    captures = [json.loads(p.read_text()) for p in sorted(folder.glob(prefix + "*.json"))]
    stages = [c["stages"][0] for c in captures]
    assert len(stages) == len(expected)
    # Dynamic local response router avoids assuming optional tracking request counts.
    data = QueueSession([])
    current = [stages[0]]

    def get(url, **kwargs):
        data.calls.append((url, kwargs))
        family = "tracking" if "purchase_tracking" in str(url) else "subscriptions"
        return Response(body=copy.deepcopy(current[0]["responses"][family]))

    data.get = get
    now = [0.0]
    start = datetime.fromisoformat(stages[0]["at"])
    async with integration(hass, data, clock=lambda: now[0]) as (coordinator, _):
        for stage, status in zip(stages, expected, strict=True):
            current[0] = stage
            wall = datetime.fromisoformat(stage["at"])
            coordinator.runtime.wall_clock = lambda wall=wall: wall
            now[0] = (wall - start).total_seconds()
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()
            assert hass.states.get("sensor.wolt_monitor_latest_order_status").state == status
            assert hass.states.get(
                "binary_sensor.wolt_monitor_latest_order_delivery_in_progress"
            ).state == ("on" if status == "on_the_way" else "off")
        assert coordinator.data["eta"] == 0
        assert coordinator.runtime.is_delivered() is True
        assert coordinator.data["distance"] is None
        assert len(data.calls) >= len(stages)
