"""Home Assistant lifecycle for the HA Wolt Monitor adapter."""

from homeassistant.const import Platform

from .api import create_data_api
from .const import NAME, options_from
from .coordinator import WoltCoordinator
from .transport import ClassicAuthClient

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]


async def async_setup_entry(hass, entry):
    if entry.title == "HA Wolt Monitor":
        hass.config_entries.async_update_entry(entry, title=NAME)
    api = client = coordinator = None
    try:
        api = create_data_api(hass)
        client = ClassicAuthClient()
        coordinator = WoltCoordinator(hass, entry, api, client)
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        if coordinator is not None:
            await coordinator.async_shutdown()
        else:
            try:
                if client is not None:
                    await client.close()
            finally:
                if api is not None:
                    await api.close()
        raise
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_update_options(hass, entry):
    coordinator = entry.runtime_data
    if coordinator.reauth_reload_requested:
        coordinator.reauth_reload_requested = False
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    if options_from(entry.options) != coordinator.scheduler.options:
        coordinator.apply_options()


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
        return True
    return False
