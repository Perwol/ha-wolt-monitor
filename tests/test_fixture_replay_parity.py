"""Pair offline replay with the actual coordinator cycle, no HA server/setup."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.wolt_monitor.api import WoltDataAPI, timestamp
from scripts.wolt_fixtures import CaptureSession, ReplayClient, replay


def item(key="order-1", status="ready", supported=True):
    return {
        "order_id": key,
        "status": status,
        "delivery_method": "homedelivery",
        "is_marketplace_v2": not supported,
    }


def listing(*orders):
    return {"order_details": list(orders)}


def tracking():
    return {"order_details": item(), "drivers": [{"delivering_your_order": True}]}


async def coordinator_values(dataset):
    from custom_components.wolt_monitor.api import WoltDataAPI
    from custom_components.wolt_monitor.coordinator import WoltCoordinator
    from custom_components.wolt_monitor.safe_logging import SafeLog
    from custom_components.wolt_monitor.scheduler import Scheduler
    from custom_components.wolt_monitor.state import Runtime

    now = [0.0]
    wall = [datetime.fromisoformat(dataset["stages"][0]["at"])]
    start = wall[0]

    async def acquire(*args, **kwargs):
        return "synthetic-unused"

    coordinator = object.__new__(WoltCoordinator)
    coordinator.hass = SimpleNamespace(is_stopping=False)
    coordinator.clock = lambda: now[0]
    coordinator.runtime = Runtime(clock=coordinator.clock)
    coordinator.runtime.wall_clock = lambda: wall[0]
    coordinator.scheduler = Scheduler(clock=coordinator.clock)
    coordinator.tokens = SimpleNamespace(not_before=0, usable=lambda: True, acquire=acquire)
    coordinator.safe_log = SafeLog()
    coordinator._stopped = False
    coordinator.auth_replaced = False
    coordinator._cycle_id = 0
    coordinator._data_not_before = 0.0
    coordinator._unified_not_before = 0.0
    coordinator._schedule = lambda: None
    coordinator.async_set_updated_data = lambda _: None
    outcomes = []
    for stage in dataset["stages"]:
        wall[0] = datetime.fromisoformat(stage["at"])
        now[0] = (wall[0] - start).total_seconds()
        coordinator.api = WoltDataAPI(ReplayClient(stage["responses"]))
        await coordinator._cycle(manual=True)
        assert coordinator.runtime.sources["order"].consecutive_errors == 0
        runtime = coordinator.runtime
        eligible = runtime.delivery_in_progress() is True
        assert runtime.values()["distance"] == (runtime._distance if eligible else None)
        assert runtime.courier_position() == (runtime._position if eligible else None)
        outcomes.append(
            {
                **coordinator.runtime.values(),
                "delivery_in_progress": coordinator.runtime.delivery_in_progress(),
                "is_delivered": coordinator.runtime.is_delivered(),
            }
        )
    return outcomes


@pytest.mark.parametrize(
    "prefix,count", [("natural-order-stage-", 7), ("natural-order-two-stage-", 9)]
)
async def test_saved_order_replays_share_courier_gate_without_changing_raw_evidence(prefix, count):
    paths = sorted((Path(__file__).parent / "fixtures" / "wolt").glob(f"{prefix}*.json"))
    assert len(paths) == count
    datasets = [json.loads(path.read_text()) for path in paths]
    dataset = {**datasets[0], "stages": [stage for item in datasets for stage in item["stages"]]}
    outcomes = await coordinator_values(dataset)
    assert len(outcomes) == count
    assert outcomes == await replay(dataset)
    assert any(value["delivery_in_progress"] is True for value in outcomes)
    assert all(
        value["distance"] is None for value in outcomes if value["delivery_in_progress"] is not True
    )


@pytest.mark.parametrize(
    "case",
    [
        "unneeded_details",
        "terminal_replacement_retirement",
        "lost_capability",
        "expire_before_selection",
    ],
)
async def test_replay_exactly_matches_coordinator(case):
    if case == "unneeded_details":
        responses = [
            {
                "subscriptions": listing(item()),
                "details": listing(item(status="delivered")),
                "tracking": tracking(),
            }
        ]
        expected = ["on_the_way"]
    elif case == "terminal_replacement_retirement":
        responses = [
            {
                "subscriptions": listing(item()),
                "tracking": {"order_details": item(), "drivers": []},
            },
            {
                "subscriptions": listing(item("order-2", "production")),
                "details": listing(item(status="delivered")),
                "tracking": {"order_details": item("order-2", "production"), "drivers": []},
            },
            {"subscriptions": listing(item(), item("order-2", "delivered"))},
        ]
        expected = ["ready", "preparing", "delivered"]
    elif case == "lost_capability":
        responses = [
            {"subscriptions": listing(item()), "tracking": tracking()},
            {"subscriptions": listing(item(supported=False))},
        ]
        expected = ["on_the_way", "ready"]
    else:
        responses = [
            {
                "subscriptions": listing(item()),
                "tracking": {"order_details": item(), "drivers": []},
            },
            {"subscriptions": listing(item(status="delivered"))},
            {"subscriptions": listing()},
        ]
        expected = ["ready", "delivered", "no_active_order"]
    session = CaptureSession(offset=timedelta(0), provenance="synthetic")
    stages = [
        {
            "stage": "ready",
            "at": (
                datetime(2026, 1, 1, tzinfo=UTC)
                + timedelta(
                    seconds=(
                        900 if case == "expire_before_selection" and index == 2 else index * 60
                    )
                )
            ).isoformat(),
            "responses": reply,
        }
        for index, reply in enumerate(responses)
    ]
    dataset = session.dataset(stages)
    actual_coordinator = await coordinator_values(dataset)
    assert [value["status"] for value in actual_coordinator] == expected
    assert await replay(dataset) == actual_coordinator


@pytest.mark.parametrize(
    "estimate,upper",
    [
        ("1-10000", 10000),
        ("00001-00002", 2),
        ("10000-1", None),
        pytest.param("1-" + "0" * 4093 + "2", 2, id="bounded-zero-padding"),
        pytest.param("1-" + "9" * 4094, None, id="bounded-overflow"),
    ],
)
async def test_numeric_range_capture_json_replay_matches_production(estimate, upper):
    payment = datetime(2026, 1, 1, tzinfo=UTC)
    offset = timedelta(days=30)
    raw = listing(
        {
            **item(status="received"),
            "order_id": "PRIVATE-CANARY",
            "payment_time": payment.isoformat(),
            "client_pre_estimate": estimate,
        }
    )
    source = (await WoltDataAPI(ReplayClient({"subscriptions": raw})).async_order("unused")).orders[
        0
    ]
    if upper is None:
        assert source.estimated_delivery_time is None
    else:
        assert source.estimated_delivery_time is not None
    session = CaptureSession(offset=offset, provenance="synthetic")
    clean = session.capture("subscriptions", raw)
    assert clean["order_details"][0]["client_pre_estimate"] == estimate
    dataset = session.dataset(
        [
            {
                "stage": "received",
                "at": session.reference_time(payment),
                "responses": {"subscriptions": clean},
            }
        ]
    )
    stored = json.loads(json.dumps(dataset, allow_nan=False))
    assert "PRIVATE-CANARY" not in json.dumps(stored)
    values = (await replay(stored))[0]
    expected = source.estimated_delivery_time
    if expected is None:
        assert values["estimated_delivery_time"] is None
        assert values["eta"] is None
    else:
        assert timestamp(values["estimated_delivery_time"]) == expected + offset
        assert values["eta"] == upper
    assert await replay(stored) == await coordinator_values(stored)
