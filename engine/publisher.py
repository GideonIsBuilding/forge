"""
engine/publisher.py

Automatic artifact publisher for the Forge CI/CD platform.

After all jobs succeed, this module reads the pipeline's `artifacts` list,
locates each declared file in the job workspace, and publishes it directly
to the registry layer — no HTTP round-trip.

Design
------
- Publishing is done *after* the scheduler confirms all jobs succeeded, so a
  partial build never produces registry artifacts.
- SHA-256 is stream-computed; we never load a large tarball fully into memory.
- If an artifact already exists (duplicate) and the SHA-256 matches, we treat
  it as a no-op success (idempotent publish).
  If the SHA-256 differs we raise PublishError — immutability violation.

Public interface
----------------
  publish_pipeline_artifacts(pipeline, workspace, run_id) -> list[dict]
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from engine.parser import Artifact, Pipeline
from registry.metadata import DuplicateArtifactError, get_artifact, put_artifact
from registry.storage import ChecksumMismatchError, save_blob

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024


class PublishError(Exception):
    """Raised when an artifact cannot be published to the registry."""


def publish_pipeline_artifacts(
    pipeline: Pipeline,
    workspace: Path,
    run_id: str,
    forge_url: str | None = None,
    forge_token: str | None = None,
) -> list[dict]:
    """
    Publish every artifact declared in pipeline.artifacts to the registry.

    forge_url and forge_token are accepted but unused — publishing goes
    directly through the registry layer, not via HTTP. They are accepted
    so the scheduler can pass them without needing to know the implementation.

    Returns a list of dicts with published artifact metadata.
    Raises PublishError on any failure.
    """
    if not pipeline.artifacts:
        logger.info("Run %s: no artifacts declared, skipping publish", run_id)
        return []

    results: list[dict] = []
    for artifact in pipeline.artifacts:
        meta = _publish_one(artifact=artifact, workspace=workspace, run_id=run_id)
        results.append(meta)
        logger.info(
            "Run %s: published %s@%s (sha256=%s)",
            run_id,
            artifact.name,
            artifact.version,
            meta["sha256"],
        )

    return results


def _publish_one(artifact: Artifact, workspace: Path, run_id: str) -> dict:
    raw_path = artifact.path.lstrip("/")
    if raw_path.startswith("workspace/"):
        raw_path = raw_path[len("workspace/"):]

    artifact_file = (workspace / raw_path).resolve()

    try:
        artifact_file.relative_to(workspace.resolve())
    except ValueError:
        raise PublishError(
            f"Artifact path escapes workspace: {artifact.path!r} resolved to {artifact_file}"
        )

    if not artifact_file.exists():
        raise PublishError(
            f"Artifact file not found: {artifact_file} "
            f"(declared path: {artifact.path!r}, run: {run_id})"
        )

    sha256 = _compute_sha256(artifact_file)
    size = artifact_file.stat().st_size

    try:
        with open(artifact_file, "rb") as f:
            save_blob(f, f"sha256:{sha256}")

        put_artifact(
            name=artifact.name,
            version=artifact.version,
            sha256=sha256,
            size=size,
            publisher=f"pipeline:{run_id}",
        )
        return {
            "name": artifact.name,
            "version": artifact.version,
            "sha256": sha256,
            "size": size,
        }

    except ChecksumMismatchError as exc:
        raise PublishError(
            f"Checksum mismatch publishing {artifact.name}@{artifact.version}: {exc}"
        )

    except DuplicateArtifactError:
        existing = get_artifact(artifact.name, artifact.version)
        if existing and existing.sha256 == sha256:
            logger.info(
                "Run %s: %s@%s already exists with matching SHA-256, skipping",
                run_id, artifact.name, artifact.version,
            )
            return {
                "name": artifact.name,
                "version": artifact.version,
                "sha256": sha256,
                "size": size,
            }
        raise PublishError(
            f"{artifact.name}@{artifact.version} already exists with a different "
            f"SHA-256 — immutability violation in run {run_id}"
        )


def _compute_sha256(path: Path) -> str:
    """Stream-compute SHA-256 without loading the file into memory."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()
