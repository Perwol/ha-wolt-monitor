import asyncio
import importlib
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.state import OrderSnapshot


@pytest.fixture
async def core(hass):
    from custom_components.wolt_monitor.api import OrderResult
    from custom_components.wolt_monitor.coordinator import WoltCoordinator

    # Explicit saved intervals keep this backoff regression independent of defaults.
    entry = MockConfigEntry(
        domain="wolt_monitor",
        data={"refresh_token": "fake-input"},
        options={
            "no_active_order_polling_interval_seconds": 300,
            "active_order_polling_interval_seconds": 60,
            "courier_polling_interval_seconds": 60,
        },
    )
    entry.add_to_hass(hass)
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(None)
    client.refresh.return_value = TokenReply("fake-access", "fake-refresh", 1000)
    now = [0.0]
    coordinator = WoltCoordinator(hass, entry, api, client, clock=lambda: now[0])
    yield coordinator, api, client, now
    await coordinator.async_shutdown()


async def test_list_selection_details_missing_is_failure_not_absence(core):
    from custom_components.wolt_monitor import api as boundary
    from custom_components.wolt_monitor.errors import Failure

    assert hasattr(boundary, "OrderListResult"), "Normalized list boundary is missing"
    coordinator, api, _, now = core
    selected = boundary.normalize_order({"order_id": "first", "status": "production"})
    other = boundary.normalize_order({"order_id": "second", "status": "received"})
    api.async_order.return_value = boundary.OrderListResult((selected, other))
    await coordinator.async_request_refresh()
    assert coordinator.runtime.order.key == "first"
    api.async_order.return_value = boundary.OrderListResult((other, selected))
    await coordinator.async_request_refresh()
    assert coordinator.runtime.order.key == "first"
    api.async_order.return_value = boundary.OrderListResult((other,))
    api.async_order_details.side_effect = Failure("server", 404)
    for i in range(3):
        if i:
            now[0] = coordinator.scheduler.next_deadline
        await coordinator.async_request_refresh()
        assert coordinator.runtime.order.key == "first"
        assert coordinator.data["status"] == ("preparing" if i < 2 else None)
    assert api.async_order_details.await_count == 3
    api.async_order_details.side_effect = None
    api.async_order_details.return_value = boundary.OrderResult(selected)
    now[0] = coordinator.scheduler.next_deadline
    await coordinator.async_request_refresh()
    assert coordinator.data["status"] == "preparing"
    api.async_order_details.return_value = boundary.OrderResult(None)
    await coordinator.async_request_refresh()
    assert coordinator.runtime.sources["order"].consecutive_errors == 1
    api.async_order_details.return_value = boundary.OrderResult(
        boundary.normalize_order({"order_id": "first", "status": "delivered"})
    )
    now[0] = coordinator.scheduler.next_deadline
    await coordinator.async_request_refresh()
    assert coordinator.runtime.order.key == "second"
    assert coordinator.runtime.is_delivered() is False


async def test_disappearance_details_preserve_selected_courier_metadata(core):
    from custom_components.wolt_monitor.api import OrderListResult, OrderResult, normalize_order
    from custom_components.wolt_monitor.state import CourierSnapshot

    coordinator, api, _, now = core
    snapshot = normalize_order({"order_id": "first", "status": "ready"})
    api.async_order.return_value = OrderListResult((snapshot,), True, True)
    api.async_courier.return_value = CourierSnapshot(5, True)
    await coordinator.async_request_refresh()
    api.async_order.return_value = OrderListResult((), False, False)
    api.async_order_details.return_value = OrderResult(snapshot, True, True)
    api.async_courier.return_value = CourierSnapshot(7, True)
    await coordinator.async_request_refresh()
    assert coordinator.courier_supported is True
    assert coordinator.scheduler.courier_eligible is True
    assert coordinator.data["distance"] == 7
    assert coordinator.runtime.delivery_in_progress() is True
    assert api.async_courier.await_count == 2
    now[0] = 60
    await coordinator.async_refresh()
    assert api.async_courier.await_count == 3


async def test_terminal_details_replacement_uses_list_courier_metadata(core):
    from custom_components.wolt_monitor.api import OrderListResult, OrderResult, normalize_order

    coordinator, api, _, _ = core
    first = normalize_order({"order_id": "first", "status": "ready"})
    second = normalize_order({"order_id": "second", "status": "production"})
    api.async_order.return_value = OrderListResult((first,), False, False)
    await coordinator.async_request_refresh()
    api.async_order.return_value = OrderListResult((second,), False, False)
    api.async_order_details.return_value = OrderResult(
        normalize_order({"order_id": "first", "status": "delivered"}), True, True
    )
    await coordinator.async_request_refresh()
    assert coordinator.runtime.order.key == "second"
    assert coordinator.courier_supported is False
    assert coordinator.scheduler.courier_eligible is False
    api.async_courier.assert_not_awaited()


async def test_eta_ticks_locally_without_fetching_or_bypassing_backoff(core, hass, freezer):
    from datetime import UTC, datetime, timedelta

    from homeassistant.util.dt import utcnow
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.wolt_monitor.api import OrderResult, normalize_order

    coordinator, api, _, _ = core
    now = utcnow()
    api.async_order.return_value = OrderResult(
        normalize_order(
            {
                "order_id": "synthetic",
                "status": "production",
                "delivery_eta": (now + timedelta(seconds=61)).isoformat(),
            }
        )
    )
    coordinator.runtime.wall_clock = utcnow
    await coordinator.async_request_refresh()
    assert coordinator.data["eta"] == 2
    assert getattr(coordinator, "_eta_cancel", None) is not None, "Local ETA timer is missing"
    coordinator._data_not_before = 900
    freezer.move_to(now + timedelta(seconds=2))
    async_fire_time_changed(hass, datetime.now(UTC))
    await hass.async_block_till_done()
    assert coordinator.data["eta"] == 1
    api.async_order.assert_awaited_once()


async def test_terminal_on_list_replaced_immediately_and_refund_stops_courier(core):
    from custom_components.wolt_monitor.api import OrderListResult, normalize_order
    from custom_components.wolt_monitor.state import CourierSnapshot

    coordinator, api, _, _ = core
    active = normalize_order({"order_id": "first", "status": "ready"})
    api.async_order.return_value = OrderListResult((active,), True, True)
    api.async_courier.return_value = CourierSnapshot(1, True)
    await coordinator.async_request_refresh()
    delivered = normalize_order({"order_id": "first", "status": "delivered"})
    other = normalize_order({"order_id": "second", "status": "production"})
    api.async_order.return_value = OrderListResult((delivered, other), True, True)
    await coordinator.async_request_refresh()
    assert coordinator.runtime.order.key == "second"
    assert "first" in coordinator.runtime.excluded_keys
    api.async_order.return_value = OrderListResult(
        (normalize_order({"order_id": "second", "status": "delivered"}),), True, True
    )
    await coordinator.async_request_refresh()
    reads = api.async_courier.await_count
    deadline = coordinator.runtime.retention_deadline
    api.async_order.return_value = OrderListResult(
        (normalize_order({"order_id": "second", "status": "refunded"}),), True, True
    )
    await coordinator.async_request_refresh()
    assert coordinator.runtime.is_delivered() is True
    assert coordinator.runtime.retention_deadline == deadline
    assert coordinator.scheduler.next_deadline == 300
    assert not coordinator.scheduler.courier_eligible
    assert api.async_courier.await_count == reads


async def test_stop_after_list_does_not_start_disappearance_details(core):
    from custom_components.wolt_monitor.api import OrderListResult, normalize_order

    coordinator, api, _, _ = core
    selected = normalize_order({"order_id": "synthetic", "status": "production"})
    api.async_order.return_value = OrderListResult((selected,))
    await coordinator.async_request_refresh()
    entered, ready = asyncio.Event(), asyncio.Event()

    async def read(_):
        entered.set()
        await ready.wait()
        return OrderListResult(())

    api.async_order.side_effect = read
    waiter = asyncio.create_task(coordinator.async_request_refresh())
    await entered.wait()
    shutdown = asyncio.create_task(coordinator.async_shutdown())
    await asyncio.sleep(0)
    ready.set()
    await waiter
    await shutdown
    api.async_order_details.assert_not_awaited()
    assert coordinator.runtime.sources["order"].consecutive_errors == 0


async def test_expired_token_after_list_is_not_sent_to_details(core):
    from custom_components.wolt_monitor.api import OrderListResult, normalize_order

    coordinator, api, client, now = core
    selected = normalize_order({"order_id": "synthetic", "status": "production"})
    api.async_order.return_value = OrderListResult((selected,))
    await coordinator.async_request_refresh()

    async def read(_):
        now[0] = 1000
        return OrderListResult(())

    api.async_order.side_effect = read
    await coordinator.async_request_refresh()
    api.async_order_details.assert_not_awaited()
    client.refresh.assert_awaited_once()
    assert coordinator.runtime.sources["order"].last_error_category == "timeout"
    assert not coordinator.runtime.reauth_required


async def test_ha_stop_closes_client_and_disables_new_refresh(core, hass):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    coordinator, api, client, _ = core
    await coordinator.async_request_refresh()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert coordinator._stopped
    assert coordinator._poll_cancel is None
    assert coordinator._retention_cancel is None
    client.close.assert_awaited_once()
    await coordinator.async_request_refresh()
    assert api.async_order.await_count == 1


async def test_stop_waits_for_inflight_order_without_starting_courier(core, hass):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    from custom_components.wolt_monitor.api import OrderResult

    coordinator, api, client, _ = core
    entered, ready = asyncio.Event(), asyncio.Event()

    async def read(_):
        entered.set()
        await ready.wait()
        return OrderResult(OrderSnapshot("fake-key", "unknown"), True, True)

    api.async_order.side_effect = read
    waiter = asyncio.create_task(coordinator.async_request_refresh())
    await entered.wait()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        assert coordinator._stopped
    finally:
        ready.set()
        await waiter
        await hass.async_block_till_done()
    api.async_courier.assert_not_awaited()
    client.close.assert_awaited_once()
    assert coordinator._poll_cancel is None


async def test_stop_during_courier_token_repair_suppresses_retry_without_error(core):
    from custom_components.wolt_monitor.api import OrderResult
    from custom_components.wolt_monitor.errors import Failure

    coordinator, api, client, _ = core
    api.async_order.return_value = OrderResult(OrderSnapshot("fake-key", "unknown"), True, True)
    api.async_courier.side_effect = Failure("authentication", 401)
    entered, ready = asyncio.Event(), asyncio.Event()
    refresh_calls = []

    async def refresh(_):
        refresh_calls.append(1)
        if len(refresh_calls) == 2:
            entered.set()
            await ready.wait()
        return TokenReply("fake-access" + str(len(refresh_calls)), "fake-refresh", 1000)

    client.refresh.side_effect = refresh
    waiter = asyncio.create_task(coordinator.async_request_refresh())
    await entered.wait()
    shutdown = asyncio.create_task(coordinator.async_shutdown())
    await asyncio.sleep(0)
    try:
        assert coordinator._stopped
        client.close.assert_not_awaited()
    finally:
        ready.set()
        await waiter
        await shutdown
    assert api.async_courier.await_count == 1
    assert coordinator.runtime.sources["courier"].consecutive_errors == 0
    assert coordinator._poll_cancel is None
    client.close.assert_awaited_once()


async def test_final_source_errors_drive_independent_exponential_intervals(core):
    from custom_components.wolt_monitor.api import OrderResult
    from custom_components.wolt_monitor.errors import Failure

    coordinator, api, _, now = core
    api.async_order.side_effect = Failure("connection")
    await coordinator.async_refresh()
    assert coordinator.scheduler.next_deadline == 600
    now[0] = 300
    await coordinator.async_refresh()
    assert api.async_order.await_count == 1
    assert coordinator.runtime.sources["order"].consecutive_errors == 1
    now[0] = 600
    api.async_order.side_effect = None
    api.async_order.return_value = OrderResult(OrderSnapshot("fake-key", "unknown"), True, True)
    api.async_courier.side_effect = Failure("timeout")
    await coordinator.async_refresh()
    assert coordinator.scheduler.next_deadline == 660
    now[0] = 660
    await coordinator.async_refresh()
    assert api.async_courier.await_count == 1
    assert coordinator.scheduler.next_deadline == 720
    now[0] = 720
    await coordinator.async_refresh()
    assert api.async_courier.await_count == 2
    now[0] = 900
    await coordinator.async_refresh()
    assert api.async_courier.await_count == 2
    now[0] = 960
    api.async_courier.side_effect = None
    api.async_courier.return_value = 5
    await coordinator.async_refresh()
    assert coordinator.runtime.sources["courier"].consecutive_errors == 0
    assert coordinator.scheduler.next_deadline == 1020


async def test_manual_order_failure_wait_preserves_requests_and_error_count(core):
    from custom_components.wolt_monitor.errors import Failure

    coordinator, api, _, now = core
    api.async_order.side_effect = Failure("connection")
    await coordinator.async_request_refresh()
    now[0] = 599
    await coordinator.async_request_refresh()
    assert api.async_order.await_count == 1
    assert coordinator.runtime.sources["order"].consecutive_errors == 1
    now[0] = 600
    await coordinator.async_request_refresh()
    assert api.async_order.await_count == 2
    assert coordinator.runtime.sources["order"].consecutive_errors == 2


async def test_manual_healthy_order_cannot_force_failed_same_order_courier(core):
    from custom_components.wolt_monitor.api import OrderResult
    from custom_components.wolt_monitor.errors import Failure

    coordinator, api, _, now = core
    snapshot = OrderSnapshot("fake-key", "ready")
    api.async_order.return_value = OrderResult(snapshot, True, True)
    api.async_courier.side_effect = Failure("timeout")
    await coordinator.async_request_refresh()
    api.async_order.return_value = OrderResult(snapshot, False, True)
    await coordinator.async_request_refresh()
    api.async_order.return_value = OrderResult(snapshot, True, True)
    now[0] = 119
    await coordinator.async_request_refresh()
    assert api.async_order.await_count == 3
    assert api.async_courier.await_count == 1
    assert coordinator.runtime.sources["courier"].consecutive_errors == 1
    now[0] = 120
    await coordinator.async_request_refresh()
    assert api.async_courier.await_count == 2
    assert coordinator.runtime.sources["courier"].consecutive_errors == 2


@pytest.mark.parametrize("initial_success", [True, False])
async def test_new_selected_order_preserves_courier_failure_backoff(core, initial_success):
    from custom_components.wolt_monitor.api import OrderListResult, OrderResult, normalize_order
    from custom_components.wolt_monitor.errors import Failure
    from custom_components.wolt_monitor.state import CourierSnapshot

    coordinator, api, _, now = core
    first = normalize_order({"order_id": "first", "status": "ready"})
    second = normalize_order({"order_id": "second", "status": "ready"})
    delivered = normalize_order({"order_id": "first", "status": "delivered"})
    api.async_order.return_value = OrderListResult(
        (first,), per_order=(OrderResult(first, True, True),)
    )
    if initial_success:
        api.async_courier.return_value = CourierSnapshot(5, True)
        await coordinator.async_refresh()
        assert coordinator.data["distance"] == 5
        now[0] = 60
    api.async_courier.side_effect = Failure("server", 503)
    await coordinator.async_refresh()
    failed_reads = api.async_courier.await_count
    deadline = now[0] + 120
    source = coordinator.runtime.sources["courier"]
    last_success = source.last_successful_update
    assert source.consecutive_errors == 1

    api.async_order.return_value = OrderListResult(
        (delivered, second),
        per_order=(OrderResult(delivered, False, True), OrderResult(second, True, True)),
    )
    api.async_courier.side_effect = None
    api.async_courier.return_value = CourierSnapshot(9, False)
    now[0] += 30
    await coordinator.async_request_refresh()
    assert coordinator.runtime.order.key == "second"
    assert "first" in coordinator.runtime.excluded_keys
    assert coordinator.scheduler.courier_eligible
    assert api.async_courier.await_count == failed_reads
    assert coordinator.data["distance"] is None
    assert coordinator.data["status"] == "ready"
    assert source.consecutive_errors == 1
    assert source.last_http_status == 503
    assert source.last_successful_update == last_success
    assert coordinator.runtime.sources["order"].consecutive_errors == 0

    now[0] = deadline - 1
    await coordinator.async_request_refresh()
    assert api.async_courier.await_count == failed_reads
    assert source.consecutive_errors == 1
    assert coordinator.data["distance"] is None
    now[0] = deadline
    await coordinator.async_refresh()
    assert api.async_courier.await_count == failed_reads + 1
    api.async_courier.assert_awaited_with("fake-access", "second")
    assert coordinator.runtime._distance == 9
    assert coordinator.data["distance"] is None
    assert source.consecutive_errors == 0
    assert source.last_http_status is None


@pytest.mark.parametrize("manual", [True, False])
async def test_healthy_new_order_reads_courier_before_normal_interval(core, manual):
    from custom_components.wolt_monitor.api import OrderListResult, OrderResult, normalize_order
    from custom_components.wolt_monitor.scheduler import Options
    from custom_components.wolt_monitor.state import CourierSnapshot

    coordinator, api, _, now = core
    coordinator.scheduler.apply_options(Options(active_seconds=30, courier_seconds=120))
    first = normalize_order({"order_id": "first", "status": "ready"})
    second = normalize_order({"order_id": "second", "status": "ready"})
    delivered = normalize_order({"order_id": "first", "status": "delivered"})
    api.async_order.return_value = OrderListResult(
        (first,), per_order=(OrderResult(first, True, True),)
    )
    api.async_courier.return_value = CourierSnapshot(5, True)
    await coordinator.async_refresh()
    api.async_order.return_value = OrderListResult(
        (delivered, second),
        per_order=(OrderResult(delivered, False, True), OrderResult(second, True, True)),
    )
    api.async_courier.return_value = CourierSnapshot(9, False)
    now[0] = 30
    if manual:
        await coordinator.async_request_refresh()
    else:
        await coordinator.async_refresh()
    assert coordinator.runtime.order.key == "second"
    assert api.async_courier.await_count == 2
    api.async_courier.assert_awaited_with("fake-access", "second")
    assert coordinator.runtime._distance == 9
    assert coordinator.data["distance"] is None
    assert coordinator.runtime.sources["courier"].consecutive_errors == 0


async def test_manual_calls_coalesce_without_queued_cycle(core):
    from custom_components.wolt_monitor.api import OrderResult

    coordinator, api, _, _ = core
    entered, ready = asyncio.Event(), asyncio.Event()

    async def read(_):
        entered.set()
        await ready.wait()
        return OrderResult(None)

    api.async_order.side_effect = read
    first = asyncio.create_task(coordinator.async_request_refresh())
    await entered.wait()
    second = asyncio.create_task(coordinator.async_request_refresh())
    await asyncio.sleep(0)
    ready.set()
    await asyncio.gather(first, second)
    assert api.async_order.await_count == 1
    await coordinator.async_request_refresh()
    assert api.async_order.await_count == 2


async def test_auth_backoff_wait_does_not_add_source_errors(core):
    from custom_components.wolt_monitor.errors import Failure

    coordinator, api, client, _ = core
    client.refresh.side_effect = Failure("rate_limit", 429, retry_after=900)
    await coordinator.async_request_refresh()
    assert coordinator.runtime.sources["order"].consecutive_errors == 1
    assert coordinator._data_not_before == 0
    await coordinator.async_request_refresh()
    assert coordinator.runtime.sources["order"].consecutive_errors == 1
    assert client.refresh.await_count == 1
    api.async_order.assert_not_awaited()


async def test_cancelled_only_waiter_allows_next_manual_cycle(core):
    from custom_components.wolt_monitor.api import OrderResult

    coordinator, api, _, _ = core
    entered, ready, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def read(_):
        entered.set()
        await ready.wait()
        finished.set()
        return OrderResult(None)

    api.async_order.side_effect = read
    waiter = asyncio.create_task(coordinator.async_request_refresh())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    ready.set()
    await finished.wait()
    await asyncio.sleep(0)
    await coordinator.async_request_refresh()
    assert api.async_order.await_count == 2


async def test_order_refresh_does_not_ignore_independent_courier_interval(core):
    from custom_components.wolt_monitor.api import OrderResult
    from custom_components.wolt_monitor.scheduler import Options

    coordinator, api, _, now = core
    coordinator.scheduler.apply_options(Options(active_seconds=30, courier_seconds=120))
    api.async_order.return_value = OrderResult(OrderSnapshot("fake-key", "unknown"), True, True)
    api.async_courier.return_value = 100
    await coordinator.async_refresh()
    now[0] = 30
    await coordinator.async_refresh()
    assert api.async_order.await_count == 2
    assert api.async_courier.await_count == 1


async def test_malformed_adapter_result_is_sanitized_invalid_data(core):
    coordinator, api, _, _ = core
    api.async_order.return_value = {"private": "fake-secret"}
    await coordinator.async_request_refresh()
    assert coordinator.runtime.sources["order"].last_error_category == "invalid_data"
    assert coordinator.data["status"] is None


async def test_courier_eligibility_loss_stops_reads_and_clears_distance(core):
    from custom_components.wolt_monitor.api import OrderResult

    coordinator, api, _, _ = core
    api.async_order.return_value = OrderResult(OrderSnapshot("fake-key", "unknown"), True, True)
    api.async_courier.return_value = 5
    await coordinator.async_request_refresh()
    api.async_order.return_value = OrderResult(OrderSnapshot("fake-key", "unknown"), False, True)
    await coordinator.async_request_refresh()
    assert api.async_courier.await_count == 1
    assert coordinator.data["distance"] is None


@pytest.mark.parametrize("bad_distance", ["fake-private", True, -1, float("nan")])
async def test_invalid_courier_value_is_error_not_success(core, bad_distance):
    from custom_components.wolt_monitor.api import OrderResult

    coordinator, api, _, _ = core
    api.async_order.return_value = OrderResult(OrderSnapshot("fake-key", "unknown"), True, True)
    api.async_courier.return_value = 5
    await coordinator.async_request_refresh()
    api.async_courier.return_value = bad_distance
    await coordinator.async_request_refresh()
    assert coordinator.runtime.sources["courier"].last_error_category == "invalid_data"
    assert coordinator.runtime._distance == 5
    assert coordinator.data["distance"] is None


async def test_retention_timer_runs_while_data_requests_are_blocked(core, hass, freezer):
    from homeassistant.util.dt import utcnow
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.wolt_monitor.api import OrderResult
    from custom_components.wolt_monitor.errors import Failure
    from custom_components.wolt_monitor.scheduler import Options

    coordinator, api, _, now = core
    coordinator.scheduler.apply_options(Options(retention_minutes=1))
    coordinator.runtime.set_retention(1)
    api.async_order.return_value = OrderResult(
        OrderSnapshot("fake-key", "delivered", terminal=True)
    )
    await coordinator.async_request_refresh()
    assert coordinator._retention_cancel is not None
    api.async_order.side_effect = Failure("rate_limit", 429, retry_after=900)
    await coordinator.async_request_refresh()
    before = api.async_order.await_count
    now[0] = 60
    freezer.tick(60)
    async_fire_time_changed(hass, utcnow())
    await hass.async_block_till_done()
    assert coordinator.data["status"] == "no_active_order"
    assert api.async_order.await_count == before
    assert coordinator._retention_cancel is None


async def test_courier_and_order_thresholds_recover_independently(core):
    from custom_components.wolt_monitor.api import OrderResult
    from custom_components.wolt_monitor.errors import Failure

    coordinator, api, _, now = core
    api.async_order.return_value = OrderResult(
        OrderSnapshot("fake-key", "unknown", "fake-restaurant"), True, True
    )
    api.async_courier.return_value = 5
    await coordinator.async_request_refresh()
    api.async_courier.side_effect = Failure("timeout")
    for deadline in (0, 120, 360):
        now[0] = deadline
        await coordinator.async_request_refresh()
    assert coordinator.data["distance"] is None
    assert coordinator.data["restaurant"] == "fake-restaurant"
    api.async_courier.side_effect = None
    now[0] = 840
    await coordinator.async_request_refresh()
    assert coordinator.runtime._distance == 5
    assert coordinator.data["distance"] is None
    api.async_order.side_effect = Failure("server", 503)
    for deadline in (840, 960, 1200):
        now[0] = deadline
        await coordinator.async_request_refresh()
    assert all(value is None for value in coordinator.data.values())
    assert coordinator.runtime.sources["courier"].consecutive_errors == 0


async def test_definitive_refresh_refusal_stops_polling_and_starts_reauth(core, hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    from custom_components.wolt_monitor.errors import Failure

    coordinator, _, client, _ = core
    client.refresh.side_effect = Failure("authentication", 401, invalid_refresh=True)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_config_entry_first_refresh()
    await hass.async_block_till_done()
    assert coordinator.runtime.reauth_required
    assert coordinator._poll_cancel is None
    assert any(
        flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress()
    )
    await coordinator.async_request_refresh()
    assert client.refresh.await_count == 1


async def test_startup_connection_failure_is_not_ready(core):
    from homeassistant.exceptions import ConfigEntryNotReady

    from custom_components.wolt_monitor.errors import Failure

    coordinator, _, client, _ = core
    client.refresh.side_effect = Failure("connection")
    with pytest.raises(ConfigEntryNotReady):
        await coordinator.async_config_entry_first_refresh()


async def test_first_cycle_fetches_order_and_newly_eligible_courier(hass):
    api_mod = importlib.import_module("custom_components.wolt_monitor.api")
    coordinator_mod = importlib.import_module("custom_components.wolt_monitor.coordinator")
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_order.return_value = api_mod.OrderResult(
        OrderSnapshot("fake-key", "unknown", "fake-private"),
        courier_eligible=True,
        courier_supported=True,
    )
    api.async_courier.return_value = 100
    client = AsyncMock()
    client.refresh.return_value = TokenReply("fake-access", "fake-rotated", 100)
    now = [0.0]
    coordinator = coordinator_mod.WoltCoordinator(hass, entry, api, client, clock=lambda: now[0])
    await coordinator.async_config_entry_first_refresh()
    assert coordinator.runtime._distance == 100
    assert coordinator.data["distance"] is None
    assert entry.data == {"refresh_token": "fake-rotated"}
    assert coordinator.scheduler.next_deadline == 30
    api.async_order.assert_awaited_once()
    api.async_courier.assert_awaited_once()
    await coordinator.async_shutdown()
    client.close.assert_awaited_once()
