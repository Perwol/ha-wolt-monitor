"""Deterministic clocks; prohibit outbound HTTP in all tests."""

import asyncio
import socket

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
async def enable_event_loop_debug():
    """Use pytest's running loop, including with the minimum HA fixture on Python 3.14."""
    asyncio.get_running_loop().set_debug(True)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network access is forbidden in this test suite")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
