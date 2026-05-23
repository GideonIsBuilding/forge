"""
engine/preflight.py

Preflight checks run between pipeline parsing and job execution.

Responsibilities
----------------
- Resolve all pipeline dependencies transitively against the registry.
- Store the resulting lockfile in the run record.
- Translate resolution errors into the correct terminal run statuses
  (conflict_failure, cycle_failure) before raising, so callers don't
  need to touch the DB themselves.
"""

from __future__ import annotations

import logging

from engine.parser import Pipeline
from engine.runs import mark_run_status, store_lockfile
from registry import metadata
from registry.resolver import DependencyCycleError, VersionConflictError, resolve

logger = logging.getLogger(__name__)


class PreflightError(Exception):
    """Raised when preflight fails; the run status has already been updated."""


def run_preflight(pipeline: Pipeline, run_id: str) -> dict:
    """Resolve pipeline dependencies and persist the lockfile.

    Returns the lockfile dict on success so the caller can pass it straight
    into the scheduler without a round-trip DB read.

    On failure the run is marked conflict_failure or cycle_failure before
    PreflightError is raised — callers surface the message but skip any
    further DB writes.
    """
    direct_deps = [(dep.name, dep.version) for dep in pipeline.dependencies]

    if not direct_deps:
        lockfile: dict = {"resolved": []}
        store_lockfile(run_id, lockfile)
        logger.info("Run %s: no dependencies — empty lockfile stored", run_id)
        return lockfile

    logger.info(
        "Run %s: resolving %d direct dep(s): %s",
        run_id,
        len(direct_deps),
        ", ".join(f"{n}@{c}" for n, c in direct_deps),
    )

    try:
        lockfile = resolve(
            direct_deps=direct_deps,
            fetch_versions=metadata.list_versions,
            fetch_meta=_fetch_meta,
        )
    except VersionConflictError as exc:
        mark_run_status(run_id, "conflict_failure")
        logger.warning("Run %s: dependency conflict — %s", run_id, exc)
        raise PreflightError(str(exc)) from exc
    except DependencyCycleError as exc:
        mark_run_status(run_id, "cycle_failure")
        logger.warning("Run %s: dependency cycle — %s", run_id, exc)
        raise PreflightError(str(exc)) from exc

    store_lockfile(run_id, lockfile)
    n = len(lockfile.get("resolved", []))
    logger.info("Run %s: resolved %d package(s), lockfile stored", run_id, n)
    return lockfile


def _fetch_meta(name: str, version: str) -> tuple[str, list[tuple[str, str]]]:
    """Bridge between the resolver's generic protocol and the metadata layer."""
    artifact = metadata.get_artifact(name, version)
    if artifact is None:
        raise VersionConflictError(f"artifact {name}@{version} not found in registry")
    deps = [(d["name"], d["version"]) for d in artifact.deps]
    return artifact.sha256, deps
