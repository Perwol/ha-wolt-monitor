import importlib
import logging

from custom_components.wolt_monitor.errors import Failure


def test_debug_cycle_logs_only_fixed_labels_flags_and_safe_delays(caplog):
    cls = importlib.import_module("custom_components.wolt_monitor.safe_logging").SafeLog
    reporter = cls()
    with caplog.at_level(logging.DEBUG, logger="custom_components.wolt_monitor"):
        reporter.cycle(manual=True, due={"order", "fake-private-key"}, auth_wait=30, data_wait=60)
    assert "manual" in caplog.text
    assert "30" in caplog.text and "60" in caplog.text
    assert "fake-private" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_recovery_after_single_failure_is_logged_once(caplog):
    cls = importlib.import_module("custom_components.wolt_monitor.safe_logging").SafeLog
    reporter = cls()
    with caplog.at_level(logging.INFO, logger="custom_components.wolt_monitor"):
        reporter.failure("order", Failure("connection"), 1)
        reporter.success("order")
        reporter.success("order")
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1


def test_logging_deduplicates_threshold_rate_limit_and_recovery_without_secrets(caplog):
    cls = importlib.import_module("custom_components.wolt_monitor.safe_logging").SafeLog
    reporter = cls()
    with caplog.at_level(logging.DEBUG, logger="custom_components.wolt_monitor"):
        for _ in range(2):
            reporter.failure("order", Failure("rate_limit", 429, retry_after=60), 1)
            reporter.failure("order", Failure("connection"), 3)
        reporter.success("order")
        reporter.success("order")
        reporter.failure("courier", Failure("fake-private-server-text"), 1)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 2
    assert len(infos) == 1
    assert "fake-private" not in caplog.text
    assert "unexpected" in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
