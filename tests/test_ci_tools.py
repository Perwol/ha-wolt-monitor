"""Offline regressions for release tooling; no network or runtime secret imports."""

import json
from pathlib import Path

import pytest

from scripts import check_metadata, select_stable_ha


def fake_metadata(monkeypatch, dependencies, stable="2026.9.4", yanked=()):
    def metadata(package, version=None):
        if package == "homeassistant":
            return {"info": {"version": stable}}
        if version is None:
            return {"releases": {v: [{"yanked": v in yanked}] for v in dependencies}}
        return {"info": {"requires_dist": dependencies[version]}}

    monkeypatch.setattr(select_stable_ha, "metadata", metadata)


def test_selector_skips_false_markers(monkeypatch, capsys):
    fake_metadata(
        monkeypatch,
        {
            "2.0.0": ['homeassistant==2026.9.4; python_version < "3.0"'],
            "1.0.0": ['homeassistant==2026.9.4; python_version >= "3.14"'],
        },
    )
    select_stable_ha.main()
    assert "component==1.0.0" in capsys.readouterr().out


def test_selector_skips_beta_and_yanked_fixtures(monkeypatch, capsys):
    fake_metadata(
        monkeypatch,
        {
            "4.0.0b1": ["homeassistant==2026.9.4"],
            "3.0.0": ["homeassistant==2026.10.0b2"],
            "2.0.0": ["homeassistant==2026.9.4"],
            "1.0.0": ["homeassistant==2026.9.4"],
        },
        yanked=("2.0.0",),
    )
    select_stable_ha.main()
    assert "component==1.0.0" in capsys.readouterr().out


def test_selector_fails_without_stable_match(monkeypatch):
    fake_metadata(monkeypatch, {"1.0.0": ["homeassistant==2026.10.0b2"]})
    with pytest.raises(RuntimeError, match="No matching fixture"):
        select_stable_ha.main()


def test_selector_rejects_beta_ha_default(monkeypatch):
    fake_metadata(monkeypatch, {}, stable="2026.10.0b2")
    with pytest.raises(RuntimeError, match="not stable"):
        select_stable_ha.main()


def test_metadata_detects_runtime_version_drift(monkeypatch, tmp_path):
    root = check_metadata.ROOT
    integration = tmp_path / "custom_components/wolt_monitor"
    integration.mkdir(parents=True)
    (integration / "manifest.json").write_text(
        (root / "custom_components/wolt_monitor/manifest.json").read_text()
    )
    (integration / "const.py").write_text('VERSION = "9.9.9"\n')
    for name in ["hacs.json", "pyproject.toml"]:
        (tmp_path / name).write_text((root / name).read_text())
    monkeypatch.setattr(check_metadata, "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="Runtime version"):
        check_metadata.main()


def test_hacs_preserves_base_identity_and_passes_target_separately():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert "GITHUB_REPOSITORY: ${{ github.repository }}" in workflow
    assert (
        "REPOSITORY: ${{ github.event.pull_request.head.repo.full_name || github.repository }}"
        in workflow
    )
    assert "-e REPOSITORY " in workflow


def test_minimum_ha_matches_hacs_and_ci():
    root = check_metadata.ROOT
    hacs = json.loads((root / "hacs.json").read_text())
    assert hacs["homeassistant"] == "2026.3.1"
    workflow = (root / ".github/workflows/ci.yml").read_text()
    assert "target: [minimum, latest-stable]" in workflow
    assert "pytest-homeassistant-custom-component==0.13.317" in workflow
    assert "homeassistant==2026.3.1" in workflow


@pytest.mark.parametrize(
    "platform,key",
    [
        ("sensor", "latest_order_eta_min"),
        ("device_tracker", "courier_position"),
        ("binary_sensor", "delivery_in_progress"),
        ("binary_sensor", "latest_order_is_delivered"),
    ],
)
def test_entity_translation_contract_rejects_legacy_aliases(platform, key):
    assert hasattr(check_metadata, "check_entities"), "Entity metadata validator is missing"
    strings = json.loads(
        (check_metadata.ROOT / "custom_components/wolt_monitor/strings.json").read_text()
    )
    check_metadata.check_entities(strings["entity"])
    strings["entity"][platform][key] = {"name": "Legacy Alias"}
    with pytest.raises(AssertionError):
        check_metadata.check_entities(strings["entity"])


@pytest.mark.parametrize(
    "removed",
    [
        None,
        "**Version 1.0.0**",
        "Unofficial Wolt interfaces may change or stop working without notice.",
        "Location freshness is not guaranteed.",
        "https://www.buymeacoffee.com/perwol",
        "### Configuration fields",
        "### Refresh token",
        "## Installation",
        "## Entities",
        "## Order behavior and limitations",
        "## Authentication, troubleshooting and privacy",
        "## Development and support",
        "## License",
    ],
)
def test_metadata_accepts_short_readme_with_preserved_contract(monkeypatch, tmp_path, removed):
    import shutil

    root = check_metadata.ROOT
    shutil.copytree(root / "custom_components", tmp_path / "custom_components")
    for name in ["hacs.json", "pyproject.toml", "LICENSE"]:
        shutil.copyfile(root / name, tmp_path / name)
    readme = (root / "README.md").read_text()
    assert len([line for line in readme.splitlines() if line.startswith("## ")]) != 8
    if removed is not None:
        assert removed in readme
        readme = readme.replace(removed, "")
    (tmp_path / "README.md").write_text(readme)
    monkeypatch.setattr(check_metadata, "ROOT", tmp_path)
    if removed is None:
        check_metadata.main()
    else:
        with pytest.raises(AssertionError):
            check_metadata.main()


@pytest.mark.parametrize("mutation", ["missing", "wrong", "extra"])
def test_entity_translation_contract_requires_exact_status_labels(mutation):
    entities = json.loads(
        (check_metadata.ROOT / "custom_components/wolt_monitor/strings.json").read_text()
    )["entity"]
    check_metadata.check_entities(entities)
    states = entities["sensor"]["latest_order_status"]["state"]
    if mutation == "missing":
        del states["acknowledged"]
    elif mutation == "wrong":
        states["acknowledged"] = "Acknowledged"
    else:
        states["arriving"] = "Arriving"
    with pytest.raises(AssertionError):
        check_metadata.check_entities(entities)


def test_first_release_version_is_consistent():
    import ast
    import tomllib

    root = check_metadata.ROOT
    manifest = json.loads((root / "custom_components/wolt_monitor/manifest.json").read_text())
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    package = next(p for p in lock["package"] if p["name"] == "ha-wolt-monitor")
    constants = ast.parse((root / "custom_components/wolt_monitor/const.py").read_text())
    runtime = next(
        ast.literal_eval(node.value)
        for node in constants.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets)
    )
    assert manifest["version"] == project["version"] == package["version"] == runtime == "1.0.0"


def test_public_readme_describes_version_without_pending_release_warning():
    readme = (check_metadata.ROOT / "README.md").read_text()
    assert "repository is not published yet" not in readme
    assert "after publication" not in readme.lower()
    assert "**Version 1.0.0**" in readme
    assert "release is pending" not in readme.lower()
    assert (
        "[![Buy Me a Coffee]"
        "(https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)]"
        "(https://www.buymeacoffee.com/perwol)" in readme
    )
    assert "free and open source" in readme


def test_local_metadata_passes(capsys):
    check_metadata.main()
    assert "checks PASS" in capsys.readouterr().out
    manifest = json.loads(
        (check_metadata.ROOT / "custom_components/wolt_monitor/manifest.json").read_text()
    )
    assert manifest["domain"] == "wolt_monitor"
