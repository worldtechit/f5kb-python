"""Regression tests: article JSON schema contracts.

These tests assert that dump_mini fixture articles have the expected shape.
Any structural change to the dump format will break these tests intentionally.
"""

import json
from pathlib import Path

FIXTURES = Path(__file__).parent.parent / "fixtures" / "dump_mini"

REQUIRED_TOP_LEVEL = {"id", "documentType", "title", "link", "capturedAt", "metadata", "content"}
REQUIRED_METADATA_FIELDS = {"permanentid", "excerpt"}


def _load(rel: str) -> dict:
    return json.loads((FIXTURES / rel).read_text())


# ---- top-level shape ----

def test_knowledge_article_top_level_keys():
    art = _load("Knowledge/K14448.json")
    assert REQUIRED_TOP_LEVEL <= set(art.keys())


def test_bug_tracker_article_top_level_keys():
    rel = "Bug_Tracker/ac8677dca4ef6ee1ebbad29b7eb407dee0d138d3260d78886cb678c18a64.json"
    art = _load(rel)
    assert REQUIRED_TOP_LEVEL <= set(art.keys())


def test_all_fixture_articles_have_required_keys():
    missing = []
    for f in FIXTURES.rglob("*.json"):
        if f.name.startswith("_"):
            continue
        art = json.loads(f.read_text())
        absent = REQUIRED_TOP_LEVEL - set(art.keys())
        if absent:
            missing.append((f.name, absent))
    assert not missing, f"Articles missing required keys: {missing}"


# ---- field types ----

def test_knowledge_id_is_string():
    art = _load("Knowledge/K14448.json")
    assert isinstance(art["id"], str)
    assert art["id"]


def test_knowledge_document_type_is_string():
    art = _load("Knowledge/K14448.json")
    assert isinstance(art["documentType"], str)


def test_knowledge_metadata_is_dict():
    art = _load("Knowledge/K14448.json")
    assert isinstance(art["metadata"], dict)


def test_knowledge_content_is_dict():
    art = _load("Knowledge/K14448.json")
    assert isinstance(art["content"], dict)


def test_captured_at_is_iso_string():
    art = _load("Knowledge/K14448.json")
    ts = art["capturedAt"]
    assert isinstance(ts, str)
    assert "T" in ts and "Z" in ts


# ---- document type values ----

def test_known_document_types_in_fixture():
    types_seen = set()
    for f in FIXTURES.rglob("*.json"):
        if f.name.startswith("_"):
            continue
        art = json.loads(f.read_text())
        types_seen.add(art.get("documentType"))
    expected_types = {
        "Knowledge", "Bug Tracker", "Manual", "Support Solution",
        "Security Advisory", "Known Issue", "Release Note",
        "Supplemental Document", "Operations Guide", "Education",
        "Compliance", "Video", "Policy",
    }
    assert expected_types <= types_seen


# ---- Bug_Tracker enriched body shape ----

def test_bug_tracker_content_has_sections():
    rel = "Bug_Tracker/ac8677dca4ef6ee1ebbad29b7eb407dee0d138d3260d78886cb678c18a64.json"
    art = _load(rel)
    content = art["content"]
    assert "sections" in content
    assert isinstance(content["sections"], dict)
    assert len(content["sections"]) > 0


def test_bug_tracker_content_has_body_text():
    rel = "Bug_Tracker/ac8677dca4ef6ee1ebbad29b7eb407dee0d138d3260d78886cb678c18a64.json"
    art = _load(rel)
    assert "body_text" in art["content"]
    assert isinstance(art["content"]["body_text"], str)
    assert len(art["content"]["body_text"]) > 0
