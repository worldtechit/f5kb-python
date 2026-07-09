"""Tests for f5kb/handlers/dump.py — Coveo keyset scraper Lambda with moto mock_aws.

The Coveo network layer is faked by patching fetch_coveo_config + CoveoClient in
the handler module; responses are scripted per-call like _ScriptedTransport.
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from f5kb.storage.s3 import S3Storage
from f5kb.track.hashing import sha256_obj

RUN_DATE = "2026-07-01"
BUCKET = "test-f5kb"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET", BUCKET)
    monkeypatch.setenv("PAGE_SIZE", "2")


@pytest.fixture
def aws(env, monkeypatch):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        sqs = boto3.client("sqs", region_name="us-east-1")
        queue_url = sqs.create_queue(QueueName="f5kb-dump-test")["QueueUrl"]
        monkeypatch.setenv("DUMP_QUEUE_URL", queue_url)
        yield S3Storage(BUCKET), sqs, queue_url


@pytest.fixture
def coveo(monkeypatch):
    """Patch the handler's Coveo layer; returns a list to fill with page dicts
    (or Exception instances to raise on that call)."""
    pages: list = []

    import f5kb.handlers.dump as dump_mod

    monkeypatch.setattr(dump_mod, "fetch_coveo_config", lambda: {"org": "test"})

    class _FakeCoveo:
        def __init__(self, config, client=None):
            self.calls: list[dict] = []

        def post(self, payload: dict) -> dict:
            _FAKE_CALLS.append(payload)
            if not pages:
                return {"totalCount": 0, "results": []}
            page = pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return page

    monkeypatch.setattr(dump_mod, "CoveoClient", _FakeCoveo)
    global _FAKE_CALLS
    _FAKE_CALLS = []
    return pages


_FAKE_CALLS: list[dict] = []


# ── Helpers ───────────────────────────────────────────────────────────────────


def _result(art_id: str, rowid: int, title: str = "", uri: str = "") -> dict:
    return {
        "title": title or f"Title {art_id}",
        "clickUri": uri or f"https://example.f5.com/{art_id}",
        "raw": {"f5_kb_id": art_id, "rowid": rowid, "f5_field": "x"},
    }


def _msg(type_key: str = "Policy", **overrides) -> dict:
    msg = {"run_date": RUN_DATE, "type_key": type_key, "mode": "full",
           "enrichable": type_key in {"Manual", "Release_Note",
                                      "Supplemental_Document", "Bug_Tracker"}}
    msg.update(overrides)
    return msg


def _sqs_event(msg: dict) -> dict:
    return {"Records": [{"body": json.dumps(msg)}]}


class _Ctx:
    def __init__(self, ms: int = 900_000) -> None:
        self._ms = ms

    def get_remaining_time_in_millis(self) -> int:
        return self._ms


# ── Envelope contract (regression: enrichers need link/title/documentType) ────


def test_envelope_carries_link_title_doctype(aws, coveo):
    store, _, _ = aws
    coveo.append({"totalCount": 1, "results": [
        _result("K001", 10, title="My article", uri="https://my.f5.com/article/K001"),
    ]})

    from f5kb.handlers.dump import handler
    result = handler(_sqs_event(_msg("Policy")), _Ctx())

    assert result["status"] == "done"
    envelope = store.get("pending/Policy/K001.json")
    assert envelope["link"] == "https://my.f5.com/article/K001"
    assert envelope["title"] == "My article"
    assert envelope["documentType"] == "Policy"
    assert envelope["type_key"] == "Policy"
    assert envelope["id"] == "K001"
    assert envelope["metadata_hash"] == sha256_obj(envelope["metadata"])
    assert envelope["metadata"]  # never {} — see wildcard regression below


# Regression: the default metadata selector must be the string "*", not ["*"].
# A list ["*"] matches no field name, which staged every article as
# {"metadata": {}, "content": {}} (hash 44136fa... = sha256 of empty object).
def test_default_config_keeps_all_fields_in_metadata(aws, coveo):
    store, _, _ = aws
    coveo.append({"totalCount": 1, "results": [_result("K005", 3)]})

    from f5kb.handlers.dump import handler
    handler(_sqs_event(_msg("Policy")), _Ctx())

    envelope = store.get("pending/Policy/K005.json")
    # With no lambda/config/types.json every flattened field lands in metadata.
    assert envelope["metadata"]["f5_kb_id"] == "K005"
    assert envelope["metadata"]["f5_field"] == "x"
    assert envelope["metadata"]["title"] == "Title K005"
    assert envelope["content"] == {}
    assert envelope["metadata_hash"] != sha256_obj({})


def test_s3_type_config_splits_content_and_documenttype(aws, coveo):
    """With lambda/config/types.json present, the Coveo filter uses the real
    documentType (spaces, not the underscored key) and the metadata/content
    field split follows the config — e.g. Policy's body sfdetails__c."""
    store, _, _ = aws
    store.put("lambda/config/types.json", {
        "Support_Solution": {
            "documentType": "Support Solution",
            "metadata": ["f5_kb_id", "f5_title"],
            "content": ["sfdetails__c"],
        },
    })
    r = _result("K006", 4)
    r["raw"]["f5_title"] = "How to fix it"
    r["raw"]["sfdetails__c"] = "<p>full article body</p>"
    coveo.append({"totalCount": 1, "results": [r]})

    from f5kb.handlers.dump import handler
    handler(_sqs_event(_msg("Support_Solution")), _Ctx())

    assert '@f5_document_type=="Support Solution"' in _FAKE_CALLS[0]["aq"]
    envelope = store.get("pending/Support_Solution/K006.json")
    assert envelope["documentType"] == "Support Solution"
    assert envelope["content"] == {"sfdetails__c": "<p>full article body</p>"}
    assert envelope["metadata"] == {"f5_kb_id": "K006", "f5_title": "How to fix it"}


def test_envelope_link_falls_back_to_clickableuri(aws, coveo):
    store, _, _ = aws
    r = _result("K002", 11)
    del r["clickUri"]
    r["raw"]["clickableuri"] = "https://cdn.f5.com/K002.html"
    coveo.append({"totalCount": 1, "results": [r]})

    from f5kb.handlers.dump import handler
    handler(_sqs_event(_msg("Policy")), _Ctx())

    assert store.get("pending/Policy/K002.json")["link"] == "https://cdn.f5.com/K002.html"


# ── Staging, manifest, completion markers ─────────────────────────────────────


def test_stages_articles_and_writes_manifest(aws, coveo):
    store, _, _ = aws
    coveo.append({"totalCount": 2, "results": [_result("K010", 1), _result("K011", 2)]})

    from f5kb.handlers.dump import handler
    result = handler(_sqs_event(_msg("Policy")), _Ctx())

    assert result["written"] == 2
    lines = [json.loads(ln) for ln in store.get_bytes(
        f"runs/{RUN_DATE}/manifest/Policy.jsonl").decode().splitlines()]
    assert [ln["id"] for ln in lines] == ["K010", "K011"]
    assert all(ln["op"] == "new" for ln in lines)
    assert store.exists(f"runs/{RUN_DATE}/dump/Policy/_done")
    index = store.get(f"runs/{RUN_DATE}/dump/Policy/_index.json")
    assert index["count_written"] == 2
    assert index["count_server"] == 2


def test_keyset_pagination_advances_rowid(aws, coveo):
    store, _, _ = aws
    # PAGE_SIZE=2: full first page → second page short → stop.
    coveo.append({"totalCount": 3, "results": [_result("K020", 5), _result("K021", 9)]})
    coveo.append({"totalCount": 3, "results": [_result("K022", 12)]})

    from f5kb.handlers.dump import handler
    result = handler(_sqs_event(_msg("Policy")), _Ctx())

    assert result["written"] == 3
    assert "@rowid>9" in _FAKE_CALLS[1]["aq"]


def test_skip_unchanged_via_hash_index(aws, coveo):
    store, _, _ = aws
    r = _result("K030", 1)

    # Precompute the metadata hash exactly as the handler will (default "*" split).
    from f5kb.config.types import normalize_type
    from f5kb.coveo.fields import flatten_fields_safe, split_entry
    cfg = normalize_type({"documentType": "Policy", "metadata": "*", "content": []})
    metadata = split_entry(flatten_fields_safe(r), cfg)["metadata"]
    store.save_hash_index({"Policy K030": sha256_obj(metadata)}, "hash-index/current.json.gz")

    coveo.append({"totalCount": 1, "results": [r]})

    from f5kb.handlers.dump import handler
    result = handler(_sqs_event(_msg("Policy")), _Ctx())

    assert result["written"] == 0
    assert not store.exists("pending/Policy/K030.json")
    # No manifest entry for skipped articles.
    assert not store.exists(f"runs/{RUN_DATE}/manifest/Policy.jsonl")


def test_changed_article_marked_changed(aws, coveo):
    store, _, _ = aws
    store.save_hash_index({"Policy K040": "stale-hash"}, "hash-index/current.json.gz")
    coveo.append({"totalCount": 1, "results": [_result("K040", 1)]})

    from f5kb.handlers.dump import handler
    handler(_sqs_event(_msg("Policy")), _Ctx())

    line = json.loads(store.get_bytes(f"runs/{RUN_DATE}/manifest/Policy.jsonl").decode().strip())
    assert line["op"] == "changed"


# ── Regression: page-fetch failure must NOT complete the type ─────────────────


def test_page_fetch_failure_saves_cursor_and_requeues(aws, coveo):
    store, sqs, queue_url = aws
    coveo.append({"totalCount": 3, "results": [_result("K050", 5), _result("K051", 9)]})
    coveo.append(RuntimeError("coveo 503"))

    from f5kb.handlers.dump import handler
    result = handler(_sqs_event(_msg("Policy")), _Ctx())

    # Fast self-requeue retry — NOT a raise (which would park the message
    # invisible for the whole 5400s visibility timeout before redelivery).
    assert result["status"] == "retry_scheduled"
    assert result["attempt"] == 1

    # The type must NOT be marked done.
    assert not store.exists(f"runs/{RUN_DATE}/dump/Policy/_done")
    cursor = store.get(f"lambda/state/{RUN_DATE}/dump-Policy.json")
    assert cursor["rowid_cursor"] == 9
    assert cursor["written"] == 2
    assert cursor["status"] == "in_progress"

    msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10,
                               AttributeNames=["All"]).get("Messages", [])
    # DelaySeconds hides the retry message; assert via queue attributes instead.
    attrs = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesDelayed"],
    )["Attributes"]
    total = (int(attrs["ApproximateNumberOfMessages"])
             + int(attrs["ApproximateNumberOfMessagesDelayed"]) + len(msgs))
    assert total == 1


def test_page_fetch_failure_exhausts_retries_then_raises(aws, coveo):
    store, _, _ = aws
    coveo.append(RuntimeError("coveo down"))

    from f5kb.handlers.dump import handler
    # attempt=3 with no progress this invocation → hand over to redrive/DLQ.
    with pytest.raises(RuntimeError, match="coveo down"):
        handler(_sqs_event(_msg("Policy", attempt=3)), _Ctx())

    assert not store.exists(f"runs/{RUN_DATE}/dump/Policy/_done")


def test_progress_resets_failure_streak(aws, coveo):
    store, _, _ = aws
    # One good page, then a failure — even at attempt=3 the streak resets to 0
    # because this invocation made progress.
    coveo.append({"totalCount": 4, "results": [_result("K055", 5), _result("K056", 9)]})
    coveo.append(RuntimeError("blip"))

    from f5kb.handlers.dump import handler
    result = handler(_sqs_event(_msg("Policy", attempt=3)), _Ctx())

    assert result["status"] == "retry_scheduled"
    assert result["attempt"] == 1  # streak restarted, not 4


def test_retry_resumes_from_cursor(aws, coveo):
    store, _, _ = aws
    store.put(f"lambda/state/{RUN_DATE}/dump-Policy.json", {
        "run_date": RUN_DATE, "type_key": "Policy",
        "rowid_cursor": 9, "written": 2, "count_server": 3,
        "status": "in_progress",
    })
    coveo.append({"totalCount": 3, "results": [_result("K052", 12)]})

    from f5kb.handlers.dump import handler
    result = handler(_sqs_event(_msg("Policy")), _Ctx())

    assert "@rowid>9" in _FAKE_CALLS[0]["aq"]
    assert result["written"] == 3  # 2 prior + 1 fresh
    assert store.exists(f"runs/{RUN_DATE}/dump/Policy/_done")
    assert not store.exists(f"lambda/state/{RUN_DATE}/dump-Policy.json")


# Regression: a resumed invocation's first page is filtered by @rowid>cursor,
# so its totalCount only covers the REMAINING articles. Overwriting the saved
# count_server shrank the displayed total on every resume (15,266/13,847).
def test_resume_keeps_original_count_server(aws, coveo):
    store, _, _ = aws
    store.put(f"lambda/state/{RUN_DATE}/dump-Policy.json", {
        "run_date": RUN_DATE, "type_key": "Policy",
        "rowid_cursor": 9, "written": 15_000, "count_server": 29_000,
        "status": "in_progress",
    })
    # Remaining-count page: Coveo reports only what's left past the cursor.
    coveo.append({"totalCount": 14_000, "results": [_result("K053", 12)]})

    from f5kb.handlers.dump import handler
    result = handler(_sqs_event(_msg("Policy")), _Ctx())

    assert result["written"] == 15_001
    index = store.get(f"runs/{RUN_DATE}/dump/Policy/_index.json")
    assert index["count_server"] == 29_000  # NOT the remaining 14,000


# ── Timeout → cursor + self re-queue ──────────────────────────────────────────


def test_timeout_requeues_self(aws, coveo):
    store, sqs, queue_url = aws
    coveo.append({"totalCount": 2, "results": [_result("K060", 1), _result("K061", 2)]})

    from f5kb.handlers.dump import handler
    result = handler(_sqs_event(_msg("Policy")), _Ctx(ms=1_000))

    assert result["status"] == "resumed"
    assert store.exists(f"lambda/state/{RUN_DATE}/dump-Policy.json")
    msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
    bodies = [json.loads(m["Body"]) for m in msgs]
    assert any(b["type_key"] == "Policy" and b["run_date"] == RUN_DATE for b in bodies)
    assert not store.exists(f"runs/{RUN_DATE}/dump/Policy/_done")


# ── Terminal gate (non-enrichable path) ───────────────────────────────────────


def test_non_enrichable_type_fires_gate_when_last(aws, coveo):
    store, _, _ = aws
    store.put(f"lambda/state/{RUN_DATE}/orchestrator.json", {
        "run_date": RUN_DATE, "types": ["Policy", "Compliance"], "enrichable": [],
    })
    store.put_marker(f"runs/{RUN_DATE}/dump/Compliance/_done")
    coveo.append({"totalCount": 1, "results": [_result("K070", 1)]})

    from f5kb.handlers.dump import handler
    handler(_sqs_event(_msg("Policy")), _Ctx())

    assert store.exists(f"runs/{RUN_DATE}/scrape/_done")
    assert store.get(f"runs/{RUN_DATE}/status.json")["phase"] == "track"


def test_enrichable_type_does_not_fire_gate(aws, coveo):
    store, _, _ = aws
    store.put(f"lambda/state/{RUN_DATE}/orchestrator.json", {
        "run_date": RUN_DATE, "types": ["Manual"], "enrichable": ["Manual"],
    })
    coveo.append({"totalCount": 1, "results": [_result("K080", 1)]})

    from f5kb.handlers.dump import handler
    handler(_sqs_event(_msg("Manual")), _Ctx())

    assert store.exists(f"runs/{RUN_DATE}/dump/Manual/_done")
    # Enrich owns the terminal marker for enrichable types.
    assert not store.exists(f"runs/{RUN_DATE}/scrape/_done")


# ── Incremental mode ──────────────────────────────────────────────────────────


def test_incremental_mode_adds_date_window(aws, coveo):
    store, _, _ = aws
    coveo.append({"totalCount": 0, "results": []})

    from f5kb.handlers.dump import handler
    handler(_sqs_event(_msg("Policy", mode="incremental")), _Ctx())

    assert "@date>=" in _FAKE_CALLS[0]["aq"]
