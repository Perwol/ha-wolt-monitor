"""Exact allowlist; never serialize config entry data or Wolt payloads."""

from .binary_sensor import DESCRIPTIONS as BINARY_DESCRIPTIONS
from .const import DOMAIN, VERSION, options_dict
from .device_tracker import DESCRIPTION as POSITION_DESCRIPTION
from .sensor import DESCRIPTIONS

NORMALIZED_STATES = {
    "unknown",
    "no_active_order",
    "received",
    "acknowledged",
    "scheduled",
    "preparing",
    "ready",
    "on_the_way",
    "delivered",
    "cancelled",
}


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    status = coordinator.data["status"]
    if status is not None and status not in NORMALIZED_STATES:
        status = "unknown"
    return {
        "integration": {"domain": DOMAIN, "version": VERSION},
        "configuration": options_dict(coordinator.scheduler.options),
        "runtime": {
            "normalized_status": status,
            "reauthentication_required": coordinator.runtime.reauth_required,
            **{
                key: {
                    "last_successful_update": state.last_successful_update,
                    "consecutive_errors": state.consecutive_errors,
                    "last_error_category": state.last_error_category,
                    "last_http_status": state.last_http_status,
                }
                for key, state in coordinator.runtime.sources.items()
            },
        },
        "entities": {
            desc.key: {
                "created": desc.key in coordinator.entities,
                "available": bool(
                    desc.key in coordinator.entities and coordinator.entities[desc.key].available
                ),
            }
            for desc in (*DESCRIPTIONS, *BINARY_DESCRIPTIONS, POSITION_DESCRIPTION)
        },
    }
