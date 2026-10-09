"""Privacy regressions: all inputs here are synthetic, never account data."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "marker",
    [
        {"time_slot_order": {"type": "DELIVERY_WITHIN_TIME_RANGE", "address": "CANARY"}},
        {"time_slot_order": {"type": "CANARY"}},
        {"time_slot_order": {"address": "CANARY"}},
        {"time_slot_order": []},
        {"time_slot_order": "CANARY"},
        {"time_slot_order": False},
        {"preorder_status": "confirmed"},
        {"preorder_status": "CANARY"},
        {"preorder": True},
        {"is_preorder": {"token": "CANARY"}},
        {"is_scheduled": True},
        {"scheduled_time": "2026-10-08T12:00:00Z"},
        {"scheduled_delivery_time": "CANARY"},
        {"planned_delivery_time": {}},
        {"client_pre_estimate_unit": "DAYS"},
        {"client_pre_estimate_unit": {"token": "CANARY"}},
    ],
)
async def test_capture_preserves_minutes_assumption_exclusions(marker):
    from custom_components.wolt_monitor.api import WoltDataAPI

    payload = {
        "order_details": [
            order(
                status="received",
                delivery_method="homedelivery",
                payment_time="2026-10-07T12:00:00Z",
                client_pre_estimate="1-2",
                delivery_eta_min=None,
                delivery_eta_max=None,
                **marker,
            )
        ]
    }
    session = tool()
    clean = session.capture("subscriptions", payload)
    assert "CANARY" not in json.dumps(clean)
    key = next(iter(marker))
    assert key in clean["order_details"][0]
    if (
        key == "time_slot_order"
        and isinstance(marker[key], dict)
        and marker[key].get("type") == "DELIVERY_WITHIN_TIME_RANGE"
    ):
        assert clean["order_details"][0][key] == {"type": "DELIVERY_WITHIN_TIME_RANGE"}

    class Client:
        async def get(self, *args):
            return clean

    assert (await WoltDataAPI(Client()).async_order("synthetic")).orders[
        0
    ].estimated_delivery_time is None
    from scripts.wolt_fixtures import replay

    dataset = session.dataset(
        [
            {
                "stage": "received",
                "at": session.reference_time(datetime(2026, 10, 7, 12, tzinfo=UTC)),
                "responses": {"subscriptions": clean},
            }
        ]
    )
    result = (await replay(json.loads(json.dumps(dataset))))[0]
    assert result["eta"] is None
    assert result["estimated_delivery_time"] is None


async def test_tracking_preserves_invalid_positions_and_optional_branch_types():
    from custom_components.wolt_monitor.api import WoltDataAPI

    session = tool()
    raw = {
        "order_details": order(
            delivery_location={
                "address": "CANARY",
                "coordinates": {"type": "Point", "coordinates": [21, 52], "url": "CANARY"},
            },
            self_delivery={"is_tracking_enabled": 1, "cookie": "CANARY"},
            preorder_status="confirmed",
            client_pre_estimate="20-10",
        ),
        "drivers": [{"delivering_your_order": True, "location": ["CANARY", 52], "name": "CANARY"}],
    }
    clean = session.capture("tracking", raw)
    assert "CANARY" not in json.dumps(clean)
    assert clean["order_details"]["self_delivery"]["is_tracking_enabled"] is not True
    assert clean["order_details"]["preorder_status"] == "confirmed"
    assert clean["order_details"]["client_pre_estimate"] == "20-10"

    class Client:
        async def get(self, *args):
            return clean

    adapter = WoltDataAPI(Client())
    courier = await adapter.async_courier("synthetic", "order-1")
    assert courier.delivery_in_progress is True
    assert courier.distance is None
    raw["drivers"][0]["location"] = [21, 52]
    clean = session.capture("tracking", raw)
    assert (await adapter.async_courier("synthetic", "order-1")).distance is not None
    raw["drivers"].append({"delivering_your_order": True, "location": [21, 52]})
    clean = session.capture("tracking", raw)
    assert (await adapter.async_courier("synthetic", "order-1")).distance is None


@pytest.mark.parametrize(
    "value", [None, "CANARY", 1, True, [], {}, [True, 52], [181, 52], [21, 91], [float("nan"), 52]]
)
def test_invalid_coordinate_types_never_become_valid(value):
    from custom_components.wolt_monitor.api import coordinates

    clean = tool().capture("tracking", {"drivers": [{"location": value}]})
    assert coordinates(clean["drivers"][0]["location"]) is None
    assert "CANARY" not in json.dumps(clean, allow_nan=False)


def test_invalid_timestamp_and_unknown_nested_values_are_safe():
    clean = tool().capture(
        "details",
        {
            "order_details": [
                order(
                    delivery_eta_min={"$date": "CANARY", "private": "CANARY"},
                    delivery_eta_max="CANARY",
                    self_delivery={"is_tracking_enabled": "CANARY"},
                    delivery_method={"url": "CANARY"},
                    client_pre_estimate="CANARY",
                    preorder_status="CANARY",
                )
            ]
        },
    )
    assert "CANARY" not in json.dumps(clean, allow_nan=False)
    assert clean["order_details"][0]["delivery_eta_min"] == {"$date": "invalid"}


@pytest.mark.parametrize(
    "payload",
    [
        {"order_details": [{"delivery_eta_min": object()}]},
        {"order_details": [{"delivery_eta_min": {"$date": 10**1000}}]},
    ],
)
def test_unsupported_input_and_extreme_dates_raise_private_errors(payload, caplog):
    session = tool()
    with pytest.raises(ValueError, match="Invalid capture input") as caught:
        session.capture("details", payload)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert "CANARY" not in repr(caught.value) + caplog.text


async def test_reversed_forecast_and_unverified_numeric_range_remain_unavailable():
    from custom_components.wolt_monitor.api import WoltDataAPI

    clean = tool().capture(
        "subscriptions",
        {
            "order_details": [
                order(
                    delivery_eta_min="2026-10-07T13:10:00+02:00",
                    delivery_eta_max="2026-10-07T13:00:00+02:00",
                    client_pre_estimate="20-10",
                )
            ]
        },
    )

    class Client:
        async def get(self, *args):
            return clean

    snapshot = (await WoltDataAPI(Client()).async_order("synthetic")).orders[0]
    assert snapshot.estimated_delivery_time is None


def test_dataset_rejects_raw_payload_and_private_nested_fields():
    session = tool()
    raw = {
        "stage": "ready",
        "at": "2026-11-06T00:00:00+00:00",
        "responses": {"subscriptions": {"order_details": [order()]}},
    }
    with pytest.raises(ValueError, match="Invalid sanitized dataset") as caught:
        session.dataset([raw])
    assert caught.value.__context__ is None
    clean = session.capture("subscriptions", raw["responses"]["subscriptions"])
    clean["order_details"][0]["self_delivery"] = {"token": "CANARY"}
    raw["responses"]["subscriptions"] = clean
    with pytest.raises(ValueError, match="Invalid sanitized dataset"):
        session.dataset([raw])


@pytest.mark.parametrize("value", [1e30, float("nan"), "CANARY", None, True])
def test_invalid_native_dates_do_not_become_valid(value):
    from custom_components.wolt_monitor.api import timestamp

    clean = tool().capture("details", {"order_details": [{"delivery_eta": {"$date": value}}]})
    assert timestamp(clean["order_details"][0]["delivery_eta"]) is None
    assert "CANARY" not in json.dumps(clean, allow_nan=False)


@pytest.mark.parametrize("value", ["CANARY", datetime(2026, 1, 1), None])
def test_reference_time_rejects_invalid_inputs_without_source_context(value):
    with pytest.raises(ValueError, match="Invalid reference time") as caught:
        tool().reference_time(value)
    assert caught.value.__context__ is None


def test_session_alias_maps_are_isolated_and_never_in_repr():
    first, second = tool(), tool()
    first.capture("details", {"order_details": [order(order_id="CANARY-other")]})
    assert (
        first.capture("details", {"order_details": [order()]})["order_details"][0]["order_id"]
        == "order-2"
    )
    assert (
        second.capture("details", {"order_details": [order()]})["order_details"][0]["order_id"]
        == "order-1"
    )
    assert "CANARY" not in repr(first) + repr(second)


@pytest.mark.parametrize("value", [None, True, 7, 7.5, [], {}])
def test_invalid_optional_scalars_retain_safe_type_witnesses(value):
    clean = tool().capture(
        "details",
        {
            "order_details": [
                order(
                    status=value, venue_name=value, delivery_method=value, is_marketplace_v2=value
                )
            ]
        },
    )
    for key in ("status", "venue_name", "delivery_method", "is_marketplace_v2"):
        assert type(clean["order_details"][0][key]) is type(value)


def test_incomplete_wrappers_and_invalid_resources_remain_safe():
    session = tool()
    assert session.capture("subscriptions", {"order_details": [None, {"order_id": None}]}) == {
        "order_details": [None, {"order_id": None}]
    }
    assert session.capture("details", {}) == {}
    clean = session.capture(
        "tracking", {"order_details": order(delivery_location={"coordinates": "CANARY"})}
    )
    assert clean["order_details"]["delivery_location"]["coordinates"] == "invalid"
    with pytest.raises(ValueError, match="Invalid capture input"):
        session.capture("CANARY", {})
    from scripts.wolt_fixtures import CaptureSession

    with pytest.raises(ValueError, match="Invalid capture configuration"):
        CaptureSession(offset=None, provenance="synthetic")


async def test_replay_missing_fallback_details_fails_without_network():
    from scripts.wolt_fixtures import replay

    session = tool()
    first = session.capture("subscriptions", {"order_details": [order()]})
    dataset = session.dataset(
        [
            {
                "stage": "ready",
                "at": "2026-11-06T00:00:00+00:00",
                "responses": {"subscriptions": first},
            },
            {
                "stage": "ready",
                "at": "2026-11-06T00:01:00+00:00",
                "responses": {"subscriptions": {"order_details": []}},
            },
        ]
    )
    with pytest.raises(ValueError, match="Missing replay response"):
        await replay(dataset)


def test_replay_stored_synthetic_stages(tmp_path):
    import asyncio

    from scripts import wolt_fixtures

    assert hasattr(wolt_fixtures, "replay"), "Offline replay runner is missing"
    session = tool()
    stages = []
    for index, status in enumerate(
        ["received", "production", "ready", "ready", "delivered", "delivered"]
    ):
        item = order(
            status=status,
            delivery_location={"coordinates": {"type": "Point", "coordinates": [21, 52]}},
        )
        stage = {
            "stage": status if index != 3 else "courier",
            "at": session.reference_time(
                datetime(2026, 10, 7, 10, 50, tzinfo=UTC)
                + timedelta(seconds=index * 60 if index < 5 else 900)
            ),
            "responses": {
                "subscriptions": session.capture("subscriptions", {"order_details": [item]}),
                "details": session.capture("details", {"order_details": [item]}),
            },
        }
        if index == 3:
            stage["responses"]["tracking"] = session.capture(
                "tracking",
                {
                    "order_details": item,
                    "drivers": [{"delivering_your_order": True, "location": [21, 52]}],
                },
            )
        stages.append(stage)
    dataset = session.dataset(stages)
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps(dataset))  # Only already sanitized synthetic structures.
    stored = json.loads(path.read_text())
    values = asyncio.run(wolt_fixtures.replay(stored))
    assert [v["status"] for v in values] == [
        "received",
        "preparing",
        "ready",
        "on_the_way",
        "delivered",
        "no_active_order",
    ]
    assert values[0]["eta"] == 20
    assert values[2]["delivery_in_progress"] is None
    assert values[3]["delivery_in_progress"] is True
    assert values[4]["is_delivered"] is True
    assert values[4]["eta"] == 0
    assert values[5]["delivery_in_progress"] is None
    assert values[5]["is_delivered"] is None
    assert stored["metadata"]["provenance"] == "synthetic"
    assert "CANARY" not in path.read_text()


def tool():
    assert importlib.util.find_spec("scripts.wolt_fixtures"), "Memory sanitizer is missing"
    from scripts.wolt_fixtures import CaptureSession

    return CaptureSession(offset=timedelta(days=30), provenance="synthetic")


def order(**extra):
    return {
        "order_id": "CANARY-order",
        "status": "ready",
        "venue_name": "CANARY-venue",
        "delivery_method": "homedelivery",
        "is_marketplace_v2": False,
        "payment_time": {"$date": 1791374400000},
        "delivery_eta_min": "2026-10-07T13:00:00+02:00",
        "delivery_eta_max": "2026-10-07T13:10:00+02:00",
        **extra,
    }


def test_memory_allowlist_aliases_and_common_timestamp_shift(caplog):
    session = tool()
    raw = {
        "order_details": [order(address={"token": "CANARY"}, status="CANARY")],
        "Authorization": "CANARY",
        "group_orders": [{"private": "CANARY"}],
    }
    first = session.capture("subscriptions", raw)
    second = session.capture("details", {"order_details": [order()]})
    assert first["order_details"][0]["order_id"] == second["order_details"][0]["order_id"]
    assert first["order_details"][0]["status"] == "unknown"
    assert "CANARY" not in json.dumps([first, second]) + repr(session) + caplog.text
    clean = second["order_details"][0]
    assert clean["payment_time"]["$date"] == 1791374400000 + 30 * 86400000
    assert datetime.fromisoformat(clean["delivery_eta_min"]) == (
        datetime.fromisoformat(order()["delivery_eta_min"]) + timedelta(days=30)
    )
    assert datetime.fromisoformat(clean["delivery_eta_max"]) - datetime.fromisoformat(
        clean["delivery_eta_min"]
    ) == timedelta(minutes=10)
    assert session.reference_time(datetime(2026, 10, 7, tzinfo=UTC)) == "2026-11-06T00:00:00+00:00"
