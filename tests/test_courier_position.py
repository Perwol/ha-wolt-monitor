"""Synthetic geometry and memory-only GPS safety contract."""

import pytest

from custom_components.wolt_monitor.api import normalize_courier
from custom_components.wolt_monitor.errors import Failure
from custom_components.wolt_monitor.state import CourierSnapshot, OrderSnapshot, Runtime


@pytest.mark.parametrize("status", ["preparing", "unknown", "ready"])
def test_both_courier_projections_require_effective_delivery_true(status):
    state = Runtime(clock=lambda: 0)
    state.update_order(OrderSnapshot("synthetic", status), "order")
    state.update_courier(
        CourierSnapshot(100, True, latitude=52, longitude=21), "gps", order_key="synthetic"
    )
    assert state.delivery_in_progress() is (status == "ready")
    assert state.values()["distance"] == (100 if status == "ready" else None)
    assert state.courier_position() == ((52, 21) if status == "ready" else None)


def runtime():
    state = Runtime(clock=lambda: 0)
    state.update_order(OrderSnapshot("synthetic", "ready"), "order")
    return state


@pytest.mark.parametrize(
    "latitude,longitude",
    [
        (True, 21),
        (52, False),
        ("52", 21),
        (52, "21"),
        (float("nan"), 21),
        (52, float("inf")),
        (91, 21),
        (52, -181),
        (10**10000, 21),
        (52, -(10**10000)),
        (52, None),
        (None, 21),
    ],
    ids=[f"invalid-{i}" for i in range(12)],
)
def test_normalized_snapshot_rejects_invalid_position_at_runtime(latitude, longitude):
    snapshot = CourierSnapshot(100, True, latitude=latitude, longitude=longitude)
    state = runtime()
    state.update_courier(snapshot, "gps", order_key="synthetic")
    assert state.courier_position() is None
    assert snapshot.latitude is None and snapshot.longitude is None
    assert state.values()["distance"] == 100


def test_nonselected_courier_cannot_supply_runtime_position():
    state = runtime()
    state.update_courier(
        CourierSnapshot(None, False, latitude=52, longitude=21), "gps", order_key="synthetic"
    )
    assert state.courier_position() is None


@pytest.mark.parametrize("source", ["order", "courier"])
def test_third_source_error_discards_position_not_just_hides_it(source):
    state = runtime()
    state.update_courier(
        CourierSnapshot(100, True, latitude=52, longitude=21), "gps", order_key="synthetic"
    )
    for n in (1, 2):
        state.fail(source, Failure("connection"), str(n))
        assert state.courier_position() == (52, 21)
    state.fail(source, Failure("connection"), "3")
    assert state.courier_position() is None
    state.sources[source].success()
    assert state.courier_position() is None


@pytest.mark.parametrize("status", ["delivered", "cancelled"])
def test_terminal_discards_coordinate_memory(status):
    state = runtime()
    state.update_courier(
        CourierSnapshot(100, True, latitude=52, longitude=21), "gps", order_key="synthetic"
    )
    state.update_order(OrderSnapshot("synthetic", status, terminal=True), "terminal")
    assert state.courier_position() is None
    assert state._position is None


@pytest.mark.parametrize(
    "drivers,expected",
    [
        (
            [
                {"delivering_your_order": True, "location": [13.24681, 45.13579]},
                {"delivering_your_order": False, "location": [10, 20]},
            ],
            (45.13579, 13.24681),
        ),
        ([{"delivering_your_order": False, "location": [10, 20]}], None),
        (
            [
                {"delivering_your_order": True},
                {"delivering_your_order": True, "location": [10, 20]},
            ],
            None,
        ),
        (
            [
                {"delivering_your_order": True, "location": [13.24681, 45.13579]},
                {"delivering_your_order": True, "location": [10, 20]},
            ],
            None,
        ),
        ([{"location": [10, 20]}], None),
        ([], None),
    ],
)
def test_position_uses_exact_distance_selector_without_destination(drivers, expected):
    snapshot = normalize_courier({"drivers": drivers})
    state = runtime()
    state.update_courier(snapshot, "gps", order_key="synthetic")
    assert state.courier_position() == expected
    assert state.values()["distance"] is None
    assert "13.24681" not in repr(snapshot) and "45.13579" not in repr(snapshot)


@pytest.mark.parametrize(
    "location",
    [
        [True, 52],
        [21, "52"],
        [float("nan"), 52],
        [21, float("inf")],
        [181, 52],
        [21, -91],
        [10**10000, 52],
        [21, 10**10000],
        [21],
        None,
    ],
    ids=[f"geometry-{i}" for i in range(10)],
)
def test_invalid_geometry_keeps_explicit_delivery_evidence(location):
    snapshot = normalize_courier(
        {"drivers": [{"delivering_your_order": True, "location": location}]}, destination=[21, 52]
    )
    assert snapshot.delivery_in_progress is True
    assert snapshot.distance is None
    assert snapshot.latitude is None and snapshot.longitude is None


@pytest.mark.parametrize(
    "latitude,longitude", [(0, 0), (-90, -180), (90, 180), (45.13579, 13.24681)]
)
def test_valid_positions_preserve_original_precision_and_zero(latitude, longitude):
    snapshot = normalize_courier(
        {"drivers": [{"delivering_your_order": True, "location": [longitude, latitude]}]},
        destination=[longitude, latitude],
    )
    assert (snapshot.latitude, snapshot.longitude) == (latitude, longitude)
    assert snapshot.distance == 0


def test_same_order_identity_replacement_and_legacy_scalar_clear_position():
    state = runtime()
    gps = CourierSnapshot(100, True, latitude=45.13579, longitude=13.24681)
    state.update_courier(gps, "mismatch", order_key="another")
    assert state.courier_position() is None
    state.update_courier(gps, "match", order_key="synthetic")
    assert state.courier_position() == (45.13579, 13.24681)
    state.update_courier(100, "scalar", order_key="synthetic")
    assert state.courier_position() is None
    state.update_courier(gps, "again", order_key="synthetic")
    state.update_order(OrderSnapshot("replacement", "ready"), "replace")
    assert state.courier_position() is None
    state.update_courier(gps, "late", order_key="synthetic")
    assert state.courier_position() is None


def test_completion_expiry_and_reauthentication_cannot_revive_position():
    from custom_components.wolt_monitor.state import CompletionResult

    now = [0]
    state = Runtime(clock=lambda: now[0], retention_minutes=1)
    state.update_order(OrderSnapshot("synthetic", "ready"), "order")
    gps = CourierSnapshot(100, True, latitude=45.13579, longitude=13.24681)
    state.update_courier(gps, "gps", order_key="synthetic")
    state.confirm_delivery(CompletionResult("synthetic", True))
    assert state.courier_position() is None
    state.update_courier(gps, "late", order_key="synthetic")
    assert state.courier_position() is None
    now[0] = 61
    assert state.courier_position() is None
    assert state.order is None and state._position is None
    state.update_order(OrderSnapshot("new", "ready"), "new")
    state.update_courier(gps, "new-gps", order_key="new")
    state.fail("order", Failure("authentication", invalid_refresh=True), "auth")
    assert state.courier_position() is None and state._position is None
