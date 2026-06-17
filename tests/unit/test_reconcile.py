"""Tests for lib/reconcile.py — reconcile() deletion logic."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from f5kb.coveo.aura import CoveoConfig
from f5kb.coveo.client import CoveoClient
from f5kb.lib.reconcile import reconcile
from f5kb.track.db import init_db


# ── helpers ───────────────────────────────────────────────────────────────────

TYPE_CONFIGS = {
    "Knowledge": {"documentType": "Knowledge", "metadata": "*", "content": []},
}


def _seed_db(db_path: Path, document_type: str, ids: list[str]) -> None:
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    with conn:
        for art_id in ids:
            conn.execute(
                """INSERT OR IGNORE INTO articles
                   (document_type, id, title, link, metadata_hash, content_hash,
                    has_body, first_seen_run, last_seen_run, last_changed_run)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (document_type, art_id, art_id, "", "mh", "ch", 0, "r1", "r1", "r1"),
            )
    conn.close()


def _write_article(dump: Path, type_key: str, art_id: str) -> Path:
    d = dump / type_key
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{art_id}.json"
    p.write_text(json.dumps({"id": art_id, "raw": {"f5_kb_id": art_id, "permanentid": art_id}}))
    return p


class _ScriptedTransport(httpx.BaseTransport):
    def __init__(self, responses):
        self._responses = list(responses)

    def handle_request(self, _):
        return self._responses.pop(0)


def _make_client(responses) -> CoveoClient:
    transport = _ScriptedTransport(responses)
    http = httpx.Client(transport=transport)
    cfg = CoveoConfig(platform_url="https://org.coveo.com", access_token="tok", organization_id="org")
    return CoveoClient(cfg, client=http, sleep=lambda _: None)


def _fetch_ids_response(ids: list[str]) -> list[httpx.Response]:
    """Build Coveo search responses that fetch_ids will consume (keyset pagination)."""
    results = [
        {"raw": {"permanentid": id_, "f5_kb_id": id_, "rowid": i + 1}}
        for i, id_ in enumerate(ids)
    ]
    return [
        httpx.Response(200, json={"totalCount": len(ids), "totalCountFiltered": len(ids), "results": []}),
        httpx.Response(200, json={"totalCount": len(ids), "totalCountFiltered": len(ids), "results": results}),
        httpx.Response(200, json={"totalCount": 0, "totalCountFiltered": 0, "results": []}),  # terminate
    ]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_no_deletions_returns_zero(tmp_path):
    """When live IDs match DB, no deletions are reported."""
    db = tmp_path / "articles.db"
    dump = tmp_path / "dump"
    _seed_db(db, "Knowledge", ["K1", "K2"])
    _write_article(dump, "Knowledge", "K1")
    _write_article(dump, "Knowledge", "K2")

    with patch("f5kb.lib.reconcile.fetch_ids", return_value=[
        {"raw": {"permanentid": "K1", "f5_kb_id": "K1"}},
        {"raw": {"permanentid": "K2", "f5_kb_id": "K2"}},
    ]):
        client = MagicMock()
        result = reconcile(
            client, dump=str(dump), db=str(db),
            type_configs=TYPE_CONFIGS, type_keys=["Knowledge"],
        )

    assert result.total_deletions == 0
    assert result.applied is False


def test_deletions_detected_report_only(tmp_path):
    """With apply=False, deletions are reported but files are not touched."""
    db = tmp_path / "articles.db"
    dump = tmp_path / "dump"
    _seed_db(db, "Knowledge", ["K1", "K_gone"])
    art_file = _write_article(dump, "Knowledge", "K_gone")

    with patch("f5kb.lib.reconcile.fetch_ids", return_value=[
        {"raw": {"permanentid": "K1", "f5_kb_id": "K1"}},
    ]):
        client = MagicMock()
        result = reconcile(
            client, dump=str(dump), db=str(db),
            type_configs=TYPE_CONFIGS, type_keys=["Knowledge"],
            apply=False,
        )

    assert result.total_deletions == 1
    assert result.applied is False
    assert art_file.exists()  # file NOT touched


def test_deletions_applied_archives_file(tmp_path):
    """apply=True archives deleted article to _deleted/."""
    db = tmp_path / "articles.db"
    dump = tmp_path / "dump"
    _seed_db(db, "Knowledge", ["K1", "K_gone"])
    art_file = _write_article(dump, "Knowledge", "K_gone")

    with patch("f5kb.lib.reconcile.fetch_ids", return_value=[
        {"raw": {"permanentid": "K1", "f5_kb_id": "K1"}},
    ]):
        client = MagicMock()
        result = reconcile(
            client, dump=str(dump), db=str(db),
            type_configs=TYPE_CONFIGS, type_keys=["Knowledge"],
            apply=True, max_delete_pct=1.0,
        )

    assert result.applied is True
    assert result.total_deletions == 1
    assert not art_file.exists()
    assert (dump / "_deleted" / "Knowledge" / "K_gone.json").exists()


def test_max_delete_pct_guard_aborts(tmp_path):
    """If deletion rate exceeds max_delete_pct, reconcile aborts without applying."""
    db = tmp_path / "articles.db"
    dump = tmp_path / "dump"
    # 5 in DB, 4 gone = 80% deletion — exceeds default 25%
    ids = [f"K{i}" for i in range(5)]
    _seed_db(db, "Knowledge", ids)
    for id_ in ids:
        _write_article(dump, "Knowledge", id_)

    with patch("f5kb.lib.reconcile.fetch_ids", return_value=[
        {"raw": {"permanentid": "K0", "f5_kb_id": "K0"}},
    ]):
        client = MagicMock()
        result = reconcile(
            client, dump=str(dump), db=str(db),
            type_configs=TYPE_CONFIGS, type_keys=["Knowledge"],
            apply=True, max_delete_pct=0.25,
        )

    assert result.applied is False
    assert result.aborted is not None
    # none of the files removed
    for id_ in ids:
        assert (dump / "Knowledge" / f"{id_}.json").exists()


def test_purge_deletes_without_archive(tmp_path):
    """purge=True deletes files without archiving them."""
    db = tmp_path / "articles.db"
    dump = tmp_path / "dump"
    _seed_db(db, "Knowledge", ["K_purge"])
    art_file = _write_article(dump, "Knowledge", "K_purge")

    with patch("f5kb.lib.reconcile.fetch_ids", return_value=[]):
        client = MagicMock()
        result = reconcile(
            client, dump=str(dump), db=str(db),
            type_configs=TYPE_CONFIGS, type_keys=["Knowledge"],
            apply=True, purge=True, max_delete_pct=1.0,
        )

    assert result.applied is True
    assert not art_file.exists()
    assert not (dump / "_deleted").exists()
