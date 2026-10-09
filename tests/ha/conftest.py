"""Real HA Python fixtures, without starting a server or contacting Wolt."""

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield
