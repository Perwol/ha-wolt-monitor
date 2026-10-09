"""Sanitized-only writes in synthetic temporary storage, never real fixtures."""

import json
from datetime import timedelta

import pytest

from scripts import wolt_fixtures as fixtures


def stages(session):
    return [
        {
            "stage": "ready",
            "at": "2026-01-01T00:00:00+00:00",
            "responses": {
                "subscriptions": session.capture(
                    "subscriptions",
                    {
                        "order_details": [
                            {"order_id": "PRIVATE", "venue_name": "PRIVATE", "status": "ready"}
                        ],
                        "token": "PRIVATE",
                    },
                )
            },
        }
    ]


def test_session_saves_only_validated_snapshot_atomically_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", tmp_path / "fixtures" / "wolt", raising=False)
    session = fixtures.CaptureSession(offset=timedelta(days=30), provenance="observed")
    clean_stages = stages(session)
    path = session.save(clean_stages, slug="natural-order")
    data = path.read_bytes()
    assert b"PRIVATE" not in data
    assert json.loads(data) == session.dataset(clean_stages)
    assert path.name == "natural-order.json"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="Unable to save sanitized fixture") as caught:
        session.save(clean_stages, slug="natural-order")
    assert caught.value.__context__ is None
    assert path.read_bytes() == data
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


@pytest.mark.parametrize(
    "slug", ["../escape", "nested/name", "", ".hidden", "Upper", "name.json", "a" * 65, "PRIVATE\n"]
)
def test_writer_rejects_unsafe_slug_before_any_file_creation(tmp_path, monkeypatch, slug):
    base = tmp_path / "wolt"
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", base)
    session = fixtures.CaptureSession(offset=timedelta(0), provenance="synthetic")
    with pytest.raises(ValueError, match="Unable to save sanitized fixture"):
        session.save(stages(session), slug=slug)
    assert not base.exists()
    assert not (tmp_path / "escape.json").exists()


def test_writer_rejects_absolute_slug(tmp_path, monkeypatch):
    base = tmp_path / "wolt"
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", base)
    session = fixtures.CaptureSession(offset=timedelta(0), provenance="synthetic")
    with pytest.raises(ValueError, match="Unable to save sanitized fixture"):
        session.save(stages(session), slug=str(tmp_path / "escape"))
    assert not base.exists()
    assert not (tmp_path / "escape.json").exists()


@pytest.mark.parametrize("component", ["base", "ancestor"])
def test_writer_rejects_symlink_directories_without_touching_canary(
    tmp_path, monkeypatch, component
):
    external = tmp_path / "external"
    external.mkdir()
    canary = external / "canary"
    canary.write_bytes(b"PRIVATE")
    link = tmp_path / "link"
    link.symlink_to(external, target_is_directory=True)
    base = link if component == "base" else link / "wolt"
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", base)
    session = fixtures.CaptureSession(offset=timedelta(0), provenance="synthetic")
    with pytest.raises(ValueError, match="Unable to save sanitized fixture") as caught:
        session.save(stages(session), slug="safe")
    assert caught.value.__context__ is None
    assert canary.read_bytes() == b"PRIVATE"
    assert sorted(p.name for p in external.iterdir()) == ["canary"]


def test_writer_enforces_byte_budget_before_creating_directory(tmp_path, monkeypatch):
    base = tmp_path / "wolt"
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", base)
    monkeypatch.setattr(fixtures, "MAX_FIXTURE_BYTES", 16, raising=False)
    session = fixtures.CaptureSession(offset=timedelta(0), provenance="synthetic")
    with pytest.raises(ValueError, match="Unable to save sanitized fixture"):
        session.save(stages(session), slug="safe")
    assert not base.exists()


def test_temporary_name_collision_does_not_remove_preexisting_file(tmp_path, monkeypatch):
    from types import SimpleNamespace

    base = tmp_path / "wolt"
    base.mkdir()
    temporary = base / ".fixture-fixed.tmp"
    temporary.write_bytes(b"PRIVATE")
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", base)
    monkeypatch.setattr(fixtures.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    session = fixtures.CaptureSession(offset=timedelta(0), provenance="synthetic")
    with pytest.raises(ValueError, match="Unable to save sanitized fixture"):
        session.save(stages(session), slug="safe")
    assert temporary.read_bytes() == b"PRIVATE"
    assert not (base / "safe.json").exists()


def test_writer_syncs_published_directory(tmp_path, monkeypatch):
    import os
    import stat

    monkeypatch.setattr(fixtures, "FIXTURE_BASE", tmp_path / "wolt")
    real_sync = os.fsync
    synced = []

    def fsync(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return real_sync(fd)

    monkeypatch.setattr(fixtures.os, "fsync", fsync)
    session = fixtures.CaptureSession(offset=timedelta(0), provenance="synthetic")
    session.save(stages(session), slug="safe")
    assert synced == [False, True]


@pytest.mark.parametrize("failure", ["file_sync", "link", "directory_sync"])
def test_failure_cleanup_never_leaks_input_or_error_context(tmp_path, monkeypatch, caplog, failure):
    import os
    import stat

    base = tmp_path / "wolt"
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", base)
    real_sync = os.fsync

    def fsync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (failure == "file_sync" and not is_directory) or (
            failure == "directory_sync" and is_directory
        ):
            raise OSError("PRIVATE")
        return real_sync(fd)

    def link(*args, **kwargs):
        raise OSError("PRIVATE")

    monkeypatch.setattr(fixtures.os, "fsync", fsync)
    if failure == "link":
        monkeypatch.setattr(fixtures.os, "link", link)
    session = fixtures.CaptureSession(offset=timedelta(0), provenance="synthetic")
    with pytest.raises(ValueError, match="Unable to save sanitized fixture") as caught:
        session.save(stages(session), slug="safe")
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert "PRIVATE" not in repr(caught.value) + caplog.text
    paths = list(base.iterdir())
    if failure == "directory_sync":
        assert [path.name for path in paths] == ["safe.json"]
        assert b"PRIVATE" not in paths[0].read_bytes()
        fixtures.validate_dataset(json.loads(paths[0].read_bytes()))
    else:
        assert paths == []


@pytest.mark.parametrize("addition", ["metadata", "raw_order", "nested_token", "nan"])
def test_modified_stages_are_rejected_before_any_write(tmp_path, monkeypatch, addition):
    base = tmp_path / "wolt"
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", base)
    session = fixtures.CaptureSession(offset=timedelta(days=30), provenance="observed")
    clean = stages(session)
    if addition == "metadata":
        clean[0]["metadata"] = {"provenance": "observed", "token": "PRIVATE"}
    elif addition == "raw_order":
        clean[0]["responses"]["subscriptions"]["order_details"][0]["order_id"] = "PRIVATE"
    elif addition == "nested_token":
        clean[0]["responses"]["subscriptions"]["order_details"][0]["self_delivery"] = {
            "token": "PRIVATE"
        }
    else:
        clean[0]["responses"]["subscriptions"]["order_details"][0]["is_marketplace_v2"] = float(
            "nan"
        )
    with pytest.raises(ValueError, match="Unable to save sanitized fixture") as caught:
        session.save(clean, slug="safe")
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert not base.exists()


def test_existing_destination_symlink_never_changes_canary(tmp_path, monkeypatch):
    base = tmp_path / "wolt"
    base.mkdir()
    canary = tmp_path / "canary"
    canary.write_bytes(b"PRIVATE")
    (base / "safe.json").symlink_to(canary)
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", base)
    session = fixtures.CaptureSession(offset=timedelta(0), provenance="synthetic")
    with pytest.raises(ValueError, match="Unable to save sanitized fixture"):
        session.save(stages(session), slug="safe")
    assert canary.read_bytes() == b"PRIVATE"
    assert (base / "safe.json").is_symlink()
    assert [path.name for path in base.iterdir()] == ["safe.json"]


def test_caller_mutation_after_validation_cannot_change_published_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures, "FIXTURE_BASE", tmp_path / "wolt")
    session = fixtures.CaptureSession(offset=timedelta(0), provenance="synthetic")
    clean = stages(session)
    real_publish = fixtures._publish

    def publish(data, slug):
        clean[0]["responses"]["subscriptions"]["token"] = "PRIVATE"
        real_publish(data, slug)

    monkeypatch.setattr(fixtures, "_publish", publish)
    path = session.save(clean, slug="safe")
    assert b"PRIVATE" not in path.read_bytes()
    fixtures.validate_dataset(json.loads(path.read_bytes()))
