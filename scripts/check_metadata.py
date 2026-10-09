"""Local release metadata checks; not a substitute for remote HACS validation."""

import ast
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_entities(entities):
    """Exact approved keys and names; no compatibility entities."""
    expected = {
        "sensor": {
            "latest_order_status": "Latest Order Status",
            "latest_order_restaurant": "Latest Order Restaurant",
            "latest_order_delivery_time": "Latest Order Estimated Delivery Time",
            "latest_order_eta": "Latest Order ETA",
            "latest_order_courier_distance": "Latest Order Courier Distance",
        },
        "device_tracker": {
            "latest_order_courier_position": "Latest Order Courier Position",
        },
        "binary_sensor": {
            "latest_order_delivery_in_progress": "Latest Order Delivery In Progress",
            "latest_order_delivered": "Latest Order Delivered",
        },
    }
    exact = {
        platform: {key: {"name": name} for key, name in names.items()}
        for platform, names in expected.items()
    }
    exact["binary_sensor"]["latest_order_delivery_in_progress"]["state"] = {
        "on": "Delivering",
        "off": "Not delivering",
    }
    exact["binary_sensor"]["latest_order_delivered"]["state"] = {
        "on": "Delivered",
        "off": "Not delivered",
    }
    exact["sensor"]["latest_order_status"]["state"] = {
        "received": "Received",
        "acknowledged": "Accepted",
        "scheduled": "Scheduled",
        "preparing": "Preparing",
        "ready": "Ready",
        "on_the_way": "On the Way",
        "delivered": "Delivered",
        "cancelled": "Cancelled",
        "unknown": "Unknown",
        "no_active_order": "No Active Order",
    }
    assert entities == exact


def main():
    integration = ROOT / "custom_components/wolt_monitor"
    manifest = json.loads((integration / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert manifest["domain"] == integration.name
    assert manifest["name"] == hacs["name"] == "Wolt Monitor"
    assert manifest["version"] == project["version"]
    constants = ast.parse((integration / "const.py").read_text())
    versions = [
        ast.literal_eval(node.value)
        for node in constants.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
    ]
    assert versions == [manifest["version"]], "Runtime version differs from manifest"
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", manifest["version"])
    assert manifest["config_flow"] is True and manifest["single_config_entry"] is True
    assert manifest["documentation"] == "https://github.com/Perwol/ha-wolt-monitor#readme"
    assert manifest["issue_tracker"] == "https://github.com/Perwol/ha-wolt-monitor/issues"
    assert manifest["codeowners"] == ["@Perwol"]
    assert hacs["content_in_root"] is False and hacs["zip_release"] is False
    assert hacs["homeassistant"] == "2026.3.1"
    assert [
        p.name
        for p in (ROOT / "custom_components").iterdir()
        if p.is_dir() and p.name != "__pycache__"
    ] == ["wolt_monitor"]
    assert json.loads((integration / "strings.json").read_text()) == json.loads(
        (integration / "translations/en.json").read_text()
    )
    check_entities(json.loads((integration / "strings.json").read_text())["entity"])
    for name in ["icon", "logo", "dark_icon", "dark_logo"]:
        assert (integration / f"brand/{name}.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "MIT License" in (ROOT / "LICENSE").read_text()
    readme = (ROOT / "README.md").read_text()
    assert f"**Version {manifest['version']}**" in readme, "README version differs from manifest"
    assert "Development preview" not in readme and "not ready for use" not in readme
    for limitation in (
        "Unofficial Wolt interfaces may change or stop working without notice.",
        "Location freshness is not guaranteed.",
    ):
        assert limitation in readme, "Required release limitation is missing"
    assert "https://www.buymeacoffee.com/perwol" in readme
    required_sections = {
        "Installation",
        "Entities",
        "Order behavior and limitations",
        "Authentication, troubleshooting and privacy",
        "Development and support",
        "License",
    }
    headings = set(re.findall(r"^#{1,6} (.+)$", readme, re.MULTILINE))
    assert required_sections <= headings, "Required README sections are missing"
    assert {"Configuration fields", "Refresh token"} <= headings, "README anchors are missing"
    print("Local metadata checks PASS (remote HACS checks not exercised)")


if __name__ == "__main__":
    main()
