"""Memory-only normalized order policy; no Wolt payload mapping."""

import importlib


def test_no_previous_success_is_unavailable_even_before_error_threshold():
    from custom_components.wolt_monitor.errors import Failure
    from custom_components.wolt_monitor.state import Runtime

    runtime = Runtime(clock=lambda: 0)
    assert runtime.values()["status"] is None
    runtime.fail("order", Failure("connection"), "first")
    assert runtime.values()["status"] is None
    runtime.update_order(None, "second")
    assert runtime.values()["status"] == "no_active_order"


def test_terminal_retention_expires_locally_and_never_reappears_in_session():
    state = importlib.import_module("custom_components.wolt_monitor.state")
    now = [0.0]
    runtime = state.Runtime(clock=lambda: now[0], retention_minutes=1)
    order = state.OrderSnapshot("fake-key", "delivered", "fake-restaurant", terminal=True)
    runtime.update_order(order, "cycle")
    assert runtime.values()["status"] == "delivered"
    assert runtime.values()["eta"] == 0
    now[0] = 30
    runtime.update_order(order, "next")
    now[0] = 60
    assert runtime.values() == {
        "status": "no_active_order",
        "restaurant": None,
        "distance": None,
        "estimated_delivery_time": None,
        "eta": None,
    }
    runtime.update_order(order, "again")
    assert runtime.values()["status"] == "no_active_order"


def test_expired_key_cannot_return_with_nonterminal_status():
    from custom_components.wolt_monitor.state import OrderSnapshot, Runtime

    now = [0.0]
    runtime = Runtime(clock=lambda: now[0], retention_minutes=1)
    runtime.update_order(OrderSnapshot("fake-key", "delivered", terminal=True), "a")
    now[0] = 60
    runtime.expire()
    for cycle, terminal in [("b", False), ("c", True)]:
        runtime.update_order(
            OrderSnapshot(
                "fake-key", "preparing" if not terminal else "delivered", terminal=terminal
            ),
            cycle,
        )
        assert runtime.values()["status"] == "no_active_order"
        assert runtime.retention_deadline is None


def test_retention_live_changes_use_original_start_and_cannot_resurrect():
    from custom_components.wolt_monitor.state import OrderSnapshot, Runtime

    now = [0.0]
    runtime = Runtime(clock=lambda: now[0], retention_minutes=2)
    runtime.update_order(OrderSnapshot("fake-key", "cancelled", terminal=True), "first")
    now[0] = 90
    runtime.set_retention(3)
    assert runtime.retention_deadline == 180
    runtime.update_order(None, "empty-source")
    assert runtime.values()["status"] == "cancelled"
    runtime.set_retention(1)
    assert runtime.values()["status"] == "no_active_order"
    runtime.set_retention(60)
    assert runtime.values()["status"] == "no_active_order"
    runtime.update_order(OrderSnapshot("fake-new", "delivered", terminal=True), "new")
    runtime.set_retention(0)
    assert runtime.values()["status"] == "no_active_order"


def test_source_errors_are_once_per_cycle_independent_and_reauth_has_precedence():
    from custom_components.wolt_monitor.errors import Failure
    from custom_components.wolt_monitor.state import OrderSnapshot, Runtime

    runtime = Runtime(clock=lambda: 0)
    runtime.update_order(OrderSnapshot("fake-key", "unknown", "fake-restaurant"), "first")
    runtime.update_courier(100, "first", order_key="fake-key")
    assert runtime._distance == 100
    assert runtime.values()["distance"] is None
    for cycle in ["a", "b", "c"]:
        runtime.fail("courier", Failure("connection"), cycle)
        runtime.fail("courier", Failure("timeout"), cycle)
    assert runtime.sources["courier"].consecutive_errors == 3
    assert runtime.values()["distance"] is None
    assert runtime.values()["restaurant"] == "fake-restaurant"
    runtime.update_courier(90, "d", order_key="fake-key")
    assert runtime.sources["courier"].consecutive_errors == 0
    before = runtime.sources["order"].last_successful_update
    for cycle in ["a", "b"]:
        runtime.fail("order", Failure("authentication", 401), cycle)
        assert runtime.values()["status"] == "unknown"
    runtime.fail("order", Failure("timeout"), "c")
    assert runtime.values()["status"] is None
    assert runtime.values()["distance"] is None
    assert runtime.sources["order"].last_successful_update == before
    runtime.update_order(OrderSnapshot("fake-key", "unknown"), "recovered")
    assert runtime.values()["restaurant"] is None
    runtime.reauth_required = True
    assert all(value is None for value in runtime.values().values())


def test_stale_courier_result_cannot_attach_to_replacement_order():
    from custom_components.wolt_monitor.state import OrderSnapshot, Runtime

    runtime = Runtime(clock=lambda: 0)
    runtime.update_order(OrderSnapshot("fake-old", "unknown"), "a")
    runtime.update_order(OrderSnapshot("fake-new", "unknown"), "b")
    runtime.update_courier(100, "a", order_key="fake-old")
    assert runtime.values()["distance"] is None
    assert runtime.sources["courier"].last_successful_update is None
    runtime.update_courier(50, "b", order_key="fake-new")
    assert runtime._distance == 50
    assert runtime.values()["distance"] is None


def test_new_order_clears_courier_and_expiry_runs_during_order_errors():
    from custom_components.wolt_monitor.errors import Failure
    from custom_components.wolt_monitor.state import OrderSnapshot, Runtime

    now = [0.0]
    runtime = Runtime(clock=lambda: now[0], retention_minutes=1)
    runtime.update_order(OrderSnapshot("fake-first", "unknown"), "first")
    runtime.update_courier(100, "first", order_key="fake-first")
    runtime.update_order(OrderSnapshot("fake-second", "unknown"), "second")
    assert runtime.values()["distance"] is None
    runtime.update_order(OrderSnapshot("fake-second", "delivered", terminal=True), "third")
    for cycle in ["a", "b", "c"]:
        runtime.fail("order", Failure("server", 503), cycle)
    now[0] = 60
    assert runtime.values()["status"] is None
    assert runtime.retention_deadline is None
    runtime.update_order(None, "success")
    assert runtime.values()["status"] == "no_active_order"
