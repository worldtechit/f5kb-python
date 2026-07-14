"""SlackAck Lambda — API Gateway entry point for Slack interactive callbacks.

Slack requires a response within 3 seconds. This Lambda does the minimum:
  1. Verify the request signature (X-Slack-Signature / X-Slack-Request-Timestamp)
     against the signing secret fetched once per cold start from SSM.
  2. Parse the interactive payload, map the clicked action_id to an Approve action.
  3. Async-invoke the Approve Lambda (InvocationType="Event") — NOT inline — so the
     slow promotion work never blocks the Slack 3 s ACK.
  4. Return 200 immediately with an ephemeral acknowledgement.

action_id formats (produced by Approve's Slack message):
  approve:{run_date}:{type_key}:{art_id}
  reject:{run_date}:{type_key}:{art_id}
  approve_all:{run_date}
  reject_all:{run_date}
  view:...                        (link button — no server action)
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse

import boto3

LAMBDA_NAME = "f5kb-slack-ack"

# Reject requests whose timestamp is older than this (Slack replay-attack guard).
SIGNATURE_MAX_SKEW_S = 300

_SIGNING_SECRET: str | None = None
_SIGNING_SECRET_LOADED = False


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _log(level: str, action: str, **fields: object) -> None:
    rec: dict[str, object] = {
        "ts": _now_iso(), "level": level, "lambda": LAMBDA_NAME, "action": action,
    }
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec), file=sys.stderr)


def _get_signing_secret() -> str | None:
    global _SIGNING_SECRET, _SIGNING_SECRET_LOADED
    if _SIGNING_SECRET_LOADED:
        return _SIGNING_SECRET
    _SIGNING_SECRET_LOADED = True
    param = os.environ.get("SLACK_SIGNING_SECRET_PARAM", "")
    if param:
        try:
            _SIGNING_SECRET = boto3.client("ssm").get_parameter(
                Name=param, WithDecryption=True
            )["Parameter"]["Value"] or None
        except Exception as e:
            _log("ERROR", "signing_secret_load_failed",
                 err_type=type(e).__name__, err_msg=str(e)[:300],
                 hint="EVERY Slack callback will be rejected with bad_signature "
                      "until the SSM parameter loads — check SLACK_SIGNING_SECRET_PARAM")
            _SIGNING_SECRET = None
    return _SIGNING_SECRET


def _raw_body(event: dict) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def _headers_lower(event: dict) -> dict[str, str]:
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _verify_signature(event: dict, raw_body: str) -> bool:
    secret = _get_signing_secret()
    if not secret:
        _log("ERROR", "signature_verify", result="no_secret")
        return False
    headers = _headers_lower(event)
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts or not sig:
        return False
    try:
        skew = abs(int(datetime.datetime.now(datetime.timezone.utc).timestamp()) - int(ts))
    except ValueError:
        return False
    if skew > SIGNATURE_MAX_SKEW_S:
        _log("WARN", "signature_verify", result="stale", skew=skew)
        return False
    basestring = f"v0:{ts}:{raw_body}".encode("utf-8")
    computed = "v0=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, sig)


def _parse_payload(raw_body: str) -> dict:
    """Slack sends application/x-www-form-urlencoded with a single 'payload' field."""
    parsed = urllib.parse.parse_qs(raw_body)
    payloads = parsed.get("payload")
    if not payloads:
        return {}
    return json.loads(payloads[0])


def _action_to_invoke(payload: dict) -> dict | None:
    """Map the clicked Slack action_id to an Approve direct-invoke event."""
    actions = payload.get("actions") or []
    if not actions:
        return None
    action_id = actions[0].get("action_id") or ""
    response_url = payload.get("response_url")
    actor = (payload.get("user") or {}).get("username") or (payload.get("user") or {}).get("id") or "slack"

    parts = action_id.split(":")
    verb = parts[0] if parts else ""

    if verb in ("approve", "reject") and len(parts) >= 4:
        return {
            "action": verb,
            "run_date": parts[1],
            "type_key": parts[2],
            "id": parts[3],
            "actor": actor,
            "response_url": response_url,
        }
    if verb in ("approve_all", "reject_all") and len(parts) >= 2:
        return {
            "action": verb,
            "run_date": parts[1],
            "actor": actor,
            "response_url": response_url,
        }
    return None  # e.g. "view" link buttons carry no server action


def _resp(status: int, text: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"text": text, "replace_original": False}),
    }


def _ack(status: int, text: str, started: float) -> dict:
    """Build the response and emit ack_sent — the SlackAckLatencyMs metric
    filter in template.yaml extracts $.duration_ms from this log line."""
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    _log("INFO", "ack_sent", status=status, duration_ms=duration_ms)
    return _resp(status, text)


def handler(event: dict, context: object) -> dict:
    started = time.monotonic()
    raw_body = _raw_body(event)

    if not _verify_signature(event, raw_body):
        _log("WARN", "callback_rejected", reason="bad_signature")
        return _ack(401, "signature verification failed", started)

    try:
        payload = _parse_payload(raw_body)
    except (ValueError, json.JSONDecodeError) as e:
        _log("WARN", "callback_rejected", reason="bad_payload", error=str(e))
        return _ack(400, "could not parse payload", started)

    invoke_event = _action_to_invoke(payload)
    if invoke_event is None:
        # Link button / unknown action — ACK without doing anything.
        return _ack(200, "ok", started)

    fn_name = os.environ["APPROVE_FUNCTION_NAME"]
    try:
        boto3.client("lambda").invoke(
            FunctionName=fn_name,
            InvocationType="Event",  # async — return to Slack immediately
            Payload=json.dumps(invoke_event).encode("utf-8"),
        )
        _log("INFO", "approve_invoked", decision=invoke_event["action"],
             run_date=invoke_event.get("run_date"), art_id=invoke_event.get("id"))
    except Exception as e:
        _log("ERROR", "approve_invoke_failed",
             err_type=type(e).__name__, err_msg=str(e)[:300],
             hint="the Slack decision was ACKed but NOT applied — the user thinks "
                  "it worked; re-apply from the console Review page or re-click. "
                  "Check IAM lambda:InvokeFunction and APPROVE_FUNCTION_NAME.")
        return _ack(200, "received (processing delayed)", started)

    return _ack(200, "Working on it…", started)
