"""Tests for f5kb/handlers/slack_ack.py — signature verification + action routing."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

import pytest

SECRET = "test-signing-secret"


@pytest.fixture
def signed(monkeypatch):
    """Force the module's cold-start secret cache to a known value and return a
    helper that builds a correctly signed API Gateway event."""
    import f5kb.handlers.slack_ack as mod
    monkeypatch.setattr(mod, "_SIGNING_SECRET", SECRET)
    monkeypatch.setattr(mod, "_SIGNING_SECRET_LOADED", True)

    def _event(payload: dict, *, ts: int | None = None, secret: str = SECRET) -> dict:
        body = urllib.parse.urlencode({"payload": json.dumps(payload)})
        ts = ts if ts is not None else int(time.time())
        sig = "v0=" + hmac.new(
            secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256
        ).hexdigest()
        return {
            "body": body,
            "headers": {
                "X-Slack-Request-Timestamp": str(ts),
                "X-Slack-Signature": sig,
            },
        }

    return _event


@pytest.fixture
def fake_lambda(monkeypatch):
    """Capture Approve async invokes made through boto3.client('lambda')."""
    import f5kb.handlers.slack_ack as mod
    monkeypatch.setenv("APPROVE_FUNCTION_NAME", "f5kb-approve-test")
    calls: list[dict] = []

    class _Client:
        def invoke(self, **kwargs):
            calls.append({**kwargs, "Payload": json.loads(kwargs["Payload"])})
            return {"StatusCode": 202}

    monkeypatch.setattr(mod.boto3, "client", lambda service: _Client())
    return calls


def _payload(action_id: str, user: str = "operator") -> dict:
    return {
        "actions": [{"action_id": action_id}],
        "user": {"username": user},
        "response_url": "https://hooks.slack.com/actions/T0/response",
    }


# ── Signature verification ────────────────────────────────────────────────────


def test_valid_signature_accepted(signed, fake_lambda):
    from f5kb.handlers.slack_ack import handler
    resp = handler(signed(_payload("approve:2026-07-01:Manual:K1")), None)
    assert resp["statusCode"] == 200


def test_wrong_secret_rejected(signed, fake_lambda):
    from f5kb.handlers.slack_ack import handler
    resp = handler(signed(_payload("approve:2026-07-01:Manual:K1"), secret="wrong"), None)
    assert resp["statusCode"] == 401
    assert fake_lambda == []


def test_stale_timestamp_rejected(signed, fake_lambda):
    from f5kb.handlers.slack_ack import handler
    stale = int(time.time()) - 600  # > 300 s replay window
    resp = handler(signed(_payload("approve:2026-07-01:Manual:K1"), ts=stale), None)
    assert resp["statusCode"] == 401


def test_missing_headers_rejected(signed, fake_lambda):
    from f5kb.handlers.slack_ack import handler
    event = signed(_payload("approve:2026-07-01:Manual:K1"))
    event["headers"] = {}
    assert handler(event, None)["statusCode"] == 401


# ── Action mapping ────────────────────────────────────────────────────────────


def test_approve_button_invokes_approve(signed, fake_lambda):
    from f5kb.handlers.slack_ack import handler
    handler(signed(_payload("approve:2026-07-01:Manual:K123")), None)

    assert len(fake_lambda) == 1
    call = fake_lambda[0]
    assert call["FunctionName"] == "f5kb-approve-test"
    assert call["InvocationType"] == "Event"
    assert call["Payload"] == {
        "action": "approve",
        "run_date": "2026-07-01",
        "type_key": "Manual",
        "id": "K123",
        "actor": "operator",
        "response_url": "https://hooks.slack.com/actions/T0/response",
    }


def test_reject_all_button(signed, fake_lambda):
    from f5kb.handlers.slack_ack import handler
    handler(signed(_payload("reject_all:2026-07-01")), None)
    assert fake_lambda[0]["Payload"]["action"] == "reject_all"
    assert fake_lambda[0]["Payload"]["run_date"] == "2026-07-01"


def test_view_button_acks_without_invoking(signed, fake_lambda):
    from f5kb.handlers.slack_ack import handler
    resp = handler(signed(_payload("view:2026-07-01:Manual:K1")), None)
    assert resp["statusCode"] == 200
    assert fake_lambda == []


def test_actor_falls_back_to_user_id(signed, fake_lambda):
    from f5kb.handlers.slack_ack import handler
    payload = _payload("approve:2026-07-01:Manual:K1")
    payload["user"] = {"id": "U777"}
    handler(signed(payload), None)
    assert fake_lambda[0]["Payload"]["actor"] == "U777"


# ── ack_sent metric log (regression: SlackAckLatencyMs filter) ────────────────


def test_ack_sent_logged_with_duration(signed, fake_lambda, capfd):
    from f5kb.handlers.slack_ack import handler
    handler(signed(_payload("approve:2026-07-01:Manual:K1")), None)

    err = capfd.readouterr().err
    line = next(json.loads(ln) for ln in err.splitlines() if '"ack_sent"' in ln)
    assert line["action"] == "ack_sent"
    assert isinstance(line["duration_ms"], (int, float))
    assert line["status"] == 200
