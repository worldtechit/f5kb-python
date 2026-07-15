"""Orchestrator Lambda (v2.1) — EventBridge cron entry point for the cloud pipeline.

Responsibilities
----------------
1. Action routing (``sweep_only`` / ``auto_escalate`` / ``run``).
2. Fail hard on a missing type list (fix #13) — never silently fall back to the
   4 enrichable types.
3. Step-zero blocking sweep of prior runs that never reached ``approve/_done``
   (v2 fix #10 / v2.1 fix #22): escalate held edits, resume closed-but-unapproved
   runs, or mark dead-mid-scrape runs failed. Abort today's run if a prior run
   with holds cannot be closed within the budget.
4. Conditional-PUT ``status.json`` so a duplicate cron delivery cannot double-start.
5. Persist ``orchestrator.json`` run manifest + ``status.json`` phase record.
6. Fan out one SQS message per type key to the Dump queue.

Run modes
---------
incremental  --days=2 metadata scan (Mon–Sat default)
full         --all @rowid keyset, entire corpus (Sunday default)

Pass ``{"mode": "incremental"|"full"}`` in the event payload to override the
day-of-week logic.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time

import boto3

from f5kb.lib.logutil import exc_fields
from f5kb.storage.s3 import S3Storage

LAMBDA_NAME = "f5kb-orchestrator"

HASH_INDEX_KEY = "hash-index/current.json.gz"

# Enrichable types (4) — the ones whose body the Coveo index leaves empty and
# that the Enrich Lambda writes the terminal /enrich/{Type}/_done marker for.
ENRICHABLE: frozenset[str] = frozenset({"Manual", "Release_Note", "Supplemental_Document", "Bug_Tracker"})

# Canonical 13 type keys fanned out to the Dump queue each run.
ALL_TYPES: list[str] = [
    "Support_Solution",
    "Known_Issue",
    "Knowledge",
    "Security_Advisory",
    "Video",
    "Policy",
    "Operations_Guide",
    "Compliance",
    "Education",
    "Manual",
    "Release_Note",
    "Supplemental_Document",
    "Bug_Tracker",
]

# Sunday = 6 in Python's weekday() (Mon=0 … Sun=6)
_FULL_SYNC_WEEKDAY = 6

# Sweep polling budget: give up closing a prior run after this many seconds.
_SWEEP_POLL_BUDGET_S = 5 * 60
_SWEEP_POLL_INTERVAL_S = 10


# ── logging ────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _log(level: str, action: str, **fields: object) -> None:
    rec: dict[str, object] = {
        "ts": _now_iso(),
        "level": level,
        "lambda": LAMBDA_NAME,
        "action": action,
    }
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec), file=sys.stderr)


# ── handler ──────────────────────────────────────────────────────────────────


def handler(event: dict, context: object) -> dict:
    try:
        return _handler(event, context)
    except Exception as e:
        _log("ERROR", "invocation_failed", **exc_fields(e),
             hint="orchestrator crashed — TODAY'S RUN DID NOT START unless run_started "
                  "was already logged. A sweep-abort RuntimeError means a PRIOR run "
                  "could not be closed: resolve its holds (console Review page or "
                  "approve Lambda auto_escalate), then re-trigger.")
        raise


def _handler(event: dict, context: object) -> dict:
    event = event or {}
    bucket = os.environ["BUCKET"]
    queue_url = os.environ["DUMP_QUEUE_URL"]

    store = S3Storage(bucket)

    action = str(event.get("action", "") or "").strip().lower()
    _log("INFO", "invocation_started", requested_action=action or "run",
         event_keys=sorted(event) or None)

    # 1. ACTION ROUTING ───────────────────────────────────────────────────────
    if action == "sweep_only":
        _run_sweep(store, bucket, context)
        return {"action": "sweep_only", "swept": True}

    if action == "auto_escalate":
        run_date = str(event["run_date"])
        _invoke_approve(run_date, "auto_escalate")
        closed = _poll_for_approve_done(store, run_date)
        return {"action": "auto_escalate", "run_date": run_date, "closed": closed}

    # action == "run" (or empty) → normal flow.
    run_date = os.environ.get("RUN_DATE") or datetime.date.today().isoformat()

    # 2. FAIL HARD on missing type list (fix #13) ──────────────────────────────
    all_types = _resolve_types(store)

    mode, mode_source = _resolve_mode(event, run_date)

    # 3. STEP ZERO SWEEP (blocking) ─────────────────────────────────────────────
    _run_sweep(store, bucket, context, today=run_date)

    # 4. CONDITIONAL PUT status.json (prevents double-start) ────────────────────
    enrichable = sorted(ENRICHABLE & set(all_types))
    status_doc = {
        "run_date": run_date,
        "mode": mode,
        "phase": "scrape",
        "phase_history": [{"phase": "scrape", "at": _now_iso(), "by": "orchestrator"}],
        "started_at": _now_iso(),
        "last_updated": _now_iso(),
        "summary": {
            "types_total": len(all_types),
            "enrichable": len(enrichable),
        },
        "errors": [],
    }
    won = store.put_conditional(
        f"runs/{run_date}/status.json",
        (json.dumps(status_doc, indent=2) + "\n").encode("utf-8"),
    )
    if not won:
        _log("INFO", "conditional_put_lost_412", run_date=run_date, key=f"runs/{run_date}/status.json")
        return {"run_date": run_date, "started": False, "reason": "already_started"}

    # 5. Write orchestrator.json run manifest ───────────────────────────────────
    store.put(
        f"lambda/state/{run_date}/orchestrator.json",
        {
            "run_date": run_date,
            "mode": mode,
            "mode_source": mode_source,
            "types_total": len(all_types),
            "types": all_types,
            "enrichable": enrichable,
            "started_at": _now_iso(),
        },
    )

    _log("INFO", "run_started", run_date=run_date, mode=mode, mode_source=mode_source, types_total=len(all_types))

    # 7. Fan out one SQS message per type to the Dump queue ──────────────────────
    sqs = boto3.client("sqs")
    for type_key in all_types:
        is_enrichable = type_key in ENRICHABLE
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(
                {
                    "run_date": run_date,
                    "type_key": type_key,
                    "mode": mode,
                    "hash_index_key": HASH_INDEX_KEY,
                    "enrichable": is_enrichable,
                }
            ),
        )
        _log("INFO", "type_queued", run_date=run_date, type_key=type_key, enrichable=is_enrichable)

    _log("INFO", "run_complete", run_date=run_date, mode=mode, types_queued=len(all_types))
    return {
        "run_date": run_date,
        "mode": mode,
        "mode_source": mode_source,
        "types_queued": len(all_types),
        "started": True,
    }


# ── type list resolution (fix #13) ───────────────────────────────────────────


def _resolve_types(store: S3Storage) -> list[str]:
    """Resolve the run's type list from env or S3 config. FAIL HARD if neither
    source is present — never silently fall back to the 4 enrichable types."""
    raw_types = os.environ.get("TYPE_KEYS", "")
    if raw_types.strip():
        env_types = [t.strip() for t in raw_types.split(",") if t.strip()]
        _log("INFO", "types_resolved", source="env:TYPE_KEYS", count=len(env_types))
        return env_types

    try:
        cfg = store.get("lambda/config/types.json")
    except KeyError:
        _log("ERROR", "config_missing_type_keys")
        raise RuntimeError("TYPE_KEYS not set and lambda/config/types.json absent — misconfiguration") from None

    types = cfg.get("types") if isinstance(cfg, dict) else None
    if not types:
        # Legacy config shape: mapping of {type_key: {...}}.
        if isinstance(cfg, dict) and cfg:
            types = list(cfg.keys())
    if not types:
        _log("ERROR", "config_missing_type_keys",
             hint="set the TYPE_KEYS template parameter or upload "
                  "lambda/config/types.json (scripts/sync_lambda_config.py)")
        raise RuntimeError("TYPE_KEYS not set and lambda/config/types.json absent — misconfiguration")
    _log("INFO", "types_resolved", source="s3:lambda/config/types.json", count=len(types))
    return list(types)


# ── mode resolution ──────────────────────────────────────────────────────────


def _resolve_mode(event: dict, run_date: str) -> tuple[str, str]:
    raw_mode = str(event.get("mode", "") or "").strip().lower()
    if raw_mode in ("incremental", "full"):
        return raw_mode, "manual"
    today = datetime.date.fromisoformat(run_date)
    mode = "full" if today.weekday() == _FULL_SYNC_WEEKDAY else "incremental"
    return mode, "schedule"


# ── step-zero sweep ───────────────────────────────────────────────────────────


def _run_sweep(store: S3Storage, bucket: str, context: object, today: str | None = None) -> None:
    """Blocking sweep of every prior run directory that has not reached
    approve/_done. Closes what can be closed; marks dead-mid-scrape runs failed;
    aborts today's run (raise) if a prior run with holds cannot be closed."""
    _log("INFO", "sweep_started", today=today)

    run_dates = _list_prior_run_dates(store, today)
    swept = 0
    for run_date in run_dates:
        if store.exists(f"runs/{run_date}/approve/_done"):
            continue
        swept += 1

        held = _has_holds(store, run_date)
        has_track_done = store.exists(f"runs/{run_date}/track/_done")

        if held:
            # Case A: held edits await escalation.
            holds_rejected = _count_holds(store, run_date)
            _log("INFO", "sweep_escalated_run", run_date=run_date, holds_rejected=holds_rejected)
            _invoke_approve(run_date, "auto_escalate")
            if not _poll_for_approve_done(store, run_date):
                _publish_ops_alert(
                    f"Orchestrator sweep could not close prior run {run_date} "
                    f"with {holds_rejected} held edits within the {_SWEEP_POLL_BUDGET_S}s budget; "
                    "aborting today's run."
                )
                raise RuntimeError(f"sweep: prior run {run_date} with holds could not be closed — aborting")
        elif has_track_done:
            # Case B: track finished but approve never ran, no holds → resume.
            _log("INFO", "sweep_resumed_run", run_date=run_date,
                 hint="prior run reached track/_done but approve never closed — "
                      "invoking the Approve Lambda with action=resume")
            _invoke_approve(run_date, "resume")
            if not _poll_for_approve_done(store, run_date):
                _publish_ops_alert(
                    f"Orchestrator sweep could not close prior run {run_date} "
                    f"(resume) within the {_SWEEP_POLL_BUDGET_S}s budget; aborting today's run."
                )
                raise RuntimeError(f"sweep: prior run {run_date} (resume) could not be closed — aborting")
        else:
            # Case C: died mid-scrape (no track/_done) → mark failed, do not finish.
            _mark_run_failed(store, run_date)
            _publish_ops_alert(
                f"Orchestrator sweep found prior run {run_date} dead mid-scrape "
                "(no track/_done); marked failed. Today's run supersedes it."
            )
            _log("INFO", "sweep_marked_failed", run_date=run_date,
                 hint="run died before track/_done (scrape never finished) — its "
                      "pending/ staging remains; today's full run re-stages anything "
                      "still changed, so no data is lost")

    _log("INFO", "sweep_complete", today=today,
         prior_runs=len(run_dates), open_runs_handled=swept)


def _list_prior_run_dates(store: S3Storage, today: str | None) -> list[str]:
    """Return sorted run-date directory names under runs/ strictly older than
    today (or all of them, when today is None as in a sweep_only invocation)."""
    keys = store.list_prefix("runs/")
    dates: set[str] = set()
    for k in keys:
        parts = k.split("/")
        if len(parts) >= 2 and parts[0] == "runs":
            dates.add(parts[1])
    if today is not None:
        dates = {d for d in dates if d < today}
    return sorted(dates)


def _has_holds(store: S3Storage, run_date: str) -> bool:
    if store.exists(f"lambda/state/{run_date}/approve_held.json"):
        try:
            data = store.get(f"lambda/state/{run_date}/approve_held.json")
        except KeyError:
            return False
        return bool(_held_entries(data))
    return False


def _count_holds(store: S3Storage, run_date: str) -> int:
    try:
        data = store.get(f"lambda/state/{run_date}/approve_held.json")
    except KeyError:
        return 0
    return len(_held_entries(data))


def _held_entries(data: object) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for field in ("held", "holds", "entries", "ids"):
            val = data.get(field)
            if isinstance(val, list):
                return val
    return []


def _mark_run_failed(store: S3Storage, run_date: str) -> None:
    try:
        status = store.get(f"runs/{run_date}/status.json")
    except KeyError:
        status = {"run_date": run_date, "phase_history": []}
    if not isinstance(status, dict):
        status = {"run_date": run_date, "phase_history": []}
    history = status.get("phase_history")
    if not isinstance(history, list):
        history = []
    history.append({"phase": "failed", "at": _now_iso(), "by": "orchestrator-sweep"})
    status["phase"] = "failed"
    status["phase_history"] = history
    status["last_updated"] = _now_iso()
    errors = status.get("errors")
    if not isinstance(errors, list):
        errors = []
    errors.append(
        {
            "at": _now_iso(),
            "by": "orchestrator-sweep",
            "message": "died mid-scrape (no track/_done); superseded by newer run",
        }
    )
    status["errors"] = errors
    store.put(f"runs/{run_date}/status.json", status)


# ── Approve Lambda invocation + polling ──────────────────────────────────────


def _invoke_approve(run_date: str, action: str) -> None:
    boto3.client("lambda").invoke(
        FunctionName=os.environ["APPROVE_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"action": action, "run_date": run_date}).encode("utf-8"),
    )


def _poll_for_approve_done(store: S3Storage, run_date: str) -> bool:
    """Poll runs/{date}/approve/_done every 10s up to a 5-minute budget.
    Returns True once the marker appears, False if the budget elapses."""
    started = time.monotonic()
    deadline = started + _SWEEP_POLL_BUDGET_S
    while True:
        if store.exists(f"runs/{run_date}/approve/_done"):
            _log("INFO", "sweep_run_closed", run_date=run_date,
                 waited_s=int(time.monotonic() - started))
            return True
        if time.monotonic() >= deadline:
            _log("WARN", "sweep_poll_timeout", run_date=run_date,
                 budget_s=_SWEEP_POLL_BUDGET_S,
                 hint="approve/_done never appeared — check the approve Lambda's logs "
                      "for this run_date (it was invoked async just before this poll)")
            return False
        time.sleep(_SWEEP_POLL_INTERVAL_S)


# ── ops alerts ────────────────────────────────────────────────────────────────


def _publish_ops_alert(message: str) -> None:
    topic = os.environ.get("OPS_TOPIC_ARN")
    if not topic:
        return
    try:
        boto3.client("sns").publish(
            TopicArn=topic,
            Subject="f5kb orchestrator sweep alert",
            Message=json.dumps(
                {
                    "schema": "f5kb.ops.v1",
                    "lambda": LAMBDA_NAME,
                    "message": message,
                    "at": _now_iso(),
                }
            ),
        )
    except Exception:  # pragma: no cover - alerting must never crash the handler
        _log("ERROR", "ops_alert_publish_failed", message=message)
