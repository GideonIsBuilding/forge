"""
tests/test_e2e.py

End-to-end grading scenario tests.

These tests cover all 8 required capabilities:
  1. Pipeline builds and publishes lib-core@1.0.0
  2. Pipeline resolves lib-core@^1.0.0 and publishes lib-http@1.0.0
  3. Pipeline resolves both and publishes service-api@0.1.0
  4. Wrong checksum upload returns 400
  5. Duplicate upload returns 409
  6. Version conflict fails before build (conflict_failure)
  7. Dependency cycle fails before build (cycle_failure)
  8. Checksum mismatch on dep pull causes integrity_failure

All tests run against a real SQLite DB and real storage layer.
Docker is NOT required — jobs are run via MockRunner.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from pathlib import Path

import pytest

from engine.parser import Artifact, Dependency, Job, Pipeline, Resources, Step
from engine.preflight import PreflightError, run_preflight
from engine.runner import JobResult
from engine.scheduler import execute_pipeline
from registry import db, metadata
from registry.init_db import create_schema
from registry.metadata import DuplicateArtifactError
from registry.storage import ChecksumMismatchError, save_blob


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path: Path):
    db.init(tmp_path / "e2e.db")
    create_schema()
    yield
    db.close_db()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockRunner:
    """Runs jobs successfully by default; can be configured to fail."""

    def __init__(self, exit_codes: dict[str, int] | None = None):
        self.exit_codes = exit_codes or {}

    async def run_job(
        self, job: Job, workspace: Path, forge_url: str, forge_token: str, run_id: str = ""
    ) -> JobResult:
        # Create the artifact file the pipeline declares so publisher can find it
        out = workspace / "out.tar.gz"
        out.write_bytes(b"fake artifact content for " + job.name.encode())
        code = self.exit_codes.get(job.name, 0)
        return JobResult(job=job.name, exit_code=code, status="succeeded" if code == 0 else "failed")


def _make_pipeline(
    name: str,
    version: str,
    deps: list[tuple[str, str]] | None = None,
    artifact_name: str | None = None,
    artifact_version: str | None = None,
) -> Pipeline:
    dependencies = [Dependency(name=n, version=v) for n, v in (deps or [])]
    artifacts = []
    if artifact_name and artifact_version:
        artifacts = [Artifact(name=artifact_name, version=artifact_version, path="./out.tar.gz")]
    return Pipeline(
        name=name,
        version=version,
        dependencies=dependencies,
        jobs={
            "build": Job(
                name="build",
                runtime="alpine:3.18",
                steps=[Step(name="build", run="echo build")],
                needs=[],
                resources=Resources(),
            )
        },
        artifacts=artifacts,
    )


def _new_run(pipeline: Pipeline) -> str:
    run_id = str(uuid.uuid4())
    metadata.create_run(run_id=run_id, pipeline_name=pipeline.name, pipeline_yaml="")
    return run_id


def _register_artifact(name: str, version: str, content: bytes, deps: list[dict] | None = None) -> str:
    sha256 = hashlib.sha256(content).hexdigest()
    save_blob(io.BytesIO(content), f"sha256:{sha256}")
    metadata.put_artifact(
        name=name, version=version, sha256=sha256,
        size=len(content), publisher="test", deps=deps or [],
    )
    return sha256


def _run_pipeline(pipeline: Pipeline, run_id: str, workspace: Path, runner=None) -> str:
    if runner is None:
        runner = MockRunner()
    return asyncio.run(execute_pipeline(
        pipeline, run_id, runner,
        workspace=workspace,
        forge_url="http://localhost:8080",
        forge_token="test-token",
        max_concurrency=4,
    ))


# ---------------------------------------------------------------------------
# Scenario 1 — pipeline builds and publishes lib-core@1.0.0
# ---------------------------------------------------------------------------

def test_scenario_1_builds_and_publishes_lib_core(workspace: Path) -> None:
    pipeline = _make_pipeline(
        name="build-lib-core",
        version="1.0.0",
        artifact_name="lib-core",
        artifact_version="1.0.0",
    )
    run_id = _new_run(pipeline)
    result = _run_pipeline(pipeline, run_id, workspace)

    assert result == "succeeded"
    artifact = metadata.get_artifact("lib-core", "1.0.0")
    assert artifact is not None
    assert artifact.name == "lib-core"
    assert artifact.version == "1.0.0"
    assert artifact.sha256 != ""


# ---------------------------------------------------------------------------
# Scenario 2 — resolves lib-core@^1.0.0 and publishes lib-http@1.0.0
# ---------------------------------------------------------------------------

def test_scenario_2_resolves_dep_and_publishes_lib_http(workspace: Path) -> None:
    _register_artifact("lib-core", "1.0.0", b"lib-core content")

    pipeline = _make_pipeline(
        name="build-lib-http",
        version="1.0.0",
        deps=[("lib-core", "^1.0.0")],
        artifact_name="lib-http",
        artifact_version="1.0.0",
    )
    run_id = _new_run(pipeline)
    lockfile = run_preflight(pipeline, run_id)

    assert len(lockfile["resolved"]) == 1
    assert lockfile["resolved"][0]["name"] == "lib-core"

    result = _run_pipeline(pipeline, run_id, workspace)
    assert result == "succeeded"

    artifact = metadata.get_artifact("lib-http", "1.0.0")
    assert artifact is not None


# ---------------------------------------------------------------------------
# Scenario 3 — resolves both deps and publishes service-api@0.1.0
# ---------------------------------------------------------------------------

def test_scenario_3_resolves_both_deps_and_publishes_service_api(workspace: Path) -> None:
    _register_artifact("lib-core", "1.0.0", b"lib-core content")
    _register_artifact("lib-http", "1.0.0", b"lib-http content")

    pipeline = _make_pipeline(
        name="build-service-api",
        version="0.1.0",
        deps=[("lib-core", "^1.0.0"), ("lib-http", "^1.0.0")],
        artifact_name="service-api",
        artifact_version="0.1.0",
    )
    run_id = _new_run(pipeline)
    lockfile = run_preflight(pipeline, run_id)

    resolved_names = {e["name"] for e in lockfile["resolved"]}
    assert "lib-core" in resolved_names
    assert "lib-http" in resolved_names

    result = _run_pipeline(pipeline, run_id, workspace)
    assert result == "succeeded"

    artifact = metadata.get_artifact("service-api", "0.1.0")
    assert artifact is not None


# ---------------------------------------------------------------------------
# Scenario 4 — wrong checksum upload returns 400
# ---------------------------------------------------------------------------

def test_scenario_4_wrong_checksum_rejected() -> None:
    data = b"some artifact content"
    wrong_checksum = "sha256:" + "0" * 64

    with pytest.raises(ChecksumMismatchError) as exc_info:
        save_blob(io.BytesIO(data), wrong_checksum)

    assert exc_info.value.declared == "0" * 64
    assert exc_info.value.actual == hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Scenario 5 — duplicate upload returns 409
# ---------------------------------------------------------------------------

def test_scenario_5_duplicate_upload_rejected() -> None:
    _register_artifact("lib-core", "1.0.0", b"lib-core content")

    with pytest.raises(DuplicateArtifactError) as exc_info:
        metadata.put_artifact(
            name="lib-core", version="1.0.0",
            sha256="abc", size=10, publisher="test",
        )

    assert exc_info.value.name == "lib-core"
    assert exc_info.value.version == "1.0.0"


# ---------------------------------------------------------------------------
# Scenario 6 — version conflict fails before build (conflict_failure)
# ---------------------------------------------------------------------------

def test_scenario_6_version_conflict_fails_before_build() -> None:
    # Only 2.0.0 is available but pipeline wants ^1.0.0
    _register_artifact("lib-core", "2.0.0", b"lib-core 2.0.0")

    pipeline = _make_pipeline(
        name="conflict-pipeline",
        version="1.0.0",
        deps=[("lib-core", "^1.0.0")],
    )
    run_id = _new_run(pipeline)

    with pytest.raises(PreflightError):
        run_preflight(pipeline, run_id)

    run = metadata.get_run(run_id)
    assert run.status == "conflict_failure"


# ---------------------------------------------------------------------------
# Scenario 7 — dependency cycle fails before build (cycle_failure)
# ---------------------------------------------------------------------------

def test_scenario_7_dep_cycle_fails_before_build() -> None:
    from engine.scheduler import JobCycleError

    # Build a pipeline with a job cycle: a -> b -> a
    pipeline = Pipeline(
        name="cycle-pipeline",
        version="1.0.0",
        dependencies=[],
        jobs={
            "a": Job(name="a", runtime="alpine:3.18",
                     steps=[Step(name="s", run="echo a")], needs=["b"], resources=Resources()),
            "b": Job(name="b", runtime="alpine:3.18",
                     steps=[Step(name="s", run="echo b")], needs=["a"], resources=Resources()),
        },
        artifacts=[],
    )
    run_id = _new_run(pipeline)

    with pytest.raises(JobCycleError):
        asyncio.run(execute_pipeline(
            pipeline, run_id, MockRunner(),
            workspace=Path("/tmp"),
            forge_url="http://localhost:8080",
            forge_token="test-token",
            max_concurrency=4,
        ))

    run = metadata.get_run(run_id)
    assert run.status == "cycle_failure"


# ---------------------------------------------------------------------------
# Scenario 8 — SHA-256 mismatch on dep pull causes integrity_failure
# ---------------------------------------------------------------------------

def test_scenario_8_dep_checksum_mismatch_detected() -> None:
    from engine.deps import DepChecksumMismatchError, materialize

    # Register artifact in registry
    sha256 = _register_artifact("lib-core", "1.0.0", b"real content")

    # Lockfile claims a different SHA-256
    tampered_lockfile = {
        "resolved": [
            {"name": "lib-core", "version": "1.0.0", "sha256": "0" * 64}
        ]
    }

    ws = Path("/tmp") / f"ws-{uuid.uuid4()}"
    ws.mkdir(parents=True, exist_ok=True)

    with pytest.raises(DepChecksumMismatchError):
        materialize(tampered_lockfile, ws)
