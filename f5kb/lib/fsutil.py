"""Filesystem + ID helpers shared across subcommands."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Generator


def sanitize_name(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def id_of(r: dict) -> str:
    """Stable, human-friendly ID for a per-article filename."""
    raw = r.get("raw", {}) or {}
    candidate = (
        raw.get("f5_kb_id")
        or raw.get("permanentid")
        or r.get("uniqueId")
        or r.get("title")
        or "article"
    )
    return sanitize_name(str(candidate))[:120]


def read_json(path: str | Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, data: object) -> None:
    """Pretty-printed JSON with trailing newline (matches existing output files)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def walk_article_files(type_dir: str | Path) -> Generator[str, None, None]:
    """Yield each article JSON file in a type dir, skipping _catalogue.json/_index.json."""
    for entry in sorted(Path(type_dir).iterdir()):
        if entry.is_file() and entry.suffix == ".json" and not entry.name.startswith("_"):
            yield str(entry)


def list_type_dirs(dump_dir: str | Path) -> list[str]:
    """Return subdirs not starting with '_' (skips _pending/, _replaced/, _deleted/)."""
    dirs = []
    for entry in Path(dump_dir).iterdir():
        if entry.is_dir() and not entry.name.startswith("_"):
            dirs.append(entry.name)
    return sorted(dirs)


def path_exists(path: str | Path) -> bool:
    return Path(path).exists()


def iso_now() -> str:
    """Current UTC time as ISO 8601 string: 2024-01-15T12:34:56Z"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
