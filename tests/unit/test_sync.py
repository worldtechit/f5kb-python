"""Tests for lib/sync.py — sync_dump() orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from f5kb.lib.sync import sync_dump, ENRICHABLE


TYPE_CONFIGS = {
    "Knowledge": {"documentType": "Knowledge", "metadata": "*", "content": []},
    "Bug_Tracker": {"documentType": "Bug Tracker", "metadata": "*", "content": []},
}

BASE_KWARGS = dict(
    type_configs=TYPE_CONFIGS,
    type_keys=["Knowledge"],
    descriptions={},
    mode="all",
    all_time=True,
    cutoff_ms=0,
    end_ms=9_999_999_999_000,
    now_ms=1_700_000_000_000,
    page_size=200,
    limit=0,
    config_path="config.yaml",
    dry_run=False,
    approval=False,
)


def _make_mock_dump_result(written=0, skipped=0, staged=0):
    from f5kb.lib.dump import DumpTypesResult, TypeStatus
    manifest = [TypeStatus(
        type_key="Knowledge", document_type="Knowledge", dir="Knowledge",
        status="ok", expected=written, fetched=written,
        written=written, skipped=skipped, staged=staged,
        replaced=0, write_errors=0,
    )]
    return DumpTypesResult(
        manifest=manifest, index_path="/tmp/x/_index.json",
        total=written, current_ids={"Knowledge": set()}, pending=[],
    )


def test_days_mode_cutoff_ms(tmp_path):
    """--days=1 cuts off 86400s before now_ms."""
    now_ms = 1_700_000_000_000
    days = 1
    expected_cutoff = now_ms - int(days * 86_400_000)

    client = MagicMock()
    http = MagicMock()

    with patch("f5kb.lib.sync.dump_types", return_value=_make_mock_dump_result()) as mock_dump, \
         patch("f5kb.lib.sync.track_dump"), \
         patch("f5kb.lib.sync.load_hash_index", return_value={}), \
         patch("f5kb.lib.sync.load_ids_by_type", return_value={}):
        sync_dump(
            client=client, http=http,
            out_dir=str(tmp_path),
            db=str(tmp_path / "articles.db"),
            mode=f"days={days}",
            all_time=False,
            cutoff_ms=expected_cutoff,
            now_ms=now_ms,
            end_ms=now_ms + 86_400_000,
            **{k: v for k, v in BASE_KWARGS.items()
               if k not in ("mode", "all_time", "cutoff_ms", "end_ms", "now_ms")},
        )
        call_kwargs = mock_dump.call_args.kwargs
        assert call_kwargs["cutoff_ms"] == expected_cutoff
        assert call_kwargs["all_time"] is False


def test_all_mode_passes_all_time(tmp_path):
    """--all mode calls dump_types with all_time=True."""
    client = MagicMock()
    http = MagicMock()

    with patch("f5kb.lib.sync.dump_types", return_value=_make_mock_dump_result()) as mock_dump, \
         patch("f5kb.lib.sync.track_dump"), \
         patch("f5kb.lib.sync.load_hash_index", return_value={}), \
         patch("f5kb.lib.sync.load_ids_by_type", return_value={}):
        sync_dump(
            client=client, http=http,
            out_dir=str(tmp_path),
            db=str(tmp_path / "articles.db"),
            **BASE_KWARGS,
        )
        assert mock_dump.call_args.kwargs["all_time"] is True


def test_no_enrich_skips_enrich_step(tmp_path):
    """enrich=False skips enrichment entirely."""
    client = MagicMock()
    http = MagicMock()

    with patch("f5kb.lib.sync.dump_types", return_value=_make_mock_dump_result(written=1)), \
         patch("f5kb.lib.sync.track_dump"), \
         patch("f5kb.lib.sync.load_hash_index", return_value={}), \
         patch("f5kb.lib.sync.load_ids_by_type", return_value={}), \
         patch("f5kb.lib.sync.enrich_dump") as mock_enrich:
        sync_dump(
            client=client, http=http,
            out_dir=str(tmp_path),
            db=str(tmp_path / "articles.db"),
            enrich=False,
            type_keys=["Bug_Tracker"],
            **{k: v for k, v in BASE_KWARGS.items() if k != "type_keys"},
        )
        mock_enrich.assert_not_called()


def test_dry_run_skips_track_and_enrich(tmp_path):
    """dry_run=True skips track_dump and enrich_dump calls."""
    client = MagicMock()
    http = MagicMock()

    with patch("f5kb.lib.sync.dump_types", return_value=_make_mock_dump_result()) as mock_dump, \
         patch("f5kb.lib.sync.track_dump") as mock_track, \
         patch("f5kb.lib.sync.enrich_dump") as mock_enrich, \
         patch("f5kb.lib.sync.load_hash_index", return_value={}), \
         patch("f5kb.lib.sync.load_ids_by_type", return_value={}):
        result = sync_dump(
            client=client, http=http,
            out_dir=str(tmp_path),
            db=str(tmp_path / "articles.db"),
            **{**BASE_KWARGS, "dry_run": True},
        )
        assert result.dry_run is True
        mock_track.assert_not_called()
        mock_enrich.assert_not_called()
        # dump called with dry_run=True
        assert mock_dump.call_args.kwargs["dry_run"] is True


def test_sync_result_written_count(tmp_path):
    """SyncResult.written reflects dump manifest totals."""
    client = MagicMock()
    http = MagicMock()

    with patch("f5kb.lib.sync.dump_types", return_value=_make_mock_dump_result(written=3)), \
         patch("f5kb.lib.sync.track_dump"), \
         patch("f5kb.lib.sync.load_hash_index", return_value={}), \
         patch("f5kb.lib.sync.load_ids_by_type", return_value={}):
        result = sync_dump(
            client=client, http=http,
            out_dir=str(tmp_path),
            db=str(tmp_path / "articles.db"),
            **BASE_KWARGS,
        )
        assert result.written == 3
