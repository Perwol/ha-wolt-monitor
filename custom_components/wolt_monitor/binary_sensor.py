"""Explicit tri-state delivery evidence, never inferred from ETA or driver presence."""

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION

PARALLEL_UPDATES = 0
DESCRIPTION = BinarySensorEntityDescription(
    key="latest_order_delivery_in_progress",
    translation_key="latest_order_delivery_in_progress",
    icon="mdi:moped",
)


DESCRIPTIONS = (
    DESCRIPTION,
    BinarySensorEntityDescription(
        key="latest_order_delivered",
        translation_key="latest_order_delivered",
        icon="mdi:check-circle-outline",
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([WoltDeliverySensor(entry.runtime_data, desc) for desc in DESCRIPTIONS])


class WoltDeliverySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    entity_description = DESCRIPTION

    def __init__(self, coordinator, description=DESCRIPTION):
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self.entity_id = f"binary_sensor.wolt_monitor_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)}, name=NAME, sw_version=VERSION
        )
        coordinator.entities[description.key] = self

    @property
    def is_on(self):
        if self.entity_description.key == "latest_order_delivered":
            return self.coordinator.runtime.is_delivered()
        return self.coordinator.runtime.delivery_in_progress()

    @property
    def available(self):
        return self.is_on is not None
