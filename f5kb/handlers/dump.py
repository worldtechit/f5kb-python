"""Dump-{Type} Lambda (v2.1) — @rowid keyset pagination with timeout-recovery cursor.

v2.1 changes:
- Writes a PER-TYPE manifest (runs/{date}/manifest/{TypeKey}.jsonl); this Lambda is
  the sole writer of its type's file, eliminating cross-Lambda contention.
- Stages articles as the v2.1 envelope (with an explicit `type_key` field).
- After a non-enrichable type finishes, runs the terminal gate check and, if every
  type in the run is terminal, attempts the conditional PUT of runs/{date}/scrape/_done.
- Enrichable types stop after dump; the Enrich Lambda writes enrich/{TypeKey}/_done
  and runs the terminal gate.
- Structured JSON logging (one object per line to stderr).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time

import boto3
import httpx

from f5kb.config.types import normalize_type
from f5kb.coveo.aura import fetch_coveo_config
from f5kb.coveo.client import CoveoClient
from f5kb.coveo.dates import date_aq
from f5kb.coveo.fields import flatten_fields_safe, split_entry
from f5kb.lib.dump import db_key
from f5kb.lib.fsutil import id_of
from f5kb.storage.s3 import S3Storage
from f5kb.track.hashing import sha256_obj

LAMBDA_NAME = "dump"

# Fetch a fresh page only if more than this many ms remain in the invocation.
TIMEOUT_MARGIN_MS = 60_000
PAGE_SIZE_DEFAULT = 500
HASH_INDEX_KEY = "hash-index/current.json.gz"

# Page-fetch failure handling: self-requeue with backoff up to this many
# consecutive no-progress attempts, then raise so the SQS redrive (3 receives,
# each gated by the 5400s visibility timeout) delivers the message to the DLQ.
# Raising IMMEDIATELY would make every transient Coveo blip stall the type for
# a full visibility-timeout window (90 min) before the first retry.
MAX_FAILURE_RETRIES = 3
FAILURE_RETRY_DELAY_S = 60  # multiplied by the attempt number, capped at 900

# Incremental mode: articles modified within this window (ms).
INCREMENTAL_WINDOW_MS = 2 * 24 * 60 * 60 * 1000  # 48 hours

# The 4 types whose body is enriched by the Enrich Lambda.
ENRICHABLE = {"Manual", "Release_Note", "Supplemental_Document", "Bug_Tracker"}


# ── Structured logging ─────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _log(level: str, action: str, **fields: object) -> None:
    rec: dict[str, object] = {"ts": _now_iso(), "level": level, "lambda": LAMBDA_NAME, "action": action}
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec), file=sys.stderr)


# ── Handler ──────────────────────────────────────────────────────────────────


def handler(event: dict, context: object) -> dict:
    bucket = os.environ["BUCKET"]
    queue_url = os.environ["DUMP_QUEUE_URL"]
    page_size = int(os.environ.get("PAGE_SIZE", PAGE_SIZE_DEFAULT))

    store = S3Storage(bucket)

    record = event["Records"][0]
    msg = json.loads(record["body"])
    run_date: str = msg["run_date"]
    type_key: str = msg["type_key"]
    mode: str = msg.get("mode", "full")  # "incremental" | "full"
    hash_index_key: str = msg.get("hash_index_key", HASH_INDEX_KEY)
    enrichable: bool = msg.get("enrichable", type_key in ENRICHABLE)
    attempt: int = int(msg.get("attempt", 0))  # consecutive failed-retry count

    _log("INFO", "invocation_started", run_date=run_date, type_key=type_key,
         mode=mode, enrichable=enrichable)

    # Load type config from S3 if available; fall back to identity mapping.
    document_type = type_key
    type_cfg_raw: dict = {}
    try:
        all_type_cfgs = store.get("lambda/config/types.json")
        type_cfg_raw = all_type_cfgs.get(type_key) or {}
        document_type = type_cfg_raw.get("documentType") or type_key
    except KeyError:
        pass

    # The wildcard MUST be the string "*" — selects() treats a LIST ["*"] as a
    # literal field name that matches nothing, which silently strips every
    # field and stages empty {} metadata/content envelopes.
    type_cfg = normalize_type({
        "documentType": document_type,
        "metadata": type_cfg_raw.get("metadata") or "*",
        "content": type_cfg_raw.get("content") or [],
    })

    # Hash index for skip-unchanged optimisation.
    hash_index = store.load_hash_index(hash_index_key)

    # Cursor state — may exist from a previous invocation that timed out.
    cursor_key = f"lambda/state/{run_date}/dump-{type_key}.json"
    cursor_state = _load_cursor(store, cursor_key)
    rowid_cursor: int | None = cursor_state.get("rowid_cursor") or None
    written_so_far: int = cursor_state.get("written", 0)
    count_server: int = cursor_state.get("count_server", 0)

    # Fetch a fresh Coveo token for this invocation.
    coveo_config = fetch_coveo_config()
    http = httpx.Client(timeout=60.0)
    client = CoveoClient(coveo_config, client=http)

    sqs = boto3.client("sqs")
    captured_at = _now_iso()
    written = 0

    # Build aq base: incremental adds a 48h date window; full uses entire corpus.
    aq_base = f'@f5_document_type=="{document_type}"'
    if mode == "incremental":
        run_dt = datetime.datetime.fromisoformat(run_date).replace(tzinfo=datetime.timezone.utc)
        cutoff_ms = int(run_dt.timestamp() * 1000) - INCREMENTAL_WINDOW_MS
        aq_base = f"{aq_base} {date_aq(start_ms=cutoff_ms)}"

    manifest_key = f"runs/{run_date}/manifest/{type_key}.jsonl"
    first_page = True
    pages_processed = 0

    try:
        cursor = rowid_cursor
        while True:
            # Check remaining Lambda time before fetching the next page.
            if _ms_remaining(context) < TIMEOUT_MARGIN_MS:
                total = written_so_far + written
                _log("INFO", "timeout_approaching", run_date=run_date,
                     type_key=type_key, written=total, rowid_cursor=cursor)
                _save_cursor(store, cursor_key, {
                    "run_date": run_date,
                    "type_key": type_key,
                    "rowid_cursor": cursor,
                    "written": total,
                    "count_server": count_server,
                    "status": "in_progress",
                    "last_updated": _now_iso(),
                })
                _log("INFO", "cursor_saved", run_date=run_date, type_key=type_key,
                     rowid_cursor=cursor, written=total)
                sqs.send_message(
                    QueueUrl=queue_url,
                    MessageBody=json.dumps({
                        "run_date": run_date,
                        "type_key": type_key,
                        "mode": mode,
                        "hash_index_key": hash_index_key,
                        "enrichable": enrichable,
                    }),
                )
                _log("INFO", "requeued_self", run_date=run_date, type_key=type_key)
                return {"status": "resumed", "type_key": type_key, "written": written}

            cursor_aq = aq_base if cursor is None else f"{aq_base} @rowid>{cursor}"
            try:
                data = client.post({
                    "q": "",
                    "aq": cursor_aq,
                    "numberOfResults": page_size,
                    "searchHub": "myF5",
                    "sortCriteria": "@rowid ascending",
                })
            except Exception as e:
                # Never swallow the error and write _done — that would mark the
                # type complete with a silently truncated corpus. Save the
                # cursor, then retry FAST via self-requeue: raising instead
                # would park the message invisible for the full 5400s
                # visibility timeout before SQS redelivers it.
                _save_cursor(store, cursor_key, {
                    "run_date": run_date,
                    "type_key": type_key,
                    "rowid_cursor": cursor,
                    "written": written_so_far + written,
                    "count_server": count_server,
                    "status": "in_progress",
                    "last_updated": _now_iso(),
                })
                # Progress this invocation resets the failure streak.
                streak = 0 if pages_processed else attempt
                if streak >= MAX_FAILURE_RETRIES:
                    # Persistent failure — hand over to the SQS redrive → DLQ.
                    _log("ERROR", "page_fetch_failed", run_date=run_date,
                         type_key=type_key, error=str(e), attempt=streak, final=True)
                    raise
                delay = min(900, FAILURE_RETRY_DELAY_S * (streak + 1))
                sqs.send_message(
                    QueueUrl=queue_url,
                    DelaySeconds=delay,
                    MessageBody=json.dumps({
                        "run_date": run_date,
                        "type_key": type_key,
                        "mode": mode,
                        "hash_index_key": hash_index_key,
                        "enrichable": enrichable,
                        "attempt": streak + 1,
                    }),
                )
                _log("ERROR", "page_fetch_failed", run_date=run_date,
                     type_key=type_key, error=str(e), attempt=streak,
                     retry_in_s=delay)
                return {"status": "retry_scheduled", "type_key": type_key,
                        "attempt": streak + 1, "retry_in_s": delay}

            pages_processed += 1

            # Capture the server-side total from the first page for progress
            # tracking — but only on a FRESH start. A resumed invocation's
            # first page is filtered by @rowid>cursor, so its totalCount is
            # only the remaining articles; overwriting would shrink the total
            # on every resume until staged > total.
            if first_page:
                if count_server == 0:
                    count_server = int(
                        data.get("totalCountFiltered") or data.get("totalCount") or 0
                    )
                first_page = False

            batch = data.get("results") or []
            _log("INFO", "page_fetched", run_date=run_date, type_key=type_key,
                 batch=len(batch), rowid_cursor=cursor, count_server=count_server)
            if not batch:
                break

            last_raw = batch[-1].get("raw") or {}
            last_rowid = last_raw.get("rowid")
            if last_rowid is None:
                break

            for r in batch:
                fields = flatten_fields_safe(r)
                split = split_entry(fields, type_cfg)
                metadata = split["metadata"]
                content = split["content"]
                raw = r.get("raw") or {}

                art_id = id_of(r)

                # Skip unchanged (compare metadata hash to hash index).
                mh = sha256_obj(metadata)
                key = db_key(type_key, art_id)
                if hash_index.get(key) == mh:
                    _log("INFO", "article_skipped", run_date=run_date,
                         type_key=type_key, id=art_id, reason="unchanged")
                    continue

                op = "changed" if key in hash_index else "new"

                # Top-level identity fields match the local dump schema
                # (f5kb/lib/dump.py) — the enrichers require article["link"].
                envelope = {
                    "run_date": run_date,
                    "captured_at": captured_at,
                    "type_key": type_key,
                    "id": art_id,
                    "documentType": document_type,
                    "title": r.get("title") or "",
                    "link": r.get("clickUri") or raw.get("clickableuri") or "",
                    "metadata_hash": mh,
                    "content_hash": sha256_obj(content),
                    "metadata": metadata,
                    "content": content,
                }
                pending_key = f"pending/{type_key}/{art_id}.json"
                store.put(pending_key, envelope)

                store.append_jsonl(manifest_key, {
                    "op": op,
                    "id": art_id,
                    "type_key": type_key,
                    "s3_key": f"live/{type_key}/{art_id}.json",
                    "run_date": run_date,
                    "approved_by": None,
                })
                _log("INFO", "article_staged", run_date=run_date,
                     type_key=type_key, id=art_id, op=op)
                written += 1

            cursor = int(last_rowid)
            if len(batch) < page_size:
                break
            time.sleep(0.12)

    finally:
        http.close()

    total = written_so_far + written
    store.delete(cursor_key)
    store.put(f"runs/{run_date}/dump/{type_key}/_index.json", {
        "type_key": type_key,
        "document_type": document_type,
        "count_written": total,
        "count_server": count_server,
        "status": "done",
        "completed_at": _now_iso(),
    })
    store.put_marker(f"runs/{run_date}/dump/{type_key}/_done")
    _log("INFO", "type_complete", run_date=run_date, type_key=type_key,
         written=total, count_server=count_server, enrichable=enrichable)

    # Enrichable types hand off to the Enrich Lambda, which writes
    # enrich/{TypeKey}/_done and runs the terminal gate. For non-enrichable
    # types, dump-done IS terminal, so run the gate check here.
    if not enrichable:
        _check_and_write_scrape_done(store, run_date, context)

    return {"status": "done", "type_key": type_key, "mode": mode, "written": total}


# ── Cursor / context helpers ────────────────────────────────────────────────


def _load_cursor(store: S3Storage, key: str) -> dict:
    try:
        return store.get(key)
    except KeyError:
        return {}


def _save_cursor(store: S3Storage, key: str, state: dict) -> None:
    store.put(key, state)


def _ms_remaining(context: object) -> int:
    """Return remaining Lambda invocation time in milliseconds."""
    fn = getattr(context, "get_remaining_time_in_millis", None)
    if callable(fn):
        return int(fn())
    return TIMEOUT_MARGIN_MS * 10  # local / test context — never timeout


# ── Terminal gate ──────────────────────────────────────────────────────────


def _check_and_write_scrape_done(
    store: S3Storage, run_date: str, context: object | None = None
) -> None:
    """If every type in the run is terminal, write the scrape/_done sentinel.

    A type is "terminal" when its final stage marker is present:
      - enrichable types → runs/{date}/enrich/{TypeKey}/_done (written by Enrich)
      - non-enrichable    → runs/{date}/dump/{TypeKey}/_done  (written here)

    The sentinel is written with a conditional PUT so only one racing Lambda
    wins; the winner advances status.json to phase=track.
    """
    try:
        orch = store.get(f"lambda/state/{run_date}/orchestrator.json")
    except KeyError:
        _log("INFO", "terminal_gate_no_orchestrator", run_date=run_date)
        return

    types: list[str] = orch.get("types") or []
    enrichable = set(orch.get("enrichable") or [])
    if not types:
        return

    done_keys = set(
        store.list_prefix(f"runs/{run_date}/dump/")
        + store.list_prefix(f"runs/{run_date}/enrich/")
    )

    terminal = []
    for t in types:
        if t in enrichable:
            terminal.append(f"runs/{run_date}/enrich/{t}/_done" in done_keys)
        else:
            terminal.append(f"runs/{run_date}/dump/{t}/_done" in done_keys)

    if not all(terminal):
        _log("INFO", "terminal_gate_pending", run_date=run_date,
             done=sum(terminal), total=len(types))
        return

    won = store.put_conditional(f"runs/{run_date}/scrape/_done", b"")
    if won:
        _log("INFO", "scrape_done_won", run_date=run_date, total=len(types))
        store.put(f"runs/{run_date}/status.json", {
            "run_date": run_date,
            "phase": "track",
            "updated_at": _now_iso(),
        })
    else:
        _log("INFO", "scrape_done_lost_412", run_date=run_date,
             detail="conditional_put_lost_412")
