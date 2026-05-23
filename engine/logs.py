"""
engine/logs.py

Per-job log file management and real-time SSE streaming.

Design:
- Each job gets its own append-only log file: data/logs/<run_id>/<job>.log
- Lines are written with a UTC timestamp prefix
- SSE clients get the full backlog from disk first, then live lines
- 50MB+ logs are streamed in 256KB chunks — never loaded fully into memory

Public interface
----------------
get_log_path(run_id, job_name)              -> Path
write_line(run_id, job_name, line)          -> None
write_lines(run_id, job_name, lines)        -> None
register_job(run_id, job_name)              -> None   call when a job starts
close_log(run_id, job_name)                 -> None   call when a job finishes
stream_logs(run_id, job_name, follow)       -> Iterator[str]  (SSE lines, one job)
stream_run_logs(run_id, follow)             -> Iterator[str]  (SSE lines, all jobs)
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from engine import config

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024  # 256 KB

# key: "<run_id>/<job_name>" -> threading.Event
# An entry exists while the job is actively running.
# Absence means the job has finished (or never started).
_watchers: dict[str, threading.Event] = {}
_watchers_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _log_root() -> Path:
    root = Path(config.log_dir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_log_path(run_id: str, job_name: str) -> Path:
    """Return (and ensure parent dirs for) the log file path for a job."""
    path = _log_root() / run_id / f"{job_name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Watcher lifecycle
# ---------------------------------------------------------------------------

def register_job(run_id: str, job_name: str) -> None:
    """Mark a job as active so live SSE followers know to wait for lines.

    Must be called before the job starts writing log lines. The matching
    close_log() call signals completion and wakes any waiting SSE clients.
    """
    key = _watcher_key(run_id, job_name)
    with _watchers_lock:
        _watchers[key] = threading.Event()
    logger.debug("Log watcher registered: %s", key)


def close_log(run_id: str, job_name: str) -> None:
    """Signal that a job has finished writing logs.

    Removes the watcher entry. SSE followers will drain any remaining lines
    written between their last read and this call, then disconnect cleanly.
    """
    key = _watcher_key(run_id, job_name)
    with _watchers_lock:
        _watchers.pop(key, None)
    logger.debug("Log watcher closed: %s", key)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_line(run_id: str, job_name: str, line: str) -> None:
    """Append a timestamped line and notify any SSE clients following this job."""
    ts = datetime.now(timezone.utc).isoformat()
    path = get_log_path(run_id, job_name)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{ts} {line}\n")
    _notify(run_id, job_name)


def write_lines(run_id: str, job_name: str, lines: list[str]) -> None:
    """Batch-write multiple lines — one open() call for the whole batch."""
    ts_prefix = datetime.now(timezone.utc).isoformat()
    path = get_log_path(run_id, job_name)
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(f"{ts_prefix} {line}\n")
    _notify(run_id, job_name)


# ---------------------------------------------------------------------------
# Per-job SSE streaming
# ---------------------------------------------------------------------------

def stream_logs(run_id: str, job_name: str, follow: bool = False) -> Iterator[str]:
    """Yield SSE-formatted events for a single job's log.

    Phase 1 — Backlog replay: reads the log file from the beginning in
    CHUNK_SIZE chunks. A client connecting mid-run sees everything that
    already happened before the live tail begins.

    Phase 2 — Live tail (follow=True only): seeks to the end of the file
    and waits on a threading.Event. write_line() sets the event; this loop
    wakes, reads new lines, and yields them. When close_log() removes the
    watcher entry, _get_event() returns None and the loop drains any lines
    written in the last window before exiting.

    Yields complete SSE events:
        data: {"ts": "...", "job": "...", "line": "..."}\n\n
    """
    path = get_log_path(run_id, job_name)
    key = _watcher_key(run_id, job_name)

    # Phase 1: replay everything already on disk.
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield from _parse_chunk(chunk, job_name)

    if not follow:
        return

    # Phase 2: live tail. Ensure the file exists so we can open it even if
    # the job hasn't written its first line yet.
    path.touch(exist_ok=True)

    with open(path, "r", encoding="utf-8") as f:
        f.seek(0, 2)  # start at end; backlog already replayed above
        while True:
            event = _get_event(key)
            if event is None:
                # Job finished — drain any lines written since our last read.
                yield from _parse_chunk(f.read(), job_name)
                break

            event.wait(timeout=1.0)
            event.clear()

            yield from _parse_chunk(f.read(), job_name)


# ---------------------------------------------------------------------------
# Run-level SSE streaming (all jobs)
# ---------------------------------------------------------------------------

def stream_run_logs(run_id: str, follow: bool = False) -> Iterator[str]:
    """Yield SSE-formatted events for every job in a run.

    follow=False: streams each job's existing log lines sequentially.
                  Memory usage is bounded to CHUNK_SIZE regardless of log size.

    follow=True:  spawns one daemon thread per job, each running stream_logs()
                  with follow=True. All threads feed into a shared Queue. The
                  generator reads from the queue until every job thread has sent
                  its sentinel, then exits. Interleaving is arrival-order, not
                  timestamp-order — each event carries "ts" and "job" fields so
                  the client can sort if needed.
    """
    from registry import metadata

    job_rows = metadata.list_jobs(run_id)
    if not job_rows:
        return

    job_names = [row.name for row in job_rows]

    if not follow:
        for job_name in job_names:
            path = get_log_path(run_id, job_name)
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield from _parse_chunk(chunk, job_name)
        return

    # follow=True: fan-in all job streams into a single queue.
    # None is the per-job sentinel; when all sentinels arrive we stop.
    q: queue.Queue[str | None] = queue.Queue()

    def _feed(job_name: str) -> None:
        try:
            for event in stream_logs(run_id, job_name, follow=True):
                q.put(event)
        except Exception:
            logger.exception("Error streaming logs for job %s/%s", run_id, job_name)
        finally:
            q.put(None)

    for job_name in job_names:
        threading.Thread(target=_feed, args=(job_name,), daemon=True).start()

    remaining = len(job_names)
    while remaining > 0:
        item = q.get()
        if item is None:
            remaining -= 1
        else:
            yield item


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _watcher_key(run_id: str, job_name: str) -> str:
    return f"{run_id}/{job_name}"


def _notify(run_id: str, job_name: str) -> None:
    """Wake any SSE followers waiting on this job."""
    key = _watcher_key(run_id, job_name)
    with _watchers_lock:
        event = _watchers.get(key)
    if event:
        event.set()


def _get_event(key: str) -> threading.Event | None:
    """Return the active watcher event, or None if the job has finished.

    Absence of the key means close_log() was already called — the job is done.
    Creating a new event here would be wrong: it would never be set, causing
    the live tail loop to spin on 1-second timeouts indefinitely.
    """
    with _watchers_lock:
        return _watchers.get(key)


def _parse_chunk(chunk: str, job_name: str) -> Iterator[str]:
    """Split a text chunk into SSE events, one per log line."""
    for raw_line in chunk.splitlines():
        if not raw_line:
            continue
        ts, _, text = raw_line.partition(" ")
        payload = json.dumps({"ts": ts, "job": job_name, "line": text})
        yield f"data: {payload}\n\n"
