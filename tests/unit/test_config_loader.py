"""Tests for config/loader.py and config/types.py."""

from pathlib import Path

import pytest

from f5kb.config.loader import load_config, load_field_descriptions_file, types_for_lambda
from f5kb.config.types import AppConfig, normalize_type


def test_normalize_type_defaults():
    tc = normalize_type({})
    assert tc.document_type == ""
    assert tc.metadata == "*"
    assert tc.content == []


def test_normalize_type_explicit():
    tc = normalize_type({
        "documentType": "Knowledge",
        "metadata": ["f5_kb_id", "f5_title"],
        "content": ["sfdetails__c"],
    })
    assert tc.document_type == "Knowledge"
    assert tc.metadata == ["f5_kb_id", "f5_title"]
    assert tc.content == ["sfdetails__c"]


def test_load_config_minimal(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
types:
  Knowledge:
    documentType: "Knowledge"
    metadata: "*"
    content: []
field_descriptions:
  f5_kb_id: "K-number identifier"
""")
    cfg = load_config(str(cfg_file))
    assert isinstance(cfg, AppConfig)
    assert "Knowledge" in cfg.types
    assert cfg.types["Knowledge"].document_type == "Knowledge"
    assert cfg.field_descriptions["f5_kb_id"] == "K-number identifier"
    assert cfg.products.entries == ()


def test_load_config_missing_sections(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("types: {}\n")
    cfg = load_config(str(cfg_file))
    assert cfg.field_descriptions == {}
    assert cfg.products.entries == ()


def test_load_config_products(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
types: {}
products:
  generatedAt: "2024-01-01"
  entries:
    - product: "BIG-IP"
      count: 1000
      source: "Coveo facet"
""")
    cfg = load_config(str(cfg_file))
    assert cfg.products.generated_at == "2024-01-01"
    assert len(cfg.products.entries) == 1
    assert cfg.products.entries[0].product == "BIG-IP"
    assert cfg.products.entries[0].count == 1000


def test_load_real_config():
    """Smoke test against the actual config.yaml."""
    cfg_path = Path(__file__).parent.parent.parent / "config.yaml"
    if not cfg_path.exists():
        pytest.skip("config.yaml not found")
    cfg = load_config(str(cfg_path))
    assert len(cfg.types) > 0
    assert len(cfg.field_descriptions) > 0


def test_types_for_lambda_shape(tmp_path):
    """The lambda/config/types.json payload: {type_key: {documentType,
    metadata, content}} with the wildcard kept as the string "*"."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
types:
  Support_Solution:
    documentType: "Support Solution"
    metadata:
      - f5_kb_id
      - f5_title
    content:
      - sfdetails__c
  Video:
    documentType: "Video"
    metadata: "*"
""")
    out = types_for_lambda(str(cfg_file))
    assert out["Support_Solution"] == {
        "documentType": "Support Solution",
        "metadata": ["f5_kb_id", "f5_title"],
        "content": ["sfdetails__c"],
    }
    assert out["Video"]["metadata"] == "*"
    assert out["Video"]["content"] == []


def test_types_for_lambda_real_config_covers_run_types():
    """Every type key the pipeline fans out must exist in config.yaml with the
    real (space-containing) Coveo documentType — the underscored key never
    matches Coveo's f5_document_type filter for multi-word types."""
    cfg_path = Path(__file__).parent.parent.parent / "config.yaml"
    if not cfg_path.exists():
        pytest.skip("config.yaml not found")
    out = types_for_lambda(str(cfg_path))
    from f5kb.handlers.orchestrator import ALL_TYPES
    for type_key in ALL_TYPES:
        assert type_key in out, f"config.yaml missing type {type_key}"
        assert out[type_key]["documentType"], f"{type_key} missing documentType"


def test_load_field_descriptions_bare_map(tmp_path):
    f = tmp_path / "fields.yaml"
    f.write_text("f5_title: 'Article title'\nf5_kb_id: 'K-number'\n")
    result = load_field_descriptions_file(str(f))
    assert result["f5_title"] == "Article title"


def test_load_field_descriptions_nested(tmp_path):
    f = tmp_path / "fields.yaml"
    f.write_text("descriptions:\n  f5_title: 'Title'\n")
    result = load_field_descriptions_file(str(f))
    assert result["f5_title"] == "Title"


def test_load_field_descriptions_missing():
    result = load_field_descriptions_file("/nonexistent/path.yaml")
    assert result == {}
