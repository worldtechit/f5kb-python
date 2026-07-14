"""Enrich Lambda (v2.1) — SQS-driven HTTP body fetch with cursor recovery.

Was: EventBridge S3 event → one-shot Lambda per type.
Now: SQS-driven (EnrichQueue) for retry / DLQ / timeout-recovery parity with the
Dump Lambda.

TWO MESSAGE SHAPES (v2.1 fix #26), detected by inspecting the SQS body:
  1. EventBridge S3-event envelope — the FIRST message for a type, published by
     EnrichRule when runs/{date}/dump/{TypeKey}/_done appears. Detected by the
     presence of "source"/"detail-type". manifest_offset starts at 0.
  2. Self re-queue cursor message — emitted by this handler when it runs low on
     time. Detected by "type_key" + "manifest_offset".

PER-TYPE FLOW:
  1. Load cursor (lambda/state/{date}/enrich-{TypeKey}.json) if resuming.
  2. Read the per-type manifest runs/{date}/manifest/{TypeKey}.jsonl (v2).
  3. Iterate manifest lines from manifest_offset; enrich each pending envelope.
  4. Every N articles check remaining time; if < margin, save cursor + re-queue
     self on EnrichQueue and return ("resumed").
  5. On completion: write enrich/{TypeKey}/_report.json, enrich/{TypeKey}/_done
     (plain marker — one Enrich Lambda per type), run the terminal gate, and
     delete the cursor.

TERMINAL GATE:
  When every type in the run has reached its terminal marker (enrich/_done for
  enrichable types, dump/_done for non-enrichable) the first Enrich Lambda to
  notice wins a conditional PUT on runs/{date}/scrape/_done and flips
  status.json into the track phase. Losing the 412 race is expected.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time

import boto3
import httpx

from f5kb.enrich.enrichers import STALE_KEYS, TYPE_ENRICHERS, has_body
from f5kb.http.fetcher import HttpClient
from f5kb.lib.logutil import exc_fields
from f5kb.storage.s3 import S3Storage

LAMBDA_NAME = "enrich"

# SSM secret cache — fetched once per cold start (fix #18).
_ssm = boto3.client("ssm")
_SECRET_CACHE: dict[str, str] = {}


def _get_github_token() -> str | None:
    param = os.environ.get("GITHUB_TOKEN_PARAM", "")
    if not param:
        return None
    if param not in _SECRET_CACHE:
        try:
            _SECRET_CACHE[param] = _ssm.get_parameter(
                Name=param, WithDecryption=True
            )["Parameter"]["Value"]
        except Exception as e:
            _log("WARN", "github_token_fetch_failed", param=param,
                 err_type=type(e).__name__, err_msg=str(e)[:300],
                 hint="F5_GitHub enrichment runs unauthenticated — GitHub API rate "
                      "limit drops to 60/h; expect bodyError failures on large batches")
            return None
    val = _SECRET_CACHE.get(param, "")
    return val if val and not val.startswith("placeholder") else None

# Enrichable types handled by this Lambda (F5_GitHub is excluded from the run).
ENRICHABLE = {"Manual", "Release_Note", "Supplemental_Document", "Bug_Tracker"}

# Canonical 13 type keys (for reference / validation).
ALL_TYPES = [
    "Support_Solution", "Known_Issue", "Knowledge", "Security_Advisory", "Video",
    "Policy", "Operations_Guide", "Compliance", "Education", "Manual",
    "Release_Note", "Supplemental_Document", "Bug_Tracker",
]

# Re-queue self if fewer than this many ms remain in the invocation.
TIMEOUT_MARGIN_MS = 60_000
# Check the clock every this-many articles (fetches are the slow part).
TIME_CHECK_EVERY = 5
# Checkpoint the cursor every N enriched articles → live progress + crash resume.
CHECKPOINT_EVERY = 50


# ── logging ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _log(level: str, action: str, **fields: object) -> None:
    rec: dict = {"ts": _now_iso(), "level": level, "lambda": LAMBDA_NAME, "action": action}
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec), file=sys.stderr)


# ── handler ─────────────────────────────────────────────────────────────────

def handler(event: dict, context: object) -> dict:
    try:
        return _handler(event, context)
    except Exception as e:
        _log("ERROR", "invocation_failed", **exc_fields(e),
             hint="uncaught crash; SQS redelivers (maxReceiveCount 3, then enrich DLQ). "
                  "Cursor at lambda/state/{run_date}/enrich-{type_key}.json shows the "
                  "last checkpointed manifest offset.")
        raise


def _handler(event: dict, context: object) -> dict:
    started_monotonic = time.monotonic()
    bucket = os.environ["BUCKET"]
    queue_url = os.environ["ENRICH_QUEUE_URL"]
    github_token = _get_github_token()

    store = S3Storage(bucket)

    record = event["Records"][0]
    msg = json.loads(record["body"])
    try:
        run_date, type_key, manifest_offset = _parse_message(msg)
    except (ValueError, KeyError) as e:
        _log("ERROR", "bad_message_shape", message_keys=sorted(msg),
             **exc_fields(e),
             hint="neither an EventBridge S3-event envelope nor a self-requeue cursor "
                  "message — inspect the enrich DLQ for the raw body")
        raise

    enricher = TYPE_ENRICHERS.get(type_key)
    if type_key not in ENRICHABLE or enricher is None:
        # Non-enrichable types never route to this Lambda in v2.1 (Dump writes
        # their enrich/_done). Guard defensively and pass through.
        _log("WARN", "non_enrichable_message", run_date=run_date, type_key=type_key)
        store.put_marker(f"runs/{run_date}/enrich/{type_key}/_done")
        _check_and_write_scrape_done(store, run_date)
        return {"status": "skipped", "type_key": type_key, "reason": "non-enrichable"}

    # Resume state from a prior invocation that timed out (may be empty).
    cursor_key = f"lambda/state/{run_date}/enrich-{type_key}.json"
    cursor_state = _load_cursor(store, cursor_key)
    if cursor_state:
        # A saved cursor is authoritative over the message offset (the message
        # carries the same value, but the cursor also carries running counts).
        manifest_offset = int(cursor_state.get("manifest_offset", manifest_offset))
    enriched = int(cursor_state.get("enriched", 0))
    failed = int(cursor_state.get("failed", 0))
    skipped = int(cursor_state.get("skipped", 0))

    manifest = _read_manifest(store, run_date, type_key)
    total = len(manifest)

    _log("INFO", "enrich_started", run_date=run_date, type_key=type_key,
         manifest_offset=manifest_offset, total=total,
         resumed=bool(cursor_state) or None,
         enriched_so_far=enriched or None, failed_so_far=failed or None,
         skipped_so_far=skipped or None,
         github_token_present=bool(github_token) if type_key == "Bug_Tracker" else None,
         remaining_ms=_ms_remaining(context))

    http_client = httpx.Client(timeout=30.0)
    http = HttpClient(client=http_client)
    now_iso = _now_iso()

    idx = manifest_offset
    try:
        while idx < total:
            # Time-budget check before starting each (network-bound) article.
            if idx % TIME_CHECK_EVERY == 0 and _ms_remaining(context) < TIMEOUT_MARGIN_MS:
                _save_cursor(store, cursor_key, run_date, type_key, idx,
                             enriched, failed, skipped)
                _requeue_self(queue_url, run_date, type_key, idx)
                _log("INFO", "cursor_saved", run_date=run_date, type_key=type_key,
                     manifest_offset=idx, enriched=enriched, failed=failed)
                _log("INFO", "requeued_self", run_date=run_date, type_key=type_key,
                     manifest_offset=idx)
                return {"status": "resumed", "type_key": type_key,
                        "manifest_offset": idx, "enriched": enriched, "failed": failed}

            entry = manifest[idx]
            art_id = entry.get("id") or ""
            pending_key = entry.get("pending_key") or f"pending/{type_key}/{art_id}.json"

            try:
                article = store.get(pending_key)
            except KeyError:
                _log("WARN", "pending_missing", run_date=run_date, type_key=type_key,
                     id=art_id, pending_key=pending_key)
                idx += 1
                continue

            if has_body(article.get("content")):
                skipped += 1
                idx += 1
                continue

            # Envelopes staged before the link fix carry no top-level link;
            # recover it from metadata (present under the default "*" split).
            if not article.get("link"):
                meta = article.get("metadata") or {}
                article["link"] = meta.get("clickUri") or meta.get("clickableuri") or ""

            try:
                result = enricher(article, now_iso, http, github_token=github_token)
            except Exception as exc:
                # Raised (vs returned bodyError) = unexpected — log the traceback
                # so the failing enricher line is identifiable from the logs alone.
                _log("WARN", "enricher_raised", run_date=run_date, type_key=type_key,
                     id=art_id, link=article.get("link") or "", **exc_fields(exc))
                result = {
                    "bodySource": article.get("link") or "",
                    "fetchedAt": now_iso,
                    "bodyError": str(exc),
                }

            # Enrichers signal soft failures (soft 404, JS-rendered host, moved
            # page) by RETURNING a bodyError instead of raising — count both
            # paths as failed so the report matches what Approve will hold.
            body_error = result.get("bodyError")
            if body_error:
                failed += 1
                # There is no per-article retry, so every failure is final;
                # `final` feeds the EnrichFailed metric filter in template.yaml.
                _log("WARN", "article_enrich_failed", run_date=run_date,
                     type_key=type_key, id=art_id, error=body_error, final=True,
                     link=article.get("link") or "",
                     hint="no per-article retry — if a live version exists this becomes "
                          "a body-error HOLD at approve; fix the source page or reject")
            else:
                enriched += 1
                _log("INFO", "article_enriched", run_date=run_date, type_key=type_key,
                     id=art_id)

            base = dict(article.get("content") or {})
            for k in STALE_KEYS:
                base.pop(k, None)
            article["content"] = {**base, **result}
            store.put(pending_key, article)
            idx += 1

            # Periodic checkpoint so progress is observable + crash-resumable,
            # not just saved at the timeout boundary.
            if idx % CHECKPOINT_EVERY == 0:
                _save_cursor(store, cursor_key, run_date, type_key, idx,
                             enriched, failed, skipped)
                _log("INFO", "progress_checkpoint", run_date=run_date,
                     type_key=type_key, manifest_offset=idx, total=total,
                     enriched=enriched, failed=failed, skipped=skipped,
                     remaining_ms=_ms_remaining(context))
    finally:
        http_client.close()

    # ── completion for this type ────────────────────────────────────────────
    store.put(f"runs/{run_date}/enrich/{type_key}/_report.json", {
        "type_key": type_key,
        "enriched": enriched,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "generated_at": _now_iso(),
    })
    store.put_marker(f"runs/{run_date}/enrich/{type_key}/_done")
    _log("INFO", "enrich_complete", run_date=run_date, type_key=type_key,
         enriched=enriched, failed=failed, skipped=skipped, total=total,
         elapsed_ms=int((time.monotonic() - started_monotonic) * 1000),
         next_step="terminal-gate check runs now; scrape/_done once every type is terminal")

    _check_and_write_scrape_done(store, run_date)
    store.delete(cursor_key)

    return {"status": "done", "type_key": type_key,
            "enriched": enriched, "failed": failed, "skipped": skipped}


# ── message parsing ───────────────────────────────────────────────────────────

def _parse_message(msg: dict) -> tuple[str, str, int]:
    """Return (run_date, type_key, manifest_offset) for either message shape."""
    # Shape 2 — self re-queue cursor message.
    if "type_key" in msg and "manifest_offset" in msg:
        return (
            str(msg["run_date"]),
            str(msg["type_key"]),
            int(msg["manifest_offset"]),
        )
    # Shape 1 — EventBridge S3-event envelope.
    if "source" in msg or "detail-type" in msg or "detail" in msg:
        s3_key = msg["detail"]["object"]["key"]  # runs/{date}/dump/{TypeKey}/_done
        parts = s3_key.split("/")
        return parts[1], parts[3], 0
    raise ValueError(f"unrecognised enrich message shape: keys={sorted(msg)}")


# ── manifest ──────────────────────────────────────────────────────────────────

def _read_manifest(store: S3Storage, run_date: str, type_key: str) -> list[dict]:
    """Read the per-type v2 manifest runs/{date}/manifest/{TypeKey}.jsonl."""
    key = f"runs/{run_date}/manifest/{type_key}.jsonl"
    try:
        raw = store.get_bytes(key).decode("utf-8")
    except KeyError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ── cursor + re-queue ───────────────────────────────────────────────────────

def _load_cursor(store: S3Storage, key: str) -> dict:
    try:
        return store.get(key)
    except KeyError:
        return {}


def _save_cursor(store: S3Storage, key: str, run_date: str, type_key: str,
                 manifest_offset: int, enriched: int, failed: int, skipped: int) -> None:
    store.put(key, {
        "run_date": run_date,
        "type_key": type_key,
        "manifest_offset": manifest_offset,
        "enriched": enriched,
        "failed": failed,
        "skipped": skipped,
        "status": "in_progress",
        "updated_at": _now_iso(),
    })


def _requeue_self(queue_url: str, run_date: str, type_key: str, manifest_offset: int) -> None:
    sqs = boto3.client("sqs")
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({
            "run_date": run_date,
            "type_key": type_key,
            "manifest_offset": manifest_offset,
        }),
    )


def _ms_remaining(context: object) -> int:
    """Remaining Lambda invocation time in ms; large sentinel under local/test."""
    fn = getattr(context, "get_remaining_time_in_millis", None)
    if callable(fn):
        return int(fn())
    return TIMEOUT_MARGIN_MS * 10


# ── terminal gate ─────────────────────────────────────────────────────────────

def _check_and_write_scrape_done(store: S3Storage, run_date: str) -> None:
    """Win-once conditional PUT on scrape/_done when every type is terminal.

    Enrichable types are terminal at enrich/{t}/_done; non-enrichable types at
    dump/{t}/_done. The single winner flips status.json into the track phase.
    """
    try:
        orch = store.get(f"lambda/state/{run_date}/orchestrator.json")
    except KeyError:
        return

    types = orch.get("types", [])
    enrichable = set(orch.get("enrichable", []))

    missing = []
    for t in types:
        if t in enrichable:
            terminal_key = f"runs/{run_date}/enrich/{t}/_done"
        else:
            terminal_key = f"runs/{run_date}/dump/{t}/_done"
        if not store.exists(terminal_key):
            missing.append(t)
    if missing:
        _log("INFO", "terminal_gate_pending", run_date=run_date,
             done=len(types) - len(missing), total=len(types), waiting_on=missing)
        return

    won = store.put_conditional(f"runs/{run_date}/scrape/_done", b"")
    if won:
        _log("INFO", "scrape_done_won", run_date=run_date)
        store.put(f"runs/{run_date}/status.json", {
            "run_date": run_date,
            "phase": "track",
            "updated_at": _now_iso(),
        })
    else:
        _log("INFO", "scrape_done_lost_412", run_date=run_date)
