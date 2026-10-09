from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_initial_form_has_masked_token_and_all_settings(hass):
    result = await hass.config_entries.flow.async_init("wolt_monitor", context={"source": "user"})
    assert result["type"] == FlowResultType.FORM
    schema = result["data_schema"].schema
    assert [str(key) for key in schema] == [
        "refresh_token",
        "no_active_order_polling_interval_seconds",
        "active_order_polling_interval_seconds",
        "courier_polling_interval_seconds",
        "completed_order_retention_minutes",
    ]
    idle_key = next(key for key in schema if str(key) == "no_active_order_polling_interval_seconds")
    assert idle_key.default() == 2
    assert schema[idle_key].config == {
        "min": 1,
        "max": 10,
        "step": 1,
        "unit_of_measurement": "min",
        "mode": "box",
    }
    assert next(iter(schema.values())).config["type"] == "password"


async def test_create_stores_reply_refresh_not_input(hass):
    with (
        patch(
            "custom_components.wolt_monitor.config_flow.validate_token",
            AsyncMock(return_value="fake-reply"),
        ),
        patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(
            "wolt_monitor", context={"source": "user"}, data={"refresh_token": "fake-input"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {"refresh_token": "fake-reply"}
    assert result["title"] == "Wolt Monitor"
    assert result["options"] == {
        "no_active_order_polling_interval_seconds": 120,
        "active_order_polling_interval_seconds": 30,
        "courier_polling_interval_seconds": 30,
        "completed_order_retention_minutes": 10,
    }


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("invalid", "invalid_auth"),
        ("connection", "cannot_connect"),
        ("invalid_data", "cannot_connect"),
        ("unexpected", "unknown"),
    ],
)
async def test_auth_error_is_generic_and_token_is_not_echoed(hass, failure, expected):
    from custom_components.wolt_monitor.errors import Failure

    error = (
        Failure("authentication", 401, invalid_refresh=True)
        if failure == "invalid"
        else Failure(failure)
    )
    with patch(
        "custom_components.wolt_monitor.config_flow.validate_token", AsyncMock(side_effect=error)
    ):
        result = await hass.config_entries.flow.async_init(
            "wolt_monitor", context={"source": "user"}, data={"refresh_token": "fake-private"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert "fake-private" not in str(result)


async def test_reauth_updates_same_entry_and_reloads_once(hass):
    entry = MockConfigEntry(
        domain="wolt_monitor",
        title="HA Wolt Monitor",
        data={"refresh_token": "fake-old"},
        options={"completed_order_retention_minutes": 20},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.wolt_monitor.config_flow.validate_token",
            AsyncMock(return_value="fake-new"),
        ),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload,
    ):
        result = await hass.config_entries.flow.async_init(
            "wolt_monitor",
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"refresh_token": "fake-input"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data == {"refresh_token": "fake-new"}
    assert entry.options == {"completed_order_retention_minutes": 20}
    reload.assert_awaited_once_with(entry.entry_id)


async def test_options_are_atomic_and_enforce_step(hass):
    from custom_components.wolt_monitor.const import options_dict
    from custom_components.wolt_monitor.scheduler import Options

    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    data = options_dict(Options())
    ui_data = {**data, "no_active_order_polling_interval_seconds": 2, "refresh_token": ""}
    schema = result["data_schema"].schema
    assert {str(key) for key in schema} == {"refresh_token", *data}
    assert (
        next(key for key in schema if str(key) == "refresh_token").__class__.__name__ == "Optional"
    )
    invalid = {**ui_data, "active_order_polling_interval_seconds": 31}
    result = await hass.config_entries.options.async_configure(result["flow_id"], invalid)
    assert result["type"] == FlowResultType.FORM
    assert entry.options == {}
    result = await hass.config_entries.options.async_configure(result["flow_id"], ui_data)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options == data
    assert entry.data == {"refresh_token": "fake"}


async def test_validation_uses_real_manager_and_always_closes_client():
    from custom_components.wolt_monitor.auth import TokenReply
    from custom_components.wolt_monitor.config_flow import validate_token

    client = AsyncMock()
    client.refresh.return_value = TokenReply("fake-a", "fake-response-refresh", 100)
    with patch("custom_components.wolt_monitor.config_flow.ClassicAuthClient", return_value=client):
        assert await validate_token("fake-input") == "fake-response-refresh"
    client.close.assert_awaited_once()


async def test_unknown_exception_and_failed_reauth_never_replace_token(hass):
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-existing"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "wolt_monitor", context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
    )
    with patch(
        "custom_components.wolt_monitor.config_flow.validate_token",
        AsyncMock(side_effect=ValueError("fake-private")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"refresh_token": "fake-input"}
        )
    assert result["errors"] == {"base": "unknown"}
    assert entry.data == {"refresh_token": "fake-existing"}
    assert "fake-private" not in str(result)


async def test_direct_second_user_step_rejects_existing_entry(hass):
    from custom_components.wolt_monitor.config_flow import WoltConfigFlow

    entry = MockConfigEntry(domain="wolt_monitor", data={})
    entry.add_to_hass(hass)
    flow = WoltConfigFlow()
    flow.hass = hass
    assert (await flow.async_step_user())["reason"] == "single_instance_allowed"


async def test_second_entry_is_rejected_without_auth_request(hass):
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init("wolt_monitor", context={"source": "user"})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_polling_defaults_do_not_override_saved_options(hass):
    from custom_components.wolt_monitor.const import options_from

    saved = {
        "no_active_order_polling_interval_seconds": 300,
        "active_order_polling_interval_seconds": 90,
        "courier_polling_interval_seconds": 120,
    }
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake"}, options=saved)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    defaults = {str(key): key.default() for key in schema if str(key) != "refresh_token"}
    assert defaults == {
        **saved,
        "no_active_order_polling_interval_seconds": 5,
        "completed_order_retention_minutes": 10,
    }
    assert entry.options == saved
    partial = options_from({"no_active_order_polling_interval_seconds": 300})
    assert (partial.idle_seconds, partial.active_seconds, partial.courier_seconds) == (300, 30, 30)
    # Bounds, steps and units are unchanged independently of the defaults.
    expected = [(1, 10, 1, "min"), (30, 300, 30, "s"), (30, 300, 30, "s"), (0, 60, 1, "min")]
    for selector, values in zip(list(schema.values())[1:], expected, strict=True):
        assert (
            tuple(selector.config[k] for k in ("min", "max", "step", "unit_of_measurement"))
            == values
        )


async def test_idle_one_minute_is_stored_as_sixty_seconds(hass):
    from custom_components.wolt_monitor.config_flow import IDLE_KEY, form_options, settings_schema
    from custom_components.wolt_monitor.const import options_from

    schema = settings_schema().schema
    key = next(k for k in schema if str(k) == IDLE_KEY)
    assert schema[key].config["min"] == 1
    assert key.default() == 2
    assert schema[key].config["max"] == 10
    assert form_options({IDLE_KEY: 1})[IDLE_KEY] == 60
    assert options_from({IDLE_KEY: 60}).idle_seconds == 60
    for invalid in (0, 0.5, 0.99, 10.5, 11):
        with pytest.raises(ValueError):
            form_options({IDLE_KEY: invalid})
    for invalid in (0, 30, 59, 601, 660):
        with pytest.raises(ValueError):
            options_from({IDLE_KEY: invalid})
