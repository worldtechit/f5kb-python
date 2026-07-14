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
import re
import threading
import time
from typing import Any, Callable

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

# Every pipeline Lambda (log-group + fan-out order for the log viewer).
LAMBDA_FNS = ["orchestrator", "dump", "enrich", "track", "approve", "restore",
              "watchdog", "slack-ack"]

# Lambda REPORT platform line, e.g.
# "REPORT RequestId: … Duration: 123.45 ms Billed Duration: 124 ms
#  Memory Size: 1024 MB Max Memory Used: 250 MB"
_REPORT_RE = re.compile(
    r"Duration: ([\d.]+) ms\s+Billed Duration: (\d+) ms\s+"
    r"Memory Size: (\d+) MB\s+Max Memory Used: (\d+) MB")

# us-east-2 x86 Lambda pricing (2026): $ per GB-second + $ per request.
_LAMBDA_GBS_USD = 0.0000166667
_LAMBDA_REQ_USD = 0.20 / 1_000_000


def parse_report_line(msg: str) -> dict | None:
    """Extract duration/memory numbers from a Lambda REPORT platform line."""
    if not msg.startswith("REPORT "):
        return None
    m = _REPORT_RE.search(msg)
    if not m:
        return None
    return {
        "duration_ms": float(m.group(1)),
        "billed_ms": int(m.group(2)),
        "memory_mb": int(m.group(3)),
        "max_memory_mb": int(m.group(4)),
    }

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
        self._lock = threading.Lock()
        self._inflight: set[str] = set()

    def get(self, key: str, ttl: float) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        return None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)

    def drop(self, prefix: str = "") -> None:
        with self._lock:
            for k in [k for k in self._data if k.startswith(prefix)]:
                self._data.pop(k, None)

    def swr(self, key: str, ttl: float, loader: Callable[[], Any]) -> Any:
        """Stale-while-revalidate: a fresh hit returns immediately; a stale hit
        returns the stale value and refreshes in a background thread (one at a
        time per key); only a cold miss blocks on the loader."""
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit and (now - hit[0]) < ttl:
                return hit[1]
            start_refresh = hit is not None and key not in self._inflight
            if start_refresh:
                self._inflight.add(key)
        if hit is not None:
            if start_refresh:
                threading.Thread(target=self._refresh, args=(key, loader),
                                 daemon=True).start()
            return hit[1]
        value = loader()  # cold: block this one request
        self.put(key, value)
        return value

    def _refresh(self, key: str, loader: Callable[[], Any]) -> None:
        try:
            self.put(key, loader())
        except Exception:
            pass  # keep serving the stale value; next stale hit retries
        finally:
            with self._lock:
                self._inflight.discard(key)


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

    def get_json_many(self, keys: list[str]) -> dict[str, Any]:
        """Fetch many keys concurrently; absent keys map to None."""
        if not keys:
            return {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(keys))) as ex:
            return dict(zip(keys, ex.map(self.get_json, keys)))

    def count_jsonl_lines(self, key: str) -> int:
        """Line count of a (potentially multi-MB) JSONL object. Briefly cached:
        it feeds live run-progress numbers, where a ~30s lag is fine and
        re-downloading a run manifest on every poll is not."""

        def load() -> int:
            txt = self.read_text(key)
            return sum(1 for ln in txt.splitlines() if ln.strip()) if txt else 0

        return self.cache.swr(f"nlines:{key}", 30, load)

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

    def _list_article_keys(self, type_key: str) -> list[str]:
        """Ground truth for one type straight from the store listing (slow on
        S3 — subclasses may serve article_keys from a cheaper source, but this
        stays the reference the refresh path counts from)."""
        return [k for k in self.list_keys(f"live/{type_key}/") if k.endswith(".json")]

    def _count_by_listing(self) -> dict[str, int]:
        types = self.corpus_types()
        if not types:
            return {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(types))) as ex:
            sizes = ex.map(lambda t: len(self._list_article_keys(t)), types)
        return {t: n for t, n in zip(types, sizes) if n}

    def corpus_counts(self, refresh: bool = False) -> dict[str, int]:
        if refresh:
            self.cache.drop("articles:")
            counts = self._count_by_listing()
            self.cache.put("corpus", counts)
            return counts
        return self.cache.swr("corpus", 300, self._count_by_listing)

    def corpus_types(self) -> list[str]:
        return ALL_TYPES

    def article_keys(self, type_key: str) -> list[str]:
        """All live-article keys for a type (cached; potentially large)."""
        return self.cache.swr(f"articles:{type_key}", 60,
                              lambda: self._list_article_keys(type_key))

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

    def _list_pending_keys(self) -> list[str]:
        return [k for k in self.list_keys("pending/") if k.endswith(".json")]

    def pending_entries(self, cap: int = 2000) -> dict:
        # During a run pending/ holds every staged article (tens of thousands of
        # keys) — cache the listing so polling pages don't re-sweep the prefix.
        keys = self.cache.swr("pending-keys", 60, self._list_pending_keys)
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

    def logs(self, fn: str = "all", minutes: int = 180, level: str = "all",
             query: str = "", limit: int = 300) -> list[dict]:
        return []

    def pipeline_state(self) -> dict:
        return {}

    def integrations(self) -> dict:
        return {}

    def health_check(self) -> list[dict]:
        return []

    def cost_report(self, minutes: int = 1440) -> dict:
        return {}

    def redrive_dlq(self, queue: str, message_id: str | None = None) -> dict:
        raise RuntimeError("DLQ redrive is only available on AWS targets")

    def set_pipeline_enabled(self, enabled: bool) -> dict:
        raise RuntimeError("pipeline controls are only available on AWS targets")

    def purge_queues(self) -> dict:
        raise RuntimeError("queue purge is only available on AWS targets")

    def delete_run(self, run_date: str, include_pending: bool = False,
                   dry_run: bool = True) -> dict:
        raise RuntimeError("delete-run is not supported on this target")

    def resolve_pending(self, action: str, items: list[dict], actor: str) -> dict:
        raise RuntimeError("pending resolution is not supported on this target")

    def resolve_pending_type(self, action: str, type_key: str, actor: str) -> dict:
        raise RuntimeError("pending resolution is not supported on this target")

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
        # Reuse the cached parse — reloading the ~3MB gzip on every poll is
        # exactly the kind of hot-path cost this cache exists to absorb.
        ids = self._live_ids_by_type() or {}
        entries = sum(len(v) for v in ids.values())
        return {"present": bool(entries), "entries": entries, "key": HASH_INDEX_KEY}

    # ── corpus via the hash index (one ~3MB GET, not 100+ LIST round-trips) ──
    # Every promotion to live/ also writes its db_key ("<type_key> <id>") into
    # hash-index/current.json.gz, so the index mirrors live/ exactly. Deriving
    # counts and key lists from it replaces the paginated list_objects_v2 sweep
    # over the whole corpus that made every Overview/Corpus load take ~30s.

    def _live_ids_by_type(self) -> dict[str, list[str]] | None:
        """Per-type live article ids parsed from the hash index; None when the
        index is absent (fresh bucket / plain mirror) so callers fall back to
        listing the store."""

        def load() -> dict[str, list[str]]:
            try:
                idx = self.store.load_hash_index(HASH_INDEX_KEY)
            except Exception:
                idx = {}
            out: dict[str, list[str]] = {}
            for k in idx:
                type_key, _, art_id = k.partition(" ")
                if type_key and art_id:
                    out.setdefault(type_key, []).append(art_id)
            for ids in out.values():
                ids.sort()
            return out

        ids = self.cache.swr("live-ids", 60, load)
        return ids or None

    def corpus_counts(self, refresh: bool = False) -> dict[str, int]:
        if refresh:
            # Ground truth on demand: re-list the store, then let the next
            # index read pick up fresh data too.
            self.cache.drop("live-ids")
            return super().corpus_counts(refresh=True)
        ids = self._live_ids_by_type()
        if ids is not None:
            known = self.corpus_types()
            ordered = known + sorted(set(ids) - set(known))
            return {t: len(ids[t]) for t in ordered if ids.get(t)}
        return super().corpus_counts(refresh)

    def article_keys(self, type_key: str) -> list[str]:
        ids = self._live_ids_by_type()
        if ids is not None:
            return [self.live_key(type_key, i) for i in ids.get(type_key, [])]
        return super().article_keys(type_key)

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
        self.cache.drop("live-ids")
        self.cache.drop("pending-keys")
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

    # ── bulk-resolve pending staged articles (console-side, full protocol) ───
    def resolve_pending(self, action: str, items: list[dict], actor: str) -> dict:
        """Approve (promote to live) or reject (drop) pending staged articles
        that are NOT held — held ones go through the Approve Lambda instead.
        Approve follows the full protocol via _promote (archive-before-
        overwrite + hash-index + audit). NOTE: no P2 handoff SNS message is
        published — use backfill afterwards if P2 must receive these."""
        if not self.writable:
            raise RuntimeError("read-only")
        done, missing, errors = 0, 0, 0
        for it in items:
            type_key = it.get("type_key") or ""
            art_id = it.get("id") or ""
            pkey = self.pending_key(type_key, art_id)
            try:
                pending = self.get_json(pkey)
                if pending is None:
                    missing += 1
                    continue
                if action == "approve":
                    self._promote(type_key, art_id, pending, actor, op="approved")
                    self.store.delete(pkey)
                else:
                    self.store.delete(pkey)
                    self.store.append_jsonl(f"audit/{_month()}/decisions.jsonl", {
                        "op": "rejected", "id": art_id, "type": type_key,
                        "actor": actor, "source": "dashboard", "ts": now_iso(),
                    })
                done += 1
            except Exception:
                errors += 1
        self.cache.drop("pending-keys")
        self.cache.drop("live-ids")
        self.cache.drop("corpus")
        return {"status": action, "done": done, "missing": missing,
                "errors": errors, "total": len(items)}

    def _append_jsonl_many(self, key: str, entries: list[dict]) -> None:
        """Batch JSONL append — one get + one put for N entries. The store's
        per-entry append_jsonl would rewrite the whole file N times (O(n^2))."""
        if not entries:
            return
        try:
            existing = self.store.get_bytes(key).decode("utf-8")
        except KeyError:
            existing = ""
        lines = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries)
        self.store.put_bytes(key, (existing + lines).encode("utf-8"),
                             content_type="application/x-ndjson")

    def resolve_pending_type(self, action: str, type_key: str, actor: str) -> dict:
        """Approve or reject EVERY pending article of one type.

        Bulk-optimised: the hash index is loaded/saved ONCE and the audit files
        appended ONCE — routing each article through _promote would rewrite the
        ~3MB index and the audit JSONL per article. Same protocol otherwise:
        archive-before-overwrite per article, hash-index update, audit trail.
        Like resolve_pending, no P2 handoff SNS is published."""
        if not self.writable:
            raise RuntimeError("read-only")
        keys = [k for k in self.list_keys(f"pending/{type_key}/") if k.endswith(".json")]
        stamp = now_iso()
        month = _month()
        result = {"status": action, "type_key": type_key, "done": 0,
                  "missing": 0, "errors": 0, "total": len(keys)}
        if not keys:
            return result

        if action == "reject":
            result["done"] = self.store.delete_many(keys)
            self.store.append_jsonl(f"audit/{month}/decisions.jsonl", {
                "op": "rejected_type", "type": type_key, "count": result["done"],
                "actor": actor, "source": "dashboard", "ts": stamp,
            })
        else:
            fs_stamp = stamp.replace(":", "-")
            idx = self.store.load_hash_index(HASH_INDEX_KEY)

            def promote(pkey: str) -> tuple[str, str | None, str | None]:
                art_id = self.article_id_from_key(pkey)
                try:
                    art = self.store.get(pkey)
                except KeyError:
                    return ("missing", art_id, None)
                try:
                    live_key = self.live_key(type_key, art_id)
                    try:
                        current = self.store.get(live_key)
                    except KeyError:
                        current = None
                    if current is not None:
                        self.store.put(f"archive/{type_key}/{art_id}/{fs_stamp}.json", current)
                    self.store.put(live_key, art)
                    self.store.delete(pkey)
                    h = art.get("metadata_hash") or sha256_obj(art.get("metadata") or {})
                    return ("done", art_id, h)
                except Exception:
                    return ("error", art_id, None)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                outcomes = list(ex.map(promote, keys))

            changed_recs: list[dict] = []
            for status, art_id, h in outcomes:
                if status == "done" and art_id and h:
                    result["done"] += 1
                    idx[db_key(type_key, art_id)] = h
                    changed_recs.append({
                        "op": "approved", "id": art_id, "type": type_key,
                        "s3_key": self.live_key(type_key, art_id),
                        "approved_by": actor, "hash": h, "ts": stamp,
                    })
                elif status == "missing":
                    result["missing"] += 1
                else:
                    result["errors"] += 1

            self.store.save_hash_index(idx, HASH_INDEX_KEY)
            self._append_jsonl_many(f"audit/{month}/changed_ids.jsonl", changed_recs)
            self.store.append_jsonl(f"audit/{month}/decisions.jsonl", {
                "op": "approved_type", "type": type_key, "count": result["done"],
                "errors": result["errors"] or None, "actor": actor,
                "source": "dashboard", "ts": stamp,
            })

        for prefix in ("pending-keys", "live-ids", "corpus", "articles:"):
            self.cache.drop(prefix)
        return result

    # ── delete a run's data (never touches live/, archive/, audit/, hash-index) ──
    def _run_delete_keys(self, run_date: str, include_pending: bool) -> dict[str, list[str]]:
        """Collect exactly what a delete-run would remove, grouped for display."""
        groups = {
            "runs": self.list_keys(f"runs/{run_date}/"),
            "state": self.list_keys(f"lambda/state/{run_date}/"),
            "pending": [],
        }
        if include_pending:
            # Only the pending articles THIS run staged (from its manifests),
            # intersected with what still exists — pending/ is shared across runs.
            staged: set[str] = set()
            for t in self.corpus_types():
                for row in parse_jsonl(self.read_text(f"runs/{run_date}/manifest/{t}.jsonl")):
                    art_id = row.get("id")
                    if art_id:
                        staged.add(self.pending_key(row.get("type_key") or t, str(art_id)))
            existing = set(self.cache.swr("pending-keys", 60, self._list_pending_keys))
            groups["pending"] = sorted(staged & existing)
        return groups

    def delete_run(self, run_date: str, include_pending: bool = False,
                   dry_run: bool = True, actor: str = "console") -> dict:
        if not dry_run and not self.writable:
            raise RuntimeError("read-only")
        groups = self._run_delete_keys(run_date, include_pending)
        counts = {g: len(keys) for g, keys in groups.items()}
        total = sum(counts.values())
        if dry_run:
            return {"status": "dry_run", "run_date": run_date, "counts": counts,
                    "total": total,
                    "sample": [k for keys in groups.values() for k in keys[:5]][:15]}
        allowed = (f"runs/{run_date}/", f"lambda/state/{run_date}/", "pending/")
        keys = [k for keys in groups.values() for k in keys]
        assert all(k.startswith(allowed) for k in keys), "refusing non-run-scoped key"
        deleted = self.store.delete_many(keys)
        self.store.append_jsonl(f"audit/{_month()}/decisions.jsonl", {
            "op": "run_deleted", "run_date": run_date, "actor": actor,
            "source": "dashboard", "counts": counts, "ts": now_iso(),
        })
        for prefix in ("run-dates", "pending-keys", "nlines:"):
            self.cache.drop(prefix)
        return {"status": "deleted", "run_date": run_date, "counts": counts,
                "total": deleted}


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

    def _list_pending_keys(self) -> list[str]:
        # pending/ can hold every staged article of a run. Type keys are
        # canonical on AWS (the orchestrator fails hard on anything else), so
        # fan the listing out per type instead of walking one sequential
        # continuation-token chain over the whole prefix.
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            parts = ex.map(lambda t: self.list_keys(f"pending/{t}/"), ALL_TYPES)
        return sorted(k for keys in parts for k in keys if k.endswith(".json"))

    def _queue_url(self, name: str) -> str:
        url = self.cache.get(f"qurl:{name}", 3600)
        if url is None:
            url = self._sqs.get_queue_url(QueueName=name)["QueueUrl"]
            self.cache.put(f"qurl:{name}", url)
        return url

    def dlq_depths(self) -> dict[str, int]:
        names = [f"f5kb-dump-dlq-{self.stage}", f"f5kb-enrich-dlq-{self.stage}",
                 f"f5kb-dump-queue-{self.stage}", f"f5kb-enrich-queue-{self.stage}"]

        def depth(q: str) -> tuple[str, int]:
            try:
                attrs = self._sqs.get_queue_attributes(
                    QueueUrl=self._queue_url(q),
                    AttributeNames=["ApproximateNumberOfMessages"])
                return q, int(attrs["Attributes"]["ApproximateNumberOfMessages"])
            except Exception:
                return q, -1  # unknown

        def load() -> dict[str, int]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                return dict(ex.map(depth, names))

        # Overview + run detail both read this on every poll — fetch the four
        # queues concurrently and serve a briefly-cached value in between.
        return self.cache.swr("dlq-depths", 8, load)

    def dlq_messages(self, queue: str) -> list[dict]:
        """Peek up to 10 DLQ messages WITHOUT consuming them. Receiving hides a
        message from other readers for ~5s (VisibilityTimeout); nothing is
        deleted — redrive/inspection tooling still sees every message.

        Uses LONG polling with retries: a short poll (WaitTimeSeconds=0) samples
        only a subset of SQS servers and routinely returns nothing on a sparse
        queue even when messages exist — the "1 message but click shows empty"
        bug. Each retry also collects messages the previous receive hid, so a
        few rounds gather the full (approximate) depth."""
        allowed = {f"f5kb-dump-dlq-{self.stage}", f"f5kb-enrich-dlq-{self.stage}"}
        if queue not in allowed:
            raise ValueError(f"not a DLQ of this stage: {queue}")
        url = self._queue_url(queue)
        try:
            attrs = self._sqs.get_queue_attributes(
                QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
            expected = int(attrs["Attributes"]["ApproximateNumberOfMessages"])
        except Exception:
            expected = 0
        target = min(max(expected, 1), 10)

        seen: dict[str, dict] = {}
        for _ in range(4):
            resp = self._sqs.receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=2,
                VisibilityTimeout=5,
                AttributeNames=["All"],
            )
            for m in resp.get("Messages", []):
                mid = m.get("MessageId") or ""
                if mid in seen:
                    continue
                raw = m.get("Body") or ""
                try:
                    body: Any = json.loads(raw)
                except (ValueError, TypeError):
                    body = raw
                mattrs = m.get("Attributes") or {}
                sent_ms = int(mattrs.get("SentTimestamp") or 0)
                seen[mid] = {
                    "message_id": mid,
                    "sent_at": (
                        datetime.datetime.fromtimestamp(sent_ms / 1000, datetime.timezone.utc)
                        .isoformat().replace("+00:00", "Z") if sent_ms else None
                    ),
                    "receive_count": int(mattrs.get("ApproximateReceiveCount") or 0),
                    "body": body,
                }
            if len(seen) >= target:
                break
        return sorted(seen.values(), key=lambda m: m.get("sent_at") or "")

    def recent_errors(self, minutes: int = 1440) -> list[dict]:
        start = int((time.time() - minutes * 60) * 1000)
        fns = LAMBDA_FNS

        def fetch(fn: str) -> list[dict]:
            lg = f"/aws/lambda/f5kb-{fn}-{self.stage}"
            rows: list[dict] = []
            try:
                resp = self._logs.filter_log_events(
                    logGroupName=lg, startTime=start,
                    filterPattern='{ $.level = "ERROR" }', limit=25)
            except Exception:
                return rows
            for ev in resp.get("events", []):
                try:
                    rec = json.loads(ev["message"])
                except json.JSONDecodeError:
                    rec = {"msg": ev["message"]}
                rec["_lambda"] = fn
                rows.append(rec)
            return rows

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(fns)) as ex:
            out = [r for rows in ex.map(fetch, fns) for r in rows]
        out.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return out[:100]

    # ── full log viewer: all levels, all lambdas, filterable ─────────────────
    def logs(self, fn: str = "all", minutes: int = 180, level: str = "all",
             query: str = "", limit: int = 300) -> list[dict]:
        start = int((time.time() - minutes * 60) * 1000)
        fns = LAMBDA_FNS if fn in ("", "all") else [fn]
        pattern = {"error": '{ $.level = "ERROR" }',
                   "info": '{ $.level = "INFO" }'}.get(level)
        per = limit if len(fns) == 1 else max(50, limit // len(fns))

        def fetch(name: str) -> list[dict]:
            lg = f"/aws/lambda/f5kb-{name}-{self.stage}"
            rows: list[dict] = []
            kwargs: dict[str, Any] = {"logGroupName": lg, "startTime": start,
                                      "limit": min(per, 1000)}
            if pattern:
                kwargs["filterPattern"] = pattern
            token = None
            try:
                while len(rows) < per:
                    if token:
                        kwargs["nextToken"] = token
                    resp = self._logs.filter_log_events(**kwargs)
                    for ev in resp.get("events", []):
                        msg = (ev.get("message") or "").rstrip("\n")
                        try:
                            rec: dict | None = json.loads(msg)
                            if not isinstance(rec, dict):
                                rec = None
                        except json.JSONDecodeError:
                            rec = None
                        ts_iso = datetime.datetime.fromtimestamp(
                            (ev.get("timestamp") or 0) / 1000, datetime.timezone.utc
                        ).isoformat().replace("+00:00", "Z")
                        lvl = (rec or {}).get("level") or (
                            "PLATFORM" if msg.split(" ", 1)[0].rstrip(":") in
                            ("START", "END", "REPORT", "INIT_START", "INIT_REPORT") else "RAW")
                        # Handlers log {"action": "article_staged", type_key, id, ...}
                        # — compose a scannable one-liner from the useful fields.
                        summary = (rec or {}).get("msg")
                        if not summary and rec:
                            bits = [str(rec[k]) for k in
                                    ("action", "type_key", "id", "run_date", "err_msg", "reason")
                                    if rec.get(k)]
                            summary = " · ".join(bits)
                        rows.append({
                            "lambda": name,
                            "ts": (rec or {}).get("ts") or ts_iso,
                            "level": lvl,
                            "msg": summary or msg[:400],
                            "record": rec,
                        })
                    token = resp.get("nextToken")
                    if not token:
                        break
            except Exception:
                pass  # missing log group (never invoked) or IAM — just skip
            return rows

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(fns)) as ex:
            out = [r for rows in ex.map(fetch, fns) for r in rows]
        if query:
            q = query.lower()
            out = [r for r in out
                   if q in json.dumps(r.get("record") or r.get("msg"), default=str).lower()
                   or q in (r.get("msg") or "").lower()]
        out.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return out[:limit]

    # ── pipeline controls: SQS trigger state + pause/resume/purge ────────────
    def _trigger_mappings(self) -> list[dict]:
        out: list[dict] = []
        for fn in (f"f5kb-dump-{self.stage}", f"f5kb-enrich-{self.stage}"):
            try:
                ms = self._lambda.list_event_source_mappings(
                    FunctionName=fn)["EventSourceMappings"]
            except Exception:
                ms = []
            for m in ms:
                out.append({"function": fn, "uuid": m.get("UUID", ""),
                            "state": m.get("State", "?")})
        return out

    def pipeline_state(self) -> dict:
        def load() -> dict:
            triggers = self._trigger_mappings()
            schedules = []
            try:
                import boto3
                scheduler = boto3.client("scheduler", region_name=self.region)
                for name in (f"f5kb-daily-{self.stage}", f"f5kb-watchdog-{self.stage}"):
                    try:
                        s = scheduler.get_schedule(Name=name)
                        schedules.append({"name": name, "state": s.get("State", "?")})
                    except Exception:
                        continue
            except Exception:
                pass
            paused = bool(triggers) and all(t["state"] != "Enabled" for t in triggers)
            return {"triggers": triggers, "schedules": schedules, "paused": paused}
        return self.cache.swr("pipeline-state", 10, load)

    def set_pipeline_enabled(self, enabled: bool) -> dict:
        if not self.writable:
            raise RuntimeError("read-only")
        touched = []
        for m in self._trigger_mappings():
            self._lambda.update_event_source_mapping(UUID=m["uuid"], Enabled=enabled)
            touched.append(m["function"])
        self.cache.drop("pipeline-state")
        return {"status": "resumed" if enabled else "paused", "functions": touched}

    def purge_queues(self) -> dict:
        """Purge the WORK queues (never the DLQs — those are the evidence)."""
        if not self.writable:
            raise RuntimeError("read-only")
        out: dict[str, str] = {}
        for q in (f"f5kb-dump-queue-{self.stage}", f"f5kb-enrich-queue-{self.stage}"):
            try:
                self._sqs.purge_queue(QueueUrl=self._queue_url(q))
                out[q] = "purged"
            except Exception as e:
                out[q] = f"error: {e}"  # PurgeQueueInProgress = purged <60s ago
        self.cache.drop("dlq-depths")
        return out

    # ── integrations: SNS topics + subscriber queue health ───────────────────
    def integrations(self) -> dict:
        def queue_status(arn: str) -> dict:
            name = arn.split(":")[-1]
            try:
                parts = arn.split(":")
                url = f"https://sqs.{parts[3]}.amazonaws.com/{parts[4]}/{parts[5]}"
                attrs = self._sqs.get_queue_attributes(
                    QueueUrl=url,
                    AttributeNames=["ApproximateNumberOfMessages",
                                    "ApproximateNumberOfMessagesNotVisible",
                                    "ApproximateNumberOfMessagesDelayed"])["Attributes"]
                return {"name": name, "accessible": True,
                        "visible": int(attrs["ApproximateNumberOfMessages"]),
                        "in_flight": int(attrs["ApproximateNumberOfMessagesNotVisible"]),
                        "delayed": int(attrs["ApproximateNumberOfMessagesDelayed"])}
            except Exception:
                return {"name": name, "accessible": False,
                        "note": "cross-account or no access — depth unknown"}

        def load() -> dict:
            arns: list[str] = []
            try:
                token = None
                while True:
                    resp = self._sns.list_topics(**({"NextToken": token} if token else {}))
                    arns += [t["TopicArn"] for t in resp.get("Topics", [])]
                    token = resp.get("NextToken")
                    if not token:
                        break
            except Exception:
                arns = [self.handoff_topic]
            mine = [a for a in arns if a.split(":")[-1].startswith("f5kb-")
                    and a.endswith(f"-{self.stage}")]
            topics = []
            for arn in mine:
                subs = []
                try:
                    raw = self._sns.list_subscriptions_by_topic(
                        TopicArn=arn).get("Subscriptions", [])
                except Exception:
                    raw = []
                for s in raw:
                    entry: dict[str, Any] = {"protocol": s.get("Protocol"),
                                             "endpoint": s.get("Endpoint"),
                                             "subscription_arn": s.get("SubscriptionArn")}
                    if s.get("Protocol") == "sqs" and s.get("Endpoint"):
                        entry["queue"] = queue_status(s["Endpoint"])
                    subs.append(entry)
                topics.append({"name": arn.split(":")[-1], "arn": arn,
                               "is_handoff": arn == self.handoff_topic,
                               "subscriptions": subs})
            last_handoff = None
            for d in self.list_run_dates(10):
                if self.exists(f"runs/{d}/approve/_done"):
                    last_handoff = d
                    break
            return {"topics": topics, "handoff_topic": self.handoff_topic,
                    "last_handoff_run": last_handoff}
        return self.cache.swr("integrations", 20, load)

    # ── DLQ redrive: DLQ message back onto its work queue ─────────────────────
    def _dlq_work_map(self) -> dict[str, str]:
        return {f"f5kb-dump-dlq-{self.stage}": f"f5kb-dump-queue-{self.stage}",
                f"f5kb-enrich-dlq-{self.stage}": f"f5kb-enrich-queue-{self.stage}"}

    def redrive_dlq(self, queue: str, message_id: str | None = None) -> dict:
        """Move one (or every) DLQ message back to its work queue: send the
        body to the work queue, then delete it from the DLQ. Fix the
        underlying cause first — an unfixed message just returns after 3 more
        failed deliveries."""
        if not self.writable:
            raise RuntimeError("read-only")
        mapping = self._dlq_work_map()
        if queue not in mapping:
            raise ValueError(f"not a DLQ of this stage: {queue}")
        src = self._queue_url(queue)
        dst = self._queue_url(mapping[queue])
        moved = 0
        seen: set[str] = set()
        for _ in range(6):
            resp = self._sqs.receive_message(
                QueueUrl=src, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                VisibilityTimeout=30)
            msgs = resp.get("Messages", [])
            if not msgs and moved:
                break
            for m in msgs:
                mid = m.get("MessageId") or ""
                if mid in seen:
                    continue
                seen.add(mid)
                if message_id and mid != message_id:
                    continue  # leave it; the 30s visibility timeout releases it
                self._sqs.send_message(QueueUrl=dst, MessageBody=m.get("Body") or "")
                self._sqs.delete_message(QueueUrl=src, ReceiptHandle=m["ReceiptHandle"])
                moved += 1
                if message_id:
                    self.cache.drop("dlq-depths")
                    return {"status": "redriven", "moved": 1,
                            "queue": queue, "to": mapping[queue]}
        self.cache.drop("dlq-depths")
        if message_id and not moved:
            return {"status": "not_found", "moved": 0, "queue": queue,
                    "note": "message not received within the polling window — retry"}
        return {"status": "redriven", "moved": moved,
                "queue": queue, "to": mapping[queue]}

    # ── delete-run also clears this run's DLQ debris ──────────────────────────
    def _run_dlq_messages(self, run_date: str, delete: bool) -> int:
        """Count (and optionally delete) DLQ messages whose body references
        run_date. Non-matching messages are left to their visibility timeout."""
        count = 0
        for q in self._dlq_work_map():
            try:
                url = self._queue_url(q)
            except Exception:
                continue
            seen: set[str] = set()
            for _ in range(4):
                resp = self._sqs.receive_message(
                    QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                    VisibilityTimeout=15)
                msgs = resp.get("Messages", [])
                if not msgs:
                    break
                for m in msgs:
                    mid = m.get("MessageId") or ""
                    if mid in seen:
                        continue
                    seen.add(mid)
                    try:
                        body = json.loads(m.get("Body") or "")
                    except (ValueError, TypeError):
                        continue
                    if isinstance(body, dict) and body.get("run_date") == run_date:
                        count += 1
                        if delete:
                            self._sqs.delete_message(
                                QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
        if delete and count:
            self.cache.drop("dlq-depths")
        return count

    def delete_run(self, run_date: str, include_pending: bool = False,
                   dry_run: bool = True, actor: str = "console") -> dict:
        res = super().delete_run(run_date, include_pending, dry_run, actor)
        try:
            res["dlq_messages"] = self._run_dlq_messages(run_date, delete=not dry_run)
        except Exception:
            res["dlq_messages"] = -1  # unknown — never block the delete on SQS
        return res

    # ── health checks ─────────────────────────────────────────────────────────
    def health_check(self) -> list[dict]:
        """Sequential end-to-end checks; each returns ok + timing + detail."""
        coveo_cfg: dict[str, Any] = {}

        def coveo_token() -> str:
            from f5kb.coveo.aura import fetch_coveo_config
            coveo_cfg["cfg"] = fetch_coveo_config()
            return "guest token fetched from the Aura endpoint"

        def coveo_query() -> str:
            import httpx as _httpx

            from f5kb.coveo.client import CoveoClient
            if "cfg" not in coveo_cfg:
                raise RuntimeError("skipped — token fetch failed")
            with _httpx.Client(timeout=30.0) as http:
                client = CoveoClient(coveo_cfg["cfg"], client=http)
                data = client.post({"q": "", "numberOfResults": 1, "searchHub": "myF5"})
            return f"search ok — {int(data.get('totalCount') or 0):,} documents indexed"

        def bucket_read() -> str:
            idx = self.store.load_hash_index(HASH_INDEX_KEY)
            return f"{self.bucket} readable — hash index {len(idx):,} entries"

        def queues() -> str:
            depths = self.dlq_depths()
            bad = [q for q, n in depths.items() if n < 0]
            if bad:
                raise RuntimeError(f"unreachable: {', '.join(bad)}")
            return f"{len(depths)} queues reachable"

        def lambdas() -> str:
            missing = []
            for f in LAMBDA_FNS:
                name = f"f5kb-{f}-{self.stage}"
                try:
                    self._lambda.get_function_configuration(FunctionName=name)
                except Exception:
                    missing.append(name)
            if missing:
                raise RuntimeError("missing: " + ", ".join(missing))
            return f"all {len(LAMBDA_FNS)} functions deployed"

        checks = [
            ("coveo token", coveo_token,
             "my.f5.com Aura endpoint down or blocking — the pipeline cannot scrape"),
            ("coveo search", coveo_query,
             "token works but the search API failed — check Coveo org status"),
            ("s3 bucket", bucket_read,
             "bucket missing or IAM denies s3:GetObject — is this stage deployed?"),
            ("sqs queues", queues,
             "queue missing or IAM denies sqs:GetQueueAttributes — is this stage deployed?"),
            ("lambda functions", lambdas,
             "stack not deployed for this stage, or IAM denies lambda:GetFunctionConfiguration"),
        ]
        out = []
        for name, fn, hint in checks:
            t0 = time.monotonic()
            try:
                detail = fn()
                out.append({"name": name, "ok": True,
                            "ms": int((time.monotonic() - t0) * 1000), "detail": detail})
            except Exception as e:
                out.append({"name": name, "ok": False,
                            "ms": int((time.monotonic() - t0) * 1000),
                            "detail": f"{type(e).__name__}: {str(e)[:200]}", "hint": hint})
        return out

    # ── cost + duration from REPORT platform lines ────────────────────────────
    def cost_report(self, minutes: int = 1440) -> dict:
        def load() -> dict:
            start = int((time.time() - minutes * 60) * 1000)

            def fetch(fn: str) -> dict:
                lg = f"/aws/lambda/f5kb-{fn}-{self.stage}"
                agg = {"lambda": fn, "invocations": 0, "billed_ms": 0,
                       "memory_mb": 0, "max_memory_mb": 0, "max_duration_ms": 0.0}
                kwargs: dict[str, Any] = {"logGroupName": lg, "startTime": start,
                                          "filterPattern": '"REPORT RequestId"',
                                          "limit": 10000}
                token = None
                try:
                    for _ in range(5):  # cap pages; 50k REPORT lines is plenty
                        if token:
                            kwargs["nextToken"] = token
                        resp = self._logs.filter_log_events(**kwargs)
                        for ev in resp.get("events", []):
                            rec = parse_report_line(ev.get("message") or "")
                            if not rec:
                                continue
                            agg["invocations"] += 1
                            agg["billed_ms"] += rec["billed_ms"]
                            agg["memory_mb"] = rec["memory_mb"]
                            agg["max_memory_mb"] = max(agg["max_memory_mb"],
                                                       rec["max_memory_mb"])
                            agg["max_duration_ms"] = max(agg["max_duration_ms"],
                                                         rec["duration_ms"])
                        token = resp.get("nextToken")
                        if not token:
                            break
                except Exception:
                    pass  # missing log group = never invoked in the window
                gb_s = (agg["billed_ms"] / 1000.0) * (agg["memory_mb"] / 1024.0)
                agg["gb_seconds"] = round(gb_s, 1)
                agg["est_usd"] = round(gb_s * _LAMBDA_GBS_USD
                                       + agg["invocations"] * _LAMBDA_REQ_USD, 4)
                return agg

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(LAMBDA_FNS)) as ex:
                rows = [r for r in ex.map(fetch, LAMBDA_FNS)]
            rows = [r for r in rows if r["invocations"]]
            return {
                "window_minutes": minutes,
                "lambdas": sorted(rows, key=lambda r: -r["est_usd"]),
                "totals": {
                    "invocations": sum(r["invocations"] for r in rows),
                    "gb_seconds": round(sum(r["gb_seconds"] for r in rows), 1),
                    "est_usd": round(sum(r["est_usd"] for r in rows), 4),
                },
                "note": "compute only (x86 us-east-2 rates, before free tier); "
                        "excludes S3/SQS/CloudWatch request costs",
            }
        return self.cache.swr(f"costs:{minutes}", 60, load)

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


def list_targets() -> list[str]:
    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    return list((cfg.get("targets") or {}).keys())


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
