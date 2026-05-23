"""
tests/test_deps.py

Unit tests for engine/deps.py — dependency materialisation.

Each test gets:
  tmp_db   — fresh SQLite schema for artifact metadata
  tmp_blobs — temporary blob-storage directory (patched into engine.config)
  tmp_path  — workspace root

Blob data is written via registry.storage.save_blob so the full
checksum-and-store path is exercised, mirroring real publish flows.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

import engine.config as cfg
from engine.deps import (
    DepChecksumMismatchError,
    DepNotFoundError,
    materialize,
)
from registry import db, metadata
from registry.init_db import create_schema
from registry.storage import save_blob


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path: Path) -> None:
    db.init(tmp_path / "test.db")
    create_schema()
    yield
    db.close_db()


@pytest.fixture(autouse=True)
def tmp_blobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    monkeypatch.setattr(cfg, "blob_dir", lambda: str(blob_dir))
    return blob_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store_artifact(name: str, version: str, content: bytes) -> str:
    """Save blob to storage and register artifact in metadata. Returns sha256."""
    sha256 = hashlib.sha256(content).hexdigest()
    save_blob(io.BytesIO(content), sha256)
    metadata.put_artifact(name=name, version=version, sha256=sha256,
                          size=len(content), publisher="test")
    return sha256


def _lockfile(*entries: dict) -> dict:
    return {"resolved": list(entries)}


def _entry(name: str, version: str, sha256: str) -> dict:
    return {"name": name, "version": version, "sha256": sha256}


# ---------------------------------------------------------------------------
# Empty lockfile
# ---------------------------------------------------------------------------

def test_materialize_empty_lockfile_is_noop(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    materialize({"resolved": []}, workspace)
    # deps/ directory should NOT be created for an empty lockfile
    assert not (workspace / "deps").exists()


def test_materialize_missing_resolved_key_is_noop(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    materialize({}, workspace)
    assert not (workspace / "deps").exists()


# ---------------------------------------------------------------------------
# Successful materialisation
# ---------------------------------------------------------------------------

def test_materialize_single_artifact(tmp_path: Path) -> None:
    content = b"hello forge"
    sha256 = _store_artifact("mylib", "1.0.0", content)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    materialize(_lockfile(_entry("mylib", "1.0.0", sha256)), workspace)

    dest = workspace / "deps" / "mylib" / "mylib-1.0.0.bin"
    assert dest.exists()
    assert dest.read_bytes() == content


def test_materialize_multiple_artifacts(tmp_path: Path) -> None:
    sha_a = _store_artifact("libA", "1.0.0", b"data-A")
    sha_b = _store_artifact("libB", "2.3.0", b"data-B")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    materialize(
        _lockfile(_entry("libA", "1.0.0", sha_a), _entry("libB", "2.3.0", sha_b)),
        workspace,
    )

    assert (workspace / "deps" / "libA" / "libA-1.0.0.bin").exists()
    assert (workspace / "deps" / "libB" / "libB-2.3.0.bin").exists()


def test_materialize_creates_deps_directory(tmp_path: Path) -> None:
    sha = _store_artifact("pkg", "0.1.0", b"data")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    materialize(_lockfile(_entry("pkg", "0.1.0", sha)), workspace)

    assert (workspace / "deps").is_dir()


def test_materialize_file_content_matches_original(tmp_path: Path) -> None:
    content = b"\x00\x01\x02" * 1024
    sha = _store_artifact("bin", "3.0.0", content)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    materialize(_lockfile(_entry("bin", "3.0.0", sha)), workspace)

    pulled = (workspace / "deps" / "bin" / "bin-3.0.0.bin").read_bytes()
    assert pulled == content
    assert hashlib.sha256(pulled).hexdigest() == sha


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_materialize_idempotent_skips_existing_correct_file(tmp_path: Path) -> None:
    content = b"idempotent"
    sha = _store_artifact("pkg", "1.0.0", content)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    materialize(_lockfile(_entry("pkg", "1.0.0", sha)), workspace)
    dest = workspace / "deps" / "pkg" / "pkg-1.0.0.bin"
    mtime_first = dest.stat().st_mtime

    # Second call — file exists with correct checksum, should be a no-op.
    materialize(_lockfile(_entry("pkg", "1.0.0", sha)), workspace)
    assert dest.stat().st_mtime == mtime_first


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_materialize_missing_artifact_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(DepNotFoundError):
        materialize(_lockfile(_entry("ghost", "9.9.9", "abc123")), workspace)


def test_materialize_lockfile_sha_mismatch_with_registry_raises(tmp_path: Path) -> None:
    content = b"real content"
    real_sha = _store_artifact("pkg", "1.0.0", content)
    # Provide a wrong sha256 in the lockfile entry
    wrong_sha = "a" * 64
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(DepChecksumMismatchError):
        materialize(_lockfile(_entry("pkg", "1.0.0", wrong_sha)), workspace)
