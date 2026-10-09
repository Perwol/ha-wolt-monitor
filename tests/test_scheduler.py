"""Scheduling policy tests with no Home Assistant runtime."""

import importlib


def test_scheduler_starts_immediately_and_does_not_queue_overlapping_cycles():
    mod = importlib.import_module("custom_components.wolt_monitor.scheduler")
    now = [0.0]
    scheduler = mod.Scheduler(clock=lambda: now[0])
    assert scheduler.begin() == frozenset({"order"})
    now[0] = 1000
    assert scheduler.begin(manual=True) == frozenset()
    scheduler.finish()
    assert scheduler.begin() == frozenset()
    now[0] = 1300
    assert scheduler.begin() == frozenset({"order"})


def test_order_courier_deadlines_are_independent_and_data_retry_after_is_shared():
    from custom_components.wolt_monitor.scheduler import Options, Scheduler

    now = [0.0]
    scheduler = Scheduler(
        clock=lambda: now[0],
        options=Options(idle_seconds=300, active_seconds=60, courier_seconds=90),
    )
    scheduler.set_activity(active=True, courier_eligible=True)
    assert scheduler.begin() == frozenset({"order", "courier"})
    scheduler.finish()
    now[0] = 60
    assert scheduler.begin() == frozenset({"order"})
    scheduler.finish()
    now[0] = 90
    assert scheduler.begin() == frozenset({"courier"})
    scheduler.defer_data(120)
    scheduler.finish()
    now[0] = 200
    assert scheduler.begin(manual=True) == frozenset()
    assert scheduler.next_deadline == 210
    now[0] = 210
    assert scheduler.begin() == frozenset({"order", "courier"})
    scheduler.set_activity(active=False, courier_eligible=True)
    scheduler.finish()
    now[0] = 510
    assert scheduler.begin() == frozenset({"order"})


def test_options_live_changes_replace_deadlines_from_last_completion():
    from custom_components.wolt_monitor.scheduler import Options, Scheduler

    now = [0.0]
    scheduler = Scheduler(clock=lambda: now[0])
    scheduler.set_activity(active=True, courier_eligible=False)
    scheduler.begin()
    scheduler.finish()
    now[0] = 45
    scheduler.apply_options(Options(active_seconds=30))
    assert scheduler.begin() == frozenset({"order"})
    scheduler.finish()
    scheduler.set_activity(active=True, courier_eligible=True)
    assert scheduler.begin() == frozenset({"courier"})


def test_source_errors_double_interval_cap_and_reset_independently():
    from custom_components.wolt_monitor.scheduler import Options, Scheduler

    now = [0.0]
    scheduler = Scheduler(
        clock=lambda: now[0], options=Options(active_seconds=30, courier_seconds=60)
    )
    scheduler.set_activity(active=True, courier_eligible=True)
    assert scheduler.begin() == {"order", "courier"}
    scheduler.set_errors("order", 1)
    scheduler.set_errors("courier", 2)
    scheduler.finish()
    assert scheduler.next_deadline == 60
    now[0] = 60
    assert scheduler.begin() == {"order"}
    scheduler.set_errors("order", 2)
    scheduler.finish()
    assert scheduler.next_deadline == 180
    now[0] = 180
    assert scheduler.begin() == {"order"}
    scheduler.set_errors("order", 0)
    scheduler.finish()
    assert scheduler.next_deadline == 210
    now[0] = 240
    scheduler.begin()
    scheduler.set_errors("order", 1000000)
    scheduler.set_errors("courier", 1000000)
    scheduler.finish()
    assert scheduler.next_deadline == 1140
    scheduler.defer_data(1800)
    assert scheduler.next_deadline == 2040
    assert not scheduler.begin(manual=True)


def test_live_options_preserve_error_streak_and_backoff_cap():
    from custom_components.wolt_monitor.scheduler import Options, Scheduler

    now = [0.0]
    scheduler = Scheduler(
        clock=lambda: now[0],
        options=Options(idle_seconds=300, active_seconds=60, courier_seconds=60),
    )
    scheduler.begin()
    scheduler.set_errors("order", 1)
    scheduler.finish()
    assert scheduler.next_deadline == 600
    scheduler.apply_options(Options(idle_seconds=600, active_seconds=60, courier_seconds=60))
    assert scheduler.next_deadline == 900
    scheduler.set_activity(active=True, courier_eligible=False)
    assert scheduler.next_deadline == 120


def test_manual_refresh_respects_each_failed_source_deadline():
    from custom_components.wolt_monitor.scheduler import Options, Scheduler

    now = [0.0]
    scheduler = Scheduler(
        clock=lambda: now[0],
        options=Options(idle_seconds=300, active_seconds=60, courier_seconds=60),
    )
    scheduler.set_activity(active=True, courier_eligible=True)
    scheduler.begin()
    scheduler.set_errors("order", 1)
    scheduler.set_errors("courier", 2)
    scheduler.finish()
    now[0] = 119
    assert not scheduler.begin(manual=True)
    now[0] = 120
    assert scheduler.begin(manual=True) == {"order"}
    scheduler.set_errors("order", 0)
    scheduler.finish()
    scheduler.set_activity(active=True, courier_eligible=False)
    scheduler.set_activity(active=True, courier_eligible=True)
    assert not scheduler.courier_due
    scheduler.apply_options(Options(idle_seconds=300, active_seconds=60, courier_seconds=60))
    now[0] = 239
    assert scheduler.begin(manual=True) == {"order"}
    scheduler.finish()
    now[0] = 240
    assert scheduler.begin(manual=True) == {"order", "courier"}


def test_manual_refresh_keeps_healthy_courier_independent_of_failed_order():
    from custom_components.wolt_monitor.scheduler import Options, Scheduler

    now = [0.0]
    scheduler = Scheduler(
        clock=lambda: now[0],
        options=Options(idle_seconds=300, active_seconds=60, courier_seconds=60),
    )
    scheduler.set_activity(active=True, courier_eligible=True)
    scheduler.begin()
    scheduler.set_errors("order", 1)
    scheduler.finish()
    now[0] = 1
    assert scheduler.begin(manual=True) == {"courier"}
    scheduler.finish()
    assert scheduler.next_deadline == 61
    now[0] = 119
    assert scheduler.begin(manual=True) == {"courier"}
    scheduler.finish()
    now[0] = 120
    assert scheduler.begin(manual=True) == {"order", "courier"}


def test_invalid_options_cannot_be_applied():
    import pytest

    from custom_components.wolt_monitor.scheduler import Options

    for kwargs in [
        {"idle_seconds": 61},
        {"active_seconds": 31},
        {"courier_seconds": True},
        {"retention_minutes": -1},
        {"idle_seconds": 601},
        {"active_seconds": 301},
    ]:
        with pytest.raises(ValueError):
            Options(**kwargs)


def test_new_polling_defaults_drive_actual_source_deadlines():
    from custom_components.wolt_monitor.const import options_from
    from custom_components.wolt_monitor.scheduler import Options, Scheduler

    defaults = options_from({})
    assert defaults == Options(idle_seconds=120, active_seconds=30, courier_seconds=30)
    now = [0.0]
    scheduler = Scheduler(clock=lambda: now[0])
    assert scheduler.begin() == {"order"}
    scheduler.finish()
    assert scheduler.next_deadline == 120
    now[0] = 119
    assert not scheduler.begin()
    now[0] = 120
    assert scheduler.begin() == {"order"}
    scheduler.finish()
    scheduler.set_activity(active=True, courier_eligible=True)
    assert scheduler.begin() == {"courier"}
    scheduler.finish()
    assert scheduler.next_deadline == 150
    now[0] = 149
    assert not scheduler.begin()
    now[0] = 150
    assert scheduler.begin() == {"order", "courier"}
    scheduler.finish()
    assert scheduler.next_deadline == 180
