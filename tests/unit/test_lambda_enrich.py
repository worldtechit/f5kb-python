"""Tests for f5kb/handlers/enrich.py — SQS-driven enrichment with moto mock_aws.

HTTP is mocked by patching httpx.Client inside the handler module with a
MockTransport-backed factory, so the real enrichers run offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import httpx
import pytest
from moto import mock_aws

from f5kb.storage.s3 import S3Storage

RUN_DATE = "2026-07-01"
BUCKET = "test-f5kb"

BUG_HTML = (Path(__file__).parent.parent / "fixtures" / "pages" / "bug_standard.html").read_text(
    encoding="utf-8"
)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET", BUCKET)
    monkeypatch.delenv("GITHUB_TOKEN_PARAM", raising=False)


@pytest.fixture
def aws(env, monkeypatch):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        sqs = boto3.client("sqs", region_name="us-east-1")
        queue_url = sqs.create_queue(QueueName="f5kb-enrich-test")["QueueUrl"]
        monkeypatch.setenv("ENRICH_QUEUE_URL", queue_url)
        yield S3Storage(BUCKET), sqs, queue_url


@pytest.fixture
def mock_http(monkeypatch):
    """Install an httpx.MockTransport route inside the enrich handler module."""

    def _install(route_fn):
        real_client = httpx.Client

        def fake_client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(route_fn), follow_redirects=True)

        monkeypatch.setattr(httpx, "Client", fake_client)

    return _install


# ── Helpers ───────────────────────────────────────────────────────────────────


def _sqs_event(msg: dict) -> dict:
    return {"Records": [{"body": json.dumps(msg)}]}


def _eb_message(run_date: str, type_key: str) -> dict:
    return {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"object": {"key": f"runs/{run_date}/dump/{type_key}/_done"}},
    }


def _put_orchestrator(store: S3Storage, types: list[str], enrichable: list[str]) -> None:
    store.put(f"lambda/state/{RUN_DATE}/orchestrator.json", {
        "run_date": RUN_DATE,
        "types": types,
        "enrichable": enrichable,
    })


def _stage(store: S3Storage, type_key: str, art_id: str, article: dict) -> None:
    store.put(f"pending/{type_key}/{art_id}.json", article)
    store.append_jsonl(f"runs/{RUN_DATE}/manifest/{type_key}.jsonl", {
        "op": "new",
        "id": art_id,
        "type_key": type_key,
        "s3_key": f"live/{type_key}/{art_id}.json",
        "run_date": RUN_DATE,
    })


def _bug_article(art_id: str, **overrides) -> dict:
    article = {
        "run_date": RUN_DATE,
        "type_key": "Bug_Tracker",
        "id": art_id,
        "documentType": "Bug_Tracker",
        "title": f"Bug {art_id}",
        "link": f"https://cdn.f5.com/product/bugtracker/ID{art_id}.html",
        "metadata": {"f5_bug_id": art_id},
        "content": {},
    }
    article.update(overrides)
    return article


class _Ctx:
    """Fake Lambda context with a fixed remaining-time budget."""

    def __init__(self, ms: int = 900_000) -> None:
        self._ms = ms

    def get_remaining_time_in_millis(self) -> int:
        return self._ms


# ── Message parsing ───────────────────────────────────────────────────────────


def test_parse_message_eventbridge_shape():
    from f5kb.handlers.enrich import _parse_message
    run_date, type_key, offset = _parse_message(_eb_message("2026-07-01", "Manual"))
    assert (run_date, type_key, offset) == ("2026-07-01", "Manual", 0)


def test_parse_message_cursor_shape():
    from f5kb.handlers.enrich import _parse_message
    msg = {"run_date": "2026-07-01", "type_key": "Bug_Tracker", "manifest_offset": 42}
    assert _parse_message(msg) == ("2026-07-01", "Bug_Tracker", 42)


def test_parse_message_unknown_shape_raises():
    from f5kb.handlers.enrich import _parse_message
    with pytest.raises(ValueError):
        _parse_message({"unexpected": True})


# ── Happy path: Bug_Tracker enrichment ────────────────────────────────────────


def test_enrich_bug_tracker_fills_body(aws, mock_http):
    store, _, _ = aws
    mock_http(lambda request: httpx.Response(200, text=BUG_HTML))

    _put_orchestrator(store, ["Bug_Tracker"], ["Bug_Tracker"])
    _stage(store, "Bug_Tracker", "1234567", _bug_article("1234567"))

    from f5kb.handlers.enrich import handler
    result = handler(_sqs_event(_eb_message(RUN_DATE, "Bug_Tracker")), _Ctx())

    assert result["status"] == "done"
    assert result["enriched"] == 1
    assert result["failed"] == 0

    enriched = store.get("pending/Bug_Tracker/1234567.json")
    assert enriched["content"]["body_text"].strip()
    assert "bodyError" not in enriched["content"]

    report = store.get(f"runs/{RUN_DATE}/enrich/Bug_Tracker/_report.json")
    assert report["enriched"] == 1
    assert store.exists(f"runs/{RUN_DATE}/enrich/Bug_Tracker/_done")
    # Sole type in the run → terminal gate fires and advances the phase.
    assert store.exists(f"runs/{RUN_DATE}/scrape/_done")
    assert store.get(f"runs/{RUN_DATE}/status.json")["phase"] == "track"
    # Cursor is cleaned up on completion.
    assert not store.exists(f"lambda/state/{RUN_DATE}/enrich-Bug_Tracker.json")


# ── Regression: link recovered from metadata (pre-fix envelopes) ─────────────


def test_enrich_recovers_link_from_metadata(aws, mock_http):
    """Envelopes staged before the dump-envelope fix carry no top-level link;
    the handler must fall back to metadata clickUri before enriching."""
    store, _, _ = aws
    mock_http(lambda request: httpx.Response(200, text=BUG_HTML))

    url = "https://cdn.f5.com/product/bugtracker/ID7654321.html"
    article = _bug_article("7654321", metadata={"clickUri": url})
    del article["link"]
    _put_orchestrator(store, ["Bug_Tracker"], ["Bug_Tracker"])
    _stage(store, "Bug_Tracker", "7654321", article)

    from f5kb.handlers.enrich import handler
    result = handler(_sqs_event(_eb_message(RUN_DATE, "Bug_Tracker")), _Ctx())

    assert result["enriched"] == 1
    assert result["failed"] == 0
    enriched = store.get("pending/Bug_Tracker/7654321.json")
    assert enriched["content"]["bodySource"] == url


# ── Failure accounting ────────────────────────────────────────────────────────


def test_enricher_exception_records_body_error(aws, mock_http, capfd):
    store, _, _ = aws
    mock_http(lambda request: httpx.Response(404, text="not here"))

    _put_orchestrator(store, ["Bug_Tracker"], ["Bug_Tracker"])
    _stage(store, "Bug_Tracker", "1111111", _bug_article("1111111"))

    from f5kb.handlers.enrich import handler
    result = handler(_sqs_event(_eb_message(RUN_DATE, "Bug_Tracker")), _Ctx())

    assert result["status"] == "done"
    assert result["failed"] == 1
    assert result["enriched"] == 0

    enriched = store.get("pending/Bug_Tracker/1111111.json")
    assert "HTTP 404" in enriched["content"]["bodyError"]

    # The EnrichFailed metric filter requires action + final IS TRUE.
    err = capfd.readouterr().err
    line = next(json.loads(ln) for ln in err.splitlines()
                if '"article_enrich_failed"' in ln)
    assert line["final"] is True


def test_soft_body_error_counts_as_failed(aws, monkeypatch):
    """Enrichers can RETURN a bodyError (soft 404) without raising — that must
    count as failed, not enriched."""
    store, _, _ = aws
    _put_orchestrator(store, ["Bug_Tracker"], ["Bug_Tracker"])
    _stage(store, "Bug_Tracker", "2222222", _bug_article("2222222"))

    import f5kb.handlers.enrich as enrich_mod

    def soft_fail(article, now_iso, http, **_):
        return {"bodySource": article.get("link") or "", "fetchedAt": now_iso,
                "bodyError": "soft 404 (HTTP 200 'Page Not Found')"}

    monkeypatch.setitem(enrich_mod.TYPE_ENRICHERS, "Bug_Tracker", soft_fail)

    result = enrich_mod.handler(_sqs_event(_eb_message(RUN_DATE, "Bug_Tracker")), _Ctx())
    assert result["failed"] == 1
    assert result["enriched"] == 0


# ── Skip logic ────────────────────────────────────────────────────────────────


def test_articles_with_body_are_skipped(aws, mock_http):
    store, _, _ = aws
    mock_http(lambda request: httpx.Response(500, text="must not be fetched"))

    article = _bug_article("3333333", content={"body_text": "already enriched body"})
    _put_orchestrator(store, ["Bug_Tracker"], ["Bug_Tracker"])
    _stage(store, "Bug_Tracker", "3333333", article)

    from f5kb.handlers.enrich import handler
    result = handler(_sqs_event(_eb_message(RUN_DATE, "Bug_Tracker")), _Ctx())

    assert result["skipped"] == 1
    assert result["enriched"] == 0
    assert result["failed"] == 0


def test_missing_pending_object_is_tolerated(aws, mock_http):
    store, _, _ = aws
    mock_http(lambda request: httpx.Response(200, text=BUG_HTML))

    _put_orchestrator(store, ["Bug_Tracker"], ["Bug_Tracker"])
    # Manifest entry without a pending/ object (e.g. resolved by a prior run).
    store.append_jsonl(f"runs/{RUN_DATE}/manifest/Bug_Tracker.jsonl", {
        "op": "new", "id": "GONE", "type_key": "Bug_Tracker", "run_date": RUN_DATE,
    })

    from f5kb.handlers.enrich import handler
    result = handler(_sqs_event(_eb_message(RUN_DATE, "Bug_Tracker")), _Ctx())

    assert result["status"] == "done"
    assert result == {"status": "done", "type_key": "Bug_Tracker",
                      "enriched": 0, "failed": 0, "skipped": 0}


# ── Non-enrichable guard ──────────────────────────────────────────────────────


def test_non_enrichable_message_passes_through(aws):
    store, _, _ = aws
    _put_orchestrator(store, ["Policy"], [])

    from f5kb.handlers.enrich import handler
    result = handler(_sqs_event(_eb_message(RUN_DATE, "Policy")), _Ctx())

    assert result["status"] == "skipped"
    assert store.exists(f"runs/{RUN_DATE}/enrich/Policy/_done")


# ── Timeout → cursor + self re-queue ──────────────────────────────────────────


def test_timeout_saves_cursor_and_requeues(aws, mock_http):
    store, sqs, queue_url = aws
    mock_http(lambda request: httpx.Response(200, text=BUG_HTML))

    _put_orchestrator(store, ["Bug_Tracker"], ["Bug_Tracker"])
    _stage(store, "Bug_Tracker", "4444444", _bug_article("4444444"))

    from f5kb.handlers.enrich import handler
    result = handler(_sqs_event(_eb_message(RUN_DATE, "Bug_Tracker")), _Ctx(ms=1_000))

    assert result["status"] == "resumed"
    assert result["manifest_offset"] == 0

    cursor = store.get(f"lambda/state/{RUN_DATE}/enrich-Bug_Tracker.json")
    assert cursor["manifest_offset"] == 0
    assert cursor["status"] == "in_progress"

    msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
    bodies = [json.loads(m["Body"]) for m in msgs]
    assert {"run_date": RUN_DATE, "type_key": "Bug_Tracker", "manifest_offset": 0} in bodies
    # No completion artifacts while resuming.
    assert not store.exists(f"runs/{RUN_DATE}/enrich/Bug_Tracker/_done")


def test_resume_uses_saved_cursor(aws, mock_http):
    store, _, _ = aws
    mock_http(lambda request: httpx.Response(200, text=BUG_HTML))

    _put_orchestrator(store, ["Bug_Tracker"], ["Bug_Tracker"])
    _stage(store, "Bug_Tracker", "5555555", _bug_article("5555555"))
    _stage(store, "Bug_Tracker", "6666666", _bug_article("6666666"))
    # Cursor says article 0 was already enriched by the timed-out invocation.
    store.put(f"lambda/state/{RUN_DATE}/enrich-Bug_Tracker.json", {
        "run_date": RUN_DATE, "type_key": "Bug_Tracker",
        "manifest_offset": 1, "enriched": 1, "failed": 0, "skipped": 0,
        "status": "in_progress",
    })

    from f5kb.handlers.enrich import handler
    msg = {"run_date": RUN_DATE, "type_key": "Bug_Tracker", "manifest_offset": 1}
    result = handler(_sqs_event(msg), _Ctx())

    assert result["status"] == "done"
    # Running totals carried over from the cursor: 1 prior + 1 fresh.
    assert result["enriched"] == 2


# ── Terminal gate ─────────────────────────────────────────────────────────────


def test_gate_waits_for_all_types(aws, mock_http):
    store, _, _ = aws
    mock_http(lambda request: httpx.Response(200, text=BUG_HTML))

    _put_orchestrator(store, ["Bug_Tracker", "Policy"], ["Bug_Tracker"])
    _stage(store, "Bug_Tracker", "1234567", _bug_article("1234567"))
    # Policy (non-enrichable) has NOT written dump/_done yet.

    from f5kb.handlers.enrich import handler
    handler(_sqs_event(_eb_message(RUN_DATE, "Bug_Tracker")), _Ctx())

    assert store.exists(f"runs/{RUN_DATE}/enrich/Bug_Tracker/_done")
    assert not store.exists(f"runs/{RUN_DATE}/scrape/_done")


def test_gate_fires_when_last_type_finishes(aws, mock_http):
    store, _, _ = aws
    mock_http(lambda request: httpx.Response(200, text=BUG_HTML))

    _put_orchestrator(store, ["Bug_Tracker", "Policy"], ["Bug_Tracker"])
    _stage(store, "Bug_Tracker", "1234567", _bug_article("1234567"))
    store.put_marker(f"runs/{RUN_DATE}/dump/Policy/_done")

    from f5kb.handlers.enrich import handler
    handler(_sqs_event(_eb_message(RUN_DATE, "Bug_Tracker")), _Ctx())

    assert store.exists(f"runs/{RUN_DATE}/scrape/_done")
    assert store.get(f"runs/{RUN_DATE}/status.json")["phase"] == "track"
