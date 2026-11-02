"""Synthetic stale payment failures must not hide explicitly active orders."""

import pytest

from custom_components.wolt_monitor import api


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize(
    "raw", ["received", "acknowledged", "preorder-confirmed", "production", "ready"]
)
def test_unknown_previous_yields_to_active_replacement(present, raw):
    previous = api.normalize_order({"order_id": "stale", "status": "deferred_payment_failed"})
    replacement = api.normalize_order({"order_id": "fresh", "status": raw})
    orders = [previous, replacement] if present else [replacement]

    selected = api.select_order(orders, previous=previous)

    assert previous.status == "unknown"
    assert not previous.terminal
    assert selected.snapshot is replacement
    assert not selected.details_required


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize(
    "raw", ["received", "acknowledged", "preorder-confirmed", "production", "ready"]
)
def test_known_previous_remains_sticky(present, raw):
    previous = api.normalize_order({"order_id": "chosen", "status": raw})
    other = api.normalize_order({"order_id": "other", "status": "ready"})
    current = api.normalize_order({"order_id": "chosen", "status": raw})
    selected = api.select_order([other, current] if present else [other], previous=previous)
    assert selected.snapshot is (current if present else previous)
    assert selected.details_required is (not present)


@pytest.mark.parametrize(
    "old_raw,new_raw,expected",
    [
        ("deferred_payment_failed", "production", "chosen"),
        ("production", "deferred_payment_failed", "other"),
    ],
)
def test_current_status_takes_precedence_over_previous(old_raw, new_raw, expected):
    previous = api.normalize_order({"order_id": "chosen", "status": old_raw})
    current = api.normalize_order({"order_id": "chosen", "status": new_raw})
    other = api.normalize_order({"order_id": "other", "status": "ready"})
    selected = api.select_order([other, current], previous=previous)
    assert selected.snapshot.key == expected
    assert not selected.details_required


@pytest.mark.parametrize("present", [False, True])
def test_unknown_previous_preserved_without_eligible_active_replacement(present):
    previous = api.normalize_order({"order_id": "stale", "status": "deferred_payment_failed"})
    other_unknown = api.normalize_order({"order_id": "unknown", "status": "refunded"})
    excluded = api.normalize_order({"order_id": "excluded", "status": "production"})
    terminal = api.normalize_order({"order_id": "terminal", "status": "delivered"})
    orders = [other_unknown, excluded, terminal] + ([previous] if present else [])
    selected = api.select_order(orders, previous=previous, excluded={"excluded"})
    assert selected.snapshot is previous
    assert selected.details_required is (not present)


@pytest.mark.parametrize(
    "raw", ["received", "acknowledged", "preorder-confirmed", "production", "ready"]
)
def test_initial_selection_prefers_first_eligible_active_order(raw):
    orders = [
        api.normalize_order({"order_id": key, "status": status})
        for key, status in [
            ("unknown", "deferred_payment_failed"),
            ("excluded", "ready"),
            ("terminal", "delivered"),
            ("first", raw),
            ("second", "ready"),
        ]
    ]
    selected = api.select_order(orders, excluded={"excluded"})
    assert selected.snapshot is orders[3]
    assert not selected.details_required


def test_no_active_candidates_fall_back_to_first_unknown_or_none():
    unknown = api.normalize_order({"order_id": "unknown", "status": "deferred_payment_failed"})
    other = api.normalize_order({"order_id": "other", "status": "refunded"})
    terminal = api.normalize_order({"order_id": "terminal", "status": "rejected"})
    assert api.select_order([terminal, unknown, other]).snapshot is unknown
    assert api.select_order([terminal, unknown], excluded={"unknown"}).snapshot is None
    assert api.select_order([]).snapshot is None


def test_completed_previous_cannot_reselect_itself_as_active():
    previous = api.normalize_order({"order_id": "chosen", "status": "ready"})
    unknown = api.normalize_order({"order_id": "unknown", "status": "refunded"})
    assert (
        api.select_order([previous, unknown], previous=previous, completed=True).snapshot is unknown
    )
    assert api.select_order([previous], previous=previous, completed=True).snapshot is previous
