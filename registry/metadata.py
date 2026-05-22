from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class DuplicateArtifactError(ValueError):
    pass


class MetadataStore:
    DuplicateArtifactError = DuplicateArtifactError

    def __init__(self, db_path: str) -> None:
        if db_path.startswith("sqlite:///"):
            db_path = db_path.removeprefix("sqlite:///")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def publish_artifact(
        self,
        name: str,
        version: str,
        sha256: str,
        size: int,
        publisher: str,
        deps: list[dict[str, str]],
    ) -> None:
        if not SEMVER_RE.match(version):
            raise ValueError("version must be strict semver: MAJOR.MINOR.PATCH")

        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO artifacts
                    (name, version, sha256, size, publisher, published_at, deps_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        version,
                        sha256,
                        size,
                        publisher,
                        datetime.now(UTC).isoformat(),
                        json.dumps(deps, sort_keys=True),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateArtifactError(f"{name}@{version} already exists") from exc

    def get_artifact(self, name: str, version: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT name, version, sha256, size, publisher, published_at, deps_json
                FROM artifacts WHERE name = ? AND version = ?
                """,
                (name, version),
            ).fetchone()
        if row is None:
            return None
        return _artifact_row(row)

    def list_versions(self, name: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT version FROM artifacts WHERE name = ? ORDER BY version",
                (name,),
            ).fetchall()
        return [row["version"] for row in rows]

    def create_run(self, pipeline_name: str) -> str:
        run_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (id, pipeline_name, status, created_at) VALUES (?, ?, ?, ?)",
                (run_id, pipeline_name, "queued", datetime.now(UTC).isoformat()),
            )
        return run_id

    def update_run_status(self, run_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, pipeline_name, status, created_at FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "jobs": {},
            "lockfile_url": f"/runs/{run_id}/lockfile",
        }

    def get_lockfile(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT lockfile_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None or row["lockfile_json"] is None:
            return None
        return json.loads(row["lockfile_json"])

    def store_token_hash(self, identity: str, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tokens (identity, token_hash, created_at) VALUES (?, ?, ?)",
                (identity, token_hash, datetime.now(UTC).isoformat()),
            )

    def token_hashes(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT identity, token_hash FROM tokens").fetchall()
        return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    publisher TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    deps_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (name, version)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    pipeline_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    lockfile_json TEXT
                );

                CREATE TABLE IF NOT EXISTS tokens (
                    identity TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )


def _artifact_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "version": row["version"],
        "sha256": row["sha256"],
        "size": row["size"],
        "publisher": row["publisher"],
        "published_at": row["published_at"],
        "deps": json.loads(row["deps_json"]),
    }
