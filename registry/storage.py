from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path


class BlobStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, expected_checksum: str) -> tuple[str, int]:
        expected = _normalize_checksum(expected_checksum)
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ValueError(f"checksum mismatch: expected sha256:{expected}, got sha256:{actual}")

        path = self.path_for(actual)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return actual, len(data)

    def open_stream(self, sha256: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        with self.path_for(sha256).open("rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk

    def path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256


def _normalize_checksum(value: str) -> str:
    if not value.startswith("sha256:"):
        raise ValueError("checksum must use sha256:<hex>")
    checksum = value.removeprefix("sha256:")
    if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum.lower()):
        raise ValueError("checksum must be a valid sha256 hex digest")
    return checksum.lower()
