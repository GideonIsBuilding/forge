"""
registry/metadata.py

Artifact and run metadata CRUD.
All DB access goes through registry.db — no raw sqlite3 calls here.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from registry import db

logger = logging.getLogger(__name__)


class DuplicateArtifactError(Exception):
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        super().__init__(f"Artifact {name}@{version} already exists (immutable)")


@dataclass
class ArtifactRow:
    id: int
    name: str
    version: str
    sha256: str
    size: int
    publisher: str
    published_at: str
    deps: list[dict]


@dataclass
class RunRow:
    id: str
    pipeline_name: str
    pipeline_yaml: str
    status: str
    lockfile: dict | None
    lockfile_url: str | None
    created_at: str
    updated_at: str
    duration_s: float | None


@dataclass
class JobRow:
    id: int
    run_id: str
    name: str
    status: str
    needs: list[str]
    runtime: str | None
    log_path: str | None
    started_at: str | None
    finished_at: str | None
    exit_code: int | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_artifact(row) -> ArtifactRow:
    return ArtifactRow(
        id=row["id"], name=row["name"], version=row["version"],
        sha256=row["sha256"], size=row["size"], publisher=row["publisher"],
        published_at=row["published_at"], deps=json.loads(row["deps"]),
    )


def _to_run(row) -> RunRow:
    return RunRow(
        id=row["id"], pipeline_name=row["pipeline_name"],
        pipeline_yaml=row["pipeline_yaml"], status=row["status"],
        lockfile=json.loads(row["lockfile"]) if row["lockfile"] else None,
        lockfile_url=row["lockfile_url"], created_at=row["created_at"],
        updated_at=row["updated_at"], duration_s=row["duration_s"],
    )


def _to_job(row) -> JobRow:
    return JobRow(
        id=row["id"], run_id=row["run_id"], name=row["name"],
        status=row["status"], needs=json.loads(row["needs"]),
        runtime=row["runtime"], log_path=row["log_path"],
        started_at=row["started_at"], finished_at=row["finished_at"],
        exit_code=row["exit_code"],
    )


# Artifacts

def put_artifact(*, name: str, version: str, sha256: str, size: int,
                 publisher: str, deps: list[dict] | None = None) -> None:
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO artifacts (name, version, sha256, size, publisher, published_at, deps) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, version, sha256, size, publisher, _now(), json.dumps(deps or [])),
            )
    except sqlite3.IntegrityError:
        raise DuplicateArtifactError(name, version)


def get_artifact(name: str, version: str) -> ArtifactRow | None:
    row = db.fetchone("SELECT * FROM artifacts WHERE name = ? AND version = ?", (name, version))
    return _to_artifact(row) if row else None


def list_versions(name: str) -> list[str]:
    rows = db.fetchall("SELECT version FROM artifacts WHERE name = ? ORDER BY published_at DESC", (name,))
    return [r["version"] for r in rows]


# Runs

def create_run(*, run_id: str, pipeline_name: str, pipeline_yaml: str) -> None:
    now = _now()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO runs (id, pipeline_name, pipeline_yaml, status, created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?)",
            (run_id, pipeline_name, pipeline_yaml, now, now),
        )


def get_run(run_id: str) -> RunRow | None:
    row = db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
    return _to_run(row) if row else None


def update_run_status(run_id: str, status: str, duration_s: float | None = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, duration_s = ?, updated_at = ? WHERE id = ?",
            (status, duration_s, _now(), run_id),
        )


def set_run_lockfile(run_id: str, lockfile: dict) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE runs SET lockfile = ?, lockfile_url = ?, updated_at = ? WHERE id = ?",
            (json.dumps(lockfile), f"/runs/{run_id}/lockfile", _now(), run_id),
        )


# Jobs

def create_job(*, run_id: str, name: str, needs: list[str] | None = None,
               runtime: str | None = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO jobs (run_id, name, status, needs, runtime) VALUES (?, ?, 'queued', ?, ?)",
            (run_id, name, json.dumps(needs or []), runtime),
        )


def get_job(run_id: str, name: str) -> JobRow | None:
    row = db.fetchone("SELECT * FROM jobs WHERE run_id = ? AND name = ?", (run_id, name))
    return _to_job(row) if row else None


def list_jobs(run_id: str) -> list[JobRow]:
    rows = db.fetchall("SELECT * FROM jobs WHERE run_id = ?", (run_id,))
    return [_to_job(r) for r in rows]


def update_job_status(run_id: str, name: str, status: str,
                      exit_code: int | None = None, started_at: str | None = None,
                      finished_at: str | None = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, exit_code = ?, started_at = ?, finished_at = ? WHERE run_id = ? AND name = ?",
            (status, exit_code, started_at, finished_at, run_id, name),
        )


def set_job_log_path(run_id: str, name: str, log_path: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET log_path = ? WHERE run_id = ? AND name = ?",
            (log_path, run_id, name),
        )
