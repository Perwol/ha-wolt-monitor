"""Offline release archive contract."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest


def checkout(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    shutil.copytree(
        ROOT / "custom_components",
        root / "custom_components",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ["hacs.json", "pyproject.toml", "uv.lock"]:
        shutil.copyfile(ROOT / name, root / name)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    return root


ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "custom_components/wolt_monitor/manifest.json").read_text())["version"]
TAG = f"v{VERSION}"


def tooling():
    path = ROOT / "scripts/package_release.py"
    assert path.exists(), "Release packager is missing"
    spec = importlib.util.spec_from_file_location("package_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_has_hacs_root_layout_and_checksum(tmp_path):
    package = tooling()
    archive = package.build(ROOT, tmp_path, TAG)
    assert archive.name == "wolt_monitor.zip"
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        assert "manifest.json" in names
        assert "LICENSE" in names
        assert zipped.read("LICENSE") == (ROOT / "LICENSE").read_bytes()
        assert "__init__.py" in names
        assert "translations/en.json" in names
        assert "translations/pl.json" in names
        assert "brand/dark_logo.png" in names
        assert all(not name.startswith("custom_components/") for name in names)
        for name in names:
            assert (
                zipped.read(name) == (ROOT / "custom_components/wolt_monitor" / name).read_bytes()
            )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert archive.with_suffix(".zip.sha256").read_text() == f"{digest}  wolt_monitor.zip\n"
    package.validate(ROOT, archive, TAG)


def test_untracked_junk_is_never_packaged(tmp_path):
    root = checkout(tmp_path)
    integration = root / "custom_components/wolt_monitor"
    for name in ["secret.py", ".env", "test_extra.py", "__pycache__/api.pyc", "tests/data.json"]:
        path = integration / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("not runtime")
    archive = tooling().build(root, tmp_path / "dist", TAG)
    with zipfile.ZipFile(archive) as zipped:
        assert not {
            "secret.py",
            ".env",
            "test_extra.py",
            "__pycache__/api.pyc",
            "tests/data.json",
        } & set(zipped.namelist())


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "secret.py",
        "test_api.py",
        "tests/data.json",
        "__pycache__/api.pyc",
        "translations/.token.json",
        "brand/extra.png",
    ],
)
def test_rejects_unapproved_tracked_content(tmp_path, name):
    root = checkout(tmp_path)
    path = root / "custom_components/wolt_monitor" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("not runtime")
    subprocess.run(["git", "-C", str(root), "add", "-f", str(path)], check=True)
    with pytest.raises(ValueError, match="Unapproved"):
        tooling().build(root, tmp_path / "dist", TAG)
    assert not (tmp_path / "dist/wolt_monitor.zip").exists()


@pytest.mark.parametrize("name", ["api.py", "brand", "translations", "."])
def test_rejects_symlink_files_and_parent_directories(tmp_path, name):
    root = checkout(tmp_path)
    integration = root / "custom_components/wolt_monitor"
    path = integration / name
    destination = tmp_path / "outside"
    if path.is_dir():
        shutil.move(str(path), destination)
    else:
        path.rename(destination)
    path.symlink_to(destination, target_is_directory=destination.is_dir())
    with pytest.raises(ValueError, match="[Ss]ymlink"):
        tooling().build(root, tmp_path / "dist", TAG)


@pytest.mark.parametrize("target", ["tag", "manifest", "project", "lock", "runtime"])
def test_version_drift_fails_before_creating_archive(tmp_path, target):
    root = checkout(tmp_path)
    tag = TAG
    paths = {
        "manifest": "custom_components/wolt_monitor/manifest.json",
        "project": "pyproject.toml",
        "lock": "uv.lock",
        "runtime": "custom_components/wolt_monitor/const.py",
    }
    if target == "tag":
        tag = "v9.9.9"
    else:
        path = root / paths[target]
        path.write_text(path.read_text().replace(VERSION, "9.9.9"))
    with pytest.raises(ValueError, match="[Vv]ersion"):
        tooling().build(root, tmp_path / "dist", tag)
    assert not (tmp_path / "dist/wolt_monitor.zip").exists()


def test_validation_rejects_archive_symlink_metadata(tmp_path):
    package = tooling()
    archive = package.build(ROOT, tmp_path, TAG)
    with zipfile.ZipFile(archive) as zipped:
        entries = [(info, zipped.read(info)) for info in zipped.infolist()]
    with zipfile.ZipFile(archive, "w") as zipped:
        for info, data in entries:
            info.external_attr = 0o120777 << 16
            zipped.writestr(info, data)
    archive.with_suffix(".zip.sha256").write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  wolt_monitor.zip\n"
    )
    with pytest.raises(ValueError, match="metadata"):
        package.validate(ROOT, archive, TAG)


@pytest.mark.parametrize(
    "field,value",
    [
        ("filename", "versioned.zip"),
        ("zip_release", False),
        ("content_in_root", True),
        ("hide_default_branch", False),
    ],
)
def test_hacs_release_contract_required(tmp_path, field, value):
    root = checkout(tmp_path)
    path = root / "hacs.json"
    hacs = json.loads(path.read_text())
    hacs[field] = value
    path.write_text(json.dumps(hacs))
    with pytest.raises(ValueError, match="HACS"):
        tooling().build(root, tmp_path / "dist", TAG)


@pytest.mark.parametrize("mutation", ["checksum", "bytes", "extra", "missing", "duplicate"])
def test_validation_detects_corruption(tmp_path, mutation):
    package = tooling()
    archive = package.build(ROOT, tmp_path, TAG)
    if mutation == "checksum":
        archive.with_suffix(".zip.sha256").write_text("invalid")
    else:
        with zipfile.ZipFile(archive) as zipped:
            entries = [(info, zipped.read(info)) for info in zipped.infolist()]
        with zipfile.ZipFile(archive, "w") as zipped:
            for index, (info, data) in enumerate(entries):
                if mutation == "missing" and index == 0:
                    continue
                zipped.writestr(info, b"wrong" if mutation == "bytes" and index == 0 else data)
            if mutation == "extra":
                zipped.writestr("../secret", b"wrong")
            if mutation == "duplicate":
                with pytest.warns(UserWarning, match="Duplicate"):
                    zipped.writestr(entries[0][0], entries[0][1])
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix(".zip.sha256").write_text(f"{digest}  wolt_monitor.zip\n")
    with pytest.raises(ValueError):
        package.validate(ROOT, archive, TAG)


def test_reproducible_cli_and_hacs_extraction(tmp_path):
    command = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/package_release.py"),
        "--tag",
        TAG,
        "--output",
        str(tmp_path),
    ]
    subprocess.run(command, check=True)
    archive = tmp_path / "wolt_monitor.zip"
    first = archive.read_bytes()
    subprocess.run(command, check=True)
    assert archive.read_bytes() == first
    subprocess.run(command + ["--validate", str(archive)], check=True)
    destination = tmp_path / "config/custom_components/wolt_monitor"
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(destination)
    assert json.loads((destination / "manifest.json").read_text())["version"] == VERSION
    assert (destination / "translations/pl.json").is_file()
    assert not (destination / "custom_components").exists()


def test_release_workflow_is_draft_only_and_separates_write_job():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/release-assets.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    package = workflow["jobs"]["package"]
    assert "github.ref == 'refs/heads/main'" in package["if"]
    attach = workflow["jobs"]["attach"]
    assert attach["permissions"] == {"contents": "write"}
    assert not any("checkout@" in step.get("uses", "") for step in attach["steps"])
    script = attach["steps"][-1]["run"]
    assert ".draft == true" in script
    assert "length == 0" in script
    assert "release upload" in script
    assert "--clobber" not in script
    assert "release create" not in script
    assert "release edit" not in script
