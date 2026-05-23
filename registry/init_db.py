"""
registry/init_db.py

Creates the Forge registry schema.

Run directly to initialise a fresh database:
    python -m registry.init_db
"""

import argparse
import logging

from registry import db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    version      TEXT    NOT NULL,
    sha256       TEXT    NOT NULL,
    size         INTEGER NOT NULL,
    publisher    TEXT    NOT NULL,
    published_at TEXT    NOT NULL,
    deps         TEXT    NOT NULL DEFAULT '[]',
    UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_name
    ON artifacts (name);

CREATE TABLE IF NOT EXISTS tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT    NOT NULL UNIQUE,
    identity   TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    pipeline_yaml TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    lockfile      TEXT,
    lockfile_url  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    duration_s    REAL
);

CREATE INDEX IF NOT EXISTS idx_runs_status
    ON runs (status);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'queued',
    needs       TEXT    NOT NULL DEFAULT '[]',
    runtime     TEXT,
    log_path    TEXT,
    started_at  TEXT,
    finished_at TEXT,
    exit_code   INTEGER,
    UNIQUE (run_id, name)
);

CREATE INDEX IF NOT EXISTS idx_jobs_run_id
    ON jobs (run_id);

CREATE INDEX IF NOT EXISTS idx_jobs_status
    ON jobs (status);
"""


def create_schema() -> None:
    """Apply schema DDL. Safe to call on every startup — fully idempotent."""
    conn = db.get_db()
    conn.executescript(_SCHEMA)
    conn.commit()
    logger.info("Forge registry schema ready at %s", db._db_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Initialise the Forge registry DB.")
    p.add_argument("--db-path", default=None, help="Path to SQLite file.")
    args = p.parse_args()
    db.init(args.db_path)
    create_schema()
    print("Schema created successfully.")
