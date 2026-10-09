"""Latest order sensors; independent availability overrides coordinator success."""

from dataclasses import dataclass

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME, VERSION

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WoltDescription(SensorEntityDescription):
    value_key: str


DESCRIPTIONS = (
    WoltDescription(
        key="latest_order_status",
        translation_key="latest_order_status",
        value_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "received",
            "acknowledged",
            "scheduled",
            "preparing",
            "ready",
            "on_the_way",
            "delivered",
            "cancelled",
            "unknown",
            "no_active_order",
        ],
        icon="mdi:progress-clock",
    ),
    WoltDescription(
        key="latest_order_restaurant",
        translation_key="latest_order_restaurant",
        value_key="restaurant",
        icon="mdi:silverware-fork-knife",
    ),
    WoltDescription(
        key="latest_order_delivery_time",
        translation_key="latest_order_delivery_time",
        value_key="estimated_delivery_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
    ),
    WoltDescription(
        key="latest_order_eta",
        translation_key="latest_order_eta",
        value_key="eta",
        native_unit_of_measurement="min",
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:timer-outline",
    ),
    WoltDescription(
        key="latest_order_courier_distance",
        translation_key="latest_order_courier_distance",
        value_key="distance",
        native_unit_of_measurement="m",
        device_class=SensorDeviceClass.DISTANCE,
        suggested_display_precision=0,
        icon="mdi:map-marker-distance",
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data

    def add_new():
        entities = [
            WoltSensor(coordinator, desc)
            for desc in DESCRIPTIONS
            if desc.key not in coordinator.entities
        ]
        if entities:
            async_add_entities(entities)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class WoltSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, description):
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self.entity_id = f"sensor.wolt_monitor_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)}, name=NAME, sw_version=VERSION
        )
        coordinator.entities[description.key] = self

    @property
    def native_value(self):
        value = self.coordinator.data[self.entity_description.value_key]
        key = self.entity_description.value_key
        if value is None:
            return None
        if key == "estimated_delivery_time":
            parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
            return parsed if parsed and parsed.tzinfo else None
        if key in {"eta", "distance"}:
            return value if type(value) is int and value >= 0 else None
        return value if isinstance(value, str) and value else None

    @property
    def available(self):
        return self.native_value is not None
