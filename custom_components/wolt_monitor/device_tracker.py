"""Memory-only courier GPS projection; never associates a courier with a person."""

from homeassistant.components.device_tracker.config_entry import (
    SourceType,
    TrackerEntity,
    TrackerEntityDescription,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION

PARALLEL_UPDATES = 0
DESCRIPTION = TrackerEntityDescription(
    key="latest_order_courier_position",
    translation_key="latest_order_courier_position",
    icon="mdi:moped",
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([WoltCourierPosition(entry.runtime_data)])


class WoltCourierPosition(CoordinatorEntity, TrackerEntity):
    _attr_has_entity_name = True
    _attr_source_type = SourceType.GPS
    entity_description = DESCRIPTION

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{DESCRIPTION.key}"
        self.entity_id = f"device_tracker.wolt_monitor_{DESCRIPTION.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)}, name=NAME, sw_version=VERSION
        )
        coordinator.entities[DESCRIPTION.key] = self

    @property
    def available(self):
        return self.coordinator.runtime.courier_position() is not None

    @property
    def latitude(self):
        position = self.coordinator.runtime.courier_position()
        return position[0] if position is not None else None

    @property
    def longitude(self):
        position = self.coordinator.runtime.courier_position()
        return position[1] if position is not None else None
