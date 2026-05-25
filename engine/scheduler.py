from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional
from engine import slack
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from engine import deps as dep_materializer
from engine.logs import close_log, register_job
from engine.parser import Job, Pipeline
from engine.runner import JobResult
from engine import runs as run_lifecycle
from engine import publisher as artifact_publisher

from registry import metadata

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SchedulerError(ValueError):
    pass


class JobCycleError(SchedulerError):
    pass


class RunnerProtocol(Protocol):
    async def run_job(
        self,
        job: Job,
        workspace: Path,
        forge_url: str,
        forge_token: str,
        run_id: str,
    ) -> JobResult: ...



def topological_batches(jobs: dict[str, list[str]]) -> list[list[str]]:
    """Return parallel-runnable batches in dependency order.

    Each batch contains jobs whose dependencies all appear in earlier batches.
    Within a batch, all jobs can start simultaneously.

    Raises SchedulerError  on unknown dependency references.
    Raises JobCycleError   on dependency cycles (detected via Kahn's algorithm).
    """
    missing = {dep for deps in jobs.values() for dep in deps if dep not in jobs}
    if missing:
        raise SchedulerError(f"unknown job dependency: {', '.join(sorted(missing))}")

    indegree: dict[str, int] = {name: len(needs) for name, needs in jobs.items()}
    dependents: dict[str, list[str]] = defaultdict(list)
    for name, needs in jobs.items():
        for dep in needs:
            dependents[dep].append(name)

    ready: deque[str] = deque(sorted(name for name, deg in indegree.items() if deg == 0))
    batches: list[list[str]] = []
    visited = 0

    while ready:
        batch = list(ready)
        ready.clear()
        batches.append(batch)
        for name in batch:
            visited += 1
            for dependent in sorted(dependents[name]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)

    if visited != len(jobs):
        cycle_nodes = sorted(name for name, deg in indegree.items() if deg > 0)
        raise JobCycleError(f"job dependency cycle detected: {' -> '.join(cycle_nodes)}")

    return batches


# ---------------------------------------------------------------------------
# Pipeline execution orchestrator
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def execute_pipeline(
    pipeline: Pipeline,
    run_id: str,
    runner: RunnerProtocol,
    *,
    workspace: Path,
    publisher_workspace: Path | None = None,
    forge_url: str,
    forge_token: str,
    max_concurrency: int,
) -> str:
    """Drive a parsed pipeline to completion and return the final run status.

    Job execution model
    -------------------
    Every job gets its own asyncio.Event. A job coroutine waits on each
    dependency's event before deciding whether to run or skip. Once all deps
    have fired their events the coroutine checks their outcomes:

      - All deps succeeded  -> acquire the semaphore, run the job
      - Any dep failed/skipped -> mark self as "skipped", fire own event

    Because skipped jobs immediately fire their event, the skip propagates
    transitively through the dependency graph without any explicit recursive
    walk. Only the job that actually broke is marked "failed".

    The asyncio.Semaphore(max_concurrency) caps how many jobs run Docker
    containers simultaneously. Waiting and skipped jobs never hold a slot.

    Status transitions
    ------------------
      Run: queued -> running -> succeeded | failed | cycle_failure
      Job: queued -> running -> succeeded | failed | skipped

    Raises JobCycleError if the dependency graph has a cycle (run is marked
    cycle_failure before raising so the DB reflects the outcome).
    """
    jobs = pipeline.jobs
    needs_map = {name: list(job.needs) for name, job in jobs.items()}

    # Guard against cycles before touching the runner or writing job records.
    # A cycle would cause coroutines to deadlock on event.wait() indefinitely.
    try:
        topological_batches(needs_map)
    except JobCycleError as exc:
        run_lifecycle.mark_run_status(run_id, "cycle_failure")
        slack.notify_resolution_failure(
            run_id=run_id,
            pipeline_name=pipeline.name,
            detail=str(exc),
        )
        raise

    # Persist all jobs as "queued" upfront. Clients polling GET /runs/{id}
    # see the complete job list immediately, not incrementally as jobs start.
    for name, job in jobs.items():
        metadata.create_job(
            run_id=run_id,
            name=name,
            needs=list(job.needs),
            runtime=job.runtime,
        )

    # Materialize resolved dependencies into workspace/deps/ before any job
    # starts. This is blocking I/O so it runs in the default executor.
    run_row = metadata.get_run(run_id)
    if run_row and run_row.lockfile and run_row.lockfile.get("resolved"):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            dep_materializer.materialize,
            run_row.lockfile,
            workspace,
        )
    # Mark run running + Slack alert.
    run_lifecycle.mark_run_status(run_id, "running")
    slack.notify_pipeline_started(run_id=run_id, pipeline_name=pipeline.name)
    run_start = time.monotonic()
    logger.info(
        "Run %s started — %d job(s), max_concurrency=%d",
        run_id,
        len(jobs),
        max_concurrency,
    )

    # Per-job completion signal. A job sets its event when it finishes,
    # regardless of outcome. Dependents wake on the event then inspect
    # job_statuses to decide whether to run or skip.
    done_events: dict[str, asyncio.Event] = {name: asyncio.Event() for name in jobs}
    job_statuses: dict[str, str] = {}

    # Concurrency cap: only max_concurrency jobs may run Docker containers at once.
    # Acquired inside the coroutine after all deps have succeeded.
    semaphore = asyncio.Semaphore(max_concurrency)

    # Track the first failing job name for the Slack alert.
    first_failing_job: Optional[str] = None

    async def run_one(job_name: str) -> None:
        nonlocal first_failing_job
        job = jobs[job_name]

        # Wait for each dependency in declaration order. Bail out on the first
        # non-success outcome — there is no point waiting for the rest.
        for dep in job.needs:
            await done_events[dep].wait()
            if job_statuses.get(dep) != "succeeded":
                job_statuses[job_name] = "skipped"
                metadata.update_job_status(run_id, job_name, "skipped")
                logger.info(
                    "Job %s/%s skipped (dep '%s' %s)",
                    run_id,
                    job_name,
                    dep,
                    job_statuses.get(dep),
                )
                done_events[job_name].set()
                return

        # All deps succeeded. Register the log watcher before acquiring the
        # semaphore so SSE clients connecting now will wait for lines rather
        # than see no watcher and assume the job is already finished.
        register_job(run_id, job_name)

        async with semaphore:
            started = _now()
            metadata.update_job_status(run_id, job_name, "running", started_at=started)
            logger.info("Job %s/%s running", run_id, job_name)

            exit_code = -1
            try:
                result = await runner.run_job(
                    job, workspace, forge_url, forge_token, run_id
                )  # NOTE
                exit_code = result.exit_code
            except Exception:
                logger.exception("Job %s/%s raised an unexpected exception", run_id, job_name)
            finally:
                finished = _now()
                outcome = "succeeded" if exit_code == 0 else "failed"
                job_statuses[job_name] = outcome
                metadata.update_job_status(
                    run_id,
                    job_name,
                    outcome,
                    exit_code=exit_code,
                    started_at=started,
                    finished_at=finished,
                )
                # close_log() must come before done_events.set() so that
                # SSE threads receive their shutdown signal before the scheduler
                # coroutines for dependent jobs are unblocked.
                close_log(run_id, job_name)
                done_events[job_name].set()
                logger.info("Job %s/%s %s (exit_code=%d)", run_id, job_name, outcome, exit_code)

    # Launch every job coroutine simultaneously. They self-regulate:
    #   - event.wait() blocks a coroutine until its deps complete
    #   - the semaphore caps the number actually running Docker at once
    # TaskGroup cancels remaining tasks if a coroutine raises unexpectedly.
    async with asyncio.TaskGroup() as tg:
        for name in jobs:
            tg.create_task(run_one(name))

    duration_s = time.monotonic() - run_start
    final_status = "failed" if any(s == "failed" for s in job_statuses.values()) else "succeeded"

    # 6. Auto-publish declared artifacts if all jobs succeeded.
    if final_status == "succeeded" and pipeline.artifacts:
        try:
            published = artifact_publisher.publish_pipeline_artifacts(
                pipeline=pipeline,
                workspace=publisher_workspace or workspace,
                run_id=run_id,
                forge_url=forge_url,
                forge_token=forge_token,
            )
            logger.info("Run %s: auto-published %d artifact(s)", run_id, len(published))
        except artifact_publisher.PublishError as exc:
            logger.error("Run %s: auto-publish failed — %s", run_id, exc)
            final_status = "failed"

    run_lifecycle.mark_run_status(run_id, final_status, duration_s)

    if final_status == "succeeded":
        slack.notify_pipeline_succeeded(
            run_id=run_id,
            pipeline_name=pipeline.name,
            duration_s=duration_s,
        )
    else:
        slack.notify_pipeline_failed(
            run_id=run_id,
            pipeline_name=pipeline.name,
            duration_s=duration_s,
            failing_job=first_failing_job,
        )

    logger.info("Run %s completed: %s (%.2fs)", run_id, final_status, duration_s)
    return final_status
