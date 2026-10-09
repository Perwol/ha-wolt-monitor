"""Synthetic payload interpretation; no HTTP or account data."""

from datetime import UTC, datetime

import pytest

from custom_components.wolt_monitor import api
from custom_components.wolt_monitor.state import Runtime


@pytest.mark.parametrize(
    ("fields", "bounds"),
    [
        (
            {
                "delivery_eta_min": "2026-10-07T12:00:01Z",
                "delivery_eta_max": "2026-10-07T12:02:00Z",
            },
            ("2026-10-07T12:00:01+00:00", "2026-10-07T12:02:00+00:00"),
        ),
        (
            {"delivery_eta_min": "2026-10-07T12:00:01Z", "delivery_eta_max": "bad"},
            ("2026-10-07T12:00:01+00:00",) * 2,
        ),
        (
            {"delivery_eta_min": False, "delivery_eta": "2026-10-07T12:00:01Z"},
            ("2026-10-07T12:00:01+00:00",) * 2,
        ),
        (
            {
                "delivery_eta_min": "2026-10-07T12:02:00Z",
                "delivery_eta_max": "2026-10-07T12:00:01Z",
                "delivery_eta": "2026-10-07T12:03:00Z",
            },
            (None, None),
        ),
        (
            {"delivery_time": "2026-10-07T12:00:01Z", "delivery_eta": "2026-10-07T12:00:01"},
            (None, None),
        ),
        ({}, (None, None)),
    ],
)
def test_forecast_hierarchy_and_eta_use_current_wall_time(fields, bounds):
    wall = [datetime(2026, 10, 7, 12, tzinfo=UTC)]
    runtime = Runtime(clock=lambda: 0)
    # Injection is explicit; the monotonic retention clock is not a date.
    runtime.wall_clock = lambda: wall[0]
    runtime.update_order(
        api.normalize_order({"order_id": "synthetic", "status": "production", **fields}), "a"
    )
    values = runtime.values()
    assert values["estimated_delivery_time"] == bounds[1]
    expected = 2 if bounds[0] and bounds[0] != bounds[1] else (1 if bounds[0] else None)
    assert values["eta"] == expected
    wall[0] = datetime(2026, 10, 7, 12, 3, tzinfo=UTC)
    assert runtime.values().get("eta") == (0 if bounds[0] else None)


@pytest.mark.parametrize("verified", [True, False])
def test_initial_estimate_requires_explicit_verified_minutes(verified):
    payload = {
        "order_id": "synthetic",
        "status": "received",
        "payment_time": {"$date": 1791374400000},
        "client_pre_estimate": "10-20",
    }
    import inspect

    assert "minutes_verified" in inspect.signature(api.normalize_order).parameters
    order = api.normalize_order(payload, **({"minutes_verified": True} if verified else {}))
    runtime = Runtime(clock=lambda: 0)
    runtime.wall_clock = lambda: datetime(2026, 10, 7, 12, tzinfo=UTC)
    runtime.update_order(order, "a")
    assert runtime.values()["eta"] == (20 if verified else None)


def test_extreme_verified_estimate_is_ignored():
    order = api.normalize_order(
        {
            "order_id": "synthetic",
            "payment_time": "2026-10-07T12:00:01Z",
            "client_pre_estimate": f"{'9' * 5000}-{'9' * 5000}",
        },
        minutes_verified=True,
    )
    assert order.estimated_delivery_time is None


@pytest.mark.parametrize("bad", [True, float("inf"), "bad", {"$date": True}, {"$date": 1e100}])
def test_timestamp_rejects_invalid_types(bad):
    assert api.timestamp(bad) is None


@pytest.mark.parametrize("bad", [10**1000, -(10**1000)], ids=["huge-positive", "huge-negative"])
def test_extreme_timestamp_falls_back_to_valid_eta(bad):
    assert api.timestamp({"$date": bad}) is None
    order = api.normalize_order(
        {
            "order_id": "synthetic",
            "delivery_eta_min": {"$date": bad},
            "delivery_eta_max": {"$date": bad},
            "delivery_eta": "2026-10-07T12:00:01Z",
        }
    )
    expected = datetime(2026, 10, 7, 12, 0, 1, tzinfo=UTC)
    assert order.estimated_delivery_time == expected


@pytest.mark.parametrize("bad", [10**1000, -(10**1000), True, "21", float("inf"), float("nan")])
@pytest.mark.parametrize("invalid_destination", [False, True])
def test_extreme_coordinates_preserve_tracking_without_distance(bad, invalid_destination):
    location, destination = [bad, 52], [21, 52]
    if invalid_destination:
        location, destination = destination, location
    result = api.normalize_courier(
        {"drivers": [{"delivering_your_order": True, "location": location}]},
        destination=destination,
    )
    assert result.delivery_in_progress is True
    assert result.distance is None


@pytest.mark.parametrize(
    ("drivers", "flag", "distance"),
    [
        ([], None, None),
        ([{}], None, None),
        ([{"delivering_your_order": 1}], None, None),
        ([{"delivering_your_order": False}], False, None),
        ([{"delivering_your_order": False}, {}], None, None),
        ([{"delivering_your_order": True}], True, None),
        ([{"delivering_your_order": True, "location": [21, 52]}], True, 0),
        (
            [
                {"delivering_your_order": True, "location": [21, 52]},
                {"delivering_your_order": True},
            ],
            None,
            None,
        ),
        ([{"delivering_your_order": True, "location": [181, 52]}], True, None),
    ],
)
def test_tracking_counts_true_before_coordinate_filtering(drivers, flag, distance):
    assert hasattr(api, "normalize_courier"), "Pure tracking interpreter is missing"
    result = api.normalize_courier({"drivers": drivers}, destination=[21, 52])
    assert result.delivery_in_progress is flag
    assert result.distance == distance


@pytest.mark.parametrize("raw", ["ready", "refunded", "production"])
def test_tracking_cache_is_independent_and_completion_overrides(raw):
    from custom_components.wolt_monitor.errors import Failure

    assert hasattr(api, "normalize_courier"), "Pure tracking interpreter is missing"
    runtime = Runtime(clock=lambda: 0)
    runtime.update_order(api.normalize_order({"order_id": "synthetic", "status": raw}), "a")
    result = api.normalize_courier(
        {"drivers": [{"delivering_your_order": True, "location": [21, 52]}]}, destination=[21, 52]
    )
    runtime.update_courier(result, "a", order_key="synthetic")
    assert runtime.delivery_in_progress() is (raw == "ready")
    assert runtime.values()["distance"] == (0 if raw == "ready" else None)
    assert (
        runtime.values()["status"]
        == {"ready": "on_the_way", "refunded": "unknown", "production": "preparing"}[raw]
    )
    for cycle in ("b", "c"):
        runtime.fail("courier", Failure("timeout"), cycle)
        assert runtime.delivery_in_progress() is (raw == "ready")
    runtime.fail("courier", Failure("timeout"), "d")
    assert runtime.delivery_in_progress() is None
    assert runtime.values()["distance"] is None
    runtime.update_order(api.normalize_order({"order_id": "synthetic", "status": "delivered"}), "e")
    assert runtime.delivery_in_progress() is False
    assert runtime.is_delivered() is True
    assert runtime.values()["eta"] == 0


def test_inconclusive_tracking_discards_old_evidence_without_failure():
    runtime = Runtime(clock=lambda: 0)
    runtime.update_order(api.normalize_order({"order_id": "synthetic", "status": "ready"}), "a")
    runtime.update_courier(
        api.normalize_courier({"drivers": [{"delivering_your_order": True}]}),
        "a",
        order_key="synthetic",
    )
    runtime.update_courier(api.normalize_courier({"drivers": [{}]}), "b", order_key="synthetic")
    assert runtime.delivery_in_progress() is None
    assert runtime.sources["courier"].consecutive_errors == 0
    runtime.update_order(
        api.normalize_order({"order_id": "synthetic", "status": "production"}), "c"
    )
    assert runtime.delivery_in_progress() is False


def test_order_selection_preserves_first_candidate_across_reordering():
    assert hasattr(api, "select_order"), "Stable selector is missing"
    orders = [
        api.normalize_order({"order_id": key, "status": status})
        for key, status in [
            ("z", "delivered"),
            ("b", "refunded"),
            ("a", "production"),
            ("c", "rejected"),
        ]
    ]
    selected = api.select_order(orders)
    assert selected.snapshot.key == "b"
    assert not selected.details_required
    selected = api.select_order(list(reversed(orders)), previous=selected.snapshot)
    assert selected.snapshot.key == "b"
    selected = api.select_order([orders[2]], previous=selected.snapshot)
    assert selected.details_required
    assert selected.snapshot.key == "b"
    selected = api.select_order([orders[2]], previous=orders[0], completed=True)
    assert not selected.details_required
    assert selected.snapshot.key == "a"


@pytest.mark.parametrize("payload", [None, [], {}, {"order_id": ""}, {"order_id": True}])
def test_malformed_order_identity_is_sanitized(payload):
    from custom_components.wolt_monitor.errors import Failure

    try:
        api.normalize_order(payload)
    except Exception as error:
        assert isinstance(error, Failure), "Malformed payload must be a sanitized data failure"
        assert error.category == "invalid_data"
    else:
        pytest.fail("Malformed identity must not enter runtime")


def test_verified_details_identity_field_is_order_id():
    from custom_components.wolt_monitor.errors import Failure

    try:
        order = api.normalize_order({"order_id": "synthetic", "status": "production"})
    except Failure:
        order = None
    assert order is not None, "Verified order_id must be accepted"
    assert order.key == "synthetic"


def test_list_payload_normalizes_in_server_order_without_history():
    assert hasattr(api, "normalize_orders"), "Pure list interpreter is missing"
    result = api.normalize_orders(
        {
            "order_details": [
                {"order_id": "z", "status": "production", "payment_time": "2026-10-07T12:00:00Z"},
                {"order_id": "a", "status": "received", "payment_time": "2026-10-07T13:00:00Z"},
            ]
        }
    )
    assert [order.key for order in result.orders] == ["z", "a"]
    assert api.select_order(result.orders).snapshot.key == "z"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("received", "received"),
        ("acknowledged", "acknowledged"),
        ("fetched", "acknowledged"),
        ("production", "preparing"),
        ("ready", "ready"),
        ("delivered", "delivered"),
        ("rejected", "cancelled"),
        ("preorder-received", "scheduled"),
        ("preorder-confirmed", "scheduled"),
        *[
            (value, "unknown")
            for value in [
                "deferred_payment_failed",
                "process_payment_failed",
                "payment_method_not_valid_error",
                "invalid",
                "estimated",
                "pending_transaction",
                "pending_revenue_transaction",
                "picked_up",
                "refunded",
                "unmapped",
                None,
                {},
            ]
        ],
    ],
)
def test_normalized_status_map(raw, expected):
    assert hasattr(api, "normalize_order"), "Pure payload interpreter is missing"
    order = api.normalize_order({"order_id": "synthetic", "status": raw})
    assert order.status == expected
    assert order.terminal == (expected in {"delivered", "cancelled"})


@pytest.mark.parametrize(
    "raw", ["received", "acknowledged", "fetched", "production", "ready", "delivered", "refunded"]
)
def test_preorder_override_is_limited_to_early_stages(raw):
    assert hasattr(api, "normalize_order"), "Pure payload interpreter is missing"
    order = api.normalize_order(
        {"order_id": "synthetic", "status": raw, "preorder_status": "confirmed"}
    )
    expected = {
        "production": "preparing",
        "ready": "ready",
        "delivered": "delivered",
        "refunded": "unknown",
    }.get(raw, "scheduled")
    assert order.status == expected
