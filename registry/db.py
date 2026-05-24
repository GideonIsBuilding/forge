"""
registry/db.py

Thread-safe SQLite connection management for the Forge registry.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

_DEFAULT_DB_PATH = Path("data/forge.db")
_local = threading.local()
_db_path: Path = _DEFAULT_DB_PATH


def init(db_path: str | Path | None = None) -> None:
    """Call once at startup to set the DB file location."""
    global _db_path
    _db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
    _db_path.parent.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_db() -> sqlite3.Connection:
    """Return the thread-local connection, opening it if needed."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = _connect()
    return _local.conn


def close_db() -> None:
    """Close the thread-local connection. Call at end of each request."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def transaction() -> Generator[sqlite3.Connection, None, None]:
    """Wraps a block in BEGIN/COMMIT, rolls back on exception."""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Run a statement. Does NOT auto-commit."""
    return get_db().execute(sql, params)


def fetchone(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_db().execute(sql, params).fetchone()


def fetchall(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return get_db().execute(sql, params).fetchall()
