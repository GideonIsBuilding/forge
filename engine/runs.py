"""
engine/runs.py

Run lifecycle management — the bridge between the HTTP layer and the
scheduler/executor. Stage 0: public interface stubs only.
Full implementation wired to scheduler in Phase 1.
"""

import logging
import uuid
from datetime import datetime, timezone

from engine import config
from registry import db, metadata

logger = logging.getLogger(__name__)

# Valid run status values
STATUSES = {
    "queued",
    "running",
    "succeeded",
    "failed",
    "integrity_failure",
    "conflict_failure",
    "cycle_failure",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(pipeline_yaml: str, pipeline_name: str) -> str:
    """
    Persist a new run record and return its run_id.
    Called by POST /runs immediately after the pipeline is uploaded.
    """
    db.init(config.db_path())
    run_id = str(uuid.uuid4())
    metadata.create_run(
        run_id=run_id,
        pipeline_name=pipeline_name,
        pipeline_yaml=pipeline_yaml,
    )
    logger.info("Run %s created (queued)", run_id)
    return run_id


def get_run(run_id: str) -> metadata.RunRow | None:
    """
    Return the current state of a run, or None if not found.
    Called by GET /runs/{id}.
    """
    db.init(config.db_path())
    return metadata.get_run(run_id)


def get_run_jobs(run_id: str) -> list[metadata.JobRow]:
    """
    Return all jobs belonging to a run.
    Called by GET /runs/{id} to build the jobs list in the response.
    """
    db.init(config.db_path())
    return metadata.list_jobs(run_id)


def get_lockfile(run_id: str) -> dict | None:
    """
    Return the resolved lockfile for a run, or None if not yet resolved.
    Called by GET /runs/{id}/lockfile.
    """
    run = metadata.get_run(run_id)
    return run.lockfile if run else None


def submit_run(pipeline_yaml: str, pipeline_name: str) -> str:
    """
    Create a run record and enqueue it for execution.
    Returns the run_id. Actual scheduling wired in Phase 1.
    """
    run_id = create_run(pipeline_yaml, pipeline_name)
    # TODO (Phase 1): hand run_id off to the scheduler
    logger.info("Run %s submitted — scheduler handoff pending Phase 1", run_id)
    return run_id


def mark_run_status(
    run_id: str,
    status: str,
    duration_s: float | None = None,
) -> None:
    """
    Update a run's status. Used by the scheduler and runner in Phase 1.
    """
    assert status in STATUSES, f"Invalid status: {status}"
    metadata.update_run_status(run_id, status, duration_s)
    logger.info("Run %s → %s", run_id, status)


def store_lockfile(run_id: str, lockfile: dict) -> None:
    """
    Persist the resolved lockfile against a run record.
    Called by the resolver before any job starts.
    """
    metadata.set_run_lockfile(run_id, lockfile)
    logger.info("Lockfile stored for run %s", run_id)
