"""Execute the release attach shell offline against a strict GitHub CLI double."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TAG = (
    "v" + json.loads((ROOT / "custom_components/wolt_monitor/manifest.json").read_text())["version"]
)
SHA = "a" * 40


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/release-assets.yml").read_text())


def release(**overrides):
    return {
        "id": 42,
        "tag_name": TAG,
        "draft": True,
        "published_at": None,
        "assets": [],
        **overrides,
    }


FAKE_GH = r"""
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
with open("calls.jsonl", "a") as log:
    log.write(json.dumps(args) + "\n")
repo = "repos/example/repo"
if args == ["api", repo + "/commits/" + os.environ["TAG"], "--jq", ".sha"]:
    print(os.environ["ACTUAL_SHA"])
elif args == ["api", "--paginate", "--slurp", repo + "/releases?per_page=100"]:
    print(Path("pages.json").read_text())
elif args == ["api", repo + "/releases/42"]:
    data = json.loads(Path("selected.json").read_text())
    if Path("uploaded").exists():
        data["assets"] = [{"name": "wolt_monitor.zip"}, {"name": "wolt_monitor.zip.sha256"}]
    print(json.dumps(data))
elif args == [
    "release", "upload", os.environ["TAG"],
    "dist/wolt_monitor.zip", "dist/wolt_monitor.zip.sha256",
]:
    Path("uploaded").touch()
elif args[:3] == ["release", "download", os.environ["TAG"]]:
    assert Path("uploaded").exists()
    assert args[3] == "--dir" and args[5:] == ["--pattern", "wolt_monitor.zip*"]
    for name in ("wolt_monitor.zip", "wolt_monitor.zip.sha256"):
        shutil.copyfile(Path("dist") / name, Path(args[4]) / name)
else:
    sys.exit("Unexpected or forbidden GitHub API call: " + repr(args))
"""


def run_attach(tmp_path, pages, *, actual_sha=SHA, selected=None):
    """Run the actual YAML shell; only the remote CLI boundary is replaced."""
    (tmp_path / "bin").mkdir()
    gh = tmp_path / "bin/gh"
    gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
    gh.chmod(0o755)
    (tmp_path / "pages.json").write_text(json.dumps(pages))
    (tmp_path / "selected.json").write_text(json.dumps(release() if selected is None else selected))
    (tmp_path / "runner").mkdir()
    # A real artifact/checksum pair exercises the shell's integrity and readback checks.
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/wolt_monitor.zip").write_bytes(b"offline artifact")
    subprocess.run(
        ["shasum", "-a", "256", "wolt_monitor.zip"],
        cwd=tmp_path / "dist",
        stdout=(tmp_path / "dist/wolt_monitor.zip.sha256").open("w"),
        check=True,
    )
    checksum = tmp_path / "bin/sha256sum"
    checksum.write_text('#!/bin/sh\nexec shasum -a 256 "$@"\n')
    checksum.chmod(0o755)
    script = workflow()["jobs"]["attach"]["steps"][-1]["run"]
    result = subprocess.run(
        ["bash", "-x", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": str(tmp_path / "bin") + os.pathsep + os.defpath,
            "GH_TOKEN": "offline-write-token",
            "GH_REPO": "example/repo",
            "TAG": TAG,
            "TARGET_SHA": SHA,
            "RELEASE_ID": "42",  # Legacy output must not bypass exact paginated discovery.
            "ACTUAL_SHA": actual_sha,
            "RUNNER_TEMP": str(tmp_path / "runner"),
        },
        capture_output=True,
        text=True,
    )
    calls = tmp_path / "calls.jsonl"
    return result, [json.loads(line) for line in calls.read_text().splitlines()]


def test_attach_discovers_exact_draft_across_pages_and_verifies_upload(tmp_path):
    pages = [[release(id=7, tag_name=TAG + "-other")], [release()]]
    result, calls = run_attach(tmp_path, pages)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ["api", "--paginate", "--slurp", "repos/example/repo/releases?per_page=100"] in calls
    assert ["api", "repos/example/repo/releases/42"] in calls
    assert (tmp_path / "uploaded").exists()
    assert (tmp_path / "runner/verify/wolt_monitor.zip").read_bytes() == b"offline artifact"
    # Assert executed commands, not merely absence of suspicious strings. Only
    # shell guards, API operations and artifact integrity/readback tools may run.
    commands = re.findall(r"^\++ (\S+)", result.stderr, re.MULTILINE)
    assert commands
    assert set(commands) <= {
        "set",
        "cd",
        "sha256sum",
        "gh",
        "cat",
        "[[",
        "jq",
        "RELEASE_ID=42",
        "mkdir",
        "cmp",
    }


@pytest.mark.parametrize(
    "pages",
    [
        [[]],
        [[release(tag_name=TAG + "-other")]],
        [[release()], [release(id=43)]],
        [[release(draft=False)]],
        [[release(published_at="2026-01-01T00:00:00Z")]],
        [[release(assets=[{"name": "existing.zip"}])]],
        [[release()], [release(id=43, draft=False)]],
    ],
)
def test_attach_refuses_missing_ambiguous_published_or_nonempty_draft(tmp_path, pages):
    result, calls = run_attach(tmp_path, pages)
    assert result.returncode != 0
    assert not (tmp_path / "uploaded").exists()
    assert not any(call[:2] == ["release", "upload"] for call in calls)


@pytest.mark.parametrize(
    "selected",
    [
        release(tag_name=TAG + "-other"),
        release(draft=False),
        release(published_at="2026-01-01T00:00:00Z"),
        release(assets=[{"name": "racing-upload.zip"}]),
    ],
)
def test_attach_rechecks_selected_draft_immediately_before_upload(tmp_path, selected):
    result, calls = run_attach(tmp_path, [[release()]], selected=selected)
    assert result.returncode != 0
    assert ["api", "repos/example/repo/releases/42"] in calls
    assert not any(call[:2] == ["release", "upload"] for call in calls)


def test_attach_rechecks_tag_sha_before_upload(tmp_path):
    result, calls = run_attach(tmp_path, [[release()]], actual_sha="b" * 40)
    assert result.returncode != 0
    assert not any(call[:2] == ["release", "upload"] for call in calls)


def test_draft_discovery_and_target_execution_stay_on_opposite_token_sides():
    config = workflow()
    assert config["permissions"] == {"contents": "read"}
    package = config["jobs"]["package"]
    assert package.get("permissions", config["permissions"]) == {"contents": "read"}
    assert set(package["outputs"]) == {"sha"}
    for step in package["steps"]:
        assert "GH_TOKEN" not in step.get("env", {})
        assert "gh api" not in step.get("run", "")
    attach = config["jobs"]["attach"]
    assert attach["needs"] == "package"
    assert attach["permissions"] == {"contents": "write"}
    assert len(attach["steps"]) == 2
    download, upload = attach["steps"]
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"] == {"name": "release-assets", "path": "dist"}
    assert upload["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "GH_REPO": "${{ github.repository }}",
        "TAG": "${{ inputs.tag }}",
        "TARGET_SHA": "${{ needs.package.outputs.sha }}",
    }
