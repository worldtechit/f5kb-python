"""Tests for StorageBackend — LocalStorage (tmp_path) and S3Storage (moto)."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from f5kb.storage import LocalStorage, S3Storage

# ══════════════════════════════════════════════════════════════════════════════
#  LocalStorage
# ══════════════════════════════════════════════════════════════════════════════

def test_local_put_get(tmp_path):
    store = LocalStorage(tmp_path)
    store.put("foo/bar.json", {"a": 1})
    assert store.get("foo/bar.json") == {"a": 1}


def test_local_get_missing_raises(tmp_path):
    store = LocalStorage(tmp_path)
    with pytest.raises(KeyError):
        store.get("missing.json")


def test_local_put_creates_parents(tmp_path):
    store = LocalStorage(tmp_path)
    store.put("deep/nested/x.json", {"x": 2})
    assert (tmp_path / "deep" / "nested" / "x.json").exists()


def test_local_put_trailing_newline(tmp_path):
    store = LocalStorage(tmp_path)
    store.put("x.json", {"k": "v"})
    text = (tmp_path / "x.json").read_text()
    assert text.endswith("\n")
    assert "\n" in text  # pretty-printed


def test_local_exists_true_and_false(tmp_path):
    store = LocalStorage(tmp_path)
    assert not store.exists("a.json")
    store.put("a.json", {})
    assert store.exists("a.json")


def test_local_delete(tmp_path):
    store = LocalStorage(tmp_path)
    store.put("a.json", {})
    store.delete("a.json")
    assert not store.exists("a.json")


def test_local_delete_missing_noop(tmp_path):
    store = LocalStorage(tmp_path)
    store.delete("nonexistent.json")  # must not raise


def test_local_put_get_bytes(tmp_path):
    store = LocalStorage(tmp_path)
    store.put_bytes("raw.bin", b"\x00\x01\x02")
    assert store.get_bytes("raw.bin") == b"\x00\x01\x02"


def test_local_get_bytes_missing_raises(tmp_path):
    store = LocalStorage(tmp_path)
    with pytest.raises(KeyError):
        store.get_bytes("missing.bin")


def test_local_list_prefix(tmp_path):
    store = LocalStorage(tmp_path)
    store.put("a/b/1.json", {})
    store.put("a/b/2.json", {})
    store.put("c/3.json", {})
    result = store.list_prefix("a/")
    assert sorted(result) == ["a/b/1.json", "a/b/2.json"]


def test_local_list_prefix_empty(tmp_path):
    store = LocalStorage(tmp_path)
    assert store.list_prefix("no/such/prefix/") == []


def test_local_move(tmp_path):
    store = LocalStorage(tmp_path)
    store.put("src.json", {"v": 1})
    store.move("src.json", "dst/dst.json")
    assert not store.exists("src.json")
    assert store.get("dst/dst.json") == {"v": 1}


def test_local_put_marker(tmp_path):
    store = LocalStorage(tmp_path)
    store.put_marker("runs/2026-07-01/track/_done")
    assert store.exists("runs/2026-07-01/track/_done")
    assert (tmp_path / "runs" / "2026-07-01" / "track" / "_done").read_bytes() == b""


def test_local_append_jsonl(tmp_path):
    store = LocalStorage(tmp_path)
    store.append_jsonl("out.jsonl", {"a": 1})
    store.append_jsonl("out.jsonl", {"b": 2})
    lines = (tmp_path / "out.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": 2}


def test_local_append_jsonl_creates_parents(tmp_path):
    store = LocalStorage(tmp_path)
    store.append_jsonl("deep/path/out.jsonl", {"x": 1})
    assert (tmp_path / "deep" / "path" / "out.jsonl").exists()


def test_local_hash_index_roundtrip(tmp_path):
    store = LocalStorage(tmp_path)
    index = {"Solution K001": "abc123", "Manual K002": "def456"}
    store.save_hash_index(index, "hash-index/current.json.gz")
    loaded = store.load_hash_index("hash-index/current.json.gz")
    assert loaded == index


def test_local_hash_index_missing_returns_empty(tmp_path):
    store = LocalStorage(tmp_path)
    assert store.load_hash_index("hash-index/current.json.gz") == {}


# ══════════════════════════════════════════════════════════════════════════════
#  S3Storage  (moto mock_aws)
# ══════════════════════════════════════════════════════════════════════════════

BUCKET = "test-f5kb"


@pytest.fixture
def s3_bucket():
    """Create a moto-mocked S3 bucket and yield the bucket name."""
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield BUCKET


def test_s3_put_get(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        store.put("foo/bar.json", {"a": 1})
        assert store.get("foo/bar.json") == {"a": 1}


def test_s3_get_missing_raises(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        with pytest.raises(KeyError):
            store.get("missing.json")


def test_s3_exists(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        assert not store.exists("a.json")
        store.put("a.json", {})
        assert store.exists("a.json")


def test_s3_delete(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        store.put("a.json", {})
        store.delete("a.json")
        assert not store.exists("a.json")


def test_s3_delete_missing_noop(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        store.delete("nonexistent.json")  # must not raise


def test_s3_put_get_bytes(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        store.put_bytes("raw.bin", b"\xff\xfe")
        assert store.get_bytes("raw.bin") == b"\xff\xfe"


def test_s3_get_bytes_missing_raises(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        with pytest.raises(KeyError):
            store.get_bytes("missing.bin")


def test_s3_list_prefix(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        store.put("pending/Solution/K001.json", {})
        store.put("pending/Solution/K002.json", {})
        store.put("pending/_manifest.json", {})
        result = store.list_prefix("pending/Solution/")
        assert "pending/Solution/K001.json" in result
        assert "pending/Solution/K002.json" in result
        assert "pending/_manifest.json" not in result


def test_s3_list_prefix_empty(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        assert store.list_prefix("no/such/prefix/") == []


def test_s3_move(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        store.put("src.json", {"v": 99})
        store.move("src.json", "dst.json")
        assert not store.exists("src.json")
        assert store.get("dst.json") == {"v": 99}


def test_s3_put_marker(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        store.put_marker("runs/2026-07-01/track/_done")
        assert store.exists("runs/2026-07-01/track/_done")
        assert store.get_bytes("runs/2026-07-01/track/_done") == b""


def test_s3_append_jsonl(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        store.append_jsonl("out.jsonl", {"a": 1})
        store.append_jsonl("out.jsonl", {"b": 2})
        raw = store.get_bytes("out.jsonl").decode("utf-8")
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"a": 1}
        assert json.loads(lines[1]) == {"b": 2}


def test_s3_hash_index_roundtrip(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        index = {"Solution K001": "abc", "Manual K002": "def"}
        store.save_hash_index(index, "hash-index/current.json.gz")
        loaded = store.load_hash_index("hash-index/current.json.gz")
        assert loaded == index


def test_s3_hash_index_missing_returns_empty(s3_bucket):
    with mock_aws():
        store = S3Storage(s3_bucket)
        assert store.load_hash_index("hash-index/current.json.gz") == {}


def test_s3_prefix_option(s3_bucket):
    """S3Storage with prefix= strips the prefix from returned list keys."""
    with mock_aws():
        store = S3Storage(s3_bucket, prefix="staging")
        store.put("pending/K001.json", {"x": 1})
        result = store.list_prefix("pending/")
        assert "pending/K001.json" in result
        assert store.get("pending/K001.json") == {"x": 1}
