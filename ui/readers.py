"""Data layer for the f5kb console.

One Reader interface, three concrete sources:

- AwsReader        — the deployed S3 bucket + live SQS/CloudWatch/Lambda/SNS.
- LocalReader      — an S3-mirror tree on disk (live/, pending/, runs/, ...),
                     i.e. what `aws s3 sync s3://bucket local-dir` produces or
                     what the handlers write through LocalStorage.
- CliOutputsReader — the classic CLI layout (outputs/dump/<Type>/<id>.json with
                     _pending/_replaced/_changelog.jsonl). Auto-selected when the
                     local root has no S3-mirror dirs but does have a dump tree.

All keys are S3-style forward-slash paths. Mutations are shared where possible:
save_article/restore_article run through the StorageBackend so local and AWS
behave identically (archive-before-overwrite, hash-index update, audit trail).
"""

from __future__ import annotations

import concurrent.futures
import datetime
import difflib
import json
import os
import pathlib
import time
from typing import Any

import yaml

from f5kb.lib.dump import db_key
from f5kb.storage.local import LocalStorage
from f5kb.track.hashing import sha256_obj

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent

# The 13 canonical type keys + which 4 are enrichable.
ALL_TYPES = [
    "Support_Solution", "Known_Issue", "Knowledge", "Security_Advisory", "Video",
    "Policy", "Operations_Guide", "Compliance", "Education", "Manual",
    "Release_Note", "Supplemental_Document", "Bug_Tracker",
]
ENRICHABLE = {"Manual", "Release_Note", "Supplemental_Document", "Bug_Tracker"}

HASH_INDEX_KEY = "hash-index/current.json.gz"

# Prefixes the generic /api/object endpoint may serve (read-only browse).
BROWSABLE_PREFIXES = (
    "live/", "pending/", "archive/", "runs/", "lambda/state/",
    "audit/", "changelogs/", "hash-index/",
)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _month() -> str:
    return today()[:7]


def _ensure_ca_bundle() -> None:
    """boto3 fails with SSLError '[Errno 2] No such file or directory' when
    SSL_CERT_FILE/AWS_CA_BUNDLE/REQUESTS_CA_BUNDLE point at a missing file
    (common on macOS with a stale env). Point boto3 at certifi's bundle unless
    an existing valid path is already set."""
    for var in ("AWS_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        val = os.environ.get(var)
        if val and pathlib.Path(val).is_file():
            return
    try:
        import certifi
    except ImportError:
        return
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        os.environ.pop(var, None)
    os.environ["AWS_CA_BUNDLE"] = certifi.where()


class _TTLCache:
    """Tiny per-process cache for expensive listings (corpus counts, key lists)."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Any | None:
        hit = self._data.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        return None

    def put(self, key: str, value: Any) -> None:
        self._data[key] = (time.time(), value)

    def drop(self, prefix: str = "") -> None:
        for k in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(k, None)


def parse_jsonl(text: str | None) -> list[dict]:
    out: list[dict] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def body_text_of(article: dict | None) -> str:
    """Body text across both envelope generations (body_text vs bodyText)."""
    content = (article or {}).get("content") or {}
    for k in ("body_text", "bodyText"):
        v = content.get(k)
        if isinstance(v, str):
            return v
    return ""


def structured_diff(old: dict | None, new: dict | None) -> dict:
    """Line diff of the two bodies + shallow metadata field diff."""
    old_body = body_text_of(old).splitlines()
    new_body = body_text_of(new).splitlines()
    hunks: list[dict] = []
    matcher = difflib.SequenceMatcher(a=old_body, b=new_body, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            seg = old_body[i1:i2]
            if len(seg) > 8:  # collapse long unchanged stretches
                hunks.append({"tag": "equal", "lines": seg[:3]})
                hunks.append({"tag": "skip", "count": len(seg) - 6})
                hunks.append({"tag": "equal", "lines": seg[-3:]})
            else:
                hunks.append({"tag": "equal", "lines": seg})
        else:
            if i2 > i1:
                hunks.append({"tag": "del", "lines": old_body[i1:i2]})
            if j2 > j1:
                hunks.append({"tag": "add", "lines": new_body[j1:j2]})

    meta_changes: list[dict] = []
    om, nm = (old or {}).get("metadata") or {}, (new or {}).get("metadata") or {}
    for k in sorted(set(om) | set(nm)):
        ov, nv = om.get(k), nm.get(k)
        if ov != nv:
            meta_changes.append({
                "field": k,
                "old": json.dumps(ov, ensure_ascii=False)[:400],
                "new": json.dumps(nv, ensure_ascii=False)[:400],
            })
    return {
        "body": hunks,
        "metadata": meta_changes,
        "old_chars": len(body_text_of(old)),
        "new_chars": len(body_text_of(new)),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Reader interface
# ══════════════════════════════════════════════════════════════════════════════
class Reader:
    mode = "abstract"
    layout = "mirror"  # "mirror" (S3-style keys) | "cli" (outputs/dump tree)
    writable = False
    stage = ""

    def __init__(self) -> None:
        self.cache = _TTLCache()

    # ── identity ──────────────────────────────────────────────────────────────
    def label(self) -> dict[str, Any]:
        raise NotImplementedError

    # ── raw key I/O (S3-style keys) ───────────────────────────────────────────
    def get_json(self, key: str) -> Any | None:
        raise NotImplementedError

    def read_text(self, key: str) -> str | None:
        raise NotImplementedError

    def list_keys(self, prefix: str) -> list[str]:
        raise NotImplementedError

    # ── pipeline views ────────────────────────────────────────────────────────
    def list_run_dates(self, limit: int = 30) -> list[str]:
        keys = self.cache.get("run-dates", 15)
        if keys is None:
            dates = set()
            for k in self.list_keys("runs/"):
                parts = k.split("/")
                if len(parts) > 1 and parts[1]:
                    dates.add(parts[1])
            keys = sorted(dates, reverse=True)
            self.cache.put("run-dates", keys)
        return keys[:limit]

    def corpus_counts(self, refresh: bool = False) -> dict[str, int]:
        ttl = 0 if refresh else 300
        cached = self.cache.get("corpus", ttl)
        if cached is not None:
            return cached
        counts: dict[str, int] = {}
        for t in self.corpus_types():
            n = sum(1 for k in self.article_keys(t) if k.endswith(".json"))
            if n:
                counts[t] = n
        self.cache.put("corpus", counts)
        return counts

    def corpus_types(self) -> list[str]:
        return ALL_TYPES

    def article_keys(self, type_key: str) -> list[str]:
        """All live-article keys for a type (cached; potentially large)."""
        ck = f"articles:{type_key}"
        keys = self.cache.get(ck, 60)
        if keys is None:
            keys = [k for k in self.list_keys(f"live/{type_key}/") if k.endswith(".json")]
            self.cache.put(ck, keys)
        return keys

    def article_id_from_key(self, key: str) -> str:
        return key.rsplit("/", 1)[-1][: -len(".json")]

    def live_key(self, type_key: str, art_id: str) -> str:
        return f"live/{type_key}/{art_id}.json"

    def pending_key(self, type_key: str, art_id: str) -> str:
        return f"pending/{type_key}/{art_id}.json"

    def exists(self, key: str) -> bool:
        return self.get_json(key) is not None

    def find_article(self, art_id: str) -> list[dict]:
        """Locate an id across every type (live and pending)."""
        out = []
        for t in self.corpus_types():
            if self.exists(self.live_key(t, art_id)):
                out.append({"type_key": t, "id": art_id, "where": "live"})
            elif self.exists(self.pending_key(t, art_id)):
                out.append({"type_key": t, "id": art_id, "where": "pending"})
        return out

    def get_article(self, type_key: str, art_id: str) -> dict | None:
        return self.get_json(self.live_key(type_key, art_id))

    def get_pending(self, type_key: str, art_id: str) -> dict | None:
        return self.get_json(self.pending_key(type_key, art_id))

    def archive_versions(self, type_key: str, art_id: str) -> list[dict]:
        keys = self.list_keys(f"archive/{type_key}/{art_id}/")
        return [{"key": k, "ts": k.rsplit("/", 1)[-1].removesuffix(".json")}
                for k in sorted(keys, reverse=True) if k.endswith(".json")]

    def pending_entries(self, cap: int = 2000) -> dict:
        keys = [k for k in self.list_keys("pending/") if k.endswith(".json")]
        entries = []
        for k in keys[:cap]:
            parts = k.split("/")
            if len(parts) >= 3:
                entries.append({"type_key": parts[1], "id": parts[2].removesuffix(".json"), "key": k})
        return {"total": len(keys), "entries": entries, "capped": len(keys) > cap}

    def changelog(self, month: str | None = None, limit: int = 500) -> list[dict]:
        month = month or _month()
        rows = parse_jsonl(self.read_text(f"audit/{month}/changed_ids.jsonl"))
        return rows[-limit:][::-1]

    def decisions(self, month: str | None = None, limit: int = 500) -> list[dict]:
        month = month or _month()
        rows = parse_jsonl(self.read_text(f"audit/{month}/decisions.jsonl"))
        return rows[-limit:][::-1]

    def changelog_months(self) -> list[str]:
        months = {k.split("/")[1] for k in self.list_keys("audit/") if len(k.split("/")) > 2}
        return sorted(months, reverse=True)

    def hash_index_stats(self) -> dict:
        return {"present": False, "entries": 0}

    # ── AWS-only extras (empty defaults keep the frontend backend-agnostic) ──
    def dlq_depths(self) -> dict[str, int]:
        return {}

    def dlq_messages(self, queue: str) -> list[dict]:
        return []

    def recent_errors(self, minutes: int = 1440) -> list[dict]:
        return []

    # ── mutations (writable targets only) ─────────────────────────────────────
    def trigger_run(self, mode: str) -> dict:
        raise RuntimeError("trigger is only available on AWS targets")

    def approve_action(self, action: str, run_date: str, type_key: str | None,
                       art_id: str | None, actor: str) -> dict:
        raise RuntimeError("approve actions are only available on AWS targets")

    def backfill(self, run_date: str, manifest_key: str, article_count: int,
                 mode: str = "incremental") -> dict:
        raise RuntimeError("backfill is only available on AWS targets")

    def restore_article(self, type_key: str, art_id: str, archive_key: str, actor: str) -> dict:
        raise RuntimeError("restore not supported on this target")

    def save_article(self, type_key: str, art_id: str, article: dict, actor: str) -> dict:
        raise RuntimeError("editing not supported on this target")


# ══════════════════════════════════════════════════════════════════════════════
#  Store-backed base: everything shared between LocalReader and AwsReader
# ══════════════════════════════════════════════════════════════════════════════
class StoreReader(Reader):
    """Reader over a StorageBackend (LocalStorage or S3Storage)."""

    def __init__(self, store: Any) -> None:
        super().__init__()
        self.store = store

    def get_json(self, key: str) -> Any | None:
        try:
            if key.endswith(".gz"):
                import gzip
                return json.loads(gzip.decompress(self.store.get_bytes(key)))
            return self.store.get(key)
        except (KeyError, json.JSONDecodeError):
            return None
        except Exception:
            return None

    def read_text(self, key: str) -> str | None:
        try:
            return self.store.get_bytes(key).decode("utf-8")
        except KeyError:
            return None
        except Exception:
            return None

    def list_keys(self, prefix: str) -> list[str]:
        try:
            return self.store.list_prefix(prefix)
        except Exception:
            return []

    def exists(self, key: str) -> bool:
        try:
            return bool(self.store.exists(key))
        except Exception:
            return False

    def hash_index_stats(self) -> dict:
        try:
            idx = self.store.load_hash_index(HASH_INDEX_KEY)
        except Exception:
            idx = {}
        return {"present": bool(idx), "entries": len(idx), "key": HASH_INDEX_KEY}

    # ── shared mutations: archive-before-overwrite + hash-index + audit ──────
    def _promote(self, type_key: str, art_id: str, article: dict, actor: str,
                 op: str, extra_audit: dict | None = None) -> dict:
        """Write an article to live/ with the full safety protocol."""
        live_key = self.live_key(type_key, art_id)
        stamp = now_iso().replace(":", "-")
        displaced_to: str | None = None
        try:
            current = self.store.get(live_key)
        except KeyError:
            current = None
        if current is not None:
            displaced_to = f"archive/{type_key}/{art_id}/{stamp}.json"
            self.store.put(displaced_to, current)

        self.store.put(live_key, article)

        # hash-index: keep skip-unchanged coherent with the new metadata.
        idx = self.store.load_hash_index(HASH_INDEX_KEY)
        idx[db_key(type_key, art_id)] = article.get("metadata_hash") or sha256_obj(
            article.get("metadata") or {})
        self.store.save_hash_index(idx, HASH_INDEX_KEY)

        month = _month()
        rec = {
            "op": op, "id": art_id, "type": type_key, "s3_key": live_key,
            "run_date": article.get("run_date") or today(),
            "approved_by": actor, "hash": idx[db_key(type_key, art_id)],
            "ts": now_iso(),
        }
        rec.update(extra_audit or {})
        self.store.append_jsonl(f"audit/{month}/changed_ids.jsonl", rec)
        self.store.append_jsonl(f"audit/{month}/decisions.jsonl", {
            "op": op, "id": art_id, "type": type_key, "actor": actor,
            "source": "dashboard", "run_date": rec["run_date"], "ts": rec["ts"],
            **({k: v for k, v in (extra_audit or {}).items() if k == "restored_from"}),
        })
        self.cache.drop("articles:")
        self.cache.drop("corpus")
        return {"live_key": live_key, "displaced_to": displaced_to, "ts": rec["ts"]}

    def save_article(self, type_key: str, art_id: str, article: dict, actor: str) -> dict:
        if not self.writable:
            raise RuntimeError("read-only")
        # Recompute hashes so the envelope stays self-consistent after an edit.
        article = dict(article)
        article["type_key"] = type_key
        article["id"] = art_id
        article["metadata_hash"] = sha256_obj(article.get("metadata") or {})
        article["content_hash"] = sha256_obj(article.get("content") or {})
        res = self._promote(type_key, art_id, article, actor, op="edited")
        return {"status": "saved", **res}

    def restore_article(self, type_key: str, art_id: str, archive_key: str, actor: str) -> dict:
        if not self.writable:
            raise RuntimeError("read-only")
        try:
            archived = self.store.get(archive_key)
        except KeyError:
            return {"status": "error", "error": f"archive key not found: {archive_key}"}
        res = self._promote(type_key, art_id, archived, actor, op="restored",
                            extra_audit={"restored_from": archive_key})
        return {"status": "restored", "restored_from": archive_key, **res}


# ══════════════════════════════════════════════════════════════════════════════
#  Local: S3-mirror tree on disk
# ══════════════════════════════════════════════════════════════════════════════
class LocalReader(StoreReader):
    mode = "local"
    layout = "mirror"

    def __init__(self, root: pathlib.Path, writable: bool) -> None:
        super().__init__(LocalStorage(str(root)))
        self.root = root
        self.writable = writable

    def label(self) -> dict[str, Any]:
        return {"mode": "local", "target": "local", "layout": self.layout,
                "root": str(self.root), "writable": self.writable,
                "bucket": None, "region": None, "stage": None}

    def corpus_types(self) -> list[str]:
        live = self.root / "live"
        if live.is_dir():
            return sorted(p.name for p in live.iterdir() if p.is_dir())
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  Local: classic CLI outputs/ layout (dump/<Type>/<id>.json)
# ══════════════════════════════════════════════════════════════════════════════
class CliOutputsReader(Reader):
    """Read (and carefully write) the CLI's outputs/ tree.

    Mapping onto the console's vocabulary:
      live corpus  -> <dump>/<Type>/<id>.json
      pending      -> <dump>/_pending/<type>/<id>.json
      archive      -> <dump>/_replaced/<type>/<id>.<stamp>.json
      changelog    -> <dump>/_changelog.jsonl
    Runs / queues / errors do not exist here.
    """

    mode = "local"
    layout = "cli"

    def __init__(self, dump_dir: pathlib.Path, db: pathlib.Path, writable: bool) -> None:
        super().__init__()
        self.dump = dump_dir
        self.db = db
        self.writable = writable

    def label(self) -> dict[str, Any]:
        return {"mode": "local", "target": "local", "layout": "cli",
                "root": str(self.dump), "db": str(self.db),
                "writable": self.writable, "bucket": None, "region": None, "stage": None}

    # ── path mapping ──────────────────────────────────────────────────────────
    def _resolve(self, key: str) -> pathlib.Path | None:
        """Translate an S3-style key into the CLI tree."""
        parts = key.split("/")
        if key.startswith("live/") and len(parts) >= 2:
            return self.dump / pathlib.Path(*parts[1:])
        if key.startswith("pending/"):
            return self.dump / "_pending" / pathlib.Path(*parts[1:])
        if key.startswith("archive/") and len(parts) == 4:
            # archive/<type>/<id>/<stamp>.json -> _replaced/<type>/<id>.<stamp>.json
            t, i, leaf = parts[1], parts[2], parts[3]
            return self.dump / "_replaced" / t / f"{i}.{leaf}"
        return None

    def get_json(self, key: str) -> Any | None:
        p = self._resolve(key)
        if p is None or not p.is_file():
            return None
        try:
            return json.loads(p.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def read_text(self, key: str) -> str | None:
        p = self._resolve(key)
        return p.read_text("utf-8") if p and p.is_file() else None

    def list_keys(self, prefix: str) -> list[str]:
        if prefix.startswith("live/"):
            t = prefix.split("/")[1] if len(prefix.split("/")) > 1 else ""
            base = self.dump / t if t else self.dump
            if not base.is_dir():
                return []
            return sorted(f"live/{t}/{p.name}" for p in base.iterdir()
                          if p.is_file() and p.suffix == ".json" and not p.name.startswith("_"))
        if prefix.startswith("pending/"):
            base = self.dump / "_pending"
            if not base.is_dir():
                return []
            out = []
            for p in base.rglob("*.json"):
                if p.name == "_manifest.json":
                    continue
                rel = p.relative_to(base)
                out.append("pending/" + "/".join(rel.parts))
            return sorted(out)
        return []

    def corpus_types(self) -> list[str]:
        if not self.dump.is_dir():
            return []
        return sorted(p.name for p in self.dump.iterdir()
                      if p.is_dir() and not p.name.startswith("_"))

    def article_keys(self, type_key: str) -> list[str]:
        ck = f"articles:{type_key}"
        keys = self.cache.get(ck, 60)
        if keys is None:
            keys = self.list_keys(f"live/{type_key}/")
            self.cache.put(ck, keys)
        return keys

    def exists(self, key: str) -> bool:
        p = self._resolve(key)
        return bool(p and p.is_file())

    def list_run_dates(self, limit: int = 30) -> list[str]:
        return []

    def archive_versions(self, type_key: str, art_id: str) -> list[dict]:
        base = self.dump / "_replaced" / type_key
        if not base.is_dir():
            return []
        out = []
        for p in sorted(base.glob(f"{art_id}.*.json"), reverse=True):
            stamp = p.name[len(art_id) + 1: -len(".json")]
            out.append({"key": f"archive/{type_key}/{art_id}/{stamp}.json", "ts": stamp})
        return out

    def pending_entries(self, cap: int = 2000) -> dict:
        keys = self.list_keys("pending/")
        entries = []
        for k in keys[:cap]:
            parts = k.split("/")
            if len(parts) >= 3:
                entries.append({"type_key": parts[1], "id": parts[2].removesuffix(".json"), "key": k})
        return {"total": len(keys), "entries": entries, "capped": len(keys) > cap}

    def changelog(self, month: str | None = None, limit: int = 500) -> list[dict]:
        p = self.dump / "_changelog.jsonl"
        rows = parse_jsonl(p.read_text("utf-8") if p.is_file() else None)
        if month:
            rows = [r for r in rows if str(r.get("ts", "")).startswith(month)]
        return rows[-limit:][::-1]

    def decisions(self, month: str | None = None, limit: int = 500) -> list[dict]:
        return []

    def changelog_months(self) -> list[str]:
        p = self.dump / "_changelog.jsonl"
        rows = parse_jsonl(p.read_text("utf-8") if p.is_file() else None)
        return sorted({str(r.get("ts", ""))[:7] for r in rows if r.get("ts")}, reverse=True)

    def hash_index_stats(self) -> dict:
        return {"present": self.db.is_file(), "entries": 0, "key": str(self.db)}

    # ── mutations: mirror the CLI's own overwrite protocol ────────────────────
    def _write_json(self, p: pathlib.Path, data: dict) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")

    def save_article(self, type_key: str, art_id: str, article: dict, actor: str) -> dict:
        if not self.writable:
            raise RuntimeError("read-only")
        live = self.dump / type_key / f"{art_id}.json"
        stamp = now_iso().replace(":", "-")
        displaced_to = None
        if live.is_file():
            dst = self.dump / "_replaced" / type_key / f"{art_id}.{stamp}.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(live.read_bytes())
            displaced_to = str(dst.relative_to(self.dump))
        self._write_json(live, article)
        self._append_changelog({"runId": f"dashboard-{stamp}", "ts": now_iso(), "op": "edited",
                                "documentType": article.get("documentType") or type_key,
                                "id": art_id, "title": article.get("title"),
                                "source": "dashboard"})
        self.cache.drop("articles:")
        self.cache.drop("corpus")
        return {"status": "saved", "live_key": str(live.relative_to(self.dump)),
                "displaced_to": displaced_to,
                "note": "articles.db not updated — run `uv run f5kb track` to re-index"}

    def restore_article(self, type_key: str, art_id: str, archive_key: str, actor: str) -> dict:
        if not self.writable:
            raise RuntimeError("read-only")
        archived = self.get_json(archive_key)
        if archived is None:
            return {"status": "error", "error": f"archive key not found: {archive_key}"}
        res = self.save_article(type_key, art_id, archived, actor)
        return {"status": "restored", "restored_from": archive_key, **{k: v for k, v in res.items() if k != "status"}}

    def _append_changelog(self, entry: dict) -> None:
        p = self.dump / "_changelog.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ══════════════════════════════════════════════════════════════════════════════
#  AWS
# ══════════════════════════════════════════════════════════════════════════════
class AwsReader(StoreReader):
    mode = "aws"
    layout = "mirror"

    def __init__(self, stage: str, region: str, bucket: str | None,
                 account_id: str | None, writable: bool) -> None:
        try:
            import boto3
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("boto3 not installed — run `uv sync`") from e
        _ensure_ca_bundle()
        from f5kb.storage.s3 import S3Storage
        self.stage = stage
        self.region = region
        self.writable = writable
        self._sqs = boto3.client("sqs", region_name=region)
        self._logs = boto3.client("logs", region_name=region)
        self._lambda = boto3.client("lambda", region_name=region)
        self._sns = boto3.client("sns", region_name=region)
        if account_id is None:
            account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
        self.account_id = account_id
        self.bucket = bucket or f"f5kb-articles-{account_id}-{stage}"
        super().__init__(S3Storage(self.bucket))
        self.orchestrator_fn = f"f5kb-orchestrator-{stage}"
        self.approve_fn = f"f5kb-approve-{stage}"
        self.restore_fn = f"f5kb-restore-{stage}"
        self.handoff_topic = f"arn:aws:sns:{region}:{account_id}:f5kb-handoff-{stage}"

    def label(self) -> dict[str, Any]:
        return {"mode": "aws", "target": self.stage, "stage": self.stage, "layout": "mirror",
                "region": self.region, "bucket": self.bucket,
                "account_id": self.account_id, "writable": self.writable,
                "handoff_topic": self.handoff_topic}

    def corpus_types(self) -> list[str]:
        return ALL_TYPES

    def dlq_depths(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for q in (f"f5kb-dump-dlq-{self.stage}", f"f5kb-enrich-dlq-{self.stage}",
                  f"f5kb-dump-queue-{self.stage}", f"f5kb-enrich-queue-{self.stage}"):
            try:
                url = self._sqs.get_queue_url(QueueName=q)["QueueUrl"]
                attrs = self._sqs.get_queue_attributes(
                    QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
                out[q] = int(attrs["Attributes"]["ApproximateNumberOfMessages"])
            except Exception:
                out[q] = -1  # unknown
        return out

    def dlq_messages(self, queue: str) -> list[dict]:
        """Peek up to 10 DLQ messages WITHOUT consuming them. Receiving hides a
        message from other readers for ~5s (VisibilityTimeout); nothing is
        deleted — redrive/inspection tooling still sees every message."""
        allowed = {f"f5kb-dump-dlq-{self.stage}", f"f5kb-enrich-dlq-{self.stage}"}
        if queue not in allowed:
            raise ValueError(f"not a DLQ of this stage: {queue}")
        url = self._sqs.get_queue_url(QueueName=queue)["QueueUrl"]
        resp = self._sqs.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
            VisibilityTimeout=5,
            AttributeNames=["All"],
        )
        out: list[dict] = []
        for m in resp.get("Messages", []):
            raw = m.get("Body") or ""
            try:
                body: Any = json.loads(raw)
            except (ValueError, TypeError):
                body = raw
            attrs = m.get("Attributes") or {}
            sent_ms = int(attrs.get("SentTimestamp") or 0)
            out.append({
                "message_id": m.get("MessageId") or "",
                "sent_at": (
                    datetime.datetime.fromtimestamp(sent_ms / 1000, datetime.timezone.utc)
                    .isoformat().replace("+00:00", "Z") if sent_ms else None
                ),
                "receive_count": int(attrs.get("ApproximateReceiveCount") or 0),
                "body": body,
            })
        return out

    def recent_errors(self, minutes: int = 1440) -> list[dict]:
        start = int((time.time() - minutes * 60) * 1000)
        out: list[dict] = []
        fns = ["orchestrator", "dump", "enrich", "track", "approve", "restore",
               "watchdog", "slack-ack"]
        for fn in fns:
            lg = f"/aws/lambda/f5kb-{fn}-{self.stage}"
            try:
                resp = self._logs.filter_log_events(
                    logGroupName=lg, startTime=start,
                    filterPattern='{ $.level = "ERROR" }', limit=25)
                for ev in resp.get("events", []):
                    try:
                        rec = json.loads(ev["message"])
                    except json.JSONDecodeError:
                        rec = {"msg": ev["message"]}
                    rec["_lambda"] = fn
                    out.append(rec)
            except Exception:
                continue
        out.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return out[:100]

    # ── writes (guarded by writable at the route layer too) ──────────────────
    def trigger_run(self, mode: str) -> dict:
        payload = json.dumps({"mode": mode}).encode()
        resp = self._lambda.invoke(FunctionName=self.orchestrator_fn,
                                   InvocationType="Event", Payload=payload)
        return {"status": resp["StatusCode"], "function": self.orchestrator_fn, "mode": mode}

    def approve_action(self, action: str, run_date: str, type_key: str | None,
                       art_id: str | None, actor: str) -> dict:
        payload: dict[str, Any] = {"action": action, "run_date": run_date, "actor": actor}
        if type_key:
            payload["type_key"] = type_key
        if art_id:
            payload["id"] = art_id
        resp = self._lambda.invoke(FunctionName=self.approve_fn,
                                   InvocationType="Event",
                                   Payload=json.dumps(payload).encode())
        return {"status": resp["StatusCode"], "action": action, "run_date": run_date}

    def backfill(self, run_date: str, manifest_key: str, article_count: int,
                 mode: str = "incremental") -> dict:
        msg = {
            "schema": "f5kb.handoff.v2", "run_date": run_date, "mode": mode,
            "batch": "backfill", "article_count": article_count,
            "manifest_key": manifest_key, "bucket": self.bucket,
            "published_at": now_iso(),
        }
        self._sns.publish(TopicArn=self.handoff_topic, Message=json.dumps(msg))
        return {"status": "published", "run_date": run_date, "batch": "backfill"}

    def restore_article(self, type_key: str, art_id: str, archive_key: str, actor: str) -> dict:
        """Prefer the Restore Lambda (owns the full protocol + SNS handoff)."""
        if not self.writable:
            raise RuntimeError("read-only")
        payload = {"type_key": type_key, "art_id": art_id,
                   "archive_key": archive_key, "actor": actor}
        try:
            resp = self._lambda.invoke(FunctionName=self.restore_fn,
                                       InvocationType="RequestResponse",
                                       Payload=json.dumps(payload).encode())
            body = json.loads(resp["Payload"].read() or b"{}")
            self.cache.drop("articles:")
            return body if isinstance(body, dict) else {"status": "invoked"}
        except Exception as e:
            return {"status": "error", "error": f"restore lambda invoke failed: {e}"}


# ══════════════════════════════════════════════════════════════════════════════
#  Article page helpers (titles need per-article fetches — bounded + parallel)
# ══════════════════════════════════════════════════════════════════════════════
def page_articles(reader: Reader, type_key: str, query: str, page: int, size: int) -> dict:
    keys = reader.article_keys(type_key)
    if query:
        q = query.lower()
        keys = [k for k in keys if q in reader.article_id_from_key(k).lower()]
    total = len(keys)
    size = max(1, min(size, 100))
    pages = max(1, (total + size - 1) // size)
    page = max(1, min(page, pages))
    window = keys[(page - 1) * size: page * size]

    def fetch(key: str) -> dict:
        art = reader.get_json(key) or {}
        meta = art.get("metadata") or {}
        title = meta.get("title") or art.get("title") or ""
        return {
            "id": reader.article_id_from_key(key),
            "key": key,
            "title": str(title)[:200],
            "updated": meta.get("updated_at") or meta.get("date") or art.get("capturedAt")
            or art.get("captured_at") or "",
            "body_chars": len(body_text_of(art)),
            "has_pending": False,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        rows = list(ex.map(fetch, window))
    pend = {(e["type_key"], e["id"]) for e in reader.pending_entries(cap=5000)["entries"]}
    for r in rows:
        r["has_pending"] = (type_key, r["id"]) in pend
    return {"type_key": type_key, "total": total, "page": page, "pages": pages,
            "size": size, "rows": rows}


def load_target(target: str, allow_writes: bool) -> Reader:
    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    targets = cfg.get("targets", {})
    if target not in targets:
        raise SystemExit(f"unknown target '{target}'. options: {', '.join(targets)}")
    t = targets[target]
    if t["mode"] == "local":
        root = pathlib.Path(t.get("root", "outputs"))
        if not root.is_absolute():
            root = REPO / root
        if (root / "live").is_dir() or (root / "runs").is_dir():
            return LocalReader(root, writable=allow_writes)
        dump_dir = pathlib.Path(t.get("dump_dir", "outputs/dump"))
        if not dump_dir.is_absolute():
            dump_dir = REPO / dump_dir
        db = pathlib.Path(t.get("db", "outputs/articles.db"))
        if not db.is_absolute():
            db = REPO / db
        return CliOutputsReader(dump_dir, db, writable=allow_writes)
    return AwsReader(t["stage"], t["region"], t.get("bucket"),
                     t.get("account_id"), writable=allow_writes)
