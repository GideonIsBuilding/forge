from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path


class LogStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, run_id: str, job: str, line: str) -> None:
        path = self._path(run_id)
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "job": job,
            "line": line.rstrip("\n"),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    async def stream(self, run_id: str, follow: bool = False) -> AsyncIterator[str]:
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

        with path.open("r", encoding="utf-8") as handle:
            while True:
                line = handle.readline()
                if line:
                    yield f"data: {line}\n" if follow else line
                    continue
                if not follow:
                    break
                await asyncio.sleep(0.25)

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.ndjson"
