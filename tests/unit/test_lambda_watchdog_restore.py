"""Tests for f5kb/handlers/watchdog.py and f5kb/handlers/restore.py with moto."""

from __future__ import annotations

import datetime
import json

import boto3
import pytest
from moto import mock_aws

from f5kb.storage.s3 import S3Storage

BUCKET = "test-f5kb"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET", BUCKET)
    monkeypatch.setenv("APPROVE_FUNCTION_NAME", "f5kb-approve-test")
    monkeypatch.delenv("OPS_TOPIC_ARN", raising=False)


@pytest.fixture
def aws(env):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3Storage(BUCKET)


def _iso_hours_ago(hours: float) -> str:
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return t.isoformat().replace("+00:00", "Z")


def _held_state(store: S3Storage, run_date: str, remaining: int, age_hours: float) -> None:
    store.put(f"lambda/state/{run_date}/approve_held.json", {
        "run_date": run_date,
        "remaining": remaining,
        "entries": [{"id": f"A{i}"} for i in range(remaining)],
        "updated_at": _iso_hours_ago(age_hours),
    })


@pytest.fixture
def escalations(monkeypatch):
    import f5kb.handlers.watchdog as mod
    calls: list[str] = []
    monkeypatch.setattr(mod, "_escalate", lambda run_date: calls.append(run_date))
    return calls


# ── Watchdog ─────────────────────────────────────────────────────────────────


def test_stale_hold_escalated(aws, escalations):
    _held_state(aws, "2026-06-28", remaining=2, age_hours=30)

    from f5kb.handlers.watchdog import handler
    result = handler({}, None)

    assert result["escalated"] == 1
    assert result["outstanding"] == 1
    assert escalations == ["2026-06-28"]


def test_fresh_hold_reported_not_escalated(aws, escalations):
    _held_state(aws, "2026-07-01", remaining=1, age_hours=2)

    from f5kb.handlers.watchdog import handler
    result = handler({}, None)

    assert result["outstanding"] == 1
    assert result["escalated"] == 0
    assert escalations == []


def test_resolved_holds_ignored(aws, escalations):
    _held_state(aws, "2026-07-01", remaining=0, age_hours=48)

    from f5kb.handlers.watchdog import handler
    result = handler({}, None)

    assert result == {"status": "done", "escalated": 0, "outstanding": 0}
    assert escalations == []


def test_multiple_runs_scanned(aws, escalations):
    _held_state(aws, "2026-06-27", remaining=1, age_hours=50)
    _held_state(aws, "2026-06-30", remaining=3, age_hours=1)

    from f5kb.handlers.watchdog import handler
    result = handler({}, None)

    assert result["outstanding"] == 2
    assert result["escalated"] == 1
    assert escalations == ["2026-06-27"]


# ── Restore ──────────────────────────────────────────────────────────────────


TODAY = datetime.date.today().isoformat()


def _restore_event(**overrides) -> dict:
    event = {
        "type_key": "Policy",
        "art_id": "K900",
        "archive_key": "archive/Policy/K900/020000Z.json",
        "actor": "devinp",
    }
    event.update(overrides)
    return event


def test_restore_refused_while_run_open(aws, monkeypatch):
    store = aws
    monkeypatch.setenv("HANDOFF_TOPIC_ARN", "arn:aws:sns:us-east-1:1:none")
    store.put(f"runs/{TODAY}/status.json", {"run_date": TODAY, "phase": "scrape"})
    store.put("archive/Policy/K900/020000Z.json", {"id": "K900", "metadata": {"v": 1}})

    from f5kb.handlers.restore import handler
    result = handler(_restore_event(), None)

    assert result["status"] == "refused"
    assert not store.exists("live/Policy/K900.json")


def test_restore_missing_archive_404(aws, monkeypatch):
    monkeypatch.setenv("HANDOFF_TOPIC_ARN", "arn:aws:sns:us-east-1:1:none")

    from f5kb.handlers.restore import handler
    result = handler(_restore_event(archive_key="archive/Policy/NOPE.json"), None)
    assert result["statusCode"] == 404


def test_restore_missing_fields_400(aws):
    from f5kb.handlers.restore import handler
    result = handler({"type_key": "Policy"}, None)
    assert result["statusCode"] == 400


def test_restore_full_flow(aws, monkeypatch):
    store = aws
    topic = boto3.client("sns", region_name="us-east-1").create_topic(Name="handoff")
    monkeypatch.setenv("HANDOFF_TOPIC_ARN", topic["TopicArn"])

    archived = {"id": "K900", "metadata": {"v": 1}, "content": {"body_text": "old good body"}}
    current = {"id": "K900", "metadata": {"v": 2}, "content": {"body_text": "bad"}}
    store.put("archive/Policy/K900/020000Z.json", archived)
    store.put("live/Policy/K900.json", current)
    # A closed run today must not block the restore.
    store.put(f"runs/{TODAY}/status.json", {"run_date": TODAY, "phase": "done"})

    from f5kb.handlers.restore import handler
    result = handler(_restore_event(), None)

    assert result["status"] == "restored"
    assert store.get("live/Policy/K900.json") == archived
    # Displaced live copy is archived before overwrite.
    assert result["displaced_to"]
    assert store.get(result["displaced_to"]) == current
    # Hash index reflects the restored metadata.
    from f5kb.track.hashing import sha256_obj
    index = store.load_hash_index("hash-index/current.json.gz")
    assert index["Policy K900"] == sha256_obj({"v": 1})
    # Audit + run manifest written.
    month = TODAY[:7]
    decisions = store.get_bytes(f"audit/{month}/decisions.jsonl").decode()
    assert json.loads(decisions.strip().splitlines()[-1])["op"] == "restored"
    assert store.exists(result["manifest_key"])
    assert result["sns_published"] is True


def test_restore_sns_failure_does_not_block(aws, monkeypatch):
    store = aws
    monkeypatch.setenv("HANDOFF_TOPIC_ARN",
                       "arn:aws:sns:us-east-1:123456789012:does-not-exist")
    store.put("archive/Policy/K901/020000Z.json", {"id": "K901", "metadata": {"v": 1}})

    from f5kb.handlers.restore import handler
    result = handler(_restore_event(art_id="K901",
                                    archive_key="archive/Policy/K901/020000Z.json"), None)

    assert result["status"] == "restored"
    assert result["sns_published"] is False
    assert store.exists("live/Policy/K901.json")
