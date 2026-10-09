"""Real enabled config entry/platforms, real registries, synthetic external Wolt boundary."""

import asyncio
import json
import socket
import sys
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import area_registry, device_registry, entity_registry
from homeassistant.setup import async_setup_component

from custom_components.wolt_monitor.api import OrderResult
from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.state import OrderSnapshot


def deny_network(*args, **kwargs):
    raise RuntimeError("Network forbidden in lifecycle probe")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network


class SyntheticClient:
    def __init__(self, phase):
        self.phase = phase
        self.received = []
        self.close_count = 0

    async def refresh(self, token):
        self.received.append(token)
        return TokenReply(f"synthetic-{self.phase}-access", f"synthetic-{self.phase}-refresh", 3600)

    async def close(self):
        self.close_count += 1


class SyntheticAPI:
    def __init__(self, phase):
        self.phase = phase

    async def async_order(self, access_token):
        assert access_token == f"synthetic-{self.phase}-access"
        snapshot = (
            OrderSnapshot("synthetic-order", "delivered", terminal=True)
            if self.phase == "first"
            else None
        )
        return OrderResult(snapshot, courier_supported=True)

    async def async_courier(self, access_token, order_key):
        raise AssertionError("Terminal/empty order must not request courier")


async def main():
    phase, directory = sys.argv[1:]
    root = Path(directory)
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    hass = HomeAssistant(str(root))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    # Older HA creates this lazily; current HA requires bootstrap's explicit setup.
    if setup_registry := getattr(device_registry, "async_setup", None):
        setup_registry(hass)
    await asyncio.gather(
        area_registry.async_load(hass),
        device_registry.async_load(hass),
        entity_registry.async_load(hass),
    )
    assert await async_setup_component(hass, "homeassistant", {})
    client, api = SyntheticClient(phase), SyntheticAPI(phase)
    with (
        patch("custom_components.wolt_monitor.create_data_api", return_value=api),
        patch("custom_components.wolt_monitor.ClassicAuthClient", return_value=client),
    ):
        if phase == "first":
            entry = ConfigEntry(
                domain="wolt_monitor",
                title="HA Wolt Monitor",
                version=1,
                minor_version=1,
                data={"refresh_token": "synthetic-input-refresh"},
                options={},
                discovery_keys=MappingProxyType({}),
                source="user",
                unique_id=None,
                subentries_data=[],
            )
            await hass.config_entries.async_add(entry)
        else:
            entries = hass.config_entries.async_entries("wolt_monitor")
            assert len(entries) == 1
            entry = entries[0]
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_start()
        await hass.async_block_till_done()
        assert hass.state == CoreState.running
        coordinator = entry.runtime_data
        assert len(coordinator.entities) == 8
        registry = entity_registry.async_get(hass)
        entities = {}
        for sensor in coordinator.entities.values():
            record = registry.async_get(sensor.entity_id)
            assert record is not None
            assert hass.states.get(sensor.entity_id) is not None
            assert record.config_entry_id == entry.entry_id
            assert record.disabled_by is None
            assert record.device_id is not None
            device = device_registry.async_get(hass).async_get(record.device_id)
            assert device is not None
            # Core 2026.10 gives each device one owner. Older supported Core
            # exposes the owners through the public subentry mapping instead.
            device_config_entries = (
                {device.config_entry_id}
                if hasattr(device, "config_entry_id")
                else set(device.config_entries_subentries)
            )
            assert device_config_entries == {entry.entry_id}
            assert ("wolt_monitor", entry.entry_id) in device.identifiers
            entities[sensor.entity_id] = {
                "unique_id": record.unique_id,
                "device_id": record.device_id,
                "registry_id": record.id,
                "config_entry_id": record.config_entry_id,
                "disabled_by": record.disabled_by,
                "device_config_entries": sorted(device_config_entries),
                "device_identifiers": sorted(device.identifiers),
            }
        status = hass.states.get("sensor.wolt_monitor_latest_order_status")
        assert status is not None
        assert coordinator._poll_cancel is not None
        assert (coordinator._retention_cancel is not None) == (phase == "first")
        result = {
            "entry_id": entry.entry_id,
            "entities": entities,
            "received_refresh": client.received[0],
            "retention_active": coordinator.runtime.retention_deadline is not None,
            "status": status.state,
            "delivery_flag": hass.states.get(
                "binary_sensor.wolt_monitor_latest_order_delivery_in_progress"
            ).state,
            "poll_installed": coordinator._poll_cancel is not None,
            "retention_installed": coordinator._retention_cancel is not None,
        }
        await hass.async_stop(force=True)
        assert client.close_count == 1
        assert coordinator._stopped
        assert coordinator._poll_cancel is None
        assert coordinator._retention_cancel is None
        assert hass.state == CoreState.stopped
        assert len(client.received) == 1
        result["closed"] = True
        print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
