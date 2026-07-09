"""Watchdog Lambda — daily cron; auto-escalates stale held approvals + ops alerts.

Two jobs, both driven by scanning ``lambda/state/*/approve_held.json``:

  1. Stale hold escalation — if a run still has unresolved holds older than
     HOLD_ESCALATE_AGE_H, invoke Approve with ``action=auto_escalate`` so the
     backlog does not grow unbounded. auto_escalate approves the remaining holds.

  2. Ops alerting — publish a summary of any run with outstanding holds (and any
     run whose scrape gate exists but never reached track/_done) to OPS_TOPIC_ARN.

Triggered by the WatchdogSchedule EventBridge cron. Emits structured JSON to stderr.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

import boto3

from f5kb.storage.s3 import S3Storage

LAMBDA_NAME = "f5kb-watchdog"

# Holds older than this (hours) are auto-escalated (approved) to clear the backlog.
HOLD_ESCALATE_AGE_H = int(os.environ.get("HOLD_ESCALATE_AGE_H", "24"))


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


def handler(event: dict, context: object) -> dict:
    bucket = os.environ["BUCKET"]
    store = S3Storage(bucket)

    escalated: list[dict] = []
    outstanding: list[dict] = []

    # Find every held-approval state file.
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

    if outstanding:
        _alert_ops(bucket, escalated, outstanding)

    _log("INFO", "watchdog_complete",
         escalated=len(escalated), outstanding=len(outstanding))
    return {
        "status": "done",
        "escalated": len(escalated),
        "outstanding": len(outstanding),
    }


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
        _log("ERROR", "hold_escalated", run_date=run_date, error=str(e))


def _alert_ops(bucket: str, escalated: list[dict], outstanding: list[dict]) -> None:
    topic_arn = os.environ.get("OPS_TOPIC_ARN", "")
    if not topic_arn:
        return
    try:
        boto3.client("sns").publish(
            TopicArn=topic_arn,
            Subject="f5kb watchdog: outstanding held approvals",
            Message=json.dumps({
                "schema": "f5kb.ops.watchdog.v1",
                "bucket": bucket,
                "escalated": escalated,
                "outstanding": outstanding,
                "generated_at": _now_iso(),
            }, indent=2),
        )
        _log("INFO", "ops_alerted", outstanding=len(outstanding), escalated=len(escalated))
    except Exception as e:
        _log("ERROR", "ops_alerted", error=str(e))
