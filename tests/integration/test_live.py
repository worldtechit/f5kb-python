"""Live integration tests — require Coveo network access.

Run with: uv run pytest -m live
Skip by default (addopts = -m 'not live').
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from f5kb.coveo.aura import fetch_coveo_config
from f5kb.coveo.client import CoveoClient
from f5kb.coveo.paging import fetch_type_since


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def coveo_client():
    config = fetch_coveo_config()
    return CoveoClient(config, sleep=__import__("time").sleep)


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.live
def test_fetch_coveo_config_returns_org_id():
    """fetch_coveo_config() returns a non-empty org ID string."""
    config = fetch_coveo_config()
    assert config.organization_id
    assert isinstance(config.organization_id, str)
    assert len(config.organization_id) > 5


@pytest.mark.live
def test_list_facet_values_returns_types(coveo_client):
    """list_facet_values('@f5_document_type') returns at least one type."""
    values = coveo_client.list_facet_values("@f5_document_type")
    assert len(values) >= 1
    names = [v.get("value") or v.get("lookupValue") for v in values]
    assert any(n for n in names)


@pytest.mark.live
def test_fetch_type_since_returns_limit(coveo_client):
    """fetch_type_since with limit=3 returns exactly 3 Knowledge articles."""
    import time
    results = fetch_type_since(
        coveo_client, "Knowledge",
        cutoff_ms=0,
        end_ms=9_999_999_999_000,
        page_size=10,
        limit=3,
        on_progress=lambda n: None,
        apply_mod_filter=False,
    )
    assert len(results) == 3


@pytest.mark.live
def test_fetch_results_have_required_keys(coveo_client):
    """Each result from Coveo has title and raw fields."""
    results = fetch_type_since(
        coveo_client, "Knowledge",
        cutoff_ms=0,
        end_ms=9_999_999_999_000,
        page_size=5,
        limit=2,
        on_progress=lambda n: None,
        apply_mod_filter=False,
    )
    for r in results:
        assert "title" in r
        assert "raw" in r
        assert r["raw"]


@pytest.mark.live
def test_dump_cmd_writes_json_files(tmp_path):
    """f5kb dump --all --types=Knowledge --limit=3 writes 3 JSON files."""
    result = subprocess.run(
        [
            sys.executable, "-m", "f5kb", "dump",
            "--all",
            "--types=Knowledge",
            "--limit=3",
            f"--out={tmp_path}/dump",
        ],
        capture_output=True, text=True,
        cwd=Path(__file__).parent.parent,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    written = list((tmp_path / "dump" / "Knowledge").glob("*.json"))
    assert len(written) == 3
    for f in written:
        data = json.loads(f.read_text())
        assert "id" in data
        assert "title" in data
        assert "metadata" in data
        assert "content" in data
