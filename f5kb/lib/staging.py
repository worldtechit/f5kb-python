"""Overwrite-protection staging: shared primitive behind the approval gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from f5kb.lib.fsutil import path_exists, read_json, write_json
from f5kb.track.hashing import content_for_hash, has_body, sha256_obj

PENDING_DIRNAME = "_pending"
REPLACED_DIRNAME = "_replaced"


def pending_dir(out_dir: str) -> str:
    return str(Path(out_dir.rstrip("/")) / PENDING_DIRNAME)


def pending_path(out_dir: str, type_key: str, id: str) -> str:
    return str(Path(pending_dir(out_dir)) / type_key / f"{id}.json")


def live_path(out_dir: str, type_key: str, id: str) -> str:
    return str(Path(out_dir.rstrip("/")) / type_key / f"{id}.json")


def manifest_path(out_dir: str) -> str:
    return str(Path(pending_dir(out_dir)) / "_manifest.json")


def replaced_path(out_dir: str, type_key: str, id: str, stamp: str) -> str:
    return str(Path(out_dir.rstrip("/")) / REPLACED_DIRNAME / type_key / f"{id}.{stamp}.json")


def now_stamp(ms: int) -> str:
    """Filesystem-safe timestamp: ISO with :/. -> -"""
    import datetime
    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    iso = dt.isoformat().replace("+00:00", "Z").replace(":", "-").replace(".", "-")
    return iso


@dataclass
class PendingEntry:
    type_key: str
    id: str
    op: str  # "edited"
    source: str
    title: str | None = None
    changed: list[str] = field(default_factory=list)
    hash_old: str | None = None
    hash_new: str | None = None
    staged_at: str = ""

    def to_dict(self) -> dict:
        d: dict = {
            "typeKey": self.type_key,
            "id": self.id,
            "op": self.op,
            "source": self.source,
            "stagedAt": self.staged_at,
        }
        if self.title is not None:
            d["title"] = self.title
        if self.changed:
            d["changed"] = self.changed
        if self.hash_old is not None:
            d["hashOld"] = self.hash_old
        if self.hash_new is not None:
            d["hashNew"] = self.hash_new
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PendingEntry":
        return cls(
            type_key=d.get("typeKey") or "",
            id=d.get("id") or "",
            op=d.get("op") or "edited",
            source=d.get("source") or "",
            title=d.get("title"),
            changed=d.get("changed") or [],
            hash_old=d.get("hashOld"),
            hash_new=d.get("hashNew"),
            staged_at=d.get("stagedAt") or "",
        )


def load_pending_manifest(out_dir: str) -> dict:
    p = manifest_path(out_dir)
    if not path_exists(p):
        return {"generatedAt": "", "entries": []}
    try:
        return read_json(p)
    except Exception:
        return {"generatedAt": "", "entries": []}


def save_pending_manifest(out_dir: str, data: dict) -> None:
    Path(pending_dir(out_dir)).mkdir(parents=True, exist_ok=True)
    write_json(manifest_path(out_dir), data)


def merge_pending(out_dir: str, entries: list[PendingEntry], now_iso: str) -> None:
    if not entries:
        return
    data = load_pending_manifest(out_dir)
    by_key: dict[str, dict] = {
        f"{e['typeKey']} {e['id']}": e for e in data.get("entries") or []
    }
    for e in entries:
        by_key[f"{e.type_key} {e.id}"] = e.to_dict()
    data["entries"] = sorted(
        by_key.values(),
        key=lambda x: (x.get("typeKey") or "", x.get("id") or ""),
    )
    data["generatedAt"] = now_iso
    save_pending_manifest(out_dir, data)


def _body_len(a: dict | None) -> int:
    content = (a or {}).get("content") or {}
    t = content.get("body_text")
    if not isinstance(t, str):
        t = content.get("bodyText")  # tolerate legacy key
    return len(t) if isinstance(t, str) else 0


def compute_risk(live: dict | None, pending: dict) -> list[str]:
    flags: list[str] = []
    if not live:
        return flags
    body_error = (pending.get("content") or {}).get("bodyError")
    if isinstance(body_error, str) and body_error:
        flags.append("body-error")
    live_had = has_body(live.get("content"))
    pend_has = has_body(pending.get("content"))
    if live_had and not pend_has:
        flags.append("body-dropped")
    elif live_had and pend_has:
        lo = _body_len(live)
        pe = _body_len(pending)
        if lo > 0 and pe < lo * 0.5:
            pct = round((1 - pe / lo) * 100)
            flags.append(f"body-shrank-{pct}%")
    return flags


def diff_parts(live: dict | None, pending: dict) -> list[str]:
    if not live:
        return []
    parts: list[str] = []
    if sha256_obj(live.get("metadata") or {}) != sha256_obj(pending.get("metadata") or {}):
        parts.append("metadata")
    if sha256_obj(content_for_hash(live.get("content"))) != sha256_obj(content_for_hash(pending.get("content"))):
        parts.append("content")
    return parts


def change_kind(parts: list[str]) -> str:
    m = "metadata" in parts
    c = "content" in parts
    if m and c:
        return "metadata+content"
    if m:
        return "metadata-only"
    if c:
        return "content-only"
    return "no-op"


def live_article(out_dir: str, type_key: str, id: str) -> dict | None:
    p = live_path(out_dir, type_key, id)
    if not path_exists(p):
        return None
    try:
        return read_json(p)
    except Exception:
        return None


def archive_replaced(out_dir: str, type_key: str, id: str, stamp: str) -> str | None:
    src = live_path(out_dir, type_key, id)
    if not path_exists(src):
        return None
    dest = replaced_path(out_dir, type_key, id, stamp)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(src).rename(dest)
    return dest
