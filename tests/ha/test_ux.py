import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolt_monitor.const import options_dict
from custom_components.wolt_monitor.scheduler import Options


@pytest.mark.parametrize("options", [False, True])
async def test_forms_supply_documentation_placeholders_on_initial_and_error(hass, options):
    if options:
        entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-old"})
        entry.add_to_hass(hass)
        manager = hass.config_entries.options
        result = await manager.async_init(entry.entry_id)
    else:
        manager = hass.config_entries.flow
        result = await manager.async_init("wolt_monitor", context={"source": "user"})
    expected = {
        "configuration_url": "https://github.com/Perwol/ha-wolt-monitor#configuration-fields",
        "token_url": "https://github.com/Perwol/ha-wolt-monitor#refresh-token",
    }
    assert result["description_placeholders"] == expected
    # Selector accepts an in-range decimal; integration requires whole minutes.
    values = {**options_dict(Options()), "no_active_order_polling_interval_seconds": 1.5}
    if not options:
        values["refresh_token"] = "fake-input"
    result = await manager.async_configure(result["flow_id"], values)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_options"}
    assert result["description_placeholders"] == expected


async def test_options_replace_token_only_after_validation_and_reload(hass):
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-old"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    values = {
        **options_dict(Options()),
        "no_active_order_polling_interval_seconds": 7,
        "refresh_token": "fake-input",
    }
    with (
        patch(
            "custom_components.wolt_monitor.config_flow.validate_token",
            AsyncMock(return_value="fake-rotated"),
        ) as validate,
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload,
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], values)
        await hass.async_block_till_done()
    validate.assert_awaited_once_with("fake-input")
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data == {"refresh_token": "fake-rotated"}
    assert entry.options["no_active_order_polling_interval_seconds"] == 420
    assert "refresh_token" not in entry.options
    reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize(
    "language,on,off",
    [("en", "Delivered", "Not delivered"), ("pl", "Dostarczono", "Nie dostarczono")],
)
async def test_binary_state_translations_loaded_by_ha(hass, language, on, off):
    translated = await async_get_translations(hass, language, "entity", {"wolt_monitor"})
    root = "component.wolt_monitor.entity.binary_sensor.latest_order_delivered.state."
    assert translated[root + "on"] == on
    assert translated[root + "off"] == off


@pytest.fixture
def readme_anchors():
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    headings = re.findall(r"^#{1,6} (.+)$", readme, re.MULTILINE)
    return {heading.lower().replace(" ", "-") for heading in headings}


@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("category,step", [("config", "user"), ("options", "init")])
async def test_every_form_field_has_compact_help(hass, language, category, step, readme_anchors):
    translated = await async_get_translations(hass, language, category, {"wolt_monitor"})
    root = f"component.wolt_monitor.{category}.step.{step}."
    for key in ["refresh_token", *options_dict(Options())]:
        assert translated[root + "data." + key]
        help_text = translated[root + "data_description." + key]
        assert help_text and "\n" not in help_text
    minutes, seconds = ("minuty", "sekundy") if language == "pl" else ("minutes", "seconds")
    for key, unit in [
        ("no_active_order_polling_interval_seconds", minutes),
        ("active_order_polling_interval_seconds", seconds),
        ("courier_polling_interval_seconds", seconds),
        ("completed_order_retention_minutes", minutes),
    ]:
        help_text = translated[root + "data_description." + key]
        assert f"({unit})" in help_text
        assert f"({seconds if unit == minutes else minutes})" not in help_text
    active_help = translated[root + "data_description.active_order_polling_interval_seconds"]
    assert active_help == (
        "Jak często odświeżać dane bieżącego zamówienia (sekundy)."
        if language == "pl"
        else "How often to refresh the current order data (seconds)."
    )
    token_help = translated[root + "data_description.refresh_token"]
    if category == "options":
        assert (
            "Opcjonalne. Pozostaw puste, aby zachować dotychczasowy refresh token."
            if language == "pl"
            else "Optional. Leave blank to keep the current refresh token."
        ) in token_help
    else:
        assert "Optional" not in token_help and "Opcjonalne" not in token_help
    base = "https://github.com/Perwol/ha-wolt-monitor"
    placeholders = {
        "configuration_url": f"{base}#configuration-fields",
        "token_url": f"{base}#refresh-token",
    }
    for text, anchor in [
        (translated[root + "description"], "configuration-fields"),
        (token_help, "refresh-token"),
    ]:
        assert "https://" not in text
        text = text.format(**placeholders)
        links = re.findall(r"\[([^\]]+)\]\((https://[^)]+)\)", text)
        assert any(label and url == f"{base}#{anchor}" for label, url in links)
        assert anchor in readme_anchors
    unpublished = "not published yet" if language == "en" else "nie jest jeszcze opublikowana"
    assert unpublished in translated[root + "description"]


@pytest.mark.parametrize(
    "language,names",
    [
        (
            "en",
            {
                "latest_order_status": "Latest Order Status",
                "latest_order_restaurant": "Latest Order Restaurant",
                "latest_order_delivery_time": "Latest Order Estimated Delivery Time",
                "latest_order_eta": "Latest Order ETA",
                "latest_order_courier_distance": "Latest Order Courier Distance",
                "latest_order_courier_position": "Latest Order Courier Position",
                "latest_order_delivery_in_progress": "Latest Order Delivery In Progress",
                "latest_order_delivered": "Latest Order Delivered",
            },
        ),
        (
            "pl",
            {
                "latest_order_status": "Ostatnie Zamówienie Status",
                "latest_order_restaurant": "Ostatnie Zamówienie Restauracja",
                "latest_order_delivery_time": "Ostatnie Zamówienie Szacowany Czas Dostawy",
                "latest_order_eta": "Ostatnie Zamówienie ETA",
                "latest_order_courier_distance": "Ostatnie Zamówienie Odległość Kuriera",
                "latest_order_courier_position": "Ostatnie Zamówienie Pozycja Kuriera",
                "latest_order_delivery_in_progress": "Ostatnie Zamówienie Dostawa W Toku",
                "latest_order_delivered": "Ostatnie Zamówienie Doręczone",
            },
        ),
    ],
)
async def test_all_eight_translated_entity_names_have_latest_order_prefix(hass, language, names):
    from custom_components.wolt_monitor.api import OrderResult
    from custom_components.wolt_monitor.auth import TokenReply

    hass.config.language = language
    translated = await async_get_translations(hass, language, "entity", {"wolt_monitor"})
    assert len([key for key in translated if key.endswith(".name")]) == 8
    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(None, courier_supported=True)
    client.refresh.return_value = TokenReply("fake-access", "fake-refresh", 3600)
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch("custom_components.wolt_monitor.ClassicAuthClient", return_value=client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    try:
        assert set(entry.runtime_data.entities) == set(names)
        from homeassistant.helpers import entity_registry

        registry = entity_registry.async_get(hass)
        registered = entity_registry.async_entries_for_config_entry(registry, entry.entry_id)
        assert {item.entity_id for item in registered} == {
            entity.entity_id for entity in entry.runtime_data.entities.values()
        }
        assert len(registered) == 8
        assert hass.states.get("binary_sensor.wolt_monitor_delivery_in_progress") is None
        assert hass.states.get("binary_sensor.wolt_monitor_latest_order_is_delivered") is None
        for key, name in names.items():
            entity = entry.runtime_data.entities[key]
            platform = entity.entity_id.split(".")[0]
            assert translated[f"component.wolt_monitor.entity.{platform}.{key}.name"] == name
            assert all(word[0].isupper() for word in name.split())
            assert entity.has_entity_name
            assert entity.name == name
            assert hass.states.get(entity.entity_id).attributes["friendly_name"] == (
                f"Wolt Monitor {name}"
            )
            assert entity.unique_id == f"{entry.entry_id}_{key}"
            assert entity.entity_id == f"{platform}.wolt_monitor_{key}"
            assert entity.entity_description.translation_key == key
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("invalid_auth", "invalid_auth"),
        ("connection", "cannot_connect"),
        ("unexpected", "unknown"),
        ("exception", "unknown"),
    ],
)
async def test_replacement_failure_keeps_both_token_and_options(hass, failure, expected):
    from custom_components.wolt_monitor.errors import Failure

    entry = MockConfigEntry(
        domain="wolt_monitor", data={"refresh_token": "fake-old"}, options=options_dict(Options())
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    error = (
        Failure("authentication", 401, invalid_refresh=True)
        if failure == "invalid_auth"
        else ValueError("fake-private")
        if failure == "exception"
        else Failure(failure)
    )
    with patch(
        "custom_components.wolt_monitor.config_flow.validate_token", AsyncMock(side_effect=error)
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                **entry.options,
                "no_active_order_polling_interval_seconds": 7,
                "refresh_token": "fake-private",
            },
        )
    assert result["errors"] == {"base": expected}
    assert entry.data == {"refresh_token": "fake-old"}
    assert entry.options == options_dict(Options())
    assert "fake-private" not in str(result)


@pytest.mark.parametrize("token", ["", None])
async def test_blank_or_omitted_token_never_authenticates_or_reloads(hass, token):
    entry = MockConfigEntry(
        domain="wolt_monitor",
        data={"refresh_token": "fake-old"},
        options={"no_active_order_polling_interval_seconds": 540},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == "no_active_order_polling_interval_seconds")
    assert key.default() == 9
    values = {**options_dict(Options()), "no_active_order_polling_interval_seconds": 9}
    if token is not None:
        values["refresh_token"] = token
    with (
        patch("custom_components.wolt_monitor.config_flow.validate_token", AsyncMock()) as auth,
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload,
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], values)
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data == {"refresh_token": "fake-old"}
    assert entry.options["no_active_order_polling_interval_seconds"] == 540
    auth.assert_not_awaited()
    reload.assert_not_awaited()


@pytest.mark.parametrize("minutes", [1, 2, 10])
async def test_initial_settings_store_seconds_and_token_separately(hass, minutes):
    result = await hass.config_entries.flow.async_init("wolt_monitor", context={"source": "user"})
    values = {
        **options_dict(Options()),
        "no_active_order_polling_interval_seconds": minutes,
        "active_order_polling_interval_seconds": 90,
        "refresh_token": "fake-input",
    }
    with (
        patch(
            "custom_components.wolt_monitor.config_flow.validate_token",
            AsyncMock(return_value="fake-new"),
        ),
        patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"]["no_active_order_polling_interval_seconds"] == minutes * 60
    assert result["options"]["active_order_polling_interval_seconds"] == 90
    assert result["data"] == {"refresh_token": "fake-new"}


@pytest.mark.parametrize("minutes", [0, 11, 2.5, True, "5", float("nan"), float("inf")])
async def test_invalid_idle_minutes_do_not_authenticate(hass, minutes):
    from custom_components.wolt_monitor.config_flow import WoltConfigFlow

    flow = WoltConfigFlow()
    flow.hass = hass
    with patch("custom_components.wolt_monitor.config_flow.validate_token", AsyncMock()) as auth:
        result = await flow.async_step_user(
            {"refresh_token": "fake-input", "no_active_order_polling_interval_seconds": minutes}
        )
    assert result["errors"] == {"base": "invalid_options"}
    auth.assert_not_awaited()


async def test_same_validated_token_still_reloads_loaded_runtime(hass):
    from types import SimpleNamespace

    from custom_components.wolt_monitor import async_update_options

    entry = MockConfigEntry(
        domain="wolt_monitor", data={"refresh_token": "fake-old"}, options=options_dict(Options())
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        reauth_reload_requested=False, scheduler=SimpleNamespace(options=Options())
    )
    entry.add_update_listener(async_update_options)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with (
        patch(
            "custom_components.wolt_monitor.config_flow.validate_token",
            AsyncMock(return_value="fake-old"),
        ),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload,
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                **entry.options,
                "no_active_order_polling_interval_seconds": 5,
                "refresh_token": "fake-input",
            },
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    reload.assert_awaited_once_with(entry.entry_id)
    assert not entry.runtime_data.reauth_reload_requested


async def test_old_runtime_cannot_persist_rotation_after_token_replacement(hass):
    from custom_components.wolt_monitor.api import OrderResult
    from custom_components.wolt_monitor.auth import TokenReply

    api, client = AsyncMock(), AsyncMock()
    api.async_order.return_value = OrderResult(None)
    client.refresh.return_value = TokenReply("fake-access", "fake-old-refresh", 1000)
    entry = MockConfigEntry(domain="wolt_monitor", data={"refresh_token": "fake-input"})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch("custom_components.wolt_monitor.ClassicAuthClient", return_value=client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    old = entry.runtime_data
    try:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        with (
            patch(
                "custom_components.wolt_monitor.config_flow.validate_token",
                AsyncMock(return_value="fake-new-refresh"),
            ),
            patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
        ):
            await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"refresh_token": "fake-new-input", "no_active_order_polling_interval_seconds": 5},
            )
            # A previous auth request can finish before the scheduled reload executes.
            old._persist("fake-stale-rotation")
            await hass.async_block_till_done()
        assert entry.data["refresh_token"] == "fake-new-refresh"
        assert not old._can_read()
        api.reset_mock()
        await old.async_request_refresh()
        api.async_order.assert_not_awaited()
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
