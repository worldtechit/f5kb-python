"""ui/readers.py — corpus views must come from the hash index, not S3 LIST sweeps.

The console once counted the corpus by paginating list_objects_v2 over every
live/<Type>/ prefix (100+ sequential round-trips for ~106k articles). These
tests pin the replacement behavior:

- counts + per-type key lists derive from hash-index/current.json.gz (one GET)
- an absent index falls back to listing the store
- refresh=True re-lists the store (ground truth), ignoring the index
- pending/ listings are cached between polls
- the SWR cache serves stale values instead of blocking requests
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time

UI = pathlib.Path(__file__).resolve().parents[2] / "ui"
if str(UI) not in sys.path:
    sys.path.insert(0, str(UI))

import readers  # noqa: E402
from readers import Reader, StoreReader, _TTLCache  # noqa: E402
from runview import build_run_detail  # noqa: E402


class FakeStore:
    """Minimal StorageBackend double: dict-backed objects + a hash index."""

    def __init__(self, objects: dict | None = None, index: dict | None = None) -> None:
        self.objects = objects or {}
        self.index = index if index is not None else {}
        self.list_calls: list[str] = []
        self.index_loads = 0
        self.appended: list[tuple[str, dict]] = []

    def get(self, key):
        if key in self.objects:
            return self.objects[key]
        raise KeyError(key)

    def get_bytes(self, key):
        if key in self.objects:
            v = self.objects[key]
            if isinstance(v, str):
                return v.encode("utf-8")
            return json.dumps(v).encode("utf-8")
        raise KeyError(key)

    def list_prefix(self, prefix):
        self.list_calls.append(prefix)
        return sorted(k for k in self.objects if k.startswith(prefix))

    def exists(self, key):
        return key in self.objects

    def load_hash_index(self, key):
        self.index_loads += 1
        return dict(self.index)

    def put(self, key, data):
        self.objects[key] = data

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data.decode("utf-8")

    def save_hash_index(self, index, key):
        self.index = dict(index)

    def delete(self, key):
        self.objects.pop(key, None)

    def delete_many(self, keys):
        for k in keys:
            self.objects.pop(k, None)
        return len(keys)

    def append_jsonl(self, key, entry):
        self.appended.append((key, entry))


# ── corpus from the hash index ────────────────────────────────────────────────
def test_corpus_counts_come_from_hash_index_without_listing():
    store = FakeStore(index={
        "Known_Issue K1": "h1",
        "Known_Issue K2": "h2",
        "Manual m1": "h3",
    })
    r = StoreReader(store)
    assert r.corpus_counts() == {"Known_Issue": 2, "Manual": 1}
    assert store.list_calls == []  # the whole point: no LIST sweep

    # canonical type order is preserved for the frontend
    assert list(r.corpus_counts()) == ["Known_Issue", "Manual"]


def test_article_keys_derive_from_hash_index_sorted():
    store = FakeStore(index={"Known_Issue K2": "h", "Known_Issue K1": "h"})
    r = StoreReader(store)
    assert r.article_keys("Known_Issue") == [
        "live/Known_Issue/K1.json", "live/Known_Issue/K2.json"]
    assert r.article_keys("Manual") == []
    assert store.list_calls == []


def test_hash_index_parsed_once_per_ttl_window():
    store = FakeStore(index={"Manual m1": "h"})
    r = StoreReader(store)
    r.corpus_counts()
    r.article_keys("Manual")
    r.hash_index_stats()
    assert store.index_loads == 1


def test_hash_index_stats_reuses_cached_parse():
    store = FakeStore(index={"Manual m1": "h", "Known_Issue K1": "h"})
    r = StoreReader(store)
    stats = r.hash_index_stats()
    assert stats == {"present": True, "entries": 2, "key": readers.HASH_INDEX_KEY}


def test_missing_index_falls_back_to_listing():
    store = FakeStore(objects={
        "live/Manual/a.json": {"id": "a"},
        "live/Manual/b.json": {"id": "b"},
    })
    r = StoreReader(store)
    assert r.corpus_counts() == {"Manual": 2}
    assert any(c.startswith("live/") for c in store.list_calls)
    assert r.article_keys("Manual") == ["live/Manual/a.json", "live/Manual/b.json"]


def test_refresh_relists_the_store_as_ground_truth():
    # Index says 1 article; the store actually holds 2 (index drift).
    store = FakeStore(
        objects={"live/Manual/a.json": {}, "live/Manual/b.json": {}},
        index={"Manual a": "h"},
    )
    r = StoreReader(store)
    assert r.corpus_counts() == {"Manual": 1}          # cheap index path
    assert r.corpus_counts(refresh=True) == {"Manual": 2}  # LIST ground truth
    assert any(c.startswith("live/Manual/") for c in store.list_calls)


# ── pending/ listing cache ────────────────────────────────────────────────────
def test_pending_entries_listing_is_cached_between_polls():
    store = FakeStore(objects={
        "pending/Manual/a.json": {},
        "pending/Known_Issue/k.json": {},
    })
    r = StoreReader(store)
    first = r.pending_entries(cap=10)
    second = r.pending_entries(cap=10)
    assert first["total"] == second["total"] == 2
    assert {e["type_key"] for e in first["entries"]} == {"Manual", "Known_Issue"}
    assert store.list_calls.count("pending/") == 1


# ── parallel multi-get ────────────────────────────────────────────────────────
def test_get_json_many_maps_missing_keys_to_none():
    store = FakeStore(objects={"a.json": {"x": 1}})
    r = StoreReader(store)
    out = r.get_json_many(["a.json", "missing.json"])
    assert out == {"a.json": {"x": 1}, "missing.json": None}
    assert r.get_json_many([]) == {}


# ── SWR cache ─────────────────────────────────────────────────────────────────
def test_swr_fresh_hit_does_not_reload():
    c = _TTLCache()
    calls = []
    assert c.swr("k", 100, lambda: calls.append(1) or "v1") == "v1"
    assert c.swr("k", 100, lambda: calls.append(1) or "v2") == "v1"
    assert len(calls) == 1


def test_swr_serves_stale_immediately_and_refreshes_in_background():
    c = _TTLCache()
    release = threading.Event()
    calls = []

    def loader():
        calls.append(1)
        if len(calls) > 1:
            release.wait(2)  # background refresh is deliberately slow
        return len(calls)

    assert c.swr("k", 100, loader) == 1  # cold load blocks once
    c.put("k", 1)
    with c._lock:  # age the entry past any TTL
        c._data["k"] = (time.time() - 999, 1)

    t0 = time.time()
    assert c.swr("k", 100, loader) == 1  # stale value, no blocking
    assert time.time() - t0 < 0.5
    release.set()
    for _ in range(200):  # wait for the background thread to land the update
        if c.get("k", 100) == 2:
            break
        time.sleep(0.01)
    assert c.get("k", 100) == 2


# ── resolve_pending (bulk approve/reject from the Review page) ────────────────
def test_resolve_pending_reject_deletes_and_audits():
    store = FakeStore(objects={"pending/Manual/K1.json": {"id": "K1", "metadata": {}}})
    r = StoreReader(store)
    r.writable = True
    res = r.resolve_pending("reject", [{"type_key": "Manual", "id": "K1"},
                                       {"type_key": "Manual", "id": "GONE"}], "tester")
    assert res == {"status": "reject", "done": 1, "missing": 1, "errors": 0, "total": 2}
    assert "pending/Manual/K1.json" not in store.objects
    assert any(e[1].get("op") == "rejected" for e in store.appended)


def test_resolve_pending_approve_promotes_full_protocol():
    store = FakeStore(objects={
        "pending/Manual/K1.json": {"id": "K1", "metadata": {"title": "t"},
                                   "metadata_hash": "mh1"},
        "live/Manual/K1.json": {"id": "K1", "metadata": {"title": "old"}},
    })
    r = StoreReader(store)
    r.writable = True
    res = r.resolve_pending("approve", [{"type_key": "Manual", "id": "K1"}], "tester")
    assert res["done"] == 1 and res["errors"] == 0
    assert store.objects["live/Manual/K1.json"]["metadata"] == {"title": "t"}
    assert "pending/Manual/K1.json" not in store.objects
    # displaced live copy archived + hash index updated
    assert any(k.startswith("archive/Manual/K1/") for k in store.objects)
    assert store.index.get("Manual K1") == "mh1"


def test_resolve_pending_type_approves_everything_one_index_save():
    store = FakeStore(objects={
        "pending/Manual/K1.json": {"id": "K1", "metadata": {}, "metadata_hash": "h1"},
        "pending/Manual/K2.json": {"id": "K2", "metadata": {}, "metadata_hash": "h2"},
        "pending/Known_Issue/K9.json": {"id": "K9", "metadata": {}},  # other type
    })
    r = StoreReader(store)
    r.writable = True
    res = r.resolve_pending_type("approve", "Manual", "tester")
    assert res["done"] == 2 and res["total"] == 2 and res["errors"] == 0
    assert "live/Manual/K1.json" in store.objects
    assert "live/Manual/K2.json" in store.objects
    assert "pending/Manual/K1.json" not in store.objects
    assert "pending/Known_Issue/K9.json" in store.objects  # untouched
    assert store.index == {"Manual K1": "h1", "Manual K2": "h2"}
    # batched audit: changed_ids written once with both lines
    changed_keys = [k for k in store.objects if k.endswith("changed_ids.jsonl")]
    assert len(changed_keys) == 1
    assert store.objects[changed_keys[0]].count('"op":"approved"') == 2


def test_resolve_pending_type_reject_batch_deletes():
    store = FakeStore(objects={
        "pending/Manual/K1.json": {}, "pending/Manual/K2.json": {},
        "live/Manual/K0.json": {},
    })
    r = StoreReader(store)
    r.writable = True
    res = r.resolve_pending_type("reject", "Manual", "tester")
    assert res["done"] == 2
    assert not any(k.startswith("pending/") for k in store.objects)
    assert "live/Manual/K0.json" in store.objects
    assert any(e[1].get("op") == "rejected_type" for e in store.appended)


def test_resolve_pending_requires_writable():
    r = StoreReader(FakeStore())
    import pytest
    with pytest.raises(RuntimeError):
        r.resolve_pending("approve", [{"type_key": "Manual", "id": "K1"}], "tester")


# ── delete_run ────────────────────────────────────────────────────────────────
def _run_store():
    manifest = ('{"id": "K1", "type_key": "Manual"}\n'
                '{"id": "K2", "type_key": "Manual"}\n')
    return FakeStore(objects={
        "runs/2026-07-09/status.json": {"phase": "scrape"},
        "runs/2026-07-09/manifest/Manual.jsonl": manifest,
        "lambda/state/2026-07-09/dump-Manual.json": {"written": 2},
        "pending/Manual/K1.json": {"id": "K1"},          # staged by this run
        "pending/Manual/K9.json": {"id": "K9"},          # staged by ANOTHER run
        "live/Manual/K0.json": {"id": "K0"},             # must never be touched
        "runs/2026-07-08/status.json": {"phase": "done"},  # other run untouched
    })


def test_delete_run_dry_run_counts_and_deletes_nothing():
    store = _run_store()
    r = StoreReader(store)
    before = dict(store.objects)
    res = r.delete_run("2026-07-09", include_pending=True, dry_run=True)
    assert res["status"] == "dry_run"
    # 2 runs/ keys + 1 state key + only K1 pending (K2 not staged, K9 other run)
    assert res["counts"] == {"runs": 2, "state": 1, "pending": 1}
    assert store.objects == before  # nothing deleted


def test_delete_run_removes_only_run_scoped_keys():
    store = _run_store()
    r = StoreReader(store)
    r.writable = True
    res = r.delete_run("2026-07-09", include_pending=True, dry_run=False)
    assert res["status"] == "deleted"
    assert res["total"] == 4
    remaining = set(store.objects)
    assert remaining == {"pending/Manual/K9.json", "live/Manual/K0.json",
                         "runs/2026-07-08/status.json"}
    # audit record written
    assert any(e[1].get("op") == "run_deleted" for e in store.appended)


def test_delete_run_without_pending_keeps_pending():
    store = _run_store()
    r = StoreReader(store)
    r.writable = True
    res = r.delete_run("2026-07-09", include_pending=False, dry_run=False)
    assert res["counts"]["pending"] == 0
    assert "pending/Manual/K1.json" in store.objects


def test_delete_run_refuses_when_read_only():
    r = StoreReader(_run_store())
    assert r.writable is False
    import pytest
    with pytest.raises(RuntimeError):
        r.delete_run("2026-07-09", dry_run=False)


# ── REPORT line parsing (cost panel) ──────────────────────────────────────────
def test_parse_report_line():
    line = ("REPORT RequestId: 894e7e1f-cdfb-564e-b3cf-1cd75de116bf\t"
            "Duration: 123.45 ms\tBilled Duration: 124 ms\t"
            "Memory Size: 1024 MB\tMax Memory Used: 250 MB\t")
    rec = readers.parse_report_line(line)
    assert rec == {"duration_ms": 123.45, "billed_ms": 124,
                   "memory_mb": 1024, "max_memory_mb": 250}
    assert readers.parse_report_line("START RequestId: x Version: $LATEST") is None
    assert readers.parse_report_line("REPORT RequestId: x (no numbers)") is None


# ── ReaderRef proxy (target switching) ────────────────────────────────────────
def test_reader_ref_forwards_and_switches():
    import importlib.util
    spec = importlib.util.spec_from_file_location("f5kb_ui_server", UI / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    a = StoreReader(FakeStore(index={"Manual m1": "h"}))
    b = StoreReader(FakeStore(index={"Manual m1": "h", "Manual m2": "h"}))
    b.writable = True
    ref = server.ReaderRef(a)
    assert ref.corpus_counts() == {"Manual": 1}
    assert ref.writable is False
    ref.switch(b)
    assert ref.corpus_counts() == {"Manual": 2}
    assert ref.writable is True


# ── runview composes from the batched fetch ───────────────────────────────────
class FakeReader(Reader):
    stage = "test"

    def __init__(self, objects: dict, keys: list[str] | None = None,
                 texts: dict | None = None) -> None:
        super().__init__()
        self.objects = objects
        self.keys = keys or []
        self.texts = texts or {}

    def get_json(self, key):
        return self.objects.get(key)

    def read_text(self, key):
        return self.texts.get(key)

    def list_keys(self, prefix):
        return [k for k in self.keys if k.startswith(prefix)]


def test_build_run_detail_returns_none_for_unknown_run():
    rdr = FakeReader(objects={})
    assert build_run_detail(rdr, "2099-01-01") is None


def test_build_run_detail_uses_prefetched_state():
    date = "2026-07-09"
    rdr = FakeReader(
        objects={
            f"lambda/state/{date}/orchestrator.json": {
                "types": ["Known_Issue"], "mode": "full", "started_at": "t0"},
            f"runs/{date}/status.json": {"phase": "scrape"},
            f"runs/{date}/dump/Known_Issue/_index.json": {
                "count_written": 5, "count_server": 5},
        },
        keys=[f"runs/{date}/dump/Known_Issue/_done",
              f"runs/{date}/dump/Known_Issue/_index.json"],
    )
    detail = build_run_detail(rdr, date)
    assert detail["phase"] == "scrape"
    (pt,) = detail["per_type"]
    assert pt["type_key"] == "Known_Issue"
    assert pt["state"] == "done"
    assert pt["dump_count"] == 5
