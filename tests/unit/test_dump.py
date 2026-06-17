"""Tests for lib/dump.py — dump_types() orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from f5kb.coveo.aura import CoveoConfig
from f5kb.coveo.client import CoveoClient
from f5kb.lib.dump import dump_types
from f5kb.track.hashing import sha256_obj


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_config() -> CoveoConfig:
    return CoveoConfig(
        platform_url="https://org.coveo.com",
        access_token="tok",
        organization_id="org",
    )


def _search_response(results=None, total: int = 0) -> dict:
    r = results or []
    return {"totalCount": total, "totalCountFiltered": total, "results": r}


def _article_result(kb_id: str, title: str = "Test", body: str = "") -> dict:
    return {
        "title": title,
        "clickUri": f"https://my.f5.com/article/{kb_id}",
        "uniqueId": kb_id,
        "raw": {
            "f5_kb_id": kb_id,
            "permanentid": kb_id,
            "f5_document_type": "Knowledge",
            "rowid": 1,
            "f5_article_content": body,
        },
    }


class _ScriptedTransport(httpx.BaseTransport):
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)

    def handle_request(self, _: httpx.Request) -> httpx.Response:
        return self._responses.pop(0)


def _json_resp(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


def _make_client(responses: list[httpx.Response]) -> CoveoClient:
    transport = _ScriptedTransport(responses)
    http = httpx.Client(transport=transport)
    return CoveoClient(_make_config(), client=http, sleep=lambda _: None)


TYPE_CONFIGS = {
    "Knowledge": {"documentType": "Knowledge", "metadata": "*", "content": []},
}

BASE_KWARGS = dict(
    type_configs=TYPE_CONFIGS,
    type_keys=["Knowledge"],
    descriptions={},
    all_time=True,
    mode="all",
    cutoff_ms=0,
    end_ms=9_999_999_999_000,
    now_ms=1_700_000_000_000,
    page_size=200,
    limit=0,
    config_path="config.yaml",
)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_new_article_written(tmp_path):
    """New article is written to disk; ID appears in current_ids."""
    art = _article_result("K12345", "My Article")
    # get_count → 1; search → 1 result
    client = _make_client([
        _json_resp({"totalCount": 1, "totalCountFiltered": 1, "results": []}),  # count
        _json_resp({"totalCount": 1, "totalCountFiltered": 1, "results": [art], "searchUid": "x"}),  # page
    ])
    result = dump_types(client, out_dir=str(tmp_path), **BASE_KWARGS)

    assert result.total == 1
    assert len(result.manifest) == 1
    st = result.manifest[0]
    assert st.written == 1
    assert st.skipped == 0
    assert st.status == "ok"
    assert "K12345" in result.current_ids["Knowledge"]

    written = [f for f in (tmp_path / "Knowledge").glob("*.json") if not f.name.startswith("_")]
    assert len(written) == 1
    data = json.loads(written[0].read_text())
    assert data["id"] == "K12345"
    assert data["title"] == "My Article"


def test_unchanged_article_skipped(tmp_path):
    """Article with matching prior hash is skipped (incremental=True)."""
    art = _article_result("K99", "Unchanged")
    # compute the hash that dump_types would compute
    from f5kb.coveo.fields import flatten_fields_safe, split_entry
    from f5kb.config.types import normalize_type
    cfg = normalize_type({"documentType": "Knowledge", "metadata": "*", "content": []})
    fields = flatten_fields_safe(art)
    split = split_entry(fields, cfg)
    prior_hash = sha256_obj(split["metadata"])

    client = _make_client([
        _json_resp({"totalCount": 1, "totalCountFiltered": 1, "results": []}),
        _json_resp({"totalCount": 1, "totalCountFiltered": 1, "results": [art]}),
    ])
    result = dump_types(
        client,
        out_dir=str(tmp_path),
        incremental=True,
        prior_hashes={"Knowledge K99": prior_hash},
        **BASE_KWARGS,
    )

    st = result.manifest[0]
    assert st.skipped == 1
    assert st.written == 0
    assert not [f for f in (tmp_path / "Knowledge").glob("*.json") if not f.name.startswith("_")]


def test_edited_article_staged_with_approval(tmp_path):
    """Edited article with approval=True goes to _pending/, not written directly."""
    art = _article_result("K77", "Edited")
    stale_hash = sha256_obj({"title": "old"})

    client = _make_client([
        _json_resp({"totalCount": 1, "totalCountFiltered": 1, "results": []}),
        _json_resp({"totalCount": 1, "totalCountFiltered": 1, "results": [art]}),
    ])
    result = dump_types(
        client,
        out_dir=str(tmp_path),
        approval=True,
        prior_hashes={"Knowledge K77": stale_hash},
        **BASE_KWARGS,
    )

    st = result.manifest[0]
    assert st.staged == 1
    assert st.written == 0
    assert len(result.pending) == 1
    assert result.pending[0].id == "K77"
    # pending file exists; live file does not
    assert (tmp_path / "_pending" / "Knowledge" / "K77.json").exists()
    assert not (tmp_path / "Knowledge" / "K77.json").exists()


def test_edited_article_written_without_approval(tmp_path):
    """Edited article with approval=False is written directly."""
    art = _article_result("K55", "Direct")
    stale_hash = sha256_obj({"title": "old"})

    client = _make_client([
        _json_resp({"totalCount": 1, "totalCountFiltered": 1, "results": []}),
        _json_resp({"totalCount": 1, "totalCountFiltered": 1, "results": [art]}),
    ])
    result = dump_types(
        client,
        out_dir=str(tmp_path),
        approval=False,
        prior_hashes={"Knowledge K55": stale_hash},
        **BASE_KWARGS,
    )

    st = result.manifest[0]
    assert st.written == 1
    assert st.staged == 0
    assert (tmp_path / "Knowledge" / "K55.json").exists()


def test_limit_caps_articles(tmp_path):
    """limit=2 caps fetch at 2 articles even if more available."""
    arts = [_article_result(f"K{i}") for i in range(5)]
    client = _make_client([
        _json_resp({"totalCount": 5, "totalCountFiltered": 5, "results": []}),
        _json_resp({"totalCount": 5, "totalCountFiltered": 5, "results": arts[:2]}),
    ])
    kwargs = {**BASE_KWARGS, "limit": 2}
    result = dump_types(client, out_dir=str(tmp_path), **kwargs)

    assert result.total == 2


def test_partial_status_when_undercount(tmp_path):
    """Status is PARTIAL when written < expected (all_time, limit=0)."""
    art = _article_result("K1")
    client = _make_client([
        _json_resp({"totalCount": 10, "totalCountFiltered": 10, "results": []}),
        _json_resp({"totalCount": 10, "totalCountFiltered": 10, "results": [art]}),
    ])
    result = dump_types(client, out_dir=str(tmp_path), **BASE_KWARGS)

    st = result.manifest[0]
    assert st.expected == 10
    assert st.written == 1
    assert st.status == "partial"


def test_no_document_type_fails_gracefully(tmp_path):
    """Type with no documentType in config gets a 'failed' status."""
    bad_configs = {"Broken": {"documentType": "", "metadata": "*", "content": []}}
    client = _make_client([])
    result = dump_types(
        client,
        out_dir=str(tmp_path),
        type_configs=bad_configs,
        type_keys=["Broken"],
        descriptions={},
        all_time=True,
        mode="all",
        cutoff_ms=0,
        end_ms=9_999_999_999_000,
        now_ms=1_700_000_000_000,
        page_size=200,
        limit=0,
        config_path="config.yaml",
    )

    assert result.manifest[0].status == "failed"
    assert result.total == 0
