"""Fresh-process HA storage probe: real disk, synthetic tokens, no server/network."""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from types import MappingProxyType

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryDisabler
from homeassistant.core import HomeAssistant

from custom_components.wolt_monitor.auth import TokenReply
from custom_components.wolt_monitor.coordinator import WoltCoordinator
from custom_components.wolt_monitor.state import OrderSnapshot


def deny_network(*args, **kwargs):
    raise RuntimeError("Network forbidden in persistence probe")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network


class SyntheticClient:
    def __init__(self):
        self.received = []
        self.closed = False

    async def refresh(self, token):
        self.received.append(token)
        return TokenReply("synthetic-access-new", "synthetic-refresh-new", 3600)

    async def close(self):
        self.closed = True


async def main():
    phase, directory = sys.argv[1:]
    root = Path(directory)
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    hass = HomeAssistant(str(root))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    if phase == "seed":
        entry = ConfigEntry(
            domain="wolt_monitor",
            title="HA Wolt Monitor",
            version=1,
            minor_version=1,
            data={"refresh_token": "synthetic-refresh-old"},
            options={},
            discovery_keys=MappingProxyType({}),
            source="user",
            unique_id=None,
            subentries_data=[],
            disabled_by=ConfigEntryDisabler.USER,
        )
        await hass.config_entries.async_add(entry)
        await hass.async_stop(force=True)
        print(json.dumps({"entry_id": entry.entry_id}))
        return
    entries = hass.config_entries.async_entries("wolt_monitor")
    assert len(entries) == 1
    entry = entries[0]
    client = SyntheticClient()
    coordinator = WoltCoordinator(hass, entry, object(), client)
    assert not coordinator.tokens.usable()
    assert coordinator.runtime.retention_deadline is None
    if phase == "read":
        print(
            json.dumps(
                {
                    "entry_id": entry.entry_id,
                    "refresh": coordinator.tokens.refresh_token,
                    "access_usable": coordinator.tokens.usable(),
                    "retention": coordinator.runtime.retention_deadline,
                }
            )
        )
        loaded = coordinator.tokens.refresh_token
        await coordinator.tokens.acquire("first-after-restart")
        assert client.received == [loaded]
        await hass.async_stop(force=True)
        assert client.closed
        return
    coordinator.runtime.update_order(
        OrderSnapshot("synthetic-order-not-persisted", "delivered", terminal=True), "before-stop"
    )
    assert coordinator.runtime.retention_deadline is not None
    if phase == "disk-error":
        (root / ".storage").chmod(0o500)
    try:
        assert await coordinator.tokens.acquire("rotation") == "synthetic-access-new"
        assert client.received == ["synthetic-refresh-old"]
        assert entry.data["refresh_token"] == "synthetic-refresh-new"
        assert not coordinator.tokens.persistence_pending
        if phase == "crash":
            # Deliberate process loss before HA's scheduled save or final-write stage.
            os._exit(0)
        if phase in {"scheduled", "disk-error"}:
            await asyncio.sleep(1.5)
            await hass.async_block_till_done()
            saved = json.loads((root / ".storage/core.config_entries").read_text())
            expected = "synthetic-refresh-old" if phase == "disk-error" else "synthetic-refresh-new"
            assert saved["data"]["entries"][0]["data"]["refresh_token"] == expected
            assert coordinator.tokens.usable()
            assert not coordinator.tokens.persistence_pending
        await hass.async_stop(force=True)
        assert client.closed
    finally:
        if phase == "disk-error":
            (root / ".storage").chmod(0o700)
    print(
        json.dumps(
            {
                "phase": phase,
                "memory_updated": True,
                "persistence_pending": coordinator.tokens.persistence_pending,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
