"""LocalStorage — filesystem backend that wraps f5kb.lib.fsutil exactly."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from f5kb.lib.fsutil import read_json, write_json
from f5kb.storage.base import StorageBackend


class LocalStorage(StorageBackend):
    """Key-based filesystem storage.  Keys are relative forward-slash paths;
    base_path is prepended internally so callers never handle OS separators."""

    def __init__(self, base_path: str | Path) -> None:
        self._base = Path(base_path)

    def _p(self, key: str) -> Path:
        return self._base / key.lstrip("/")

    # ── JSON ─────────────────────────────────────────────────────────────────

    def get(self, key: str) -> Any:
        p = self._p(key)
        if not p.exists():
            raise KeyError(key)
        return read_json(p)

    def put(self, key: str, data: Any) -> None:
        write_json(self._p(key), data)

    # ── Bytes ────────────────────────────────────────────────────────────────

    def get_bytes(self, key: str) -> bytes:
        p = self._p(key)
        if not p.exists():
            raise KeyError(key)
        return p.read_bytes()

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    # ── Existence / deletion ─────────────────────────────────────────────────

    def exists(self, key: str) -> bool:
        return self._p(key).exists()

    def delete(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)

    # ── Listing ──────────────────────────────────────────────────────────────

    def list_prefix(self, prefix: str) -> list[str]:
        """Return sorted forward-slash keys under prefix, relative to base_path."""
        target = self._p(prefix)
        if not target.exists():
            return []
        if target.is_file():
            return [prefix]
        out: list[str] = []
        for entry in sorted(target.rglob("*")):
            if entry.is_file():
                out.append(entry.relative_to(self._base).as_posix())
        return out

    # ── Move / markers ───────────────────────────────────────────────────────

    def move(self, src: str, dst: str) -> None:
        sp, dp = self._p(src), self._p(dst)
        dp.parent.mkdir(parents=True, exist_ok=True)
        sp.rename(dp)

    def copy(self, src: str, dst: str) -> None:
        sp, dp = self._p(src), self._p(dst)
        if not sp.exists():
            raise KeyError(src)
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_bytes(sp.read_bytes())

    def put_conditional(self, key: str, data: bytes) -> bool:
        """Create-if-absent. Returns True if created, False if it already existed."""
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create closes the check-then-write race within one process.
        try:
            with p.open("xb") as fh:
                fh.write(data)
        except FileExistsError:
            return False
        return True

    def put_marker(self, key: str) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")

    # ── JSONL ────────────────────────────────────────────────────────────────

    def append_jsonl(self, key: str, entry: dict) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with p.open("a", encoding="utf-8") as f:
            f.write(line)

    def append_jsonl_many(self, key: str, entries: list[dict]) -> None:
        if not entries:
            return
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")

    # ── Hash index ───────────────────────────────────────────────────────────

    def load_hash_index(self, key: str) -> dict[str, str]:
        p = self._p(key)
        if not p.exists():
            return {}
        try:
            with gzip.open(p, "rb") as fh:
                return json.loads(fh.read().decode("utf-8"))
        except Exception:
            return {}

    def save_hash_index(self, index: dict[str, str], key: str) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(index, separators=(",", ":")).encode("utf-8")
        with gzip.open(p, "wb") as fh:
            fh.write(data)
