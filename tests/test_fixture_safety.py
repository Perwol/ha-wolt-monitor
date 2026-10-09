"""Offline synthetic sanitizer regressions; no account access."""

from datetime import UTC, datetime, timedelta

import pytest

from scripts.wolt_fixtures import CaptureSession


@pytest.mark.parametrize(
    "status",
    [
        "deferred_payment_failed",
        "process_payment_failed",
        "payment_method_not_valid_error",
        "invalid",
        "estimated",
        "pending_transaction",
        "pending_revenue_transaction",
        "picked_up",
        "refunded",
    ],
)
def test_known_raw_status_survives_without_changing_normalization(status):
    from custom_components.wolt_monitor.api import normalize_order

    session = CaptureSession(offset=timedelta(0), provenance="synthetic")
    clean = session.capture(
        "details", {"order_details": [{"order_id": "PRIVATE", "status": status}]}
    )["order_details"][0]
    assert clean["status"] == status
    assert normalize_order(clean).status == "unknown"


@pytest.mark.parametrize("value", [7, 7.5, True, None, [], {}])
def test_invalid_identity_and_geojson_type_keep_type(value):
    session = CaptureSession(offset=timedelta(0), provenance="synthetic")
    clean = session.capture(
        "details",
        {
            "order_details": [
                {"order_id": value, "delivery_location": {"coordinates": {"type": value}}}
            ]
        },
    )["order_details"][0]
    assert type(clean["order_id"]) is type(value)
    assert type(clean["delivery_location"]["coordinates"]["type"]) is type(value)


@pytest.mark.parametrize(
    "field",
    ["payment_time", "delivery_eta", "delivery_eta_min", "delivery_eta_max", "delivery_time"],
)
@pytest.mark.parametrize(
    "source,days",
    [("9999-12-30T23:00:00-02:00", 1), ("0001-01-02T01:00:00+02:00", -1)],
)
def test_capture_rejects_shifted_utc_overflow_without_artifact(
    field, source, days, tmp_path, monkeypatch, caplog
):
    from custom_components.wolt_monitor.api import timestamp
    from scripts import wolt_fixtures

    assert timestamp(source) is not None
    monkeypatch.setattr(wolt_fixtures, "FIXTURE_BASE", tmp_path)
    session = CaptureSession(offset=timedelta(days=days), provenance="observed")
    with pytest.raises(ValueError, match="^Invalid capture input$") as caught:
        session.save(
            [
                {
                    "stage": "ready",
                    "at": "2026-01-01T00:00:00+00:00",
                    "responses": {
                        "subscriptions": session.capture(
                            "subscriptions",
                            {
                                "order_details": [{"order_id": "PRIVATE-CANARY", field: source}],
                                "private": "PRIVATE-CANARY",
                            },
                        )
                    },
                }
            ],
            slug="overflow",
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert source not in repr(caught.value) + caplog.text
    assert "PRIVATE-CANARY" not in repr(caught.value) + caplog.text
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "estimate",
    [
        pytest.param("1-" + "9" * 5000, id="huge-number"),
        pytest.param("1-" + "0" * 5000 + "2", id="huge-padding"),
    ],
)
def test_estimate_length_budget_still_fails_closed(estimate, caplog):
    session = CaptureSession(offset=timedelta(0), provenance="synthetic")
    with pytest.raises(ValueError, match="^Invalid capture input$") as caught:
        session.capture("details", {"order_details": [{"client_pre_estimate": estimate}]})
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert estimate not in repr(caught.value) + caplog.text


@pytest.mark.parametrize(
    "source,days",
    [
        ("9999-12-30T23:00:00-02:00", 1),
        ("0001-01-02T01:00:00+02:00", -1),
        ("9999-12-31T23:00:00-02:00", 0),
        ("0001-01-01T01:00:00+02:00", 0),
        ("9999-12-31T23:00:00-02:00", -1),
        ("0001-01-01T01:00:00+02:00", 1),
    ],
)
def test_reference_time_rejects_source_or_shifted_utc_overflow_privately(source, days, caplog):
    session = CaptureSession(offset=timedelta(days=days), provenance="synthetic")
    with pytest.raises(ValueError, match="^Invalid reference time$") as caught:
        session.reference_time(datetime.fromisoformat(source))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert source not in repr(caught.value) + caplog.text


@pytest.mark.parametrize(
    "offset,provenance",
    [
        (timedelta(0), "observed"),
        (timedelta(microseconds=1), "observed"),
        (timedelta(microseconds=1), "synthetic"),
    ],
)
def test_capture_rejects_unsafe_timeshift_configuration(offset, provenance):
    with pytest.raises(ValueError, match="Invalid capture configuration"):
        CaptureSession(offset=offset, provenance=provenance)


def test_native_date_type_precision_and_delivery_time_common_shift():
    from custom_components.wolt_monitor.api import timestamp

    session = CaptureSession(offset=timedelta(milliseconds=123), provenance="observed")
    source = {"$date": 1791374400123}
    clean = session.capture(
        "details", {"order_details": [{"payment_time": source, "delivery_time": source}]}
    )["order_details"][0]
    assert type(clean["payment_time"]["$date"]) is int
    assert clean["payment_time"]["$date"] == 1791374400246
    assert timestamp(clean["delivery_time"]) - timestamp(source) == timedelta(milliseconds=123)
    assert (
        session.reference_time(datetime(2026, 1, 1, tzinfo=UTC))
        == "2026-01-01T00:00:00.123000+00:00"
    )


def test_empty_identity_never_becomes_valid_alias():
    from custom_components.wolt_monitor.api import normalize_order
    from custom_components.wolt_monitor.errors import Failure

    session = CaptureSession(offset=timedelta(0), provenance="synthetic")
    clean = session.capture("details", {"order_details": [{"order_id": ""}]})["order_details"][0]
    assert clean["order_id"] == ""
    with pytest.raises(Failure):
        normalize_order(clean)
