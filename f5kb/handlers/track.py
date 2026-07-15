"""Track Lambda (v2.1) — hash diff + risk classification.

Triggered by the terminal-gate sentinel ``runs/{date}/scrape/_done`` (written
once all per-type dump/enrich stages complete). Reads the 13 per-type manifests
(v2 fix #6), computes op + risk per staged article, and streams one change
record per article into ``runs/{date}/track/changes.jsonl``.

Timeout-safe (v2 fix #7): types are processed one at a time. Completed types are
recorded in ``runs/{date}/track/progress.json`` so a re-invocation resumes where
it left off; when time runs short the handler async self-invokes with the
original event and returns.

On completion it writes ``track/summary.json`` with the real risk breakdown,
conditionally writes the ``track/_done`` gate, and advances ``status.json`` to
``phase=approve``.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import os
import sys
import time

import boto3

from f5kb.lib.dump import db_key
from f5kb.lib.logutil import exc_fields
from f5kb.lib.staging import compute_risk, diff_parts
from f5kb.storage.s3 import S3Storage
from f5kb.track.hashing import sha256_obj

LAMBDA_NAME = "f5kb-track"

HASH_INDEX_KEY = "hash-index/current.json.gz"

# Self-reinvoke when fewer than this many ms of Lambda time remain.
TIMEOUT_MARGIN_MS = 60_000

# Canonical 13 type keys (fallback if the orchestrator state omits `types`).
ALL_TYPES = [
    "Support_Solution", "Known_Issue", "Knowledge", "Security_Advisory", "Video",
    "Policy", "Operations_Guide", "Compliance", "Education", "Manual",
    "Release_Note", "Supplemental_Document", "Bug_Tracker",
]

ENRICHABLE = {"Manual", "Release_Note", "Supplemental_Document", "Bug_Tracker"}

_lambda_client = None


# ── logging ────────────────────────────────────────────────────────────────

def _log(level: str, action: str, **fields: object) -> None:
    rec: dict = {"ts": _now_iso(), "level": level, "lambda": LAMBDA_NAME, "action": action}
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec), file=sys.stderr)


# ── entrypoint ───────────────────────────────────────────────────────────────

def handler(event: dict, context: object) -> dict:
    try:
        return _handler(event, context)
    except Exception as e:
        _log("ERROR", "invocation_failed", **exc_fields(e),
             hint="uncaught crash in track. Progress survives in "
                  "runs/{run_date}/track/progress.json (completed types + counts); "
                  "re-trigger by re-invoking with the scrape/_done event, or use the "
                  "Approve Lambda resume action after fixing the cause.")
        raise


def _handler(event: dict, context: object) -> dict:
    started_monotonic = time.monotonic()
    bucket = os.environ["BUCKET"]
    hash_index_key = os.environ.get("HASH_INDEX_KEY", HASH_INDEX_KEY)

    store = S3Storage(bucket)

    # Triggered by runs/{date}/scrape/_done — key[1] is the run date.
    s3_key = _s3_key_from_event(event)
    run_date = s3_key.split("/")[1]

    # Which types belong to this run (from the orchestrator state).
    try:
        orch = store.get(f"lambda/state/{run_date}/orchestrator.json")
    except KeyError:
        orch = {}
    types: list[str] = orch.get("types") or ALL_TYPES

    # Resume support: load prior progress (empty on a first invocation).
    progress = _load_progress(store, run_date)
    completed: set[str] = set(progress.get("completed_types") or [])
    counts: dict[str, int] = progress.get("counts") or {
        "new": 0, "changed": 0, "unchanged": 0,
        "body_shrank": 0, "body_dropped": 0, "body_error": 0,
    }

    hash_index = store.load_hash_index(hash_index_key)

    # Read all per-type manifests up front so we can log the total article count.
    manifests: dict[str, list[dict]] = {}
    articles_total = 0
    for type_key in types:
        entries = _read_manifest(store, run_date, type_key)
        manifests[type_key] = entries
        articles_total += len(entries)

    _log("INFO", "track_started",
         run_date=run_date, manifests_read=len(types), articles_total=articles_total,
         resumed=bool(completed) or None, types_completed=len(completed) or None)

    changes_key = f"runs/{run_date}/track/changes.jsonl"

    # Fresh start: reset changes.jsonl. A prior invocation that died MID-type
    # (before its first progress checkpoint) leaves partial lines behind;
    # appending on top would duplicate that type's records.
    if not completed:
        store.delete(changes_key)

    for type_key in types:
        if type_key in completed:
            continue

        # Re-invoke before starting a type if we are low on time (v2 fix #7).
        if _ms_remaining(context) < TIMEOUT_MARGIN_MS:
            _save_progress(store, run_date, sorted(completed), counts)
            _log("INFO", "self_reinvoked", run_date=run_date,
                 types_completed=len(completed), remaining_ms=_ms_remaining(context),
                 next_type=type_key,
                 hint="normal — progress saved, async self-invoke continues from next_type")
            _self_invoke(event, context)
            return {"status": "resumed", "run_date": run_date, "types_completed": len(completed)}

        entries = manifests.get(type_key) or []
        _log("INFO", "type_track_started", run_date=run_date, type_key=type_key,
             entries=len(entries), remaining_ms=_ms_remaining(context))

        # Per-article pending/live fetches run concurrently; the type's change
        # records land in ONE batched append. The old per-article append_jsonl
        # re-uploaded the whole growing changes.jsonl every article — O(n^2)
        # bytes (~1 TB of PUT traffic on a full run), which made a single type
        # outlast the 900s Lambda limit and restart from zero forever.
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(
                lambda e: _build_record(store, run_date, e, type_key, hash_index),
                entries))

        records: list[dict] = []
        for rec in results:
            if rec is None:
                continue
            records.append(rec)
            counts[rec["op"]] += 1
            risk = rec.get("risk") or []
            if any(f == "body-error" for f in risk):
                counts["body_error"] += 1
            if any(f == "body-dropped" for f in risk):
                counts["body_dropped"] += 1
            if any(f.startswith("body-shrank-") for f in risk):
                counts["body_shrank"] += 1

        store.append_jsonl_many(changes_key, records)

        completed.add(type_key)
        _save_progress(store, run_date, sorted(completed), counts)
        _log("INFO", "type_track_complete", run_date=run_date, type_key=type_key,
             entries=len(entries), records=len(records), running_counts=dict(counts))

    # All types processed — write the real risk-breakdown summary.
    summary = {
        "run_date": run_date,
        "total": articles_total,
        "new": counts["new"],
        "changed": counts["changed"],
        "unchanged": counts["unchanged"],
        "risk_breakdown": {
            "body_shrank": counts["body_shrank"],
            "body_dropped": counts["body_dropped"],
            "body_error": counts["body_error"],
        },
        "generated_at": _now_iso(),
    }
    store.put(f"runs/{run_date}/track/summary.json", summary)

    # Conditional gate: exactly one writer advances the pipeline.
    won = store.put_conditional(f"runs/{run_date}/track/_done", b"")
    if won:
        _log("INFO", "track_done_won", run_date=run_date,
             next_step="track/_done S3 event triggers the Approve Lambda; phase=approve")
        store.put(f"runs/{run_date}/status.json", {
            "run_date": run_date,
            "phase": "approve",
            "updated_at": _now_iso(),
        })
    else:
        _log("INFO", "conditional_put_lost_412", run_date=run_date, key=f"runs/{run_date}/track/_done")

    _log("INFO", "track_complete",
         run_date=run_date, total=articles_total,
         new=counts["new"], changed=counts["changed"], unchanged=counts["unchanged"],
         body_shrank=counts["body_shrank"], body_dropped=counts["body_dropped"],
         body_error=counts["body_error"],
         elapsed_ms=int((time.monotonic() - started_monotonic) * 1000),
         hint=("body_dropped + body_error articles with a live version become HOLDS "
               "at approve" if (counts["body_dropped"] or counts["body_error"]) else None))

    return {
        "run_date": run_date,
        "total": articles_total,
        "new": counts["new"],
        "changed": counts["changed"],
        "unchanged": counts["unchanged"],
    }


# ── per-article processing ─────────────────────────────────────────────────

def _build_record(
    store: S3Storage,
    run_date: str,
    entry: dict,
    type_key: str,
    hash_index: dict[str, str],
) -> dict | None:
    """Fetch pending+live and compute one change record. Pure per-article work
    (S3 reads + hashing only, no writes) — safe to run concurrently; the
    caller aggregates counts and batch-appends the records."""
    art_id = entry.get("id") or ""
    doc_type = type_key  # hash index keyed on type_key (matches dump.py:203)
    pending_key = entry.get("pending_key") or f"pending/{type_key}/{art_id}.json"

    try:
        pending = store.get(pending_key)
    except KeyError:
        _log("WARN", "pending_missing", run_date=run_date, type_key=type_key,
             id=art_id, pending_key=pending_key,
             hint="manifest lists this article but its pending/ object is gone — "
                  "already promoted/rejected by an earlier pass, or deleted manually; "
                  "it is skipped and will NOT appear in track counts")
        return None

    try:
        live = store.get(f"live/{type_key}/{art_id}.json")
    except KeyError:
        live = None

    # Risk flags (body-error / body-dropped / body-shrank-N%). Only dropped and
    # error force a hold at approve; body-shrank is informational and always
    # auto-approves — matching the local CLI's gate semantics.
    risk = compute_risk(live, pending)
    changed = diff_parts(live, pending)

    if risk:
        _log("WARN" if ("body-dropped" in risk or "body-error" in risk) else "INFO",
             "article_risk_assessed",
             run_date=run_date, type_key=type_key, id=art_id, risk=risk)

    # op via metadata-hash comparison against the current hash index.
    metadata = pending.get("metadata") or {}
    current_hash = sha256_obj(metadata)
    prior_hash = hash_index.get(db_key(doc_type, art_id))

    if prior_hash is None:
        op = "new"
    elif prior_hash != current_hash:
        op = "changed"
    else:
        op = "unchanged"

    live_body = _body_text(live) if live else ""
    pending_body = _body_text(pending)

    return {
        "id": art_id,
        "type_key": type_key,
        "document_type": doc_type,
        "op": op,
        "risk": risk,
        "changed": changed,
        "live_chars": len(live_body) if live else 0,
        "pending_chars": len(pending_body),
        "run_date": run_date,
    }


# ── manifests & progress ─────────────────────────────────────────────────────

def _read_manifest(store: S3Storage, run_date: str, type_key: str) -> list[dict]:
    """Read one per-type manifest (v2 fix #6). Missing = type had no changes."""
    manifest_key = f"runs/{run_date}/manifest/{type_key}.jsonl"
    try:
        raw = store.get_bytes(manifest_key).decode("utf-8")
    except KeyError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            _log("WARN", "manifest_line_malformed", run_date=run_date,
                 type_key=type_key, line=line[:200],
                 hint="skipped — a corrupt manifest line means one staged article is "
                      "invisible to track/approve; inspect the manifest jsonl")
    return out


def _load_progress(store: S3Storage, run_date: str) -> dict:
    try:
        return store.get(f"runs/{run_date}/track/progress.json")
    except KeyError:
        return {}


def _save_progress(store: S3Storage, run_date: str, completed: list[str], counts: dict[str, int]) -> None:
    store.put(f"runs/{run_date}/track/progress.json", {
        "run_date": run_date,
        "completed_types": completed,
        "total_processed": sum(counts.get(k, 0) for k in ("new", "changed", "unchanged")),
        "counts": counts,
        "updated_at": _now_iso(),
    })


# ── self-reinvoke ────────────────────────────────────────────────────────────

def _self_invoke(event: dict, context: object) -> None:
    """Async self-invoke with the original event to resume after timeout."""
    global _lambda_client
    fn_name = getattr(context, "function_name", None) or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not fn_name:
        return
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    try:
        _lambda_client.invoke(
            FunctionName=fn_name,
            InvocationType="Event",
            Payload=json.dumps(event).encode("utf-8"),
        )
    except Exception as e:  # never wedge the pipeline on a re-invoke failure
        _log("ERROR", "self_invoke_failed", **exc_fields(e),
             hint="track stopped mid-run and could NOT continue itself — progress.json "
                  "holds completed types; re-invoke track manually with the same event")


# ── helpers ──────────────────────────────────────────────────────────────────

def _body_text(article: dict | None) -> str:
    t = (article or {}).get("content", {}).get("body_text")
    return t if isinstance(t, str) else ""


def _ms_remaining(context: object) -> int:
    fn = getattr(context, "get_remaining_time_in_millis", None)
    if callable(fn):
        return int(fn())
    return TIMEOUT_MARGIN_MS * 10  # local / test context — never timeout


def _s3_key_from_event(event: dict) -> str:
    if "Records" in event:
        return event["Records"][0]["s3"]["object"]["key"]
    return event["detail"]["object"]["key"]  # EventBridge S3 event


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
