"""Build and validate the flat integration ZIP consumed by HACS (stdlib only)."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILENAME = "wolt_monitor.zip"
# Deliberate runtime allowlist: new modules/resources require packaging review.
RUNTIME = {
    "__init__.py",
    "api.py",
    "auth.py",
    "binary_sensor.py",
    "config_flow.py",
    "const.py",
    "coordinator.py",
    "device_tracker.py",
    "diagnostics.py",
    "errors.py",
    "reads.py",
    "safe_logging.py",
    "scheduler.py",
    "sensor.py",
    "state.py",
    "transport.py",
    "manifest.json",
    "strings.json",
    "translations/en.json",
    "translations/pl.json",
    "brand/icon.png",
    "brand/logo.png",
    "brand/dark_icon.png",
    "brand/dark_logo.png",
}


def contents(root):
    """Return integration runtime bytes relative to the HACS installation directory."""
    integration = root / "custom_components/wolt_monitor"
    tracked = (
        subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "-z", "--", "custom_components/wolt_monitor/"]
        )
        .decode()
        .split("\0")
    )
    names = {
        Path(name).relative_to("custom_components/wolt_monitor").as_posix()
        for name in filter(None, tracked)
    }
    if names != RUNTIME:
        raise ValueError(f"Unapproved or missing runtime files: {names ^ RUNTIME}")
    for name in names:
        path = integration / name
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
            raise ValueError(f"Symlink in runtime path: {name}")
        if not path.is_file():
            raise ValueError(f"Missing runtime file: {name}")
    return {name: (integration / name).read_bytes() for name in sorted(names)}


def check_version(root, files, tag):
    """Require an exact v-prefixed release tag and matching version declarations."""
    hacs = json.loads((root / "hacs.json").read_text())
    if (
        hacs.get("filename") != FILENAME
        or hacs.get("zip_release") is not True
        or hacs.get("content_in_root") is not False
        or hacs.get("hide_default_branch") is not True
    ):
        raise ValueError("HACS release configuration differs from package contract")
    version = json.loads(files["manifest.json"])["version"]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked = [p["version"] for p in lock["package"] if p["name"] == "ha-wolt-monitor"]
    runtime = [
        ast.literal_eval(node.value)
        for node in ast.parse(files["const.py"]).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets)
    ]
    if (
        not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version)
        or tag != f"v{version}"
        or project != version
        or locked != [version]
        or runtime != [version]
    ):
        raise ValueError("Version mismatch between tag, manifest, project, lock or runtime")


def build(root, output, tag):
    """Create a deterministic archive and sha256sum-compatible sidecar."""
    files = contents(root)
    check_version(root, files, tag)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / FILENAME
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zipped.writestr(info, data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {FILENAME}\n")
    validate(root, archive, tag)
    return archive


def validate(root, archive, tag):
    """Check archive layout, runtime bytes, CRC and checksum against the checkout."""
    expected = contents(root)
    check_version(root, expected, tag)
    with zipfile.ZipFile(archive) as zipped:
        if any(info.external_attr >> 16 != 0o100644 for info in zipped.infolist()):
            raise ValueError("Unsafe archive metadata")
        if zipped.namelist() != list(expected) or zipped.testzip() is not None:
            raise ValueError("Archive members or CRC differ")
        if any(zipped.read(name) != data for name, data in expected.items()):
            raise ValueError("Archive runtime bytes differ")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if archive.with_suffix(".zip.sha256").read_text() != f"{digest}  {FILENAME}\n":
        raise ValueError("Archive checksum differs")


def main():
    """Command-line build or independent validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args()
    if args.validate:
        validate(args.root, args.validate, args.tag)
        print(f"Validated {args.validate}")
    else:
        print(build(args.root, args.output, args.tag))


if __name__ == "__main__":
    main()
