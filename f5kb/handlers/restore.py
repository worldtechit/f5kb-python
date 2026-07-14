"""Restore Lambda (v2.1) — revert an article to a previous archived version.

Invoked manually via AWS Lambda console or CLI:
  aws lambda invoke \\
    --function-name f5kb-restore-prod \\
    --payload '{"type_key":"Support_Solution","art_id":"K12345",
                "archive_key":"archive/Support_Solution/K12345/020000Z.json",
                "actor":"devinp"}' \\
    response.json

To list available archived versions for an article:
  aws s3 ls s3://<bucket>/archive/<type_key>/<art_id>/

The five atomic steps performed on every restore:
  a. archive the current live/ object   → archive/{type_key}/{art_id}/{ts}.json
  b. write restored content              → live/{type_key}/{art_id}.json
  c. update hash index                   → hash-index/current.json.gz
  d. append audit trail                  → audit/{YYYY-MM}/changed_ids.jsonl
                                           audit/{YYYY-MM}/decisions.jsonl
  e. publish SNS (batch=restore) + write → runs/{date}/restore/{ts}/changed_ids.jsonl

RUN-OPEN LOCK (v2.1 fix #23): the restore refuses to run while today's run is
open (any phase != "done"), because concurrent writes to live/ + hash-index/
would race the scrape/track/approve pipeline. Wait for approve/_done or the
02:00 sweep.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

import boto3

from f5kb.lib.dump import db_key
from f5kb.lib.logutil import exc_fields
from f5kb.storage.s3 import S3Storage
from f5kb.track.hashing import sha256_obj

LAMBDA_NAME = "f5kb-restore"

HASH_INDEX_KEY = "hash-index/current.json.gz"
ARCHIVE_PREFIX = "archive"

_sns = boto3.client("sns")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _log(level: str, action: str, **fields: object) -> None:
    rec: dict[str, object] = {"ts": _now_iso(), "level": level, "lambda": LAMBDA_NAME, "action": action}
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec), file=sys.stderr)


def handler(event: dict, context: object) -> dict:
    try:
        return _handler(event, context)
    except Exception as e:
        _log("ERROR", "invocation_failed", **exc_fields(e),
             hint="restore crashed — live/ may or may not have been rewritten; "
                  "check the last live_archived / live_written log line to see how "
                  "far it got before failing")
        raise


def _handler(event: dict, context: object) -> dict:
    """
    event keys:
      type_key    (required) e.g. "Support_Solution"
      art_id      (required) e.g. "K12345"
      archive_key (required) full S3 key of archived version to restore
      actor       (optional) who triggered the restore; defaults to "unknown"
    """
    bucket = os.environ["BUCKET"]
    store = S3Storage(bucket)

    type_key: str = event.get("type_key") or ""
    art_id: str = event.get("art_id") or ""
    archive_key: str = event.get("archive_key") or ""
    actor: str = event.get("actor") or "unknown"

    today = datetime.date.today().isoformat()
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H%M%SZ")  # HHMMSSZ, filesystem-safe

    _log("INFO", "restore_started", run_date=today, type_key=type_key, art_id=art_id,
         archive_key=archive_key, actor=actor)

    if not (type_key and art_id and archive_key):
        _log("ERROR", "restore_bad_request", type_key=type_key, art_id=art_id,
             archive_key=archive_key)
        return {"statusCode": 400, "error": "type_key, art_id, and archive_key are required"}

    # ── RUN-OPEN LOCK (v2.1 fix #23) ──────────────────────────────────────────
    # Refuse to run while ANY run is open (phase != "done"). A restore mutates
    # live/ + hash-index/ and would race an in-flight scrape/track/approve run.
    try:
        status = store.get(f"runs/{today}/status.json")
        if status.get("phase") != "done":
            reason = (
                f"run {today} open (phase={status.get('phase')}) — "
                "wait for approve/_done or the 02:00 sweep"
            )
            _log("WARN", "restore_refused_run_open", run_date=today,
                 phase=status.get("phase"))
            return {"status": "refused", "reason": reason}
    except KeyError:
        pass  # No run today — OK to restore

    # Load the archived version to restore.
    try:
        archived = store.get(archive_key)
    except KeyError:
        _log("ERROR", "restore_archive_not_found", archive_key=archive_key)
        return {"statusCode": 404, "error": f"archive key not found: {archive_key}"}

    live_key = f"live/{type_key}/{art_id}.json"

    # ── (a) Archive the current live version before overwriting ───────────────
    displaced_to: str | None = None
    if store.exists(live_key):
        displaced_to = f"{ARCHIVE_PREFIX}/{type_key}/{art_id}/{ts}.json"
        try:
            current = store.get(live_key)
            store.put(displaced_to, current)
            _log("INFO", "live_archived", live_key=live_key, displaced_to=displaced_to)
        except Exception as e:  # noqa: BLE001 — archive failure never blocks restore
            _log("WARN", "live_archive_failed", live_key=live_key, **exc_fields(e),
                 hint="restore continued WITHOUT a rollback copy of the displaced "
                      "live version; S3 bucket versioning is the remaining safety net")
            displaced_to = None

    # ── (b) Write restored content to live/ ───────────────────────────────────
    doc_type: str = type_key  # hash index keyed on type_key (matches dump.py:203)
    store.put(live_key, archived)
    _log("INFO", "live_written", live_key=live_key)

    # ── (c) Update hash index (recompute from restored metadata) ──────────────
    hash_index = store.load_hash_index(HASH_INDEX_KEY)
    metadata = archived.get("metadata") or {}
    hash_index[db_key(doc_type, art_id)] = sha256_obj(metadata)
    store.save_hash_index(hash_index, HASH_INDEX_KEY)
    _log("INFO", "hash_index_updated", hash_index_key=HASH_INDEX_KEY,
         db_key=db_key(doc_type, art_id))

    # ── (d) Append audit trail (monthly-partitioned) ──────────────────────────
    now_iso = _now_iso()
    month = datetime.date.today().strftime("%Y-%m")
    changed_ids_audit = f"audit/{month}/changed_ids.jsonl"
    decisions_audit = f"audit/{month}/decisions.jsonl"

    manifest_entry: dict = {
        "op": "restored",
        "id": art_id,
        "type_key": type_key,
        "s3_key": live_key,
        "run_date": today,
        "approved_by": actor,
        "restored_from": archive_key,
    }
    if displaced_to:
        manifest_entry["displaced_to"] = displaced_to

    store.append_jsonl(changed_ids_audit, {**manifest_entry, "ts": now_iso})
    store.append_jsonl(decisions_audit, {
        "op": "restored",
        "id": art_id,
        "type_key": type_key,
        "actor": actor,
        "restored_from": archive_key,
        "run_date": today,
        "ts": now_iso,
    })
    _log("INFO", "audit_written", changed_ids=changed_ids_audit, decisions=decisions_audit)

    # ── (e) Write single-entry manifest, then publish SNS (batch=restore) ─────
    # Manifest path follows the runs/{date}/ convention (v2 fix #5).
    manifest_key = f"runs/{today}/restore/{ts}/changed_ids.jsonl"
    store.append_jsonl(manifest_key, manifest_entry)

    sns_published = False
    try:
        _sns.publish(
            TopicArn=os.environ["HANDOFF_TOPIC_ARN"],
            Message=json.dumps({
                "schema": "f5kb.handoff.v2",
                "run_date": today,
                "mode": "restore",
                "batch": "restore",
                "article_count": 1,
                "manifest_key": manifest_key,
                "bucket": bucket,
                "published_at": _now_iso(),
            }),
        )
        sns_published = True
        _log("INFO", "sns_published", batch="restore", manifest_key=manifest_key,
             article_count=1)
    except Exception as e:  # noqa: BLE001 — SNS failure never blocks the restore
        _log("ERROR", "sns_publish_failed", manifest_key=manifest_key, **exc_fields(e),
             hint="the restore APPLIED but P2 was not notified — publish a backfill "
                  "with this manifest_key from the console Operations page")

    _log("INFO", "restore_complete", run_date=today, type_key=type_key, art_id=art_id,
         live_key=live_key, manifest_key=manifest_key, sns_published=sns_published)

    return {
        "status": "restored",
        "id": art_id,
        "type_key": type_key,
        "run_date": today,
        "archive_key": archive_key,
        "live_key": live_key,
        "displaced_to": displaced_to,
        "manifest_key": manifest_key,
        "actor": actor,
        "sns_published": sns_published,
        "ts": now_iso,
    }
