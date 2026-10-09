"""Single forecast policy and immutable observed replay; no live requests."""

from datetime import UTC, datetime

import pytest

from custom_components.wolt_monitor.api import normalize_order
from custom_components.wolt_monitor.state import Runtime


@pytest.mark.parametrize("status", ["production", "delivered", "rejected"])
def test_runtime_exposes_one_forecast_and_countdown(status):
    runtime = Runtime(clock=lambda: 0)
    runtime.wall_clock = lambda: datetime(2026, 10, 7, 12, tzinfo=UTC)
    runtime.update_order(
        normalize_order(
            {
                "order_id": "synthetic",
                "status": status,
                "delivery_eta_min": "2026-10-07T12:00:01Z",
                "delivery_eta_max": "2026-10-07T12:02:01Z",
            }
        ),
        "a",
    )
    values = runtime.values()
    assert set(values) == {"status", "restaurant", "distance", "estimated_delivery_time", "eta"}
    assert values["estimated_delivery_time"] == (
        "2026-10-07T12:02:01+00:00" if status == "production" else None
    )
    assert values["eta"] == {"production": 3, "delivered": 0, "rejected": None}[status]
    assert runtime.eta_update_delay == (1 if status == "production" else None)
    runtime.wall_clock = lambda: datetime(2026, 10, 7, 12, 3, tzinfo=UTC)
    assert runtime.values()["eta"] == (None if status == "rejected" else 0)
    assert runtime.eta_update_delay is None


def test_normalized_forecast_selects_upper_bound_once():
    order = normalize_order(
        {
            "order_id": "synthetic",
            "delivery_eta_min": "2026-10-07T12:00:01Z",
            "delivery_eta_max": "2026-10-07T12:02:00Z",
            "delivery_eta": "2026-10-07T12:03:00Z",
        }
    )
    assert getattr(order, "estimated_delivery_time", None) == datetime(
        2026, 10, 7, 12, 2, tzinfo=UTC
    )
    assert not hasattr(order, "estimated_delivery_time_min")
    assert not hasattr(order, "estimated_delivery_time_max")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"delivery_eta_min": "2026-10-07T12:00:01Z"}, "2026-10-07T12:00:01+00:00"),
        ({"delivery_eta_max": "2026-10-07T12:00:01Z"}, "2026-10-07T12:00:01+00:00"),
        (
            {
                "delivery_eta_min": "2026-10-07T12:00:01Z",
                "delivery_eta_max": "2026-10-07T12:00:01Z",
            },
            "2026-10-07T12:00:01+00:00",
        ),
        (
            {
                "delivery_eta_min": "2026-10-07T14:02:00+02:00",
                "delivery_eta_max": "2026-10-07T12:00:01Z",
            },
            None,
        ),
        (
            {"delivery_eta_min": "2026-10-07T11:00:01Z", "delivery_eta_max": {"$date": 10**1000}},
            "2026-10-07T11:00:01+00:00",
        ),
        ({"delivery_eta_min": False, "delivery_eta_max": "bad"}, "2026-10-07T12:03:00+00:00"),
    ],
)
def test_single_forecast_validates_bounds_before_other_sources(fields, expected):
    order = normalize_order(
        {
            "order_id": "synthetic",
            "status": "production",
            "delivery_eta": "2026-10-07T12:03:00Z",
            "payment_time": "2026-10-07T12:00:00Z",
            "client_pre_estimate": "46-56",
            **fields,
        },
        minutes_verified=True,
    )
    assert (
        order.estimated_delivery_time.isoformat() if order.estimated_delivery_time else None
    ) == expected


@pytest.mark.parametrize(
    ("initial", "upper"), [("46-56", 56), ("10-20", 20), ("0-0", 0), ("5-5", 5)]
)
def test_verified_minutes_use_fixed_payment_plus_upper(initial, upper):
    from datetime import timedelta

    payment = datetime(2026, 10, 7, 12, tzinfo=UTC)
    payload = {
        "order_id": "synthetic",
        "status": "received",
        "payment_time": payment.isoformat(),
        "client_pre_estimate": initial,
    }
    runtime = Runtime(clock=lambda: 0)
    runtime.wall_clock = lambda: payment
    runtime.update_order(normalize_order(payload, minutes_verified=True), "a")
    expected = payment + timedelta(minutes=upper)
    assert runtime.values()["estimated_delivery_time"] == expected.isoformat()
    assert runtime.values()["eta"] == upper
    runtime.wall_clock = lambda: payment + timedelta(minutes=3)
    runtime.update_order(normalize_order(payload, minutes_verified=True), "b")
    assert runtime.values()["estimated_delivery_time"] == expected.isoformat()
    assert runtime.values()["eta"] == max(0, upper - 3)
    # A later API point replaces the initial upper estimate; never freeze it.
    payload["delivery_eta"] = (payment + timedelta(minutes=upper + 10)).isoformat()
    runtime.update_order(normalize_order(payload, minutes_verified=True), "c")
    assert runtime.values()["eta"] == upper + 7
    assert runtime.values()["estimated_delivery_time"] == payload["delivery_eta"]


@pytest.mark.parametrize(
    "initial",
    [
        None,
        True,
        56,
        56.0,
        [],
        {},
        "56",
        "56-46",
        "-1-56",
        "46.0-56",
        "46 - 56",
        "1-" + "9" * 100,
        "9" * 5000 + "-" + "9" * 5000,
    ],
)
def test_invalid_verified_minutes_are_unavailable_without_exceptions(initial):
    order = normalize_order(
        {
            "order_id": "synthetic",
            "payment_time": "2026-10-07T12:00:00Z",
            "client_pre_estimate": initial,
        },
        minutes_verified=True,
    )
    assert order.estimated_delivery_time is None


@pytest.mark.parametrize("verification", [False, None, 1, "minutes"])
@pytest.mark.parametrize("initial", ["1-2", "46-56"])
def test_unverified_or_day_variant_never_implies_minutes(verification, initial):
    order = normalize_order(
        {
            "order_id": "synthetic",
            "status": "preorder-confirmed",
            "payment_time": "2026-10-07T12:00:00Z",
            "client_pre_estimate": initial,
        },
        minutes_verified=verification,
    )
    assert order.estimated_delivery_time is None


@pytest.mark.parametrize(
    "marker",
    [
        {"preorder_status": "confirmed"},
        {"preorder_status": "received"},
        {"preorder_status": []},
        {"time_slot_order": {"type": "DELIVERY_WITHIN_TIME_RANGE"}},
        {"time_slot_order": {"type": "PRIORITY"}},
        {"time_slot_order": {}},
        {"time_slot_order": []},
        {"time_slot_order": False},
        {"client_pre_estimate_unit": "DAYS"},
        {"client_pre_estimate_unit": []},
        {"preorder": True},
        {"is_preorder": True},
        {"is_preorder": 0},
        {"is_scheduled": True},
        {"scheduled_time": "2026-10-08T12:00:00Z"},
        {"scheduled_delivery_time": {}},
        {"planned_delivery_time": "2026-10-08T12:00:00Z"},
    ],
)
@pytest.mark.parametrize("status", ["received", "production", "ready"])
async def test_http_assumption_excludes_scheduling_and_unit_markers(marker, status):
    from test_data_api import details
    from test_data_transport import GetSession
    from test_transport import Response

    from custom_components.wolt_monitor.api import WoltDataAPI
    from custom_components.wolt_monitor.transport import WoltDataClient

    payload = {
        "order_details": [
            details(
                status=status,
                delivery_method="homedelivery",
                payment_time="2026-10-07T12:00:00Z",
                client_pre_estimate="1-2",
                **marker,
            )
        ]
    }
    adapter = WoltDataAPI(WoltDataClient(GetSession(Response(body=payload))))
    listed = await adapter.async_order("synthetic-unused")
    assert listed.orders[0].estimated_delivery_time is None
    adapter = WoltDataAPI(WoltDataClient(GetSession(Response(body=payload))))
    detailed = await adapter.async_order_details("synthetic-unused", "synthetic")
    assert detailed.snapshot.estimated_delivery_time is None


@pytest.mark.parametrize("initial", ["1-2", "46-56"])
@pytest.mark.parametrize("status", ["preorder-received", "preorder-confirmed"])
async def test_http_adapter_does_not_assume_minutes_for_preorders(initial, status):
    from custom_components.wolt_monitor.api import WoltDataAPI

    payload = {
        "order_details": [
            {
                "order_id": "synthetic",
                "status": status,
                "delivery_method": "homedelivery",
                "is_marketplace_v2": False,
                "payment_time": "2026-10-07T12:00:00Z",
                "client_pre_estimate": initial,
            }
        ]
    }

    class Client:
        async def get(self, *_args):
            return payload

    adapter = WoltDataAPI(Client())
    listed = await adapter.async_order("synthetic-unused")
    detailed = await adapter.async_order_details("synthetic-unused", "synthetic")
    assert listed.orders[0].estimated_delivery_time is None
    assert detailed.snapshot is not None
    assert detailed.snapshot.estimated_delivery_time is None


async def test_assumed_fallback_rejects_utc_overflow_after_adding_minutes():
    from test_data_api import details
    from test_data_transport import GetSession
    from test_transport import Response

    from custom_components.wolt_monitor.api import WoltDataAPI
    from custom_components.wolt_monitor.transport import WoltDataClient

    payload = {
        "order_details": [
            details(
                status="received",
                delivery_method="homedelivery",
                payment_time="9999-12-31T23:58:00-00:01",
                client_pre_estimate="1-1",
            )
        ]
    }
    snapshot = (
        await WoltDataAPI(WoltDataClient(GetSession(Response(body=payload)))).async_order(
            "synthetic"
        )
    ).orders[0]
    assert snapshot.estimated_delivery_time is None


def test_eta_timer_uses_only_selected_upper_and_zero_does_not_mean_delivery():
    runtime = Runtime(clock=lambda: 0)
    runtime.wall_clock = lambda: datetime(2026, 10, 7, 12, tzinfo=UTC)
    runtime.update_order(
        normalize_order(
            {
                "order_id": "synthetic",
                "status": "production",
                "delivery_eta_min": "2026-10-07T12:00:01Z",
                "delivery_eta_max": "2026-10-07T12:02:30Z",
            }
        ),
        "a",
    )
    assert runtime.values()["eta"] == 3
    assert runtime.eta_update_delay == 30
    runtime.wall_clock = lambda: datetime(2026, 10, 7, 12, 2, 30, tzinfo=UTC)
    assert runtime.values()["eta"] == 0
    assert runtime.values()["estimated_delivery_time"] == "2026-10-07T12:02:30+00:00"
    assert runtime.is_delivered() is False
    assert not runtime.completed


async def test_literal_seven_observations_replay_single_forecast_and_ready_switch():
    import json
    from pathlib import Path

    from scripts.wolt_fixtures import replay

    directory = Path(__file__).parent / "fixtures" / "wolt"
    paths = sorted(directory.glob("natural-order-stage-*.json"))
    assert [path.name for path in paths] == [
        f"natural-order-stage-{index:02}.json" for index in range(1, 8)
    ]
    datasets = [json.loads(path.read_text()) for path in paths]
    assert all(dataset["metadata"]["provenance"] == "observed" for dataset in datasets)
    # Merge only in memory: keep literal captures immutable and terminal continuity.
    dataset = {
        "metadata": datasets[0]["metadata"],
        "stages": [stage for part in datasets for stage in part["stages"]],
    }
    values = await replay(dataset)  # Real adapter, shared resolver and Runtime.
    assert len(values) == 7
    assert [value["status"] for value in values] == [
        "preparing",
        "preparing",
        "preparing",
        "on_the_way",
        "ready",
        "on_the_way",
        "delivered",
    ]
    assert [value["eta"] for value in values] == [48, 48, 44, 31, 24, 13, 0]
    assert [value["delivery_in_progress"] for value in values] == [
        False,
        False,
        False,
        True,
        False,
        True,
        False,
    ]
    assert [value["is_delivered"] for value in values] == [False] * 6 + [True]
    for value, stage in zip(values[:-1], dataset["stages"][:-1], strict=True):
        raw = stage["responses"]["subscriptions"]["order_details"][0]
        forecast = datetime.fromtimestamp(raw["delivery_eta"]["$date"] / 1000, UTC)
        assert value["estimated_delivery_time"] == forecast.isoformat()
    assert values[-1]["estimated_delivery_time"] is None
    assert values[-1]["distance"] is None
    assert all(
        set(value)
        == {
            "status",
            "restaurant",
            "distance",
            "estimated_delivery_time",
            "eta",
            "delivery_in_progress",
            "is_delivered",
        }
        for value in values
    )
