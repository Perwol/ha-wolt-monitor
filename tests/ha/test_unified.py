"""Synthetic structural replies, not raw observed unified fixtures."""

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_data_transport import GetSession
from test_transport import Response

from custom_components.wolt_monitor import transport
from custom_components.wolt_monitor.api import WoltDataAPI
from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.coordinator import WoltCoordinator


class RepeatingSession(GetSession):
    def get(self, url, **kwargs):
        self.response.offset = 0
        return super().get(url, **kwargs)


async def make_dual(hass):
    legacy = RepeatingSession(
        Response(
            body={
                "order_details": [
                    {
                        "order_id": "synthetic",
                        "status": "production",
                        "delivery_method": "homedelivery",
                        "payment_time": "2024-01-12T15:00:00+00:00",
                        "client_pre_estimate": "20-30",
                    }
                ]
            }
        )
    )
    unified = RepeatingSession(
        Response(
            body={
                "result": {
                    "order_view": {
                        "order_pdrn": "4ujWWng.synthetic",
                        "status": "ORDER_COMPLETE",
                        "registration_token": "private-body-canary",
                    }
                }
            }
        )
    )
    api = WoltDataAPI(
        transport.WoltDataClient(legacy),
        unified_client=transport.UnifiedClient(unified, timezone="Europe/Warsaw"),
    )
    auth = AsyncMock()
    auth.refresh.return_value = TokenReply("fake-access", "fake-refresh", 10000)
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
    now = [0.0]
    coordinator = WoltCoordinator(hass, entry, api, auth, clock=lambda: now[0])
    return coordinator, legacy, unified, auth, now


@pytest.fixture
async def dual(hass):
    instances = []

    async def create():
        result = await make_dual(hass)
        instances.append(result[0])
        return result

    yield create
    for coordinator in instances:
        await coordinator.async_shutdown()


async def test_same_order_complete_overrides_legacy_through_http(dual, caplog):
    assert hasattr(transport, "UnifiedClient"), "Supplemental HTTP transport is missing"
    coordinator, legacy, unified, _, _ = await dual()
    await coordinator.async_refresh()
    assert coordinator.data["status"] == "delivered"
    assert coordinator.data["eta"] == 0
    assert coordinator.data["estimated_delivery_time"] is None
    assert coordinator.data["distance"] is None
    assert coordinator.runtime.is_delivered() is True
    assert coordinator.runtime.delivery_in_progress() is False
    assert len(legacy.calls) == len(unified.calls) == 1
    url, options = unified.calls[0]
    assert str(url) == (
        "https://unified-gateway.dashapi.com/order-tracking/v1/unified/marketplace/"
        "4ujWWng.synthetic?client_timezone=Europe/Warsaw&hour_cycle=HOUR_CYCLE_H23"
    )
    assert options["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer fake-access",
        "Pedregal-Brand": "wolt",
        "App-Language": "en",
        "baggage": "ptid=dashprod,platform=web",
        "x-unified-gateway-generated-source": "v1",
    }
    assert options["allow_redirects"] is False
    assert options["timeout"].total == 20
    assert "private-body-canary" not in repr(coordinator.data) + caplog.text


@pytest.mark.parametrize(
    "response,category",
    [
        (Response(404, {"private": "private-body-canary"}), "unexpected"),
        (Response(429, {}, {"Retry-After": "900"}), "rate_limit"),
        (Response(503, {}), "server"),
        (Response(error=TimeoutError("private-body-canary")), "timeout"),
        (Response(error=OSError("private-body-canary")), "connection"),
        (Response(error=ValueError("private-body-canary")), "unexpected"),
        (Response(body="private-body-canary"), "invalid_data"),
        (Response(body={"private": "x" * 1048577}), "invalid_data"),
        (
            Response(
                body={
                    "result": {"order_view": {"order_pdrn": "foreign", "status": "ORDER_COMPLETE"}}
                }
            ),
            "invalid_data",
        ),
        (Response(body={"result": {"order_view": {"status": "ORDER_COMPLETE"}}}), "invalid_data"),
    ],
)
async def test_supplement_failure_never_degrades_healthy_legacy(dual, response, category, caplog):
    coordinator, legacy, unified, _, now = await dual()
    unified.response = response
    for i in range(3):
        response.offset = 0
        now[0] = i * 900
        await coordinator.async_request_refresh()
    assert coordinator.data["status"] == "preparing"
    assert coordinator.runtime.is_delivered() is False
    assert coordinator.runtime.sources["order"].consecutive_errors == 0
    assert coordinator.runtime.sources["courier"].consecutive_errors == 0
    assert coordinator.runtime.sources["unified"].consecutive_errors == 3
    assert coordinator.runtime.sources["unified"].last_error_category == category
    assert len(legacy.calls) == len(unified.calls) == 3
    assert "private-body-canary" not in caplog.text + repr(coordinator.data)


@pytest.mark.parametrize("retry_after,deadline", [("0", 120), ("900", 900)])
async def test_unified_backoff_survives_manual_refresh_and_new_selection(
    dual, retry_after, deadline
):
    coordinator, legacy, unified, _, now = await dual()
    unified.response = Response(429, {}, {"Retry-After": retry_after})
    await coordinator.async_refresh()
    for t in (30, deadline - 1):
        now[0] = t
        legacy.response = Response(
            body={
                "order_details": [
                    {"order_id": "synthetic", "status": "delivered"},
                    {"order_id": "second", "status": "production"},
                ]
            }
        )
        await coordinator.async_request_refresh()
        assert coordinator.data["status"] == "preparing"
        assert len(unified.calls) == 1
        assert coordinator.runtime.sources["unified"].consecutive_errors == 1
        assert coordinator.runtime.sources["order"].consecutive_errors == 0
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.second",
                    "status": "ORDER_COMPLETE",
                }
            }
        }
    )
    now[0] = deadline
    await coordinator.async_request_refresh()
    assert len(unified.calls) == 2
    assert coordinator.data["status"] == "delivered"
    assert coordinator.runtime.sources["unified"].consecutive_errors == 0
    assert coordinator._data_not_before == 0


async def test_selected_cancellation_conflict_completion_wins_without_extending_retention(
    dual, caplog
):
    coordinator, legacy, unified, _, now = await dual()
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_PLACED",
                }
            }
        }
    )
    await coordinator.async_refresh()
    now[0] = 60
    legacy.response = Response(
        body={"order_details": [{"order_id": "synthetic", "status": "rejected"}]}
    )
    # Establish cancelled retention while the supplement is still pending.
    await coordinator.async_request_refresh()
    deadline = coordinator.runtime.retention_deadline
    now[0] = 120
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_COMPLETE",
                }
            }
        }
    )
    await coordinator.async_request_refresh()
    assert coordinator.data["status"] == "delivered"
    assert coordinator.runtime.is_delivered() is True
    assert coordinator.runtime.retention_deadline == deadline == 660
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        "Conflicting terminal evidence: same-order completion overrides cancellation"
    ]
    assert "synthetic" not in caplog.text
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_PLACED",
                }
            }
        }
    )
    legacy.response = Response(
        body={"order_details": [{"order_id": "synthetic", "status": "refunded"}]}
    )
    await coordinator.async_request_refresh()
    assert coordinator.data["status"] == "delivered"
    assert coordinator.runtime.retention_deadline == deadline
    assert len(unified.calls) == 3
    now[0] = deadline
    await coordinator.async_request_refresh()
    assert coordinator.data["status"] == "no_active_order"
    assert len(unified.calls) == 3


async def test_production_factory_enables_completion_with_ha_timezone_and_one_owned_session(hass):
    from custom_components.wolt_monitor.api import create_data_api

    hass.config.time_zone = "Europe/Warsaw"
    adapter = create_data_api(hass)
    try:
        assert adapter.completion_supported is True
        assert adapter._unified_client._timezone == "Europe/Warsaw"
        session = adapter._client._client._session
        assert adapter._unified_client._client._session is session
        assert session.trust_env is False
        assert isinstance(session.cookie_jar, transport.aiohttp.DummyCookieJar)
    finally:
        await adapter.close()
    assert session.closed


async def test_legacy_retry_after_blocks_supplement_inside_same_cycle(dual):
    coordinator, legacy, unified, _, now = await dual()
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_PLACED",
                }
            }
        }
    )
    await coordinator.async_refresh()
    legacy.response = Response(429, {}, {"Retry-After": "900"})
    now[0] = 60
    await coordinator.async_refresh()
    assert len(unified.calls) == 1
    assert coordinator.data["status"] == "preparing"
    assert coordinator.runtime.sources["unified"].consecutive_errors == 0


async def test_expired_selected_cancellation_is_not_polled_after_legacy_failure(dual):
    coordinator, legacy, unified, _, now = await dual()
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_PLACED",
                }
            }
        }
    )
    await coordinator.async_refresh()
    legacy.response = Response(
        body={"order_details": [{"order_id": "synthetic", "status": "rejected"}]}
    )
    await coordinator.async_request_refresh()
    before = len(unified.calls)
    now[0] = coordinator.runtime.retention_deadline
    legacy.response = Response(503, {})
    await coordinator.async_request_refresh()
    assert len(unified.calls) == before
    assert coordinator.data["status"] == "no_active_order"


async def test_failed_unified_401_repair_does_not_count_as_courier_failure(dual):
    coordinator, legacy, unified, auth, _ = await dual()
    legacy.response = Response(
        body={
            "order_details": [
                {
                    "order_id": "synthetic",
                    "status": "ready",
                    "delivery_method": "homedelivery",
                    "is_marketplace_v2": False,
                }
            ]
        }
    )
    unified.response = Response(401, {"error_code": 126})
    await coordinator.async_refresh()
    assert len(unified.calls) == 2
    assert auth.refresh.await_count == 2
    assert coordinator.runtime.sources["unified"].consecutive_errors == 1
    assert coordinator.runtime.sources["order"].consecutive_errors == 0
    assert coordinator.runtime.sources["courier"].consecutive_errors == 0
    assert len(legacy.calls) == 1
    assert coordinator.data["status"] == "ready"
    assert coordinator.runtime.reauth_required is False


@pytest.mark.parametrize(
    "status",
    [
        "ORDER_PLACED",
        "ORDER_CANCELLED",
        "ORDER_DELIVERED",
        "ORDER_REFUNDED",
        "UNKNOWN",
        "order_complete",
    ],
)
async def test_non_complete_order_view_never_changes_forecast_or_status(dual, status):
    coordinator, _, unified, _, _ = await dual()
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": status,
                    "handoff_groups": [{"status": "ORDER_COMPLETE"}],
                }
            }
        }
    )
    await coordinator.async_refresh()
    assert coordinator.data["status"] == "preparing"
    assert coordinator.data["estimated_delivery_time"] == "2024-01-12T15:30:00+00:00"
    assert coordinator.runtime.delivery_in_progress() is False
    assert coordinator.runtime.is_delivered() is False
    assert coordinator.runtime.retention_deadline is None


@pytest.mark.parametrize("status", ["delivered", "rejected"])
async def test_idle_history_never_selects_arbitrary_terminal_order_for_supplement(dual, status):
    coordinator, legacy, unified, _, _ = await dual()
    legacy.response = Response(
        body={"order_details": [{"order_id": "historical", "status": status}]}
    )
    await coordinator.async_refresh()
    assert coordinator.data["status"] == "no_active_order"
    assert len(unified.calls) == 0


async def test_legacy_delivery_still_confirms_during_unified_backoff(dual):
    coordinator, legacy, unified, _, _ = await dual()
    unified.response = Response(429, {}, {"Retry-After": "900"})
    await coordinator.async_refresh()
    legacy.response = Response(
        body={"order_details": [{"order_id": "synthetic", "status": "delivered"}]}
    )
    await coordinator.async_request_refresh()
    assert coordinator.data["status"] == "delivered"
    assert coordinator.runtime.is_delivered() is True
    assert len(unified.calls) == 1


async def test_supplement_401_uses_existing_one_repair_and_fresh_access(dual):
    coordinator, _, unified, auth, _ = await dual()
    initial_get = unified.get
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs["headers"]["Authorization"])
        unified.response = (
            Response(401, {})
            if len(calls) == 1
            else Response(
                body={
                    "result": {
                        "order_view": {
                            "order_pdrn": "4ujWWng.synthetic",
                            "status": "ORDER_COMPLETE",
                        }
                    }
                }
            )
        )
        return initial_get(url, **kwargs)

    unified.get = get
    auth.refresh.side_effect = [
        TokenReply("fake-first", "fake-r1", 1000),
        TokenReply("fake-second", "fake-r2", 1000),
    ]
    await coordinator.async_refresh()
    assert calls == ["Bearer fake-first", "Bearer fake-second"]
    assert auth.refresh.await_count == 2
    assert coordinator.data["status"] == "delivered"
    assert coordinator.runtime.sources["unified"].consecutive_errors == 0


async def test_successful_supplement_does_not_reset_legacy_failure_or_backoff(dual):
    coordinator, legacy, unified, _, now = await dual()
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_PLACED",
                }
            }
        }
    )
    await coordinator.async_refresh()
    legacy.response = Response(503, {})
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_COMPLETE",
                }
            }
        }
    )
    now[0] = 60
    await coordinator.async_refresh()
    assert coordinator.runtime.sources["order"].consecutive_errors == 1
    assert coordinator.runtime.sources["unified"].consecutive_errors == 0
    assert coordinator.scheduler._errors["order"] == 1
    assert coordinator.runtime.is_delivered() is True
    assert coordinator.scheduler.next_deadline == 660


async def test_late_completion_for_replaced_order_cannot_deliver_new_order(dual):
    from custom_components.wolt_monitor.state import OrderSnapshot

    coordinator, _, _, _, _ = await dual()
    original = coordinator.api.async_completion

    async def complete(token, key):
        result = await original(token, key)
        coordinator.runtime.update_order(OrderSnapshot("second", "preparing"), "replacement")
        return result

    coordinator.api.async_completion = complete
    await coordinator.async_refresh()
    assert coordinator.runtime.order.key == "second"
    assert coordinator.runtime.is_delivered() is False
    assert coordinator.data["status"] == "preparing"
    assert coordinator.runtime.retention_deadline is None


async def test_completion_arriving_at_cancelled_retention_expiry_has_no_conflict_warning(
    dual, caplog
):
    coordinator, legacy, unified, _, now = await dual()
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_PLACED",
                }
            }
        }
    )
    await coordinator.async_refresh()
    legacy.response = Response(
        body={"order_details": [{"order_id": "synthetic", "status": "rejected"}]}
    )
    await coordinator.async_request_refresh()
    original = coordinator.api.async_completion
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_COMPLETE",
                }
            }
        }
    )

    async def complete(token, key):
        result = await original(token, key)
        now[0] = coordinator.runtime.retention_deadline
        return result

    coordinator.api.async_completion = complete
    await coordinator.async_request_refresh()
    assert coordinator.data["status"] == "no_active_order"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_malformed_normalized_completion_is_failure_not_success(dual):
    from custom_components.wolt_monitor.state import CompletionResult

    coordinator, _, _, _, _ = await dual()
    coordinator.api.async_completion = AsyncMock(
        return_value=CompletionResult("synthetic", "secret-not-a-bool")
    )
    await coordinator.async_refresh()
    assert coordinator.runtime.sources["unified"].consecutive_errors == 1
    assert coordinator.runtime.sources["unified"].last_error_category == "invalid_data"
    assert coordinator.data["status"] == "preparing"


async def test_production_dual_http_completion_publishes_existing_entities_and_safe_diagnostics(
    hass,
):
    from unittest.mock import patch

    from test_data_api import details
    from test_data_path import QueueSession, token_response

    from custom_components.wolt_monitor.diagnostics import async_get_config_entry_diagnostics

    order = details("private-order-canary", delivery_method="homedelivery", is_marketplace_v2=False)
    data = QueueSession(
        [
            Response(body={"order_details": [order]}),
            Response(
                body={
                    "result": {
                        "order_view": {
                            "order_pdrn": "4ujWWng.private-order-canary",
                            "status": "ORDER_COMPLETE",
                            "registration_token": "private-registration-canary",
                            "edit_token": "private-edit-canary",
                            "share_token": "private-share-canary",
                        }
                    }
                }
            ),
            Response(body={"order_details": [order]}),
        ]
    )
    auth = QueueSession([token_response()])
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wolt_monitor.transport.aiohttp.ClientSession", side_effect=[data, auth]
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            states = {
                s.entity_id: s.state
                for s in hass.states.async_all()
                if s.entity_id.startswith(
                    (
                        "sensor.wolt_monitor_",
                        "binary_sensor.wolt_monitor_",
                        "device_tracker.wolt_monitor_",
                    )
                )
            }
            assert states == {
                "device_tracker.wolt_monitor_latest_order_courier_position": "unavailable",
                "sensor.wolt_monitor_latest_order_status": "delivered",
                "sensor.wolt_monitor_latest_order_restaurant": "synthetic venue",
                "sensor.wolt_monitor_latest_order_delivery_time": "unavailable",
                "sensor.wolt_monitor_latest_order_eta": "0",
                "sensor.wolt_monitor_latest_order_courier_distance": "unavailable",
                "binary_sensor.wolt_monitor_latest_order_delivery_in_progress": "off",
                "binary_sensor.wolt_monitor_latest_order_delivered": "on",
            }
            deadline = entry.runtime_data.runtime.retention_deadline
            await entry.runtime_data.async_request_refresh()
            assert len(data.calls) == 3
            assert entry.runtime_data.runtime.retention_deadline == deadline
            report = await async_get_config_entry_diagnostics(hass, entry)
            assert "unified" in report["runtime"]
            assert "canary" not in repr(report)
            assert set(report["runtime"]["unified"]) == {
                "last_successful_update",
                "consecutive_errors",
                "last_error_category",
                "last_http_status",
            }
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
    assert data.close_count == auth.close_count == 1


async def test_courier_only_cycle_does_not_poll_unified(dual):
    from custom_components.wolt_monitor.scheduler import Options

    coordinator, legacy, unified, _, now = await dual()
    coordinator.scheduler.apply_options(Options(active_seconds=120, courier_seconds=30))
    order = {
        "order_id": "synthetic",
        "status": "ready",
        "delivery_method": "homedelivery",
        "is_marketplace_v2": False,
    }
    original = legacy.get

    def get(url, **kwargs):
        legacy.response = (
            Response(body={"order_details": order, "drivers": []})
            if "purchase_tracking" in str(url)
            else Response(body={"order_details": [order]})
        )
        return original(url, **kwargs)

    legacy.get = get
    unified.response = Response(
        body={
            "result": {"order_view": {"order_pdrn": "4ujWWng.synthetic", "status": "ORDER_PLACED"}}
        }
    )
    await coordinator.async_refresh()
    now[0] = 30
    await coordinator.async_refresh()
    assert len(unified.calls) == 1
    assert len(legacy.calls) == 3
    assert coordinator.runtime.sources["courier"].consecutive_errors == 0
    assert coordinator.data["status"] == "ready"


@pytest.mark.parametrize("synthetic_legacy_lag", [False, True])
async def test_two_order_nine_stage_replay_with_eight_reconstructed_unified_summaries(
    dual, synthetic_legacy_lag
):
    """Unified bodies/PDRNs and stage-08 404 are synthetic, not observed raw replies.

    The lag variant deliberately changes ONLY the final legacy status to ready.
    Observed fixture files are read-only and their same-order aliases stay private.
    """
    import copy
    import json
    from datetime import datetime
    from pathlib import Path

    from scripts.wolt_fixtures import replay

    coordinator, legacy, unified, _, now = await dual()
    folder = Path(__file__).parents[1] / "fixtures" / "wolt"
    captures = [
        json.loads(f.read_text()) for f in sorted(folder.glob("natural-order-two-stage-*.json"))
    ]
    assert len(captures) == 9
    stages = [c["stages"][0] for c in captures]
    reference = await replay({"metadata": captures[0]["metadata"], "stages": stages})
    start = datetime.fromisoformat(stages[0]["at"])
    original_get = legacy.get
    responses = {}

    def get(url, **kwargs):
        family = "tracking" if "purchase_tracking" in str(url) else "subscriptions"
        legacy.response = Response(body=responses[family])
        return original_get(url, **kwargs)

    legacy.get = get
    for index, stage in enumerate(stages):
        responses = copy.deepcopy(stage["responses"])
        if synthetic_legacy_lag and index == 8:
            responses["subscriptions"]["order_details"][0]["status"] = "ready"
        wall = datetime.fromisoformat(stage["at"])
        coordinator.runtime.wall_clock = lambda wall=wall: wall
        now[0] = (wall - start).total_seconds()
        summary_file = folder / f"natural-order-two-unified-{index + 1:02}.json"
        if summary_file.exists():
            summary = json.loads(summary_file.read_text())
            assert summary["metadata"]["schema"] == "unified-technical-summary-v1"
            assert summary["response"]["same_order"] is True
            unified.response = Response(
                body={
                    "result": {
                        "order_view": {
                            "order_pdrn": "4ujWWng.order-1",
                            "status": summary["response"]["status"],
                        }
                    }
                }
            )
        else:
            assert index == 7
            unified.response = Response(404, {})  # Synthetic gap, not an observed failure.
        await coordinator.async_request_refresh()
        actual = {
            **coordinator.data,
            "delivery_in_progress": coordinator.runtime.delivery_in_progress(),
            "is_delivered": coordinator.runtime.is_delivered(),
        }
        assert actual == reference[index]
    assert len(unified.calls) == (9 if synthetic_legacy_lag else 8)
    assert coordinator.data["status"] == "delivered"
    deadline = coordinator.runtime.retention_deadline
    now[0] = deadline
    before = len(unified.calls)
    await coordinator.async_request_refresh()  # Synthetic expiry, not an observed stage.
    assert coordinator.data["status"] == "no_active_order"
    assert len(unified.calls) == before


@pytest.mark.parametrize("invalid_refresh", [False, True])
async def test_legacy_auth_repair_failure_suppresses_unified_without_counting_failure(
    dual, invalid_refresh
):
    from custom_components.wolt_monitor.errors import Failure

    (
        coordinator,
        legacy,
        unified,
        auth,
        _,
    ) = await dual()
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.synthetic",
                    "status": "ORDER_PLACED",
                }
            }
        }
    )
    await coordinator.async_refresh()
    legacy.response = Response(401, {})
    auth.refresh.side_effect = (
        Failure("authentication", 401, invalid_refresh=True)
        if invalid_refresh
        else Failure("connection")
    )
    await coordinator.async_request_refresh()
    assert len(unified.calls) == 1
    assert coordinator.runtime.sources["unified"].consecutive_errors == 0
    assert coordinator.runtime.sources["unified"].last_error_category is None
    assert auth.refresh.await_count == 2


async def test_zero_retention_completion_immediately_returns_to_idle_schedule(dual):
    coordinator, _, unified, _, _ = await dual()
    coordinator.runtime.set_retention(0)
    await coordinator.async_refresh()
    assert coordinator.data["status"] == "no_active_order"
    assert coordinator.runtime.order is None
    assert "synthetic" in coordinator.runtime.excluded_keys
    assert coordinator.scheduler.courier_eligible is False
    assert coordinator.scheduler.next_deadline == 300
    await coordinator.async_request_refresh()
    assert len(unified.calls) == 1


async def test_new_active_order_replaces_confirmed_order_without_inheriting_delivery(dual):
    coordinator, legacy, unified, _, now = await dual()
    await coordinator.async_refresh()
    legacy.response = Response(
        body={
            "order_details": [
                {"order_id": "synthetic", "status": "ready"},
                {"order_id": "second", "status": "production"},
            ]
        }
    )
    unified.response = Response(
        body={
            "result": {
                "order_view": {
                    "order_pdrn": "4ujWWng.second",
                    "status": "ORDER_PLACED",
                }
            }
        }
    )
    await coordinator.async_request_refresh()
    assert coordinator.runtime.order.key == "second"
    assert coordinator.data["status"] == "preparing"
    assert coordinator.runtime.is_delivered() is False
    assert coordinator.runtime.retention_deadline is None
    assert "synthetic" in coordinator.runtime.excluded_keys
    assert len(unified.calls) == 2
    legacy.response = Response(
        body={
            "order_details": [
                {"order_id": "synthetic", "status": "ready"},
                {"order_id": "second", "status": "delivered"},
            ]
        }
    )
    await coordinator.async_request_refresh()
    assert coordinator.runtime.order.key == "second"
    now[0] = coordinator.runtime.retention_deadline
    await coordinator.async_request_refresh()
    assert coordinator.data["status"] == "no_active_order"
    assert len(unified.calls) == 2


async def test_stop_during_unified_auth_repair_suppresses_retry_and_waits_for_close(dual):
    import asyncio

    coordinator, _, unified, auth, _ = await dual()
    unified.response = Response(401, {})
    entered, ready = asyncio.Event(), asyncio.Event()
    calls = []

    async def refresh(_):
        calls.append(1)
        if len(calls) == 2:
            entered.set()
            await ready.wait()
        return TokenReply("fake-access", "fake-refresh", 1000)

    auth.refresh.side_effect = refresh
    waiter = asyncio.create_task(coordinator.async_request_refresh())
    await entered.wait()
    shutdown = asyncio.create_task(coordinator.async_shutdown())
    await asyncio.sleep(0)
    try:
        assert coordinator._stopped
        auth.close.assert_not_awaited()
    finally:
        ready.set()
        await waiter
        await shutdown
    assert len(unified.calls) == 1
    assert auth.refresh.await_count == 2
    assert coordinator.runtime.sources["unified"].consecutive_errors == 0
    assert coordinator._poll_cancel is None
    auth.close.assert_awaited_once()
