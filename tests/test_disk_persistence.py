"""Verify actual HA config-entry disk persistence across isolated Python processes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# macOS HA teardown currently segfaults during native garbage collection in this probe
# (reproduced with Python 3.14.7 and 3.14.8). Never hide that as a successful run.
# Linux runs exercise every disk case as a non-root user.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Real-process HA storage probe verified on Linux only; macOS native teardown crash",
)

ROOT = Path(__file__).resolve().parents[1]


def probe(phase, directory):
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
        if key in os.environ
    }
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/persistence_probe.py"), phase, str(directory)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    output = json.loads(result.stdout) if result.stdout.strip() else {}
    return output, result.stderr


@pytest.mark.parametrize("phase", ["scheduled", "shutdown"])
def test_rotation_survives_real_disk_and_fresh_process(tmp_path, phase):
    seed, _ = probe("seed", tmp_path)
    probe(phase, tmp_path)
    restored, _ = probe("read", tmp_path)
    assert restored == {
        "entry_id": seed["entry_id"],
        "refresh": "synthetic-refresh-new",
        "access_usable": False,
        "retention": None,
    }
    storage = (tmp_path / ".storage/core.config_entries").read_text()
    assert "synthetic-order-not-persisted" not in storage
    assert "synthetic-access-new" not in storage
    assert "synthetic-refresh-old" not in storage


def test_process_loss_before_save_preserves_previous_disk_token(tmp_path):
    seed, _ = probe("seed", tmp_path)
    probe("crash", tmp_path)
    restored, _ = probe("read", tmp_path)
    assert restored["entry_id"] == seed["entry_id"]
    assert restored["refresh"] == "synthetic-refresh-old"
    assert not restored["access_usable"]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="Root bypasses real filesystem permission failure",
)
def test_late_disk_error_does_not_become_false_persistence_retry(tmp_path):
    seed, _ = probe("seed", tmp_path)
    rotated, stderr = probe("disk-error", tmp_path)
    assert "Error writing config for core.config_entries" in stderr
    assert rotated["memory_updated"] and not rotated["persistence_pending"]
    restored, _ = probe("read", tmp_path)
    assert restored["entry_id"] == seed["entry_id"]
    assert restored["refresh"] == "synthetic-refresh-old"
