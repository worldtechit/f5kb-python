"""Tests for f5kb/handlers/track.py — hash diff + risk classification with moto."""

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


@pytest.fixture
def aws(env):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3Storage(BUCKET)


def _scrape_done_event() -> dict:
    return {"detail": {"object": {"key": f"runs/{RUN_DATE}/scrape/_done"}}}


def _register(store: S3Storage, types: list[str]) -> None:
    store.put(f"lambda/state/{RUN_DATE}/orchestrator.json", {
        "run_date": RUN_DATE, "types": types, "enrichable": [],
    })


def _stage(store: S3Storage, type_key: str, art_id: str,
           body: str = "", body_error: str = "", metadata: dict | None = None) -> None:
    content: dict = {}
    if body:
        content["body_text"] = body
    if body_error:
        content["bodyError"] = body_error
    store.put(f"pending/{type_key}/{art_id}.json", {
        "id": art_id, "type_key": type_key,
        "metadata": metadata or {"v": 1}, "content": content,
    })
    store.append_jsonl(f"runs/{RUN_DATE}/manifest/{type_key}.jsonl", {
        "op": "new", "id": art_id, "type_key": type_key, "run_date": RUN_DATE,
    })


def _live(store: S3Storage, type_key: str, art_id: str, body: str,
          metadata: dict | None = None) -> None:
    store.put(f"live/{type_key}/{art_id}.json", {
        "id": art_id, "metadata": metadata or {"v": 0},
        "content": {"body_text": body},
    })


def _changes(store: S3Storage) -> list[dict]:
    raw = store.get_bytes(f"runs/{RUN_DATE}/track/changes.jsonl").decode()
    return [json.loads(ln) for ln in raw.splitlines() if ln.strip()]


# ── op classification ─────────────────────────────────────────────────────────


def test_new_article_classified_new(aws):
    store = aws
    _register(store, ["Policy"])
    _stage(store, "Policy", "K001", body="fresh body " * 20)

    from f5kb.handlers.track import handler
    result = handler(_scrape_done_event(), None)

    assert result["new"] == 1
    assert result["changed"] == 0
    rec = _changes(store)[0]
    assert rec["op"] == "new"
    assert rec["risk"] == []


def test_changed_and_unchanged_ops_via_hash_index(aws):
    store = aws
    _register(store, ["Policy"])
    _stage(store, "Policy", "CHANGED", metadata={"v": 2})
    _stage(store, "Policy", "SAME", metadata={"v": 1})
    store.save_hash_index({
        "Policy CHANGED": "old-hash",
        "Policy SAME": sha256_obj({"v": 1}),
    }, "hash-index/current.json.gz")

    from f5kb.handlers.track import handler
    result = handler(_scrape_done_event(), None)

    assert result["changed"] == 1
    assert result["unchanged"] == 1
    ops = {r["id"]: r["op"] for r in _changes(store)}
    assert ops == {"CHANGED": "changed", "SAME": "unchanged"}


# ── risk classification ───────────────────────────────────────────────────────


def test_body_dropped_flagged(aws):
    store = aws
    _register(store, ["Manual"])
    _live(store, "Manual", "K010", body="live body " * 50)
    _stage(store, "Manual", "K010", body="")  # pending lost its body

    from f5kb.handlers.track import handler
    handler(_scrape_done_event(), None)

    rec = _changes(store)[0]
    assert "body-dropped" in rec["risk"]
    summary = store.get(f"runs/{RUN_DATE}/track/summary.json")
    assert summary["risk_breakdown"]["body_dropped"] == 1


def test_body_error_flagged(aws):
    store = aws
    _register(store, ["Manual"])
    _live(store, "Manual", "K011", body="live body " * 50)
    _stage(store, "Manual", "K011", body_error="HTTP 503")

    from f5kb.handlers.track import handler
    handler(_scrape_done_event(), None)

    rec = _changes(store)[0]
    assert "body-error" in rec["risk"]


# Regression: body-shrank is INFORMATIONAL only — it never holds, no matter how
# large the shrink. Holds are exclusively body-dropped / body-error.
def test_shrink_flagged_but_never_held(aws):
    store = aws
    _register(store, ["Manual"])
    _live(store, "Manual", "K012", body="x" * 1000)
    _stage(store, "Manual", "K012", body="x" * 300)  # 70% shrink

    from f5kb.handlers.track import handler
    result = handler(_scrape_done_event(), None)

    rec = _changes(store)[0]
    assert rec["risk"] == ["body-shrank-70%"]
    assert "held_shrank" not in result
    summary = store.get(f"runs/{RUN_DATE}/track/summary.json")
    assert summary["risk_breakdown"]["body_shrank"] == 1
    assert "held_shrank" not in summary["risk_breakdown"]


def test_extreme_shrink_still_informational(aws):
    store = aws
    _register(store, ["Manual"])
    _live(store, "Manual", "K013", body="x" * 1000)
    _stage(store, "Manual", "K013", body="x" * 50)  # 95% shrink

    from f5kb.handlers.track import handler
    handler(_scrape_done_event(), None)

    rec = _changes(store)[0]
    assert rec["risk"] == ["body-shrank-95%"]  # plain flag, no hold marker


# ── completion artifacts ──────────────────────────────────────────────────────


def test_writes_summary_done_and_advances_phase(aws):
    store = aws
    _register(store, ["Policy"])
    _stage(store, "Policy", "K020", body="body " * 20)

    from f5kb.handlers.track import handler
    handler(_scrape_done_event(), None)

    assert store.exists(f"runs/{RUN_DATE}/track/_done")
    assert store.get(f"runs/{RUN_DATE}/status.json")["phase"] == "approve"
    summary = store.get(f"runs/{RUN_DATE}/track/summary.json")
    assert summary["total"] == 1
    assert summary["new"] == 1


def test_empty_manifests_complete_cleanly(aws):
    store = aws
    _register(store, ["Policy", "Compliance"])

    from f5kb.handlers.track import handler
    result = handler(_scrape_done_event(), None)

    assert result["total"] == 0
    assert store.exists(f"runs/{RUN_DATE}/track/_done")


# ── resume ────────────────────────────────────────────────────────────────────


def test_completed_types_not_reprocessed(aws):
    store = aws
    _register(store, ["Policy", "Compliance"])
    _stage(store, "Policy", "K030", body="body " * 20)
    _stage(store, "Compliance", "K031", body="body " * 20)
    # Prior invocation already processed Policy (1 article counted).
    store.put(f"runs/{RUN_DATE}/track/progress.json", {
        "run_date": RUN_DATE,
        "completed_types": ["Policy"],
        "counts": {"new": 1, "changed": 0, "unchanged": 0,
                   "body_shrank": 0, "body_dropped": 0, "body_error": 0,
                   },
    })

    from f5kb.handlers.track import handler
    result = handler(_scrape_done_event(), None)

    # Only Compliance is processed fresh; Policy's count carries over untouched.
    assert result["new"] == 2
    recs = _changes(store)
    assert [r["id"] for r in recs] == ["K031"]


def test_timeout_saves_progress_and_reinvokes(aws, monkeypatch):
    store = aws
    _register(store, ["Policy"])
    _stage(store, "Policy", "K040", body="body " * 20)

    import f5kb.handlers.track as track_mod
    reinvoked: list[dict] = []
    monkeypatch.setattr(track_mod, "_self_invoke", lambda event, context: reinvoked.append(event))

    class _Ctx:
        def get_remaining_time_in_millis(self):
            return 1_000

    result = track_mod.handler(_scrape_done_event(), _Ctx())

    assert result["status"] == "resumed"
    assert len(reinvoked) == 1
    assert store.exists(f"runs/{RUN_DATE}/track/progress.json")
    assert not store.exists(f"runs/{RUN_DATE}/track/_done")


# ── batched writes (O(n^2) append regression) ─────────────────────────────────


def test_changes_written_in_one_batch_per_type(aws, monkeypatch):
    """A 600-article type must produce ONE changes.jsonl write, not 600 —
    per-article append_jsonl re-uploads the whole growing file (O(n^2) bytes)
    and made full-corpus track outlast the 900s Lambda limit forever."""
    store = aws
    _register(store, ["Policy"])
    for i in range(600):
        _stage(store, "Policy", f"K{i:05}", body="body " * 10)

    writes = {"changes": 0}
    orig = S3Storage.put_bytes

    def counting(self, key, data, content_type="application/octet-stream"):
        if key.endswith("track/changes.jsonl"):
            writes["changes"] += 1
        return orig(self, key, data, content_type)

    monkeypatch.setattr(S3Storage, "put_bytes", counting)

    from f5kb.handlers.track import handler
    result = handler(_scrape_done_event(), None)

    assert result["new"] == 600
    assert len(_changes(store)) == 600
    assert writes["changes"] == 1


def test_fresh_start_resets_stale_partial_changes(aws):
    """A prior invocation that died MID-type leaves partial changes.jsonl lines
    with no progress checkpoint; a fresh start must reset the file, not append
    duplicates on top."""
    store = aws
    _register(store, ["Policy"])
    _stage(store, "Policy", "K001", body="body " * 20)
    # stale partial from a crashed attempt (no progress.json)
    store.append_jsonl(f"runs/{RUN_DATE}/track/changes.jsonl",
                       {"id": "STALE", "type_key": "Policy", "op": "new"})

    from f5kb.handlers.track import handler
    handler(_scrape_done_event(), None)

    ids = [r["id"] for r in _changes(store)]
    assert ids == ["K001"]  # stale line gone, no duplicates
