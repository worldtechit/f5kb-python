"""Tests for lambda/approve.py — full Gate 1 flow with moto mock_aws."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from f5kb.storage.s3 import S3Storage

RUN_DATE = "2026-07-01"
BUCKET = "test-f5kb"


MONTH = RUN_DATE[:7]  # "2026-07"
AUDIT_CHANGED = f"audit/{MONTH}/changed_ids.jsonl"
AUDIT_DECISIONS = f"audit/{MONTH}/decisions.jsonl"


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("BUCKET", BUCKET)
    monkeypatch.setenv("HANDOFF_TOPIC_ARN", "")
    # No SLACK_WEBHOOK_PARAM → Slack notification is skipped in tests.


@pytest.fixture
def s3(aws_env):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3Storage(BUCKET)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _track_done_event(run_date: str) -> dict:
    return {"Records": [{"s3": {"object": {"key": f"runs/{run_date}/track/_done"}}}]}


def _slack_event(decision: str, run_date: str, type_key: str, art_id: str, actor: str = "operator") -> dict:
    # v2.1: Slack callbacks arrive as direct invokes (routed by SlackAck), NOT as
    # API Gateway events. Approve routes on the presence of an "action" field.
    return {
        "action": decision,
        "run_date": run_date,
        "type_key": type_key,
        "id": art_id,
        "actor": actor,
    }


def _make_article(type_key: str, art_id: str, *, has_body: bool = True, body_error: str = "") -> dict:
    content: dict = {}
    if has_body:
        content["bodyText"] = "hello world " * 100
    if body_error:
        content["bodyError"] = body_error
    return {
        "id": art_id,
        "documentType": type_key,
        "title": f"Title {art_id}",
        "link": f"https://example.com/{art_id}",
        "metadata": {"version": 1},
        "content": content,
    }


def _stage(s3: S3Storage, type_key: str, art_id: str, article: dict) -> str:
    key = f"pending/{type_key}/{art_id}.json"
    s3.put(key, article)
    return key


def _register_type(s3: S3Storage, run_date: str, type_key: str) -> None:
    """Ensure the orchestrator state lists type_key so approve's per-type manifest
    loader (which iterates orchestrator.json 'types') reads its manifest file."""
    key = f"lambda/state/{run_date}/orchestrator.json"
    try:
        orch = s3.get(key)
    except KeyError:
        orch = {"run_date": run_date, "mode": "incremental", "types": []}
    types = orch.get("types") or []
    if type_key not in types:
        types.append(type_key)
    orch["types"] = types
    orch["total_types"] = len(types)
    s3.put(key, orch)


def _add_manifest_entry(s3: S3Storage, run_date: str, type_key: str, art_id: str,
                         doc_type: str | None = None) -> None:
    _register_type(s3, run_date, type_key)
    s3.append_jsonl(f"runs/{run_date}/manifest/{type_key}.jsonl", {
        "type_key": type_key,
        "id": art_id,
        "document_type": doc_type or type_key,
        "pending_key": f"pending/{type_key}/{art_id}.json",
        "captured_at": "2026-07-01T02:00:00Z",
    })


def _add_changes_entry(s3: S3Storage, run_date: str, type_key: str, art_id: str,
                        risk: list[str], op: str = "new") -> None:
    s3.append_jsonl(f"runs/{run_date}/track/changes.jsonl", {
        "id": art_id,
        "type_key": type_key,
        "document_type": type_key,
        "op": op,
        "risk": risk,
        "changed": ["content"] if op == "changed" else [],
        "run_date": run_date,
    })


# ── Auto-approve: clean articles ──────────────────────────────────────────────

def test_auto_approve_clean_article(s3):
    with mock_aws():
        article = _make_article("Solution", "K001")
        _stage(s3, "Solution", "K001", article)
        _add_manifest_entry(s3, RUN_DATE, "Solution", "K001")
        _add_changes_entry(s3, RUN_DATE, "Solution", "K001", risk=[], op="new")

        from f5kb.handlers.approve import handler
        result = handler(_track_done_event(RUN_DATE), None)

        assert result["status"] == "done"
        assert result["auto_approved"] == 1
        assert result["held"] == 0
        assert s3.exists("live/Solution/K001.json")
        assert not s3.exists("pending/Solution/K001.json")
        assert s3.exists(f"runs/{RUN_DATE}/approve/_done")

        raw = s3.get_bytes(AUDIT_CHANGED).decode()
        line = json.loads(raw.strip())
        assert line["id"] == "K001"
        assert line["op"] == "new"
        assert line["approved_by"] == "auto"


def test_auto_approve_body_shrank(s3):
    with mock_aws():
        article = _make_article("Manual", "K002")
        _stage(s3, "Manual", "K002", article)
        _add_manifest_entry(s3, RUN_DATE, "Manual", "K002")
        _add_changes_entry(s3, RUN_DATE, "Manual", "K002", risk=["body-shrank-65%"])

        from f5kb.handlers.approve import handler
        result = handler(_track_done_event(RUN_DATE), None)

        assert result["status"] == "done"
        assert result["auto_approved"] == 1
        assert s3.exists("live/Manual/K002.json")
        assert s3.exists(f"runs/{RUN_DATE}/approve/_done")


# ── Hold: risky articles ──────────────────────────────────────────────────────

def test_hold_body_dropped(s3):
    with mock_aws():
        article = _make_article("Bug_Tracker", "BUG-1", has_body=False)
        _stage(s3, "Bug_Tracker", "BUG-1", article)
        _add_manifest_entry(s3, RUN_DATE, "Bug_Tracker", "BUG-1")
        _add_changes_entry(s3, RUN_DATE, "Bug_Tracker", "BUG-1", risk=["body-dropped"])

        from f5kb.handlers.approve import handler
        result = handler(_track_done_event(RUN_DATE), None)

        assert result["status"] == "awaiting_review"
        assert result["held"] == 1
        assert result["auto_approved"] == 0
        assert s3.exists("pending/Bug_Tracker/BUG-1.json")
        assert not s3.exists("live/Bug_Tracker/BUG-1.json")
        assert not s3.exists(f"runs/{RUN_DATE}/approve/_done")
        held_state = s3.get(f"lambda/state/{RUN_DATE}/approve_held.json")
        assert held_state["remaining"] == 1


def test_hold_body_error(s3):
    with mock_aws():
        article = _make_article("Manual", "K003", body_error="timeout")
        _stage(s3, "Manual", "K003", article)
        _add_manifest_entry(s3, RUN_DATE, "Manual", "K003")
        _add_changes_entry(s3, RUN_DATE, "Manual", "K003", risk=["body-error"])

        from f5kb.handlers.approve import handler
        result = handler(_track_done_event(RUN_DATE), None)

        assert result["status"] == "awaiting_review"
        assert result["held"] == 1
        assert not s3.exists(f"runs/{RUN_DATE}/approve/_done")


# ── Mixed: auto + held ────────────────────────────────────────────────────────

def test_mixed_auto_and_held(s3):
    with mock_aws():
        for art_id, tk, risk in [
            ("K001", "Solution", []),
            ("K002", "Solution", []),
            ("BUG-1", "Bug_Tracker", ["body-dropped"]),
        ]:
            article = _make_article(tk, art_id, has_body=not risk)
            _stage(s3, tk, art_id, article)
            _add_manifest_entry(s3, RUN_DATE, tk, art_id)
            _add_changes_entry(s3, RUN_DATE, tk, art_id, risk=risk, op="new")

        from f5kb.handlers.approve import handler
        result = handler(_track_done_event(RUN_DATE), None)

        assert result["auto_approved"] == 2
        assert result["held"] == 1
        assert result["status"] == "awaiting_review"
        assert not s3.exists(f"runs/{RUN_DATE}/approve/_done")


# ── Slack callback: approve ───────────────────────────────────────────────────

def test_slack_approve_promotes_and_writes_done(s3):
    with mock_aws():
        article = _make_article("Bug_Tracker", "BUG-2", has_body=False)
        _stage(s3, "Bug_Tracker", "BUG-2", article)
        s3.put(f"lambda/state/{RUN_DATE}/approve_held.json", {
            "run_date": RUN_DATE,
            "remaining": 1,
            "entries": [{"type_key": "Bug_Tracker", "id": "BUG-2"}],
        })

        from f5kb.handlers.approve import handler
        result = handler(_slack_event("approve", RUN_DATE, "Bug_Tracker", "BUG-2"), None)

        assert result["status"] == "all_resolved"
        assert result["resolved"] == 1
        assert s3.exists("live/Bug_Tracker/BUG-2.json")
        assert not s3.exists("pending/Bug_Tracker/BUG-2.json")
        assert s3.exists(f"runs/{RUN_DATE}/approve/_done")

        # Human-approved holds are written to the run-scoped holds manifest.
        raw = s3.get_bytes(f"runs/{RUN_DATE}/approve/changed_ids-holds.jsonl").decode()
        line = json.loads(raw.strip())
        assert line["approved_by"] == "operator"
        assert line["type"] == "Bug_Tracker"


def test_slack_approve_actor_recorded(s3):
    with mock_aws():
        article = _make_article("Solution", "K010")
        _stage(s3, "Solution", "K010", article)
        s3.put(f"lambda/state/{RUN_DATE}/approve_held.json", {
            "run_date": RUN_DATE,
            "remaining": 1,
            "entries": [{"type_key": "Solution", "id": "K010"}],
        })

        from f5kb.handlers.approve import handler
        handler(_slack_event("approve", RUN_DATE, "Solution", "K010", actor="jsmith"), None)

        raw = s3.get_bytes(f"runs/{RUN_DATE}/approve/changed_ids-holds.jsonl").decode()
        assert json.loads(raw.strip())["approved_by"] == "jsmith"


# ── Slack callback: reject ────────────────────────────────────────────────────

def test_slack_reject_deletes_pending(s3):
    with mock_aws():
        article = _make_article("Bug_Tracker", "BUG-3", has_body=False)
        _stage(s3, "Bug_Tracker", "BUG-3", article)
        s3.put(f"lambda/state/{RUN_DATE}/approve_held.json", {
            "run_date": RUN_DATE,
            "remaining": 1,
            "entries": [{"type_key": "Bug_Tracker", "id": "BUG-3"}],
        })

        from f5kb.handlers.approve import handler
        result = handler(_slack_event("reject", RUN_DATE, "Bug_Tracker", "BUG-3"), None)

        assert result["status"] == "all_resolved"
        assert not s3.exists("pending/Bug_Tracker/BUG-3.json")
        assert not s3.exists("live/Bug_Tracker/BUG-3.json")
        assert s3.exists(f"runs/{RUN_DATE}/approve/_done")

        raw = s3.get_bytes(AUDIT_DECISIONS).decode()
        line = json.loads(raw.strip())
        assert line["op"] == "rejected"
        assert line["id"] == "BUG-3"


def test_slack_reject_does_not_write_changed_ids(s3):
    with mock_aws():
        article = _make_article("Bug_Tracker", "BUG-4", has_body=False)
        _stage(s3, "Bug_Tracker", "BUG-4", article)
        s3.put(f"lambda/state/{RUN_DATE}/approve_held.json", {
            "run_date": RUN_DATE,
            "remaining": 1,
            "entries": [{"type_key": "Bug_Tracker", "id": "BUG-4"}],
        })

        from f5kb.handlers.approve import handler
        handler(_slack_event("reject", RUN_DATE, "Bug_Tracker", "BUG-4"), None)

        # A rejected hold must never appear in the human-approved holds manifest.
        assert not s3.exists(f"runs/{RUN_DATE}/approve/changed_ids-holds.jsonl")


# ── Hash index ────────────────────────────────────────────────────────────────

def test_hash_index_updated_on_auto_approve(s3):
    with mock_aws():
        article = _make_article("Solution", "K100")
        _stage(s3, "Solution", "K100", article)
        _add_manifest_entry(s3, RUN_DATE, "Solution", "K100")
        _add_changes_entry(s3, RUN_DATE, "Solution", "K100", risk=[], op="new")

        from f5kb.handlers.approve import handler
        handler(_track_done_event(RUN_DATE), None)

        index = s3.load_hash_index("hash-index/current.json.gz")
        assert "Solution K100" in index  # db_key format: "<doc_type> <id>"


def test_hash_index_updated_on_slack_approve(s3):
    with mock_aws():
        article = _make_article("Solution", "K200")
        _stage(s3, "Solution", "K200", article)
        s3.put(f"lambda/state/{RUN_DATE}/approve_held.json", {
            "run_date": RUN_DATE,
            "remaining": 1,
            "entries": [{"type_key": "Solution", "id": "K200"}],
        })

        from f5kb.handlers.approve import handler
        handler(_slack_event("approve", RUN_DATE, "Solution", "K200"), None)

        index = s3.load_hash_index("hash-index/current.json.gz")
        assert "Solution K200" in index


# ── Empty manifest ────────────────────────────────────────────────────────────

def test_empty_manifest_writes_done_immediately(s3):
    with mock_aws():
        # No manifest entries → approve/_done should fire immediately
        from f5kb.handlers.approve import handler
        result = handler(_track_done_event(RUN_DATE), None)

        assert result["status"] == "done"
        assert result["auto_approved"] == 0
        assert result["held"] == 0
        assert s3.exists(f"runs/{RUN_DATE}/approve/_done")


# ── Handoff contract: manifest lines carry type_key (consumer guide/playbook) ─

def test_manifest_lines_carry_type_key_contract_field(s3):
    with mock_aws():
        article = _make_article("Solution", "K300")
        _stage(s3, "Solution", "K300", article)
        _add_manifest_entry(s3, RUN_DATE, "Solution", "K300")
        _add_changes_entry(s3, RUN_DATE, "Solution", "K300", risk=[], op="new")

        from f5kb.handlers.approve import handler
        handler(_track_done_event(RUN_DATE), None)

        raw = s3.get_bytes(f"runs/{RUN_DATE}/approve/changed_ids.jsonl").decode()
        line = json.loads(raw.strip())
        # The documented contract field AND the legacy duplicate must both exist.
        assert line["type_key"] == "Solution"
        assert line["type"] == "Solution"
        assert line["s3_key"] == "live/Solution/K300.json"


def test_holds_manifest_lines_carry_type_key(s3):
    with mock_aws():
        article = _make_article("Manual", "K301")
        _stage(s3, "Manual", "K301", article)
        s3.put(f"lambda/state/{RUN_DATE}/approve_held.json", {
            "run_date": RUN_DATE,
            "remaining": 1,
            "entries": [{"type_key": "Manual", "id": "K301"}],
        })

        from f5kb.handlers.approve import handler
        handler(_slack_event("approve", RUN_DATE, "Manual", "K301"), None)

        raw = s3.get_bytes(f"runs/{RUN_DATE}/approve/changed_ids-holds.jsonl").decode()
        line = json.loads(raw.strip())
        assert line["type_key"] == "Manual"
        assert line["type"] == "Manual"


# ── Gate rule: body-shrank NEVER holds, regardless of magnitude ───────────────

def test_extreme_shrink_auto_approves(s3):
    with mock_aws():
        article = _make_article("Manual", "K400")
        _stage(s3, "Manual", "K400", article)
        _add_manifest_entry(s3, RUN_DATE, "Manual", "K400")
        _add_changes_entry(s3, RUN_DATE, "Manual", "K400", risk=["body-shrank-97%"])

        from f5kb.handlers.approve import handler
        result = handler(_track_done_event(RUN_DATE), None)

        assert result["status"] == "done"
        assert result["auto_approved"] == 1
        assert result["held"] == 0
        assert s3.exists("live/Manual/K400.json")
        assert s3.exists(f"runs/{RUN_DATE}/approve/_done")
