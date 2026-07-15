"""Tests for f5kb/handlers/orchestrator.py — cron entry point with moto mock_aws."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from f5kb.storage.s3 import S3Storage

RUN_DATE = "2026-07-01"  # a Wednesday → incremental by schedule
SUNDAY = "2026-07-05"
BUCKET = "test-f5kb"

TYPE_KEYS = "Policy,Compliance,Bug_Tracker"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET", BUCKET)
    monkeypatch.setenv("RUN_DATE", RUN_DATE)
    monkeypatch.setenv("TYPE_KEYS", TYPE_KEYS)
    monkeypatch.setenv("APPROVE_FUNCTION_NAME", "f5kb-approve-test")
    monkeypatch.delenv("OPS_TOPIC_ARN", raising=False)


@pytest.fixture
def aws(env, monkeypatch):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        sqs = boto3.client("sqs", region_name="us-east-1")
        queue_url = sqs.create_queue(QueueName="f5kb-dump-test")["QueueUrl"]
        monkeypatch.setenv("DUMP_QUEUE_URL", queue_url)
        yield S3Storage(BUCKET), sqs, queue_url


def _drain(sqs, queue_url) -> list[dict]:
    out: list[dict] = []
    while True:
        msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
        if not msgs:
            return out
        for m in msgs:
            out.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])


# ── Normal run ────────────────────────────────────────────────────────────────


def test_run_fans_out_one_message_per_type(aws):
    store, sqs, queue_url = aws
    from f5kb.handlers.orchestrator import handler
    result = handler({}, None)

    assert result["started"] is True
    assert result["types_queued"] == 3

    msgs = _drain(sqs, queue_url)
    assert {m["type_key"] for m in msgs} == {"Policy", "Compliance", "Bug_Tracker"}
    by_type = {m["type_key"]: m for m in msgs}
    assert by_type["Bug_Tracker"]["enrichable"] is True
    assert by_type["Policy"]["enrichable"] is False
    assert all(m["run_date"] == RUN_DATE for m in msgs)

    orch = store.get(f"lambda/state/{RUN_DATE}/orchestrator.json")
    assert orch["types"] == ["Policy", "Compliance", "Bug_Tracker"]
    assert orch["enrichable"] == ["Bug_Tracker"]

    status = store.get(f"runs/{RUN_DATE}/status.json")
    assert status["phase"] == "scrape"


def test_duplicate_cron_delivery_does_not_double_start(aws):
    store, sqs, queue_url = aws
    from f5kb.handlers.orchestrator import handler
    handler({}, None)
    _drain(sqs, queue_url)

    result = handler({}, None)
    assert result["started"] is False
    assert result["reason"] == "already_started"
    assert _drain(sqs, queue_url) == []  # no second fan-out


# ── Mode resolution ───────────────────────────────────────────────────────────


def test_mode_manual_override(aws):
    from f5kb.handlers.orchestrator import handler
    result = handler({"mode": "full"}, None)
    assert result["mode"] == "full"
    assert result["mode_source"] == "manual"


def test_mode_sunday_is_full(aws, monkeypatch):
    monkeypatch.setenv("RUN_DATE", SUNDAY)
    from f5kb.handlers.orchestrator import handler
    result = handler({}, None)
    assert result["mode"] == "full"
    assert result["mode_source"] == "schedule"


def test_mode_weekday_is_incremental(aws):
    from f5kb.handlers.orchestrator import handler
    result = handler({}, None)
    assert result["mode"] == "incremental"


# ── Fail-hard on missing type list (fix #13) ──────────────────────────────────


def test_missing_type_keys_fails_hard(aws, monkeypatch):
    monkeypatch.setenv("TYPE_KEYS", "")
    from f5kb.handlers.orchestrator import handler
    with pytest.raises(RuntimeError, match="TYPE_KEYS"):
        handler({}, None)


def test_type_keys_fall_back_to_s3_config(aws, monkeypatch):
    store, sqs, queue_url = aws
    monkeypatch.setenv("TYPE_KEYS", "")
    store.put("lambda/config/types.json", {"types": ["Policy", "Manual"]})

    from f5kb.handlers.orchestrator import handler
    result = handler({}, None)
    assert result["types_queued"] == 2
    assert {m["type_key"] for m in _drain(sqs, queue_url)} == {"Policy", "Manual"}


# ── Step-zero sweep ───────────────────────────────────────────────────────────


def test_sweep_marks_dead_mid_scrape_run_failed(aws):
    store, sqs, queue_url = aws
    prior = "2026-06-28"
    store.put(f"runs/{prior}/status.json", {"run_date": prior, "phase": "scrape"})
    store.put_marker(f"runs/{prior}/scrape/_done")  # scrape finished, track never ran

    from f5kb.handlers.orchestrator import handler
    result = handler({}, None)

    assert result["started"] is True  # today's run proceeds
    failed = store.get(f"runs/{prior}/status.json")
    assert failed["phase"] == "failed"
    assert failed["errors"]


def test_sweep_skips_closed_runs(aws):
    store, sqs, queue_url = aws
    prior = "2026-06-28"
    store.put(f"runs/{prior}/status.json", {"run_date": prior, "phase": "done"})
    store.put_marker(f"runs/{prior}/approve/_done")

    from f5kb.handlers.orchestrator import handler
    result = handler({}, None)

    assert result["started"] is True
    assert store.get(f"runs/{prior}/status.json")["phase"] == "done"  # untouched


def test_sweep_escalates_prior_run_with_holds(aws, monkeypatch):
    store, sqs, queue_url = aws
    prior = "2026-06-28"
    store.put(f"lambda/state/{prior}/approve_held.json", {
        "run_date": prior, "remaining": 2,
        "entries": [{"id": "A"}, {"id": "B"}],
    })
    store.put(f"runs/{prior}/status.json", {"run_date": prior, "phase": "approve"})

    import f5kb.handlers.orchestrator as orch_mod
    invoked: list[tuple[str, str]] = []
    monkeypatch.setattr(orch_mod, "_invoke_approve", lambda rd, action: invoked.append((rd, action)))
    monkeypatch.setattr(orch_mod, "_poll_for_approve_done", lambda store, rd: True)

    result = orch_mod.handler({}, None)
    assert invoked == [(prior, "auto_escalate")]
    assert result["started"] is True


def test_sweep_aborts_today_when_holds_cannot_close(aws, monkeypatch):
    store, sqs, queue_url = aws
    prior = "2026-06-28"
    store.put(f"lambda/state/{prior}/approve_held.json", {
        "run_date": prior, "remaining": 1, "entries": [{"id": "A"}],
    })
    store.put(f"runs/{prior}/status.json", {"run_date": prior, "phase": "approve"})

    import f5kb.handlers.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_invoke_approve", lambda rd, action: None)
    monkeypatch.setattr(orch_mod, "_poll_for_approve_done", lambda store, rd: False)

    with pytest.raises(RuntimeError, match="aborting"):
        orch_mod.handler({}, None)
    # Today's run never started.
    assert not store.exists(f"runs/{RUN_DATE}/status.json")
    assert _drain(sqs, queue_url) == []


def test_sweep_resumes_tracked_but_unapproved_run(aws, monkeypatch):
    store, sqs, queue_url = aws
    prior = "2026-06-28"
    store.put(f"runs/{prior}/status.json", {"run_date": prior, "phase": "approve"})
    store.put_marker(f"runs/{prior}/track/_done")

    import f5kb.handlers.orchestrator as orch_mod
    invoked: list[tuple[str, str]] = []
    monkeypatch.setattr(orch_mod, "_invoke_approve", lambda rd, action: invoked.append((rd, action)))
    monkeypatch.setattr(orch_mod, "_poll_for_approve_done", lambda store, rd: True)

    result = orch_mod.handler({}, None)
    assert invoked == [(prior, "resume")]
    assert result["started"] is True


# ── Action routing ────────────────────────────────────────────────────────────


def test_sweep_only_action(aws):
    from f5kb.handlers.orchestrator import handler
    result = handler({"action": "sweep_only"}, None)
    assert result == {"action": "sweep_only", "swept": True}


def test_auto_escalate_action(aws, monkeypatch):
    import f5kb.handlers.orchestrator as orch_mod
    invoked: list[tuple[str, str]] = []
    monkeypatch.setattr(orch_mod, "_invoke_approve", lambda rd, action: invoked.append((rd, action)))
    monkeypatch.setattr(orch_mod, "_poll_for_approve_done", lambda store, rd: True)

    result = orch_mod.handler({"action": "auto_escalate", "run_date": "2026-06-28"}, None)
    assert invoked == [("2026-06-28", "auto_escalate")]
    assert result["closed"] is True
