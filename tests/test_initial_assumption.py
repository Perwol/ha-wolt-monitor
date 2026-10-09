"""Synthetic HTTP regressions for the approved, explicitly unverified assumption."""

from datetime import UTC, datetime

import pytest
from test_data_api import details
from test_data_transport import GetSession
from test_transport import Response

from custom_components.wolt_monitor.api import WoltDataAPI
from custom_components.wolt_monitor.transport import WoltDataClient


async def snapshot(fields, *, resource="subscriptions"):
    order = {
        **details(
            status="received",
            delivery_method="homedelivery",
            payment_time="2026-10-07T12:00:00Z",
            client_pre_estimate="46-56",
        ),
        **fields,
    }
    adapter = WoltDataAPI(WoltDataClient(GetSession(Response(body={"order_details": [order]}))))
    if resource == "details":
        return (await adapter.async_order_details("synthetic", "synthetic")).snapshot
    result = await adapter.async_order("synthetic")
    assert result.for_order(result.orders[0]).snapshot is result.orders[0]
    return result.orders[0]


@pytest.mark.parametrize("resource", ["subscriptions", "details"])
@pytest.mark.parametrize("status", ["received", "acknowledged", "fetched", "production", "ready"])
@pytest.mark.parametrize("initial", ["1-2", "46-56", "0-0"])
async def test_ordinary_home_delivery_assumes_minutes_not_numeric_magnitude(
    resource, status, initial
):
    upper = int(initial.split("-")[1])
    result = await snapshot({"status": status, "client_pre_estimate": initial}, resource=resource)
    assert result.estimated_delivery_time == datetime(2026, 10, 7, 12, upper, tzinfo=UTC)


@pytest.mark.parametrize(
    "fields",
    [
        {"preorder_status": None, "time_slot_order": None},
        {"is_preorder": False, "preorder": False, "is_scheduled": False},
        {"client_pre_estimate_unit": "minutes"},
        {"client_pre_estimate_unit": "MINUTES"},
        {"is_marketplace_v2": True},
        {"self_delivery": {"is_tracking_enabled": False}},
    ],
)
async def test_explicit_no_schedule_and_tracking_metadata_do_not_change_assumption(fields):
    assert (await snapshot(fields)).estimated_delivery_time == datetime(
        2026, 10, 7, 12, 56, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"delivery_method": "takeaway"},
        {"delivery_method": None},
        {"delivery_method": []},
        {"delivery_method": "unknown"},
        {"status": "unknown"},
        {"status": "delivered"},
        {"status": "rejected"},
        {"status": "preorder-received"},
        {"status": "preorder-confirmed"},
        {"payment_time": None},
        {"payment_time": "invalid"},
        {"client_pre_estimate": None},
        {"client_pre_estimate": True},
        {"client_pre_estimate": "56-46"},
        {"client_pre_estimate": "-1-56"},
        {"client_pre_estimate": "46 - 56"},
        {"client_pre_estimate": "1-" + "9" * 100},
        {"client_pre_estimate": "9" * 5000 + "-" + "9" * 5000},
        {"payment_time": {"$date": 10**1000}},
        {"delivery_eta_min": "2026-10-07T13:00:00Z", "delivery_eta_max": "2026-10-07T12:00:00Z"},
    ],
)
async def test_ineligible_or_invalid_fallback_is_unavailable(fields):
    assert (await snapshot(fields)).estimated_delivery_time is None


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"delivery_eta_min": "2026-10-07T13:00:00Z"}, 13),
        ({"delivery_eta_max": "2026-10-07T13:00:00Z"}, 13),
        ({"delivery_eta": "2026-10-07T13:00:00Z", "preorder_status": "confirmed"}, 13),
        (
            {
                "delivery_eta": "2026-10-07T13:00:00Z",
                "time_slot_order": {"type": "DELIVERY_WITHIN_TIME_RANGE"},
            },
            13,
        ),
        ({"delivery_eta": "invalid", "delivery_time": "2026-10-07T13:00:00Z"}, 12),
    ],
)
async def test_dynamic_forecast_still_wins_and_actual_delivery_time_is_not_forecast(
    fields, expected
):
    result = await snapshot(fields)
    assert result.estimated_delivery_time == datetime(
        2026, 10, 7, expected, 56 if expected == 12 else 0, tzinfo=UTC
    )
