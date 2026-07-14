"""Watchdog Lambda — hourly cron; escalates stale holds + auto-redrives stalls.

Three jobs:

  1. Stale hold escalation — if a run still has unresolved holds older than
     HOLD_ESCALATE_AGE_H, invoke Approve with ``action=auto_escalate`` so the
     backlog does not grow unbounded. auto_escalate approves the remaining holds.

  2. Stall auto-redrive — a type's self-requeue chain dies when its resume
     message fails 3 deliveries (e.g. the pipeline was paused mid-retry) and
     lands in a DLQ; the run then sits at the scrape barrier forever. The
     watchdog redrives such a message back to its work queue when ALL hold:
       - the message's run is OPEN (no approve/_done)
       - the work queue is EMPTY (nothing visible or in flight)
       - the type's cursor is STALE (> STALL_AGE_H) or absent
       - the message has been redriven fewer than MAX_REDRIVES times
         (a ``watchdog_redrives`` counter rides in the message body)
     At the cap it stops touching the message and escalates alerts instead —
     bounded compute (~3 deliveries per redrive), never an infinite loop.

  3. Ops alerting — action alerts (escalations, redrives, cap-exceeded) go out
     any hour; the outstanding-holds digest only on the 06:00 UTC pass so the
     hourly schedule does not spam email.

Triggered by the WatchdogCron EventBridge schedule (rate: 1 hour). Emits
structured JSON to stderr.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Any

import boto3

from f5kb.lib.logutil import exc_fields
from f5kb.storage.s3 import S3Storage

LAMBDA_NAME = "f5kb-watchdog"

# Holds older than this (hours) are auto-escalated (approved) to clear the backlog.
HOLD_ESCALATE_AGE_H = int(os.environ.get("HOLD_ESCALATE_AGE_H", "24"))

# A cursor untouched for this long (with an empty queue + a DLQ message for the
# run) marks the type as stalled.
STALL_AGE_H = float(os.environ.get("STALL_AGE_H", "1"))

# Give up auto-redriving a message after this many attempts; escalate instead.
MAX_REDRIVES = int(os.environ.get("WATCHDOG_MAX_REDRIVES", "3"))

# UTC hour of the daily digest pass (outstanding-holds email).
DIGEST_HOUR_UTC = 6


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _log(level: str, action: str, **fields: object) -> None:
    rec: dict[str, object] = {
        "ts": _now_iso(), "level": level, "lambda": LAMBDA_NAME, "action": action,
    }
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec), file=sys.stderr)


def _age_hours(iso_ts: str) -> float:
    try:
        t = datetime.datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600.0


def stall_decision(run_open: bool, queue_empty: bool, cursor_age_h: float | None,
                   redrives: int, stall_age_h: float = STALL_AGE_H,
                   max_redrives: int = MAX_REDRIVES) -> str:
    """Decide what to do with one DLQ message. Pure — unit-tested offline.

    Returns:
      "orphan"      — run closed/deleted; leave the message alone
      "skip_active" — queue has traffic or the cursor is fresh; a Lambda may
                      still be working, do not double-drive
      "cap"         — redriven max_redrives times already; escalate, hands off
      "redrive"     — chain is dead and the message is the resume instruction
    """
    if not run_open:
        return "orphan"
    if not queue_empty:
        return "skip_active"
    if cursor_age_h is not None and cursor_age_h < stall_age_h:
        return "skip_active"  # cursor updated recently — invocation in flight
    if redrives >= max_redrives:
        return "cap"
    return "redrive"


def handler(event: dict, context: object) -> dict:
    try:
        return _handler(event, context)
    except Exception as e:
        _log("ERROR", "invocation_failed", **exc_fields(e),
             hint="watchdog crashed — stale-hold escalation did NOT run this cycle; "
                  "next scheduled run retries")
        raise


def _handler(event: dict, context: object) -> dict:
    bucket = os.environ["BUCKET"]
    store = S3Storage(bucket)
    _log("INFO", "invocation_started", escalate_age_h=HOLD_ESCALATE_AGE_H,
         stall_age_h=STALL_AGE_H, max_redrives=MAX_REDRIVES)

    escalated: list[dict] = []
    outstanding: list[dict] = []

    # ── Job 1: stale hold escalation ─────────────────────────────────────────
    for key in store.list_prefix("lambda/state/"):
        if not key.endswith("/approve_held.json"):
            continue
        try:
            state = store.get(key)
        except KeyError:
            continue

        run_date = state.get("run_date") or key.split("/")[2]
        remaining = int(state.get("remaining", len(state.get("entries") or [])))
        if remaining <= 0:
            continue

        age_h = _age_hours(state.get("updated_at") or "")
        outstanding.append({"run_date": run_date, "remaining": remaining, "age_hours": round(age_h, 1)})

        if age_h >= HOLD_ESCALATE_AGE_H:
            _escalate(run_date)
            escalated.append({"run_date": run_date, "remaining": remaining, "age_hours": round(age_h, 1)})
            _log("INFO", "hold_escalated", run_date=run_date,
                 remaining=remaining, age_hours=round(age_h, 1))

    # ── Job 2: stall auto-redrive ────────────────────────────────────────────
    stall_report = _redrive_stalls(store)

    # ── Job 3: alerts — actions any hour, holds digest once daily ────────────
    now_hour = datetime.datetime.now(datetime.timezone.utc).hour
    action_taken = bool(escalated or stall_report["redriven"] or stall_report["capped"])
    if action_taken or (outstanding and now_hour == DIGEST_HOUR_UTC):
        _alert_ops(bucket, escalated, outstanding, stall_report)

    _log("INFO", "watchdog_complete",
         escalated=len(escalated), outstanding=len(outstanding),
         redriven=len(stall_report["redriven"]), capped=len(stall_report["capped"]),
         orphans=len(stall_report["orphans"]))
    return {
        "status": "done",
        "escalated": len(escalated),
        "outstanding": len(outstanding),
        "redriven": stall_report["redriven"],
        "capped": stall_report["capped"],
    }


# ── stall auto-redrive ─────────────────────────────────────────────────────────

def _redrive_stalls(store: S3Storage) -> dict:
    """Scan both DLQs; redrive dead resume messages of open runs (bounded)."""
    report: dict = {"redriven": [], "capped": [], "orphans": [], "skipped": []}
    sqs = boto3.client("sqs")
    pairs = [
        (os.environ.get("DUMP_DLQ_URL", ""), os.environ.get("DUMP_QUEUE_URL", ""), "dump"),
        (os.environ.get("ENRICH_DLQ_URL", ""), os.environ.get("ENRICH_QUEUE_URL", ""), "enrich"),
    ]
    for dlq_url, work_url, kind in pairs:
        if not dlq_url or not work_url:
            _log("WARN", "stall_sweep_skipped", kind=kind,
                 hint="DLQ/work queue URL env vars missing — template not redeployed "
                      "with the watchdog stall-redrive additions")
            continue
        seen: set[str] = set()
        for _ in range(4):
            try:
                resp = sqs.receive_message(
                    QueueUrl=dlq_url, MaxNumberOfMessages=10,
                    WaitTimeSeconds=1, VisibilityTimeout=30)
            except Exception as e:
                _log("ERROR", "dlq_receive_failed", kind=kind, **exc_fields(e),
                     hint="check IAM sqs:ReceiveMessage on the DLQ (PipelineQueues Sid)")
                break
            msgs = resp.get("Messages", [])
            if not msgs:
                break
            for m in msgs:
                mid = m.get("MessageId") or ""
                if mid in seen:
                    continue
                seen.add(mid)
                _handle_dlq_message(store, sqs, m, dlq_url, work_url, kind, report)
    return report


def _handle_dlq_message(store: S3Storage, sqs: Any, m: dict, dlq_url: str,
                        work_url: str, kind: str, report: dict) -> None:
    try:
        body = json.loads(m.get("Body") or "")
        assert isinstance(body, dict)
    except Exception:
        report["orphans"].append({"kind": kind, "note": "non-JSON body"})
        return
    run_date = str(body.get("run_date") or "")
    type_key = str(body.get("type_key") or "")
    redrives = int(body.get("watchdog_redrives", 0))
    label = {"kind": kind, "run_date": run_date, "type_key": type_key,
             "redrives": redrives}

    run_open = bool(run_date) and not store.exists(f"runs/{run_date}/approve/_done") \
        and bool(store.list_prefix(f"runs/{run_date}/"))
    queue_empty = _queue_empty(sqs, work_url)
    cursor_age = _cursor_age_h(store, run_date, type_key, kind)

    decision = stall_decision(run_open, queue_empty, cursor_age, redrives)
    _log("INFO", "stall_checked", **label, decision=decision,
         run_open=run_open, queue_empty=queue_empty,
         cursor_age_h=round(cursor_age, 2) if cursor_age is not None else None)

    if decision == "redrive":
        body["watchdog_redrives"] = redrives + 1
        try:
            sqs.send_message(QueueUrl=work_url, MessageBody=json.dumps(body))
            sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"])
        except Exception as e:
            _log("ERROR", "stall_redrive_failed", **label, **exc_fields(e),
                 hint="send or delete failed — message stays in the DLQ; next "
                      "hourly pass retries")
            return
        report["redriven"].append(label)
        _log("INFO", "stall_redriven", **label,
             hint=f"resume message re-sent to the {kind} queue (attempt "
                  f"{redrives + 1}/{MAX_REDRIVES}); the type continues from its "
                  "saved cursor")
    elif decision == "cap":
        report["capped"].append(label)
        _log("ERROR", "stall_redrive_cap", **label,
             hint=f"redriven {MAX_REDRIVES}x and it keeps dying — a human must "
                  "fix the underlying cause, then redrive from the console "
                  "Operations page")
    elif decision == "orphan":
        report["orphans"].append(label)
    else:
        report["skipped"].append(label)


def _queue_empty(sqs: Any, url: str) -> bool:
    try:
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return (int(attrs["ApproximateNumberOfMessages"]) == 0
                and int(attrs["ApproximateNumberOfMessagesNotVisible"]) == 0)
    except Exception:
        return False  # unknown → assume busy → never redrive blind


def _cursor_age_h(store: S3Storage, run_date: str, type_key: str,
                  kind: str) -> float | None:
    """Hours since the type's cursor was updated; None if no cursor exists
    (chain may have died before its first checkpoint — treated as stale)."""
    if not run_date or not type_key:
        return None
    key = f"lambda/state/{run_date}/{kind}-{type_key}.json"
    try:
        state = store.get(key)
    except KeyError:
        return None
    return _age_hours(str(state.get("updated_at") or state.get("last_updated") or ""))


def _escalate(run_date: str) -> None:
    """Invoke Approve to auto-approve the remaining holds for a stale run."""
    fn_name = os.environ["APPROVE_FUNCTION_NAME"]
    try:
        boto3.client("lambda").invoke(
            FunctionName=fn_name,
            InvocationType="Event",
            Payload=json.dumps({
                "action": "auto_escalate",
                "run_date": run_date,
                "actor": "watchdog",
            }).encode("utf-8"),
        )
    except Exception as e:
        _log("ERROR", "hold_escalate_invoke_failed", run_date=run_date,
             **exc_fields(e),
             hint="the Approve Lambda was NOT invoked — holds stay open; check IAM "
                  "lambda:InvokeFunction and APPROVE_FUNCTION_NAME")


def _alert_ops(bucket: str, escalated: list[dict], outstanding: list[dict],
               stall_report: dict | None = None) -> None:
    topic_arn = os.environ.get("OPS_TOPIC_ARN", "")
    if not topic_arn:
        return
    stall_report = stall_report or {}
    bits = []
    if escalated:
        bits.append(f"{len(escalated)} hold(s) escalated")
    if stall_report.get("redriven"):
        bits.append(f"{len(stall_report['redriven'])} stalled type(s) auto-redriven")
    if stall_report.get("capped"):
        bits.append(f"{len(stall_report['capped'])} redrive cap exceeded — NEEDS HUMAN")
    if not bits and outstanding:
        bits.append(f"{len(outstanding)} run(s) with outstanding holds")
    try:
        boto3.client("sns").publish(
            TopicArn=topic_arn,
            Subject=f"f5kb watchdog: {'; '.join(bits) or 'status'}",
            Message=json.dumps({
                "schema": "f5kb.ops.watchdog.v1",
                "bucket": bucket,
                "escalated": escalated,
                "outstanding": outstanding,
                "stalls_redriven": stall_report.get("redriven") or [],
                "stalls_capped": stall_report.get("capped") or [],
                "generated_at": _now_iso(),
            }, indent=2),
        )
        _log("INFO", "ops_alerted", outstanding=len(outstanding),
             escalated=len(escalated),
             redriven=len(stall_report.get("redriven") or []),
             capped=len(stall_report.get("capped") or []))
    except Exception as e:
        _log("ERROR", "ops_alert_publish_failed", **exc_fields(e),
             hint="SNS publish to OPS_TOPIC_ARN failed — nobody was emailed about "
                  "the outstanding holds listed in the previous log lines")
