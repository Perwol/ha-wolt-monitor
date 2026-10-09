"""Enabled integration and sensor identities across real HA process boundaries."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Process HA tests verified on Linux"
)
ROOT = Path(__file__).resolve().parents[1]


def lifecycle(phase, directory):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/lifecycle_probe.py"), phase, str(directory)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout)


def test_enabled_integration_restores_entities_and_token_in_new_process(tmp_path):
    before = lifecycle("first", tmp_path)
    after = lifecycle("second", tmp_path)
    assert before["entry_id"] == after["entry_id"]
    assert len(before["entities"]) == 8
    assert before["entities"] == after["entities"]
    suffixes = {
        "latest_order_status",
        "latest_order_restaurant",
        "latest_order_delivery_time",
        "latest_order_eta",
        "latest_order_delivered",
        "latest_order_courier_distance",
        "latest_order_courier_position",
        "latest_order_delivery_in_progress",
    }
    for result in (before, after):
        assert set(result["entities"]) == {
            (
                "binary_sensor"
                if s in {"latest_order_delivery_in_progress", "latest_order_delivered"}
                else "device_tracker"
                if s == "latest_order_courier_position"
                else "sensor"
            )
            + f".wolt_monitor_{s}"
            for s in suffixes
        }
        assert {r["unique_id"] for r in result["entities"].values()} == {
            f"{result['entry_id']}_{s}" for s in suffixes
        }
        devices = set()
        for record in result["entities"].values():
            assert record["config_entry_id"] == result["entry_id"]
            assert record["disabled_by"] is None
            assert record["device_id"]
            assert result["entry_id"] in record["device_config_entries"]
            assert ["wolt_monitor", result["entry_id"]] in record["device_identifiers"]
            devices.add(record["device_id"])
        assert len(devices) == 1
        assert result["poll_installed"]
    assert before["retention_installed"] and not after["retention_installed"]
    assert before["received_refresh"] == "synthetic-input-refresh"
    assert after["received_refresh"] == "synthetic-first-refresh"
    assert before["retention_active"] and not after["retention_active"]
    assert before["status"] == "delivered"
    assert before["delivery_flag"] == "off"
    assert before["delivered_flag"] == "on"
    assert after["status"] == "no_active_order"
    assert after["delivery_flag"] == "unavailable"
    assert after["delivered_flag"] == "unavailable"
    assert before["closed"] and after["closed"]
    config = json.loads((tmp_path / ".storage/core.config_entries").read_text())
    assert config["data"]["entries"][0]["data"]["refresh_token"] == "synthetic-second-refresh"
    for path in (tmp_path / ".storage").iterdir():
        if path.is_file():
            text = path.read_text()
            assert "synthetic-first-access" not in text
            assert "synthetic-second-access" not in text
