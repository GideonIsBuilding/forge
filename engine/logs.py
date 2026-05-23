"""
engine/logs.py

Per-job log file management and real-time SSE streaming.

Design:
- Each job gets its own append-only log file under data/logs/<run_id>/<job>.log
- Lines are written with a timestamp prefix at write time
- SSE clients get the full backlog from disk first, then live lines
- 50MB+ logs are streamed in chunks — never loaded fully into memory

Public interface
----------------
get_log_path(run_id, job_name)         -> Path
write_line(run_id, job_name, line)     -> None
stream_logs(run_id, job_name, follow)  -> Iterator[str]  (SSE lines)
"""

import logging
import threading
from typing import Dict, Iterator, List, Optional
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from engine import config

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024  # 256 KB
_watchers: dict[str, threading.Event] = {}  # key: "<run_id>/<job_name>"
_watchers_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _log_root() -> Path:
    root = Path(config.log_dir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_log_path(run_id: str, job_name: str) -> Path:
    """Return (and create parent dirs for) the log file path for a job."""
    path = _log_root() / run_id / f"{job_name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_line(run_id: str, job_name: str, line: str) -> None:
    """
    Append a timestamped line to the job's log file and notify
    any SSE clients currently following this job.
    """
    ts = datetime.now(timezone.utc).isoformat()
    entry = f"{ts} {line}\n"
    path = get_log_path(run_id, job_name)

    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)

    _notify(run_id, job_name)


def write_lines(run_id: str, job_name: str, lines: list[str]) -> None:
    """Batch write multiple lines — one open() call for the whole batch."""
    ts_prefix = datetime.now(timezone.utc).isoformat()
    path = get_log_path(run_id, job_name)

    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(f"{ts_prefix} {line}\n")

    _notify(run_id, job_name)


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------

def stream_logs(
    run_id: str,
    job_name: str,
    follow: bool = False,
) -> Iterator[str]:
    """
    Yield SSE-formatted lines for a job log.

    - Always replays the full backlog from disk first.
    - If follow=True, waits for new lines until the job finishes
      (signalled by calling close_log()).
    - Streams in chunks — never buffers the whole file in memory.

    Each yielded string is a complete SSE event:
        data: {"ts": "...", "job": "...", "line": "..."}\n\n
    """
    import json

    path = get_log_path(run_id, job_name)
    key = _watcher_key(run_id, job_name)

    # Replay backlog
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                for raw_line in chunk.splitlines():
                    ts, _, text = raw_line.partition(" ")
                    payload = json.dumps({"ts": ts, "job": job_name, "line": text})
                    yield f"data: {payload}\n\n"

    if not follow:
        return

    # Live tail
    with open(path, "r", encoding="utf-8") as f:
        f.seek(0, 2)  # seek to end
        while True:
            event = _get_event(key)
            if event is None:
                # Job finished, drain any remaining lines then stop
                for raw_line in f.read().splitlines():
                    ts, _, text = raw_line.partition(" ")
                    payload = json.dumps({"ts": ts, "job": job_name, "line": text})
                    yield f"data: {payload}\n\n"
                break

            event.wait(timeout=1.0)
            event.clear()

            for raw_line in f.read().splitlines():
                if not raw_line:
                    continue
                ts, _, text = raw_line.partition(" ")
                payload = json.dumps({"ts": ts, "job": job_name, "line": text})
                yield f"data: {payload}\n\n"


def close_log(run_id: str, job_name: str) -> None:
    """
    Signal that a job has finished writing logs.
    SSE followers will drain remaining lines and disconnect.
    """
    key = _watcher_key(run_id, job_name)
    with _watchers_lock:
        if key in _watchers:
            del _watchers[key]


# ---------------------------------------------------------------------------
# Internal watcher helpers
# ---------------------------------------------------------------------------

def _watcher_key(run_id: str, job_name: str) -> str:
    return f"{run_id}/{job_name}"


def _notify(run_id: str, job_name: str) -> None:
    """Wake up any SSE followers waiting on this job."""
    key = _watcher_key(run_id, job_name)
    with _watchers_lock:
        event = _watchers.get(key)
    if event:
        event.set()


def _get_event(key: str) -> Optional[threading.Event]:
    """
    Return the threading.Event for a live job, or None if the job
    has finished (i.e. close_log() was already called).
    """
    with _watchers_lock:
        if key not in _watchers:
            _watchers[key] = threading.Event()
        return _watchers.get(key)


def register_job(run_id: str, job_name: str) -> None:
    """
    Register a job as active so SSE followers know to wait for lines.
    Call this when a job starts executing.
    """
    key = _watcher_key(run_id, job_name)
    with _watchers_lock:
        _watchers[key] = threading.Event()
    logger.debug("Log watcher registered: %s", key)
