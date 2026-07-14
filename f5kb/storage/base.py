"""Storage backend ABC — key-based I/O over local filesystem or S3."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    """Abstract I/O layer.

    All keys use forward-slash paths (S3-style). LocalStorage prepends base_path
    internally. Callers never handle OS path separators or boto3 exceptions —
    both are normalised at the backend boundary.
    """

    @abstractmethod
    def get(self, key: str) -> Any:
        """Read and JSON-decode an object. Raises KeyError if not found."""

    @abstractmethod
    def put(self, key: str, data: Any) -> None:
        """JSON-encode and write (pretty-printed, trailing newline)."""

    @abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """Read raw bytes. Raises KeyError if not found."""

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        """Write raw bytes."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return True if the key exists."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete the object. No-op if it does not exist."""

    def delete_many(self, keys: list[str]) -> int:
        """Delete many keys; returns the count requested. Backends may batch."""
        for k in keys:
            self.delete(k)
        return len(keys)

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """Return all keys that begin with prefix. Full keys returned, sorted."""

    @abstractmethod
    def move(self, src: str, dst: str) -> None:
        """Rename src to dst. For S3: copy + delete."""

    @abstractmethod
    def copy(self, src: str, dst: str) -> None:
        """Copy src to dst, leaving src in place. For S3: server-side copy."""

    @abstractmethod
    def put_conditional(self, key: str, data: bytes) -> bool:
        """Conditional create (If-None-Match: *). Returns True if this writer
        created the object, False if it already existed (S3 412 flow). Used for
        single-winner sentinels / started markers."""

    @abstractmethod
    def put_marker(self, key: str) -> None:
        """Write a zero-byte object (e.g. _done completion markers)."""

    @abstractmethod
    def append_jsonl(self, key: str, entry: dict) -> None:
        """Append one JSONL line to key. Single-writer assumption — get+append+put."""

    def append_jsonl_many(self, key: str, entries: list[dict]) -> None:
        """Append many JSONL lines in ONE write. Backends override — the naive
        per-entry loop is O(n^2) bytes on S3 (each append re-uploads the whole
        growing file; a full-corpus run pushes terabytes)."""
        for e in entries:
            self.append_jsonl(key, e)

    @abstractmethod
    def load_hash_index(self, key: str) -> dict[str, str]:
        """Read gzip-compressed JSON at key. Returns {} if not found."""

    @abstractmethod
    def save_hash_index(self, index: dict[str, str], key: str) -> None:
        """Write dict as gzip-compressed JSON to key."""
