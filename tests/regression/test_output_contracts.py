"""Regression tests: output shape contracts for track, status, and changelog.

These tests assert that the JSON-serializable output structures produced by
core library functions have the expected keys and types. Breaking changes to
output shape will fail here.
"""

import json
from pathlib import Path

from f5kb.lib.changelog import Changelog
from f5kb.lib.status import TypeStatus, compute_status
from f5kb.track.db import TrackSummary, track_dump

DUMP_MINI = str(Path(__file__).parent.parent / "fixtures" / "dump_mini")

TRACK_SUMMARY_KEYS = {"run_id", "db", "dump", "types", "scanned", "new", "changed", "unchanged", "removed", "per_type"}
PER_TYPE_KEYS = {"scanned", "new", "changed"}
STATUS_REPORT_KEYS = {"dump", "db", "db_present", "per_type", "overall", "error_classes", "notes"}
STATUS_OVERALL_KEYS = {
    "totalArticles", "bodied", "health", "lastRun", "newestCapturedAt",
    "stalenessMs", "changelogPath", "changelogLastRun", "pendingApproval",
}
TYPE_STATUS_KEYS = {"type_key", "disk_count", "expected", "written", "status", "bodied", "errors"}
CHANGELOG_ENTRY_KEYS = {"runId", "ts", "op", "documentType", "id", "title", "source"}


# ---- track_dump output shape ----

def test_track_summary_has_required_fields(tmp_path):
    result = track_dump(DUMP_MINI, db_path=str(tmp_path / "t.db"), run_id="regtest")
    assert isinstance(result, TrackSummary)
    assert TRACK_SUMMARY_KEYS == set(result.__dict__.keys())


def test_track_summary_field_types(tmp_path):
    result = track_dump(DUMP_MINI, db_path=str(tmp_path / "t.db"), run_id="regtest")
    assert isinstance(result.run_id, str)
    assert isinstance(result.types, int)
    assert isinstance(result.scanned, int)
    assert isinstance(result.new, int)
    assert isinstance(result.changed, int)
    assert isinstance(result.unchanged, int)
    assert isinstance(result.removed, int)
    assert isinstance(result.per_type, dict)


def test_track_summary_counts_from_mini(tmp_path):
    result = track_dump(DUMP_MINI, db_path=str(tmp_path / "t.db"), run_id="regtest")
    assert result.scanned == 25
    assert result.types == 13
    assert result.new == 25
    assert result.changed == 0
    assert result.unchanged == 0
    assert result.removed == 0


def test_track_per_type_entry_shape(tmp_path):
    result = track_dump(DUMP_MINI, db_path=str(tmp_path / "t.db"), run_id="regtest")
    assert "Knowledge" in result.per_type
    pt = result.per_type["Knowledge"]
    assert PER_TYPE_KEYS == set(pt.__dict__.keys())
    assert isinstance(pt.scanned, int)
    assert isinstance(pt.new, int)
    assert isinstance(pt.changed, int)


def test_track_per_type_knowledge_count(tmp_path):
    result = track_dump(DUMP_MINI, db_path=str(tmp_path / "t.db"), run_id="regtest")
    assert result.per_type["Knowledge"].scanned == 2
    assert result.per_type["Knowledge"].new == 2


# ---- status output shape ----

def test_status_report_has_required_fields(tmp_path):
    report = compute_status(DUMP_MINI, str(tmp_path / "t.db"))
    assert STATUS_REPORT_KEYS == set(report.__dict__.keys())


def test_status_overall_has_required_keys(tmp_path):
    report = compute_status(DUMP_MINI, str(tmp_path / "t.db"))
    assert STATUS_OVERALL_KEYS <= set(report.overall.keys())


def test_status_per_type_entries_shape(tmp_path):
    report = compute_status(DUMP_MINI, str(tmp_path / "t.db"))
    assert len(report.per_type) > 0
    for pt in report.per_type:
        assert isinstance(pt, TypeStatus)
        assert TYPE_STATUS_KEYS == set(pt.__dict__.keys())


def test_status_per_type_count_matches_fixture(tmp_path):
    report = compute_status(DUMP_MINI, str(tmp_path / "t.db"))
    assert len(report.per_type) == 13


def test_status_db_missing_gives_partial(tmp_path):
    report = compute_status(DUMP_MINI, str(tmp_path / "missing.db"))
    assert report.db_present is False
    assert report.overall["health"] == "PARTIAL"


def test_status_notes_is_list(tmp_path):
    report = compute_status(DUMP_MINI, str(tmp_path / "t.db"))
    assert isinstance(report.notes, list)


# ---- changelog entry shape ----

def test_changelog_entry_has_required_keys(tmp_path):
    cl_path = str(tmp_path / "cl.jsonl")
    cl = Changelog(cl_path, run_id="regtest")
    cl.record("added", "Knowledge", "K001", title="Test Article", source="track")
    cl.flush()
    entry = json.loads(Path(cl_path).read_text().strip().splitlines()[0])
    assert CHANGELOG_ENTRY_KEYS <= set(entry.keys())


def test_changelog_op_values(tmp_path):
    cl_path = str(tmp_path / "cl.jsonl")
    cl = Changelog(cl_path, run_id="regtest")
    for op in ("added", "edited", "removed", "approved", "rejected"):
        cl.record(op, "Knowledge", f"K{op}", title="T", source="track")
    cl.flush()
    entries = [json.loads(line) for line in Path(cl_path).read_text().strip().splitlines()]
    ops = {e["op"] for e in entries}
    assert ops == {"added", "edited", "removed", "approved", "rejected"}


def test_changelog_entry_field_types(tmp_path):
    cl_path = str(tmp_path / "cl.jsonl")
    cl = Changelog(cl_path, run_id="regtest")
    cl.record("added", "Knowledge", "K001", title="Test", source="track")
    cl.flush()
    entry = json.loads(Path(cl_path).read_text().strip())
    assert isinstance(entry["runId"], str)
    assert isinstance(entry["ts"], str)
    assert isinstance(entry["op"], str)
    assert isinstance(entry["documentType"], str)
    assert isinstance(entry["id"], str)
