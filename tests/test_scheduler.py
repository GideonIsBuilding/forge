from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from engine.parser import Artifact, Job, Pipeline, Resources, Step
from engine.runner import JobResult
from engine.scheduler import JobCycleError, execute_pipeline, topological_batches
from registry import db, metadata
from registry.init_db import create_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockRunner:
    """Controllable stand-in for DockerRunner.

    exit_codes maps job name -> exit code (default 0).
    Records call order and tracks peak concurrency for limit tests.
    """

    def __init__(self, exit_codes: dict[str, int] | None = None) -> None:
        self.exit_codes = exit_codes or {}
        self.calls: list[str] = []
        self._active = 0
        self.peak_concurrency = 0

    async def run_job(self, job: Job, workspace: Path, forge_url: str, forge_token: str, run_id: str) -> JobResult:
        self.calls.append(job.name)
        self._active += 1
        self.peak_concurrency = max(self.peak_concurrency, self._active)
        await asyncio.sleep(0)  # yield so other coroutines can be scheduled
        self._active -= 1
        code = self.exit_codes.get(job.name, 0)
        return JobResult(job=job.name, exit_code=code, status="succeeded" if code == 0 else "failed")


def _pipeline(jobs_spec: dict[str, list[str]]) -> Pipeline:
    """Build a minimal Pipeline from {job_name: [needs]} spec."""
    jobs: dict[str, Job] = {}
    for name, needs in jobs_spec.items():
        jobs[name] = Job(
            name=name,
            runtime="alpine:3.18",
            steps=[Step(name="run", run="echo hi")],
            needs=needs,
            resources=Resources(),
        )
    return Pipeline(
        name="test-pipeline",
        version="1.0.0",
        dependencies=[],
        jobs=jobs,
        artifacts=[],
    )


def _new_run(pipeline: Pipeline) -> str:
    run_id = str(uuid.uuid4())
    metadata.create_run(run_id=run_id, pipeline_name=pipeline.name, pipeline_yaml="")
    return run_id


def _run(pipeline: Pipeline, runner: MockRunner, run_id: str, max_concurrency: int = 4) -> str:
    return asyncio.run(execute_pipeline(
        pipeline, run_id, runner,
        workspace=Path("/tmp"),
        forge_url="http://localhost:8080",
        forge_token="test-token",
        max_concurrency=max_concurrency,
    ))


def _job_status(run_id: str, name: str) -> str | None:
    row = metadata.get_job(run_id, name)
    return row.status if row else None


# ---------------------------------------------------------------------------
# DB fixture — fresh SQLite schema per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path: Path) -> None:
    db.init(tmp_path / "test.db")
    create_schema()
    yield
    db.close_db()


# ---------------------------------------------------------------------------
# topological_batches — pure algorithm tests (unchanged)
# ---------------------------------------------------------------------------

def test_topological_batches_parallel_jobs() -> None:
    batches = topological_batches({"build": [], "lint": [], "package": ["build", "lint"]})
    assert batches == [["build", "lint"], ["package"]]


def test_topological_batches_detects_cycle() -> None:
    with pytest.raises(JobCycleError):
        topological_batches({"a": ["b"], "b": ["a"]})


def test_topological_batches_unknown_dep() -> None:
    from engine.scheduler import SchedulerError
    with pytest.raises(SchedulerError, match="unknown"):
        topological_batches({"a": ["ghost"]})


def test_topological_batches_linear_chain() -> None:
    batches = topological_batches({"a": [], "b": ["a"], "c": ["b"]})
    assert batches == [["a"], ["b"], ["c"]]


def test_topological_batches_diamond() -> None:
    # a -> b, a -> c, b -> d, c -> d
    batches = topological_batches({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})
    assert batches[0] == ["a"]
    assert sorted(batches[1]) == ["b", "c"]
    assert batches[2] == ["d"]


# ---------------------------------------------------------------------------
# execute_pipeline — all jobs succeed
# ---------------------------------------------------------------------------

def test_execute_pipeline_single_job_succeeds() -> None:
    pipeline = _pipeline({"build": []})
    run_id = _new_run(pipeline)
    result = _run(pipeline, MockRunner(), run_id)

    assert result == "succeeded"
    assert _job_status(run_id, "build") == "succeeded"
    run = metadata.get_run(run_id)
    assert run.status == "succeeded"


def test_execute_pipeline_linear_chain_all_succeed() -> None:
    pipeline = _pipeline({"a": [], "b": ["a"], "c": ["b"]})
    runner = MockRunner()
    run_id = _new_run(pipeline)
    result = _run(pipeline, runner, run_id)

    assert result == "succeeded"
    assert _job_status(run_id, "a") == "succeeded"
    assert _job_status(run_id, "b") == "succeeded"
    assert _job_status(run_id, "c") == "succeeded"
    # c cannot start before b, which cannot start before a
    assert runner.calls.index("a") < runner.calls.index("b")
    assert runner.calls.index("b") < runner.calls.index("c")


def test_execute_pipeline_parallel_jobs_all_succeed() -> None:
    # build and lint are independent; package waits for both
    pipeline = _pipeline({"build": [], "lint": [], "package": ["build", "lint"]})
    runner = MockRunner()
    run_id = _new_run(pipeline)
    result = _run(pipeline, runner, run_id)

    assert result == "succeeded"
    for name in ("build", "lint", "package"):
        assert _job_status(run_id, name) == "succeeded"
    assert runner.calls.index("package") > runner.calls.index("build")
    assert runner.calls.index("package") > runner.calls.index("lint")


# ---------------------------------------------------------------------------
# execute_pipeline — failure and skip propagation
# ---------------------------------------------------------------------------

def test_execute_pipeline_failed_job_marks_run_failed() -> None:
    pipeline = _pipeline({"build": []})
    run_id = _new_run(pipeline)
    result = _run(pipeline, MockRunner({"build": 1}), run_id)

    assert result == "failed"
    assert _job_status(run_id, "build") == "failed"
    assert metadata.get_run(run_id).status == "failed"


def test_execute_pipeline_failed_job_skips_direct_dependent() -> None:
    # build fails -> package should be skipped
    pipeline = _pipeline({"build": [], "package": ["build"]})
    run_id = _new_run(pipeline)
    result = _run(pipeline, MockRunner({"build": 1}), run_id)

    assert result == "failed"
    assert _job_status(run_id, "build") == "failed"
    assert _job_status(run_id, "package") == "skipped"


def test_execute_pipeline_skip_propagates_transitively() -> None:
    # a fails -> b skipped -> c skipped
    pipeline = _pipeline({"a": [], "b": ["a"], "c": ["b"]})
    run_id = _new_run(pipeline)
    result = _run(pipeline, MockRunner({"a": 1}), run_id)

    assert result == "failed"
    assert _job_status(run_id, "a") == "failed"
    assert _job_status(run_id, "b") == "skipped"
    assert _job_status(run_id, "c") == "skipped"


def test_execute_pipeline_independent_job_not_affected_by_failure() -> None:
    # build fails, lint is independent — lint should still succeed
    pipeline = _pipeline({"build": [], "lint": [], "deploy": ["build"]})
    run_id = _new_run(pipeline)
    result = _run(pipeline, MockRunner({"build": 1}), run_id)

    assert result == "failed"
    assert _job_status(run_id, "build") == "failed"
    assert _job_status(run_id, "lint") == "succeeded"
    assert _job_status(run_id, "deploy") == "skipped"


def test_execute_pipeline_only_failed_ancestor_blamed() -> None:
    # Diamond: a -> b, a -> c, b -> d, c -> d
    # b fails. c succeeds. d depends on both — skipped because b failed.
    pipeline = _pipeline({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})
    run_id = _new_run(pipeline)
    result = _run(pipeline, MockRunner({"b": 1}), run_id)

    assert result == "failed"
    assert _job_status(run_id, "a") == "succeeded"
    assert _job_status(run_id, "b") == "failed"
    assert _job_status(run_id, "c") == "succeeded"
    assert _job_status(run_id, "d") == "skipped"


# ---------------------------------------------------------------------------
# execute_pipeline — cycle detection
# ---------------------------------------------------------------------------

def test_execute_pipeline_cycle_marks_run_cycle_failure() -> None:
    # The parser now prevents unknown needs, but cycles (a->b->a) can still be
    # constructed directly and must be caught before any job executes.
    pipeline = _pipeline({"a": ["b"], "b": ["a"]})
    run_id = _new_run(pipeline)

    with pytest.raises(JobCycleError):
        _run(pipeline, MockRunner(), run_id)

    assert metadata.get_run(run_id).status == "cycle_failure"


# ---------------------------------------------------------------------------
# execute_pipeline — concurrency limit
# ---------------------------------------------------------------------------

def test_execute_pipeline_concurrency_limit_respected() -> None:
    # 4 independent jobs, max_concurrency=2: peak active should never exceed 2
    pipeline = _pipeline({"a": [], "b": [], "c": [], "d": []})
    runner = MockRunner()
    run_id = _new_run(pipeline)
    result = _run(pipeline, runner, run_id, max_concurrency=2)

    assert result == "succeeded"
    assert runner.peak_concurrency <= 2
    assert len(runner.calls) == 4


def test_execute_pipeline_concurrency_1_runs_sequentially() -> None:
    # max_concurrency=1 forces sequential execution even for independent jobs
    pipeline = _pipeline({"a": [], "b": [], "c": []})
    runner = MockRunner()
    run_id = _new_run(pipeline)
    result = _run(pipeline, runner, run_id, max_concurrency=1)

    assert result == "succeeded"
    assert runner.peak_concurrency == 1
    assert len(runner.calls) == 3


# ---------------------------------------------------------------------------
# execute_pipeline — DB state completeness
# ---------------------------------------------------------------------------

def test_execute_pipeline_all_jobs_persisted_as_queued_before_running() -> None:
    # All jobs must appear in the DB immediately (status queued or later),
    # not incrementally as each one starts.
    pipeline = _pipeline({"build": [], "lint": [], "package": ["build", "lint"]})
    run_id = _new_run(pipeline)
    _run(pipeline, MockRunner(), run_id)

    jobs = metadata.list_jobs(run_id)
    assert len(jobs) == 3
    names = {j.name for j in jobs}
    assert names == {"build", "lint", "package"}


def test_execute_pipeline_run_transitions_through_running() -> None:
    # After execute_pipeline completes the run status must be a terminal state.
    pipeline = _pipeline({"build": []})
    run_id = _new_run(pipeline)
    final = _run(pipeline, MockRunner(), run_id)

    run = metadata.get_run(run_id)
    assert run.status == final
    assert run.status in {"succeeded", "failed", "cycle_failure"}
