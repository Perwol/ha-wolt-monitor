"""Public integration identifiers and atomic options."""

from .scheduler import Options

DOMAIN = "wolt_monitor"
NAME = "Wolt Monitor"
VERSION = "1.0.0"
REFRESH_TOKEN = "refresh_token"
OPTION_KEYS = {
    "no_active_order_polling_interval_seconds": "idle_seconds",
    "active_order_polling_interval_seconds": "active_seconds",
    "courier_polling_interval_seconds": "courier_seconds",
    "completed_order_retention_minutes": "retention_minutes",
}


def options_from(data):
    if set(data) - set(OPTION_KEYS):
        raise ValueError("Invalid options")
    values = {
        OPTION_KEYS[key]: int(value) if type(value) is float and value.is_integer() else value
        for key, value in data.items()
    }
    return Options(**values)


def options_dict(options):
    return {key: getattr(options, attr) for key, attr in OPTION_KEYS.items()}
