"""
engine/deps.py

Materialize resolved pipeline dependencies into the job workspace.

Each locked artifact is downloaded from blob storage, SHA-256 verified,
and written atomically to workspace/deps/<name>/<name>-<version>.bin.
Materialisation is idempotent: an existing file with the correct checksum
is left untouched.

Public interface
----------------
materialize(lockfile, workspace)  ->  None
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from registry import metadata
from registry.storage import load_blob

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024


class DepChecksumMismatchError(Exception):
    """Blob on disk or pulled from storage does not match the lockfile SHA-256."""


class DepNotFoundError(Exception):
    """An artifact listed in the lockfile is absent from the registry."""


def materialize(lockfile: dict, workspace: Path) -> None:
    """Download and verify all resolved deps; write them under workspace/deps/.

    Layout:  workspace/deps/<name>/<name>-<version>.bin

    Raises DepNotFoundError         if an artifact is absent from the registry.
    Raises DepChecksumMismatchError if the registry SHA-256 disagrees with the
                                    lockfile, or if a pulled blob is corrupted.
    """
    entries = lockfile.get("resolved", [])
    if not entries:
        return

    deps_dir = workspace / "deps"
    deps_dir.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        name = entry["name"]
        version = entry["version"]
        expected_sha256 = entry["sha256"]

        artifact = metadata.get_artifact(name, version)
        if artifact is None:
            raise DepNotFoundError(f"{name}@{version} not found in registry")

        if artifact.sha256 != expected_sha256:
            raise DepChecksumMismatchError(
                f"{name}@{version}: lockfile expects sha256={expected_sha256} "
                f"but registry holds {artifact.sha256}"
            )

        dest_dir = deps_dir / name
        dest_dir.mkdir(exist_ok=True)
        dest_file = dest_dir / f"{name}-{version}.bin"

        if dest_file.exists() and _checksum_file(dest_file) == expected_sha256:
            logger.debug("Dep %s@%s already present, skipping pull", name, version)
            continue

        _pull_blob(artifact.sha256, expected_sha256, dest_file)
        logger.info("Materialized %s@%s -> %s", name, version, dest_file)


def _pull_blob(sha256: str, expected: str, dest: Path) -> None:
    """Stream blob from storage, verify SHA-256, write atomically to dest."""
    tmp = dest.with_suffix(".tmp")
    hasher = hashlib.sha256()

    try:
        with open(tmp, "wb") as f:
            for chunk in load_blob(sha256):
                hasher.update(chunk)
                f.write(chunk)

        actual = hasher.hexdigest()
        if actual != expected:
            raise DepChecksumMismatchError(
                f"blob checksum mismatch: expected {expected}, got {actual}"
            )

        tmp.rename(dest)

    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _checksum_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file on disk."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()
