"""
engine/runs.py

Run lifecycle management.
The DB must already be initialised (via db.init()) before any function here
is called — either by the FastAPI lifespan handler in production or by the
test fixture in tests.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from registry import metadata

logger = logging.getLogger(__name__)

STATUSES = {
    "queued", "running", "succeeded", "failed",
    "integrity_failure", "conflict_failure", "cycle_failure",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(pipeline_yaml: str, pipeline_name: str) -> str:
    """Persist a new run record and return its run_id."""
    run_id = str(uuid.uuid4())
    metadata.create_run(
        run_id=run_id,
        pipeline_name=pipeline_name,
        pipeline_yaml=pipeline_yaml,
    )
    logger.info("Run %s created (queued)", run_id)
    return run_id


def get_run(run_id: str) -> Optional[metadata.RunRow]:
    """Return the current state of a run, or None if not found."""
    return metadata.get_run(run_id)


def get_run_jobs(run_id: str) -> List[metadata.JobRow]:
    """Return all jobs belonging to a run."""
    return metadata.list_jobs(run_id)


def get_lockfile(run_id: str) -> Optional[Dict]:
    """Return the resolved lockfile for a run, or None if not yet resolved."""
    run = metadata.get_run(run_id)
    return run.lockfile if run else None


def submit_run(pipeline_yaml: str, pipeline_name: str) -> str:
    """Create a run record and enqueue it for execution."""
    run_id = create_run(pipeline_yaml, pipeline_name)
    logger.info("Run %s submitted — scheduler handoff pending Phase 1", run_id)
    return run_id


def mark_run_status(
    run_id: str,
    status: str,
    duration_s: Optional[float] = None,
) -> None:
    """Update a run's status."""
    assert status in STATUSES, f"Invalid status: {status}"
    metadata.update_run_status(run_id, status, duration_s)
    logger.info("Run %s -> %s", run_id, status)


def store_lockfile(run_id: str, lockfile: Dict) -> None:
    """Persist the resolved lockfile against a run record."""
    metadata.set_run_lockfile(run_id, lockfile)
    logger.info("Lockfile stored for run %s", run_id)
