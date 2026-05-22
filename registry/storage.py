"""
registry/storage.py

Content-addressed blob storage for the Forge artifact registry.
Blobs are stored under data/blobs/<sha256[:2]>/<sha256> (two-char prefix
directory keeps filesystem dentries small at scale).

Public interface
---------------
save_blob(stream, declared_checksum)  -> sha256 hex string
load_blob(sha256)                     -> byte iterator (streaming)
blob_exists(sha256)                   -> bool
blob_path(sha256)                     -> Path
"""

import hashlib
import logging
from pathlib import Path
from typing import BinaryIO, Iterator

from engine import config

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024  # 256 KB read chunks


class ChecksumMismatchError(Exception):
    """Raised when the uploaded file's SHA-256 does not match declared checksum."""
    def __init__(self, declared: str, actual: str) -> None:
        self.declared = declared
        self.actual = actual
        super().__init__(
            f"Checksum mismatch: declared={declared} actual={actual}"
        )


class BlobNotFoundError(Exception):
    """Raised when a requested blob does not exist in storage."""
    def __init__(self, sha256: str) -> None:
        self.sha256 = sha256
        super().__init__(f"Blob not found: {sha256}")


def _blob_root() -> Path:
    root = Path(config.blob_dir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def blob_path(sha256: str) -> Path:
    """Return the filesystem path for a given SHA-256 hash."""
    return _blob_root() / sha256[:2] / sha256


def blob_exists(sha256: str) -> bool:
    """Return True if the blob is already stored."""
    return blob_path(sha256).exists()


def save_blob(stream: BinaryIO, declared_checksum: str) -> str:
    """
    Stream an uploaded file to a temp path, compute its SHA-256,
    verify against declared_checksum, then move it into content-addressed
    storage. Returns the hex SHA-256.

    declared_checksum must be in the format "sha256:<hex>" or plain hex.
    Raises ChecksumMismatchError on mismatch (HTTP 400).
    """
    declared_hex = _parse_checksum(declared_checksum)

    root = _blob_root()
    tmp_path = root / f".tmp-{declared_hex}"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()
    size = 0

    try:
        with open(tmp_path, "wb") as f:
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                f.write(chunk)
                size += len(chunk)

        actual_hex = hasher.hexdigest()

        if actual_hex != declared_hex:
            tmp_path.unlink(missing_ok=True)
            raise ChecksumMismatchError(declared_hex, actual_hex)

        dest = blob_path(actual_hex)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists():
            # Blob already stored — deduplicated, just clean up temp.
            tmp_path.unlink(missing_ok=True)
        else:
            tmp_path.rename(dest)

        logger.info("Blob stored: sha256=%s size=%d", actual_hex, size)
        return actual_hex

    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def load_blob(sha256: str) -> Iterator[bytes]:
    """
    Yield the blob in CHUNK_SIZE chunks for streaming responses.
    Raises BlobNotFoundError if the blob does not exist.
    """
    path = blob_path(sha256)
    if not path.exists():
        raise BlobNotFoundError(sha256)

    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


def get_blob_size(sha256: str) -> int:
    """Return the size in bytes of a stored blob."""
    path = blob_path(sha256)
    if not path.exists():
        raise BlobNotFoundError(sha256)
    return path.stat().st_size


def _parse_checksum(checksum: str) -> str:
    """
    Accept either 'sha256:<hex>' or plain '<hex>'.
    Returns the lowercase hex string.
    """
    if checksum.startswith("sha256:"):
        return checksum[len("sha256:"):].lower()
    return checksum.lower()
