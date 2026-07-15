"""Approve Lambda (v2.1) — Gate 1: auto-promote safe articles, Slack-hold risky ones.

Invocation modes (routed by event *shape*, never httpMethod):

  1. EventBridge S3 event on ``runs/{date}/track/_done``
         → automatic approval pass.
  2. Direct invoke carrying an ``"action"`` field
         → hold resolution (approve / reject / approve_all / reject_all /
           resume / auto_escalate).  Slack button callbacks now arrive here
           via the SlackAck Lambda as direct invokes — NOT as API Gateway
           events.

v2.1 major changes vs. the previous handler:
  * per-type manifests (``runs/{date}/manifest/{TypeKey}.jsonl``);
  * idempotency guard using a conditional ``approve/started.json`` sentinel
    with clean-start / resume / concurrent-loser branches;
  * SPLIT PUBLISH (fix #15) — the auto batch is handed to P2 over SNS
    immediately, before any holds are resolved;
  * two-phase Slack dedup (fix #17) — ``slack_attempt`` (conditional) then
    ``slack_sent`` markers, with a stale-attempt crash-recovery window;
  * SSM-backed Slack webhook fetched once at cold start;
  * self-reinvoke when the remaining Lambda budget drops below 60 s;
  * structured single-line JSON logging to stderr.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request

import boto3

from f5kb.lib.dump import db_key
from f5kb.lib.logutil import exc_fields
from f5kb.storage.s3 import S3Storage
from f5kb.track.hashing import sha256_obj

# ── Constants ──────────────────────────────────────────────────────────────────

LAMBDA_NAME = "f5kb-approve"

HASH_INDEX_KEY = "hash-index/current.json.gz"
ARCHIVE_PREFIX = "archive"

# The ONLY risk flags that force a human hold. body-shrank-N% is informational
# and always auto-approves (matching the local CLI's gate semantics).
MANUAL_FLAGS: frozenset[str] = frozenset({"body-dropped", "body-error"})

# Max held-article blocks in a single Slack message (rest summarised in one line).
SLACK_MAX_HELD_BLOCKS = int(os.environ.get("SLACK_MAX_HELD_BLOCKS", "5"))

# Self-reinvoke guard: bail out and re-queue when this little budget remains.
REINVOKE_FLOOR_MS = 60_000

# A stale Slack attempt older than this (seconds) is treated as a crashed sender.
SLACK_ATTEMPT_STALE_S = 300

ENRICHABLE: frozenset[str] = frozenset(
    {"Manual", "Release_Note", "Supplemental_Document", "Bug_Tracker"}
)

ALL_TYPES: list[str] = [
    "Support_Solution", "Known_Issue", "Knowledge", "Security_Advisory", "Video",
    "Policy", "Operations_Guide", "Compliance", "Education", "Manual",
    "Release_Note", "Supplemental_Document", "Bug_Tracker",
]

# ── AWS clients / cold-start secret cache ────────────────────────────────────────

_sns = boto3.client("sns")
_SLACK_WEBHOOK: str | None = None


def _handoff_topic_arn() -> str:
    return os.environ["HANDOFF_TOPIC_ARN"]


def _get_slack_webhook() -> str | None:
    """Fetch the Slack webhook URL from SSM once per cold start."""
    global _SLACK_WEBHOOK
    if _SLACK_WEBHOOK is None:
        param = os.environ.get("SLACK_WEBHOOK_PARAM", "")
        if param:
            _SLACK_WEBHOOK = boto3.client("ssm").get_parameter(
                Name=param, WithDecryption=True
            )["Parameter"]["Value"]
    return _SLACK_WEBHOOK


# ── Structured logging ───────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _now_stamp() -> str:
    # Dashes for filesystem/S3-safe archive filenames (never colons).
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _log(level: str, action: str, **fields: object) -> None:
    rec: dict[str, object] = {
        "ts": _now_iso(), "level": level, "lambda": LAMBDA_NAME, "action": action,
    }
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec), file=sys.stderr)


def _age_seconds(iso_ts: str) -> float:
    try:
        t = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return float("inf")
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()


def _ms_remaining(context: object) -> float:
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if callable(getter):
        try:
            return float(getter())
        except Exception:
            return float("inf")
    return float("inf")


# ── Entry point ──────────────────────────────────────────────────────────────────

def handler(event: dict, context: object) -> dict:
    try:
        return _handler(event, context)
    except Exception as e:
        _log("ERROR", "invocation_failed", **exc_fields(e),
             hint="approve crashed. The started.json idempotency guard means a "
                  "re-invoke (or the orchestrator sweep's resume action) safely "
                  "continues — already-promoted articles are skipped via "
                  "approved-ids. Check the traceback frame first.")
        raise


def _handler(event: dict, context: object) -> dict:
    bucket = os.environ["BUCKET"]
    store = S3Storage(bucket)

    # Route by event shape, NOT httpMethod.
    if isinstance(event, dict) and event.get("action"):
        _log("INFO", "invocation_started", route="action",
             requested_action=event.get("action"), run_date=event.get("run_date"))
        return _handle_action(event, store, bucket, context)
    _log("INFO", "invocation_started", route="automatic")
    return _handle_automatic(event, store, bucket, context)


# ── Mode 1: automatic approval pass ───────────────────────────────────────────────

def _handle_automatic(event: dict, store: S3Storage, bucket: str, context: object) -> dict:
    run_date = _run_date_from_event(event)
    mode = _run_mode(store, run_date)

    done_key = f"runs/{run_date}/approve/_done"
    started_key = f"runs/{run_date}/approve/started.json"

    # ── Idempotency guard (FIRST action) ─────────────────────────────────────────
    if store.exists(done_key):
        _log("INFO", "approve_started", run_date=run_date, idempotency="already_done")
        return {"status": "already_done", "run_date": run_date}

    if store.exists(started_key):
        already_approved = _load_approved_ids(store, run_date)
        idempotency = "resume"
    else:
        won = store.put_conditional(
            started_key, json.dumps({"started_at": _now_iso()}).encode("utf-8")
        )
        if not won:
            _log("INFO", "conditional_put_lost_412", run_date=run_date, key=started_key)
            _log("INFO", "approve_started", run_date=run_date, idempotency="concurrent_lost")
            return {"status": "concurrent_invoke_won", "run_date": run_date}
        already_approved = set()
        idempotency = "clean_start"

    _log("INFO", "approve_started", run_date=run_date, idempotency=idempotency,
         resumed_count=len(already_approved) or None)

    # ── Read per-type manifests (v2) ─────────────────────────────────────────────
    entries = _load_manifest_entries(store, run_date)
    risk_index = _load_risk_index(store, run_date)
    hash_index = store.load_hash_index(HASH_INDEX_KEY)

    auto_approved: list[dict] = []
    held: list[dict] = []

    for entry in entries:
        # Budget check — self-reinvoke rather than risk a mid-promote timeout.
        if _ms_remaining(context) < REINVOKE_FLOOR_MS:
            store.save_hash_index(hash_index, HASH_INDEX_KEY)
            return _self_reinvoke(event, context, run_date)

        type_key = entry.get("type_key") or entry.get("type") or ""
        art_id = entry.get("id") or ""
        doc_type = type_key  # hash index keyed on type_key (matches dump.py:203)
        pending_key = entry.get("pending_key") or f"pending/{type_key}/{art_id}.json"
        live_key = f"live/{type_key}/{art_id}.json"

        # Skip anything already promoted on a prior (crashed) invocation.
        if f"{type_key}/{art_id}" in already_approved:
            continue

        risk_entry = risk_index.get(f"{type_key}/{art_id}") or {}
        risk: list[str] = risk_entry.get("risk") or []
        changed: list[str] = risk_entry.get("changed") or []
        op: str = risk_entry.get("op") or "changed"

        try:
            pending = store.get(pending_key)
        except KeyError:
            continue

        if _is_held(risk):
            held.append(_build_held_entry(store, type_key, art_id, doc_type,
                                          pending_key, live_key, pending, risk, changed, op))
            _log("INFO", "article_held", run_date=run_date, type_key=type_key,
                 art_id=art_id, risk=risk)
            continue

        # ── Auto-approve: archive → promote pending → live ────────────────────────
        _promote(store, pending, pending_key, live_key, type_key, art_id)
        metadata = pending.get("metadata") or {}
        new_hash = sha256_obj(metadata)
        hash_index[db_key(doc_type, art_id)] = new_hash

        changed_entry = {
            "op": op,
            "id": art_id,
            "type_key": type_key,  # contract field (playbook/consumer guide)
            "type": type_key,      # legacy duplicate — keep for old readers
            "document_type": doc_type,
            "s3_key": live_key,
            "run_date": run_date,
            "approved_by": "auto",
            "hash": new_hash,
        }
        if changed:
            changed_entry["changed"] = changed
        store.append_jsonl(f"runs/{run_date}/approve/changed_ids.jsonl", changed_entry)
        auto_approved.append(changed_entry)
        _log("INFO", "article_auto_approved", run_date=run_date, type_key=type_key,
             art_id=art_id, op=op)

    # ── SPLIT PUBLISH (fix #15) — auto batch is finalised & handed off NOW ────────
    auto_manifest_key = f"runs/{run_date}/approve/changed_ids.jsonl"
    store.save_hash_index(hash_index, HASH_INDEX_KEY)
    _write_audit(store, run_date, auto_approved, actor="auto", source="auto")
    _publish_handoff(store, bucket, run_date, mode, "auto", auto_manifest_key, len(auto_approved))

    # One Slack message (two-phase dedup); holds do NOT delay the P2 handoff above.
    _send_slack_once(store, run_date, auto_approved, held)

    if not held:
        _write_done_conditional(store, run_date)
        _log("INFO", "approve_complete", run_date=run_date,
             auto_approved=len(auto_approved), held=0)
        return {"status": "done", "auto_approved": len(auto_approved), "held": 0}

    # Persist holds for later resolution (Approve is the sole writer of this file).
    held_manifest_key = f"runs/{run_date}/approve/changed_ids-holds.jsonl"
    store.put(f"lambda/state/{run_date}/approve_held.json", {
        "run_date": run_date,
        "mode": mode,
        "remaining": len(held),
        "entries": held,
        "auto_approved_count": len(auto_approved),
        "auto_manifest_key": auto_manifest_key,
        "held_manifest_key": held_manifest_key,
        "resolved": [],
        "updated_at": _now_iso(),
    })
    _log("INFO", "approve_complete", run_date=run_date,
         auto_approved=len(auto_approved), held=len(held), status="awaiting_review")
    return {"status": "awaiting_review", "auto_approved": len(auto_approved), "held": len(held)}


# ── Mode 2: hold resolution (direct invoke with action field) ─────────────────────

def _handle_action(event: dict, store: S3Storage, bucket: str, context: object) -> dict:
    action = event.get("action") or ""
    run_date = event.get("run_date") or ""
    actor = event.get("actor", "unknown")
    response_url = event.get("response_url")  # optional Slack response webhook

    if action == "resume":
        # Re-drive the automatic pass (idempotency guard sorts out the rest).
        return _handle_automatic({"run_date": run_date}, store, bucket, context)

    state_key = f"lambda/state/{run_date}/approve_held.json"
    try:
        held_state = store.get(state_key)
    except KeyError:
        _log("WARN", "hold_resolved", run_date=run_date, requested_action=action,
             actor=actor, result="no_held_state")
        _post_response_url(response_url, f"No held state found for {run_date}.")
        return {"status": "no_held_state", "run_date": run_date}

    entries: list[dict] = held_state.get("entries") or []
    hash_index = store.load_hash_index(HASH_INDEX_KEY)
    resolved: list[dict] = []

    if action in ("approve", "reject"):
        type_key = event.get("type_key") or ""
        art_id = event.get("id") or event.get("art_id") or ""
        target = next(
            (h for h in entries if h.get("type_key") == type_key and h.get("id") == art_id),
            None,
        )
        if target is None:
            _post_response_url(response_url, f"{art_id} already resolved or not held.")
            return {"status": "not_held", "run_date": run_date, "id": art_id}
        rec = _resolve_one(store, run_date, target, hash_index,
                           approve=(action == "approve"), actor=actor)
        if rec:
            resolved.append(rec)
        entries = [h for h in entries
                   if not (h.get("type_key") == type_key and h.get("id") == art_id)]
        _log("INFO", "hold_resolved", run_date=run_date, decision=action,
             type_key=type_key, art_id=art_id, actor=actor, source="slack")

    elif action in ("approve_all", "reject_all", "auto_escalate"):
        approve_all = action == "approve_all"
        for h in entries:
            rec = _resolve_one(store, run_date, h, hash_index,
                               approve=approve_all, actor=actor)
            if rec:
                resolved.append(rec)
        decision = "approved" if approve_all else "rejected"
        _log("INFO", "hold_resolved", run_date=run_date, decision=decision, actor=actor,
             source=("auto_escalate" if action == "auto_escalate" else "slack"),
             count=len(entries))
        entries = []

    else:
        _log("WARN", "hold_resolved", run_date=run_date, requested_action=action,
             result="unknown_action")
        return {"status": "unknown_action", "action": action}

    if resolved:
        store.save_hash_index(hash_index, HASH_INDEX_KEY)

    # Persist decremented held state.
    held_state["entries"] = entries
    held_state["remaining"] = len(entries)
    held_state.setdefault("resolved", []).extend(
        {"id": r["id"], "type": r["type"], "op": r.get("op"), "actor": r.get("approved_by")}
        for r in resolved
    )
    held_state["updated_at"] = _now_iso()
    store.put(state_key, held_state)

    result = {"status": "hold_resolved", "run_date": run_date,
              "resolved": len(resolved), "remaining": len(entries)}

    if not entries:
        _finalize_holds(store, bucket, run_date, held_state)
        result["status"] = "all_resolved"

    _post_response_url(
        response_url, f"{run_date}: {len(resolved)} resolved, {len(entries)} remaining."
    )
    return result


def _resolve_one(
    store: S3Storage,
    run_date: str,
    held: dict,
    hash_index: dict[str, str],
    *,
    approve: bool,
    actor: str,
) -> dict | None:
    """Promote or drop a single held article. Returns its changed record (approve)."""
    type_key = held.get("type_key") or ""
    art_id = held.get("id") or ""
    doc_type = type_key  # hash index keyed on type_key (matches dump.py:203)
    pending_key = held.get("pending_key") or f"pending/{type_key}/{art_id}.json"
    live_key = f"live/{type_key}/{art_id}.json"
    op = held.get("op") or "changed"

    if not approve:
        store.delete(pending_key)
        store.append_jsonl(f"audit/{run_date[:7]}/decisions.jsonl", {
            "id": art_id, "type": type_key, "op": "rejected",
            "actor": actor, "run_date": run_date, "ts": _now_iso(),
        })
        return None

    try:
        pending = store.get(pending_key)
    except KeyError:
        return None

    _promote(store, pending, pending_key, live_key, type_key, art_id)
    new_hash = sha256_obj(pending.get("metadata") or {})
    hash_index[db_key(doc_type, art_id)] = new_hash

    rec = {
        "op": op,
        "id": art_id,
        "type_key": type_key,  # contract field (playbook/consumer guide)
        "type": type_key,      # legacy duplicate — keep for old readers
        "document_type": doc_type,
        "s3_key": live_key,
        "run_date": run_date,
        "approved_by": actor,
        "hash": new_hash,
    }
    store.append_jsonl(f"runs/{run_date}/approve/changed_ids-holds.jsonl", rec)
    return rec


def _finalize_holds(store: S3Storage, bucket: str, run_date: str, held_state: dict) -> None:
    """After the last hold is resolved: publish the holds batch and write _done."""
    holds_key = f"runs/{run_date}/approve/changed_ids-holds.jsonl"
    mode = held_state.get("mode") or _run_mode(store, run_date)

    approved_holds = _count_jsonl(store, holds_key)
    if approved_holds > 0:
        approved = _read_jsonl(store, holds_key)
        _write_audit(store, run_date, approved, actor="human", source="holds")
        _publish_handoff(store, bucket, run_date, mode, "holds", holds_key, approved_holds)

    _write_done_conditional(store, run_date)
    _log("INFO", "approve_complete", run_date=run_date,
         held_approved=approved_holds, status="all_resolved")


# ── Promotion / archival ──────────────────────────────────────────────────────────

def _promote(
    store: S3Storage,
    pending: dict,
    pending_key: str,
    live_key: str,
    type_key: str,
    art_id: str,
) -> None:
    """Archive the current live copy (if any), copy pending → live, delete pending."""
    if store.exists(live_key):
        archive_key = f"{ARCHIVE_PREFIX}/{type_key}/{art_id}/{_now_stamp()}.json"
        try:
            store.copy(live_key, archive_key)
        except Exception as e:
            # Promotion still proceeds, but the pre-overwrite copy is LOST —
            # this article cannot be rolled back from archive/ for this change.
            _log("WARN", "archive_before_overwrite_failed", type_key=type_key,
                 id=art_id, archive_key=archive_key, **exc_fields(e),
                 hint="promotion continued without a rollback copy; S3 bucket "
                      "versioning is the remaining safety net for this overwrite")
    store.copy(pending_key, live_key)
    store.delete(pending_key)


def _build_held_entry(
    store: S3Storage,
    type_key: str,
    art_id: str,
    doc_type: str,
    pending_key: str,
    live_key: str,
    pending: dict,
    risk: list[str],
    changed: list[str],
    op: str,
) -> dict:
    pending_body = (pending.get("content") or {}).get("body_text") or ""
    entry: dict = {
        "type_key": type_key,
        "id": art_id,
        "doc_type": doc_type,
        "pending_key": pending_key,
        "live_key": live_key,
        "risk": risk,
        "changed": changed,
        "op": op,
        "pending_chars": len(pending_body),
        "error_msg": (pending.get("content") or {}).get("bodyError") or "",
        "article_url": (pending.get("metadata") or {}).get("url") or "",
    }
    try:
        live_doc = store.get(live_key)
        live_body = (live_doc.get("content") or {}).get("body_text") or ""
        entry["live_chars"] = len(live_body)
        entry["live_excerpt"] = live_body[:200].replace("\n", " ")
    except KeyError:
        entry["live_chars"] = 0
        entry["live_excerpt"] = ""
    return entry


# ── Risk classification ────────────────────────────────────────────────────────────

def _is_held(risk: list[str]) -> bool:
    """HELD only for body-dropped / body-error. body-shrank is FYI-only."""
    return any(flag in MANUAL_FLAGS for flag in risk)


# ── Manifest / risk-index loading ───────────────────────────────────────────────────

def _run_types(store: S3Storage, run_date: str) -> list[str]:
    try:
        orch = store.get(f"lambda/state/{run_date}/orchestrator.json")
        types = orch.get("types")
        if isinstance(types, list) and types:
            return types
    except KeyError:
        pass
    return list(ALL_TYPES)


def _run_mode(store: S3Storage, run_date: str) -> str:
    try:
        orch = store.get(f"lambda/state/{run_date}/orchestrator.json")
        return orch.get("mode") or "incremental"
    except KeyError:
        return "incremental"


def _load_manifest_entries(store: S3Storage, run_date: str) -> list[dict]:
    """Concatenate every per-type manifest (v2 layout)."""
    entries: list[dict] = []
    for type_key in _run_types(store, run_date):
        try:
            raw = store.get_bytes(f"runs/{run_date}/manifest/{type_key}.jsonl").decode("utf-8")
        except KeyError:
            continue
        for line in raw.splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _load_risk_index(store: S3Storage, run_date: str) -> dict[str, dict]:
    """Build {type_key/art_id: change_record} from track/changes.jsonl."""
    out: dict[str, dict] = {}
    try:
        raw = store.get_bytes(f"runs/{run_date}/track/changes.jsonl").decode("utf-8")
    except KeyError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = f"{rec.get('type_key', '')}/{rec.get('id', '')}"
        out[key] = rec
    return out


def _load_approved_ids(store: S3Storage, run_date: str) -> set[str]:
    """Resume support: {type_key/art_id} already in the auto changed_ids.jsonl."""
    out: set[str] = set()
    for rec in _read_jsonl(store, f"runs/{run_date}/approve/changed_ids.jsonl"):
        out.add(f"{rec.get('type', '')}/{rec.get('id', '')}")
    return out


# ── Audit / changelog writers ───────────────────────────────────────────────────────

def _write_audit(store: S3Storage, run_date: str, records: list[dict], *, actor: str, source: str) -> None:
    """Append promoted records to the monthly audit + changelog trails."""
    month = run_date[:7]
    for rec in records:
        store.append_jsonl(f"audit/{month}/changed_ids.jsonl", rec)
        store.append_jsonl(f"audit/{month}/decisions.jsonl", {
            "id": rec.get("id"), "type": rec.get("type"), "op": "approved",
            "actor": rec.get("approved_by") or actor, "source": source,
            "run_date": run_date, "ts": _now_iso(),
        })
        if rec.get("op") == "changed":
            store.append_jsonl(f"changelogs/{month}/changes.jsonl", rec)


# ── SNS handoff ─────────────────────────────────────────────────────────────────────

def _publish_handoff(
    store: S3Storage,
    bucket: str,
    run_date: str,
    mode: str,
    batch: str,
    manifest_key: str,
    count: int,
) -> None:
    try:
        _sns.publish(
            TopicArn=_handoff_topic_arn(),
            Message=json.dumps({
                "schema": "f5kb.handoff.v2",
                "run_date": run_date,
                "mode": mode,
                "batch": batch,
                "article_count": count,
                "manifest_key": manifest_key,
                "bucket": bucket,
                "published_at": _now_iso(),
            }),
        )
        _log("INFO", "sns_published", run_date=run_date, batch=batch,
             article_count=count, manifest_key=manifest_key)
    except Exception as e:  # never block the pipeline on a publish failure
        _log("ERROR", "sns_published", run_date=run_date, batch=batch,
             error=str(e), manifest_key=manifest_key)


# ── _done sentinel ────────────────────────────────────────────────────────────────

def _write_done_conditional(store: S3Storage, run_date: str) -> None:
    done_key = f"runs/{run_date}/approve/_done"
    won = store.put_conditional(done_key, b"")
    if not won:
        _log("INFO", "conditional_put_lost_412", run_date=run_date, key=done_key)
    store.put(f"runs/{run_date}/status.json", {
        "run_date": run_date,
        "phase": "done",
        "updated_at": _now_iso(),
    })


# ── Two-phase Slack dedup (fix #17) ────────────────────────────────────────────────

def _send_slack_once(
    store: S3Storage,
    run_date: str,
    auto_approved: list[dict],
    held: list[dict],
) -> None:
    webhook = _get_slack_webhook()
    if not webhook:
        return

    attempt_key = f"lambda/state/{run_date}/slack_attempt.json"
    sent_key = f"lambda/state/{run_date}/slack_sent.json"

    won_attempt = store.put_conditional(
        attempt_key, json.dumps({"ts": _now_iso()}).encode("utf-8")
    )
    if not won_attempt:
        if store.exists(sent_key):
            return  # already delivered
        try:
            attempt = store.get(attempt_key)
        except KeyError:
            attempt = {"ts": ""}
        age = _age_seconds(attempt.get("ts") or "")
        if age < SLACK_ATTEMPT_STALE_S:
            _log("INFO", "slack_attempt_won", run_date=run_date, result="in_flight")
            return  # another invocation is mid-send
        # Stale attempt: the previous sender crashed — take over.
        store.put(attempt_key, {"ts": _now_iso(), "retry": True})
        _log("INFO", "slack_stale_retry", run_date=run_date, age_s=round(age))
    else:
        _log("INFO", "slack_attempt_won", run_date=run_date, result="won")

    _send_slack_message(webhook, run_date, auto_approved, held)
    store.put(sent_key, {"sent_at": _now_iso()})
    _log("INFO", "slack_sent", run_date=run_date, held=len(held),
         auto_approved=len(auto_approved))


def _send_slack_message(
    webhook: str,
    run_date: str,
    auto_approved: list[dict],
    held: list[dict],
) -> None:
    n_auto = len(auto_approved)
    n_held = len(held)

    if not held:
        text = (
            f"✅ cloud-red {run_date} — auto-approved "
            f"{n_auto} articles → live · P2 notified"
        )
        _post_slack(webhook, {"text": text})
        return

    header = (
        f"\U0001f6ab cloud-red {run_date} — {n_held} held for review\n"
        f"{n_auto} auto-approved and ALREADY handed to P2"
    )
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve All"},
                    "style": "primary",
                    "action_id": f"approve_all:{run_date}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject All"},
                    "style": "danger",
                    "action_id": f"reject_all:{run_date}",
                },
            ],
        },
        {"type": "divider"},
    ]

    visible = held[:SLACK_MAX_HELD_BLOCKS]
    overflow = n_held - len(visible)

    for h in visible:
        type_key = h.get("type_key") or ""
        art_id = h.get("id") or ""
        risk = h.get("risk") or []
        live_chars = h.get("live_chars") or 0
        pending_chars = h.get("pending_chars") or 0
        live_excerpt = h.get("live_excerpt") or ""
        error_msg = h.get("error_msg") or ""
        article_url = h.get("article_url") or ""

        if "body-dropped" in risk:
            diff_line = (
                f"Live ({live_chars:,} chars): _{live_excerpt[:160]}_\n"
                f"Pending (0 chars): [no body returned by F5]"
            )
        elif "body-error" in risk:
            diff_line = (
                f"Error: `{error_msg}`\n"
                f"Live ({live_chars:,} chars): _{live_excerpt[:160]}_\n"
                f"Pending: fetch failed"
            )
        else:
            diff_line = f"Live ({live_chars:,} chars) → Pending ({pending_chars:,} chars)"

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{art_id}* · {type_key} · `{', '.join(risk)}`\n{diff_line}",
            },
        })
        elements: list[dict] = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve"},
                "style": "primary",
                "action_id": f"approve:{run_date}:{type_key}:{art_id}",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Reject"},
                "style": "danger",
                "action_id": f"reject:{run_date}:{type_key}:{art_id}",
            },
        ]
        if article_url:
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "\U0001f517 View on my.f5.com"},
                "url": article_url,
                "action_id": f"view:{run_date}:{type_key}:{art_id}",
            })
        blocks.append({"type": "actions", "elements": elements})
        blocks.append({"type": "divider"})

    if overflow > 0:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"⚠️ *{overflow} more held article(s) not shown* — "
                    f"possible upstream outage. Use *Approve All* / *Reject All* above, "
                    f"or read `lambda/state/{run_date}/approve_held.json` for the full list."
                ),
            },
        })
        blocks.append({"type": "divider"})

    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "_Auto batch already live & handed to P2. Holds await your decision._",
        },
    })
    _post_slack(webhook, {"blocks": blocks})


def _post_slack(webhook: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310 — trusted SSM webhook URL
    except Exception as e:
        _log("WARN", "slack_webhook_failed",
             err_type=type(e).__name__, err_msg=str(e)[:300],
             hint="hold notification NOT delivered — nobody was pinged; the 24h "
                  "auto-escalation watchdog is the backstop. Check the SSM webhook "
                  "parameter and Slack app.")


def _post_response_url(response_url: str | None, text: str) -> None:
    """Post an ephemeral update back to Slack via the interaction response_url."""
    if not response_url:
        return
    data = json.dumps({"text": text, "replace_original": False}).encode("utf-8")
    req = urllib.request.Request(
        response_url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310
    except Exception as e:
        _log("WARN", "slack_response_url_failed",
             err_type=type(e).__name__, err_msg=str(e)[:300],
             hint="the decision WAS applied — only the ephemeral Slack reply failed")


# ── Self-reinvoke on timeout ────────────────────────────────────────────────────────

def _self_reinvoke(event: dict, context: object, run_date: str) -> dict:
    """Re-queue the original event. ReservedConcurrentExecutions=1 makes it wait
    behind any in-flight work automatically."""
    fn_name = getattr(context, "function_name", None) or os.environ.get(
        "AWS_LAMBDA_FUNCTION_NAME", ""
    )
    try:
        boto3.client("lambda").invoke(
            FunctionName=fn_name,
            InvocationType="Event",
            Payload=json.dumps(event).encode("utf-8"),
        )
    except Exception as e:
        _log("ERROR", "self_reinvoke_failed", run_date=run_date, **exc_fields(e),
             hint="approve stopped mid-pass and could NOT continue itself — "
                  "started.json remains, so invoke approve with action=resume "
                  "(or wait for the orchestrator sweep) to finish the run")
        return {"status": "reinvoke_failed", "run_date": run_date}
    _log("INFO", "self_reinvoked", run_date=run_date)
    return {"status": "self_reinvoked", "run_date": run_date}


# ── JSONL helpers ─────────────────────────────────────────────────────────────────

def _read_jsonl(store: S3Storage, key: str) -> list[dict]:
    try:
        raw = store.get_bytes(key).decode("utf-8")
    except KeyError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _count_jsonl(store: S3Storage, key: str) -> int:
    try:
        raw = store.get_bytes(key).decode("utf-8")
    except KeyError:
        return 0
    return sum(1 for ln in raw.splitlines() if ln.strip())


# ── Event parsing ─────────────────────────────────────────────────────────────────

def _run_date_from_event(event: dict) -> str:
    """Extract run_date from a direct payload or an S3/EventBridge event."""
    if event.get("run_date"):
        return str(event["run_date"])
    key = _s3_key_from_event(event)
    if key:
        parts = key.split("/")
        if len(parts) > 1 and parts[0] == "runs":
            return parts[1]
    raise ValueError("could not determine run_date from event")


def _s3_key_from_event(event: dict) -> str:
    if "Records" in event:
        try:
            return event["Records"][0]["s3"]["object"]["key"]
        except (KeyError, IndexError, TypeError):
            return ""
    detail = event.get("detail")
    if isinstance(detail, dict):
        obj = detail.get("object")
        if isinstance(obj, dict):
            return obj.get("key") or ""
    return ""
