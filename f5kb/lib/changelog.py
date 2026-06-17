"""Changelog recorder: structured, append-only log of every change a mutating
operation makes to the dump or DB. Records buffered and flushed as JSONL
(one JSON object per line). A null path makes it a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from f5kb.lib.fsutil import iso_now

ChangeOp = Literal[
    "added",
    "edited",
    "deleted",
    "body-added",
    "body-changed",
    "body-error",
]

CHANGELOG_BASENAME = "_changelog.jsonl"


def changelog_path_from_flag(flag_value: str | bool | None, dump_dir: str) -> str | None:
    """Resolve a --changelog[=FILE] flag to a path, or None when absent.

      (absent)         -> None  (logging disabled)
      --changelog      -> <dumpDir>/_changelog.jsonl
      --changelog=FILE -> FILE
    """
    if flag_value is None:
        return None
    if flag_value is True or flag_value == "":
        base = str(dump_dir).rstrip("/")
        return f"{base}/{CHANGELOG_BASENAME}"
    return str(flag_value)


class Changelog:
    def __init__(self, path: str | None, run_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._buf: list[str] = []
        self._counts: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(self, op: ChangeOp, document_type: str, id: str, **kwargs: object) -> None:
        self._counts[op] = self._counts.get(op, 0) + 1
        if not self._path:
            return
        entry: dict = {
            "runId": self._run_id,
            "ts": iso_now(),
            "op": op,
            "documentType": document_type,
            "id": id,
            **kwargs,
        }
        self._buf.append(json.dumps(entry))

    def by_op(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def flush(self) -> None:
        if not self._path or not self._buf:
            return
        text = "\n".join(self._buf) + "\n"
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(text)
        self._buf.clear()


