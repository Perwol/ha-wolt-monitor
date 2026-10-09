"""Refresh-only configuration and atomic live options."""

from time import monotonic

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .auth import TokenManager
from .const import DOMAIN, NAME, REFRESH_TOKEN, options_dict, options_from
from .errors import Failure
from .transport import ClassicAuthClient


async def validate_token(token):
    client = ClassicAuthClient()
    try:
        manager = TokenManager(token, client.refresh, lambda _: None, clock=monotonic)
        await manager.acquire("validation")
        return manager.refresh_token
    finally:
        await client.close()


def token_schema():
    return vol.Schema(
        {
            vol.Required(REFRESH_TOKEN): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
        }
    )


IDLE_KEY = "no_active_order_polling_interval_seconds"
DOCUMENTATION_PLACEHOLDERS = {
    "configuration_url": "https://github.com/Perwol/ha-wolt-monitor#configuration-fields",
    "token_url": "https://github.com/Perwol/ha-wolt-monitor#refresh-token",
}


def settings_schema(options=None, *, optional_token=False):
    defaults = options_dict(options_from(options or {}))
    defaults[IDLE_KEY] //= 60
    fields = dict(token_schema().schema)
    if optional_token:
        selector = next(iter(fields.values()))
        fields = {vol.Optional(REFRESH_TOKEN): selector}
    constraints = [(1, 10, 1, "min"), (30, 300, 30, "s"), (30, 300, 30, "s"), (0, 60, 1, "min")]
    for (key, value), (low, high, step, unit) in zip(defaults.items(), constraints, strict=True):
        fields[vol.Required(key, default=value)] = NumberSelector(
            NumberSelectorConfig(
                min=low, max=high, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX
            )
        )
    return vol.Schema(fields)


def form_options(data):
    values = {key: value for key, value in data.items() if key != REFRESH_TOKEN}
    if IDLE_KEY in values:
        value = values[IDLE_KEY]
        if type(value) not in (int, float) or not 1 <= value <= 10 or int(value) != value:
            raise ValueError("Invalid options")
        values[IDLE_KEY] = int(value) * 60
    return options_dict(options_from(values))


class WoltConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return WoltOptionsFlow()

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return await self._token_step("user", user_input)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self._token_step("reauth_confirm", user_input)

    async def _token_step(self, step, user_input):
        error = None
        if user_input is not None:
            try:
                selected = form_options(user_input) if step == "user" else None
            except ValueError, TypeError:
                return self.async_show_form(
                    step_id=step,
                    data_schema=settings_schema(),
                    errors={"base": "invalid_options"},
                    description_placeholders=DOCUMENTATION_PLACEHOLDERS,
                )
            try:
                refresh = await validate_token(user_input[REFRESH_TOKEN])
            except Failure as failure:
                error = (
                    "invalid_auth"
                    if failure.invalid_refresh
                    else ("unknown" if failure.category == "unexpected" else "cannot_connect")
                )
            except Exception:
                error = "unknown"
            else:
                data = {REFRESH_TOKEN: refresh}
                if step == "reauth_confirm":
                    entry = self._get_reauth_entry()
                    coordinator = getattr(entry, "runtime_data", None)
                    if coordinator is not None:
                        coordinator.auth_replaced = True
                    use_listener = (
                        coordinator is not None and entry.update_listeners and entry.data != data
                    )
                    if use_listener:
                        coordinator.reauth_reload_requested = True
                    result = self.async_update_and_abort(
                        entry, data=data, reason="reauth_successful"
                    )
                    if not use_listener:
                        self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return result
                return self.async_create_entry(title=NAME, data=data, options=selected)
        return self.async_show_form(
            step_id=step,
            data_schema=settings_schema() if step == "user" else token_schema(),
            errors={"base": error} if error else {},
            description_placeholders=DOCUMENTATION_PLACEHOLDERS if step == "user" else None,
        )


class WoltOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                selected = form_options(user_input)
            except ValueError, TypeError:
                errors["base"] = "invalid_options"
            else:
                token = user_input.get(REFRESH_TOKEN, "")
                if token:
                    try:
                        refresh = await validate_token(token)
                    except Failure as failure:
                        errors["base"] = (
                            "invalid_auth"
                            if failure.invalid_refresh
                            else "unknown"
                            if failure.category == "unexpected"
                            else "cannot_connect"
                        )
                    except Exception:
                        errors["base"] = "unknown"
                    else:
                        entry = self.config_entry
                        coordinator = getattr(entry, "runtime_data", None)
                        if coordinator is not None:
                            coordinator.auth_replaced = True
                        data = {**entry.data, REFRESH_TOKEN: refresh}
                        use_listener = (
                            coordinator is not None
                            and bool(entry.update_listeners)
                            and (entry.data != data or entry.options != selected)
                        )
                        if use_listener and coordinator is not None:
                            coordinator.reauth_reload_requested = True
                        self.hass.config_entries.async_update_entry(
                            entry, data=data, options=selected
                        )
                        if not use_listener:
                            self.hass.config_entries.async_schedule_reload(entry.entry_id)
                if not errors:
                    return self.async_create_entry(title="", data=selected)
        return self.async_show_form(
            step_id="init",
            data_schema=settings_schema(self.config_entry.options, optional_token=True),
            errors=errors,
            description_placeholders=DOCUMENTATION_PLACEHOLDERS,
        )
