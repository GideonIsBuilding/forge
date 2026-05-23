"""
tests/test_preflight.py

Unit tests for engine/preflight.py.
Each test gets a fresh in-memory SQLite DB via the tmp_db fixture.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from engine.parser import Artifact, Dependency, Job, Pipeline, Resources, Step
from engine.preflight import PreflightError, run_preflight
from registry import db, metadata
from registry.init_db import create_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path: Path) -> None:
    db.init(tmp_path / "test.db")
    create_schema()
    yield
    db.close_db()


def _pipeline(deps: list[Dependency] | None = None) -> Pipeline:
    return Pipeline(
        name="test-pipeline",
        version="1.0.0",
        dependencies=deps or [],
        jobs={
            "build": Job(
                name="build",
                runtime="alpine:3.18",
                steps=[Step(name="s", run="echo hi")],
            )
        },
        artifacts=[],
    )


def _new_run(name: str = "test-pipeline") -> str:
    run_id = str(uuid.uuid4())
    metadata.create_run(run_id=run_id, pipeline_name=name, pipeline_yaml="")
    return run_id


def _pub(name: str, version: str, sha256: str = "abc123", deps: list[dict] | None = None) -> None:
    metadata.put_artifact(
        name=name, version=version, sha256=sha256, size=10,
        publisher="test", deps=deps or [],
    )


# ---------------------------------------------------------------------------
# No dependencies
# ---------------------------------------------------------------------------

def test_no_deps_returns_empty_lockfile() -> None:
    run_id = _new_run()
    lockfile = run_preflight(_pipeline(), run_id)
    assert lockfile == {"resolved": []}


def test_no_deps_stores_lockfile_in_db() -> None:
    run_id = _new_run()
    run_preflight(_pipeline(), run_id)
    run = metadata.get_run(run_id)
    assert run is not None
    assert run.lockfile == {"resolved": []}
    assert run.lockfile_url == f"/runs/{run_id}/lockfile"


# ---------------------------------------------------------------------------
# Successful resolution
# ---------------------------------------------------------------------------

def test_single_direct_dep_resolved() -> None:
    _pub("mylib", "1.2.3", sha256="deadbeef")
    run_id = _new_run()
    lockfile = run_preflight(
        _pipeline(deps=[Dependency(name="mylib", version="^1.0.0")]),
        run_id,
    )
    entries = lockfile["resolved"]
    assert len(entries) == 1
    assert entries[0]["name"] == "mylib"
    assert entries[0]["version"] == "1.2.3"
    assert entries[0]["sha256"] == "deadbeef"


def test_exact_version_constraint() -> None:
    _pub("tool", "2.0.0")
    run_id = _new_run()
    lockfile = run_preflight(
        _pipeline(deps=[Dependency(name="tool", version="2.0.0")]),
        run_id,
    )
    assert lockfile["resolved"][0]["version"] == "2.0.0"


def test_highest_matching_version_selected() -> None:
    for v in ("1.0.0", "1.3.0", "2.0.0"):
        _pub("lib", v, sha256=f"sha-{v}")
    run_id = _new_run()
    lockfile = run_preflight(
        _pipeline(deps=[Dependency(name="lib", version="^1.0.0")]),
        run_id,
    )
    assert lockfile["resolved"][0]["version"] == "1.3.0"


def test_transitive_dep_resolved() -> None:
    # bar depends on baz; pipeline declares bar only
    _pub("baz", "3.0.0", sha256="sha-baz")
    _pub("bar", "1.0.0", sha256="sha-bar",
         deps=[{"name": "baz", "version": "^3.0.0"}])

    run_id = _new_run()
    lockfile = run_preflight(
        _pipeline(deps=[Dependency(name="bar", version="1.0.0")]),
        run_id,
    )
    names = {e["name"] for e in lockfile["resolved"]}
    assert names == {"bar", "baz"}


def test_lockfile_stored_on_success() -> None:
    _pub("pkg", "1.0.0", sha256="sha1")
    run_id = _new_run()
    run_preflight(
        _pipeline(deps=[Dependency(name="pkg", version="1.0.0")]),
        run_id,
    )
    run = metadata.get_run(run_id)
    assert run.lockfile is not None
    assert run.lockfile_url == f"/runs/{run_id}/lockfile"


# ---------------------------------------------------------------------------
# conflict_failure — missing or unsatisfiable packages
# ---------------------------------------------------------------------------

def test_missing_package_sets_conflict_failure() -> None:
    run_id = _new_run()
    with pytest.raises(PreflightError):
        run_preflight(
            _pipeline(deps=[Dependency(name="ghost-pkg", version="^1.0.0")]),
            run_id,
        )
    assert metadata.get_run(run_id).status == "conflict_failure"


def test_unsatisfiable_constraint_sets_conflict_failure() -> None:
    _pub("lib", "1.0.0")
    run_id = _new_run()
    with pytest.raises(PreflightError):
        run_preflight(
            _pipeline(deps=[Dependency(name="lib", version="^2.0.0")]),
            run_id,
        )
    assert metadata.get_run(run_id).status == "conflict_failure"


def test_transitive_conflict_sets_conflict_failure() -> None:
    # bar@1.0.0 requires baz@^2.0.0, but only baz@1.0.0 exists
    _pub("baz", "1.0.0", sha256="sha-baz")
    _pub("bar", "1.0.0", sha256="sha-bar",
         deps=[{"name": "baz", "version": "^2.0.0"}])
    run_id = _new_run()
    with pytest.raises(PreflightError):
        run_preflight(
            _pipeline(deps=[Dependency(name="bar", version="1.0.0")]),
            run_id,
        )
    assert metadata.get_run(run_id).status == "conflict_failure"


# ---------------------------------------------------------------------------
# cycle_failure — circular dependency in artifact graph
# ---------------------------------------------------------------------------

def test_dep_cycle_sets_cycle_failure() -> None:
    # foo@1.0.0 depends on bar@^1.0.0 and bar@1.0.0 depends on foo@^1.0.0
    _pub("foo", "1.0.0", sha256="sha-foo",
         deps=[{"name": "bar", "version": "^1.0.0"}])
    _pub("bar", "1.0.0", sha256="sha-bar",
         deps=[{"name": "foo", "version": "^1.0.0"}])

    run_id = _new_run()
    with pytest.raises(PreflightError):
        run_preflight(
            _pipeline(deps=[Dependency(name="foo", version="^1.0.0")]),
            run_id,
        )
    assert metadata.get_run(run_id).status == "cycle_failure"


# ---------------------------------------------------------------------------
# Preflight does NOT change run status on success
# ---------------------------------------------------------------------------

def test_success_does_not_overwrite_run_status() -> None:
    _pub("stable", "1.0.0")
    run_id = _new_run()
    # Run starts as "queued"; preflight should leave that untouched on success.
    run_preflight(
        _pipeline(deps=[Dependency(name="stable", version="1.0.0")]),
        run_id,
    )
    run = metadata.get_run(run_id)
    assert run.status == "queued"
