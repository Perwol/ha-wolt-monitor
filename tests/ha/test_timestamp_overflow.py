"""UTC-overflow dates must not reach real HA timestamp state publishing."""

import logging
from datetime import UTC, datetime

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.api import normalize_order, timestamp
from custom_components.wolt_monitor.sensor import DESCRIPTIONS, WoltSensor
from custom_components.wolt_monitor.state import Runtime

BAD_DATES = [
    "0001-01-01T00:00:00+14:00",
    "9999-12-31T23:59:59-14:00",
    "0001-01-01T00:00:00+00:01",
    "9999-12-31T23:59:59-00:01",
]
POINT = "2026-10-07T12:03:00+02:00"
LOWER = "2026-10-07T12:01:00+02:00"
UPPER = "2026-10-07T12:02:00+02:00"
PAYMENT = "2026-10-07T12:00:00+02:00"


@pytest.mark.parametrize("bad", BAD_DATES)
def test_timestamp_rejects_utc_overflow(bad):
    assert timestamp(bad) is None


@pytest.mark.parametrize("bad", BAD_DATES)
@pytest.mark.parametrize(
    ("fields", "verified", "expected"),
    [
        ({"delivery_eta_max": "BAD"}, False, None),
        ({"delivery_eta_min": "BAD"}, False, None),
        ({"delivery_eta": "BAD"}, False, None),
        ({"delivery_eta_max": "BAD", "delivery_eta": POINT}, False, POINT),
        (
            {"delivery_eta_max": "BAD", "delivery_eta_min": LOWER, "delivery_eta": POINT},
            False,
            LOWER,
        ),
        (
            {"delivery_eta_min": "BAD", "delivery_eta_max": UPPER, "delivery_eta": POINT},
            False,
            UPPER,
        ),
        (
            {"delivery_eta": "BAD", "payment_time": PAYMENT, "client_pre_estimate": "1-2"},
            True,
            UPPER,
        ),
        (
            {"delivery_eta": "BAD", "payment_time": PAYMENT, "client_pre_estimate": "1-2"},
            False,
            None,
        ),
        ({"payment_time": "BAD", "client_pre_estimate": "1-2"}, True, None),
    ],
)
async def test_invalid_dates_publish_with_existing_fallback_priority(
    hass, bad, fields, verified, expected
):
    fields = {key: bad if value == "BAD" else value for key, value in fields.items()}
    await assert_published_forecast(hass, fields, verified, expected)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"delivery_eta": POINT}, POINT),
        ({"delivery_eta": "0001-01-01T14:00:00+14:00"}, "0001-01-01T14:00:00+14:00"),
        ({"delivery_eta": "9999-12-31T09:59:59-14:00"}, "9999-12-31T09:59:59-14:00"),
        ({"delivery_eta_min": LOWER, "delivery_eta_max": UPPER, "delivery_eta": POINT}, UPPER),
        ({"delivery_eta_min": UPPER, "delivery_eta_max": LOWER, "delivery_eta": POINT}, None),
    ],
)
async def test_valid_dates_preserve_offsets_and_reversed_bounds(hass, fields, expected):
    for value in fields.values():
        assert timestamp(value) == datetime.fromisoformat(value)
        assert timestamp(value).isoformat() == value
    await assert_published_forecast(hass, fields, False, expected)


async def assert_published_forecast(hass, fields, verified, expected):
    order = normalize_order(
        {"order_id": "synthetic", "status": "production", **fields}, minutes_verified=verified
    )
    runtime = Runtime(clock=lambda: 0)
    runtime.wall_clock = lambda: datetime(2026, 10, 7, 10, tzinfo=UTC)
    runtime.update_order(order, "synthetic")
    entry = MockConfigEntry(domain="wolt_monitor")
    coordinator = DataUpdateCoordinator(
        hass, logging.getLogger(__name__), name="timestamp test", config_entry=entry
    )
    coordinator.entry = entry
    coordinator.entities = {}
    coordinator.data = runtime.values()
    description = next(desc for desc in DESCRIPTIONS if desc.key == "latest_order_delivery_time")
    sensor = WoltSensor(coordinator, description)
    assert await async_setup_component(hass, "sensor", {})
    await hass.data["sensor"].async_add_entities([sensor])
    await hass.async_block_till_done()
    # SensorEntity.state performs HA's real UTC conversion, not just native_value parsing.
    state = sensor.state
    if expected is None:
        assert order.estimated_delivery_time is None
        assert state is None
        assert not sensor.available
    else:
        assert order.estimated_delivery_time.isoformat() == expected
        assert state == datetime.fromisoformat(expected).astimezone(UTC).isoformat()
        assert sensor.available
    sensor.async_write_ha_state()
    assert hass.states.get(sensor.entity_id).state == (state if expected else "unavailable")
