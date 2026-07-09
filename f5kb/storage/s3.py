"""S3Storage — boto3 backend for the cloud pipeline."""

from __future__ import annotations

import gzip
import json
from typing import Any

import boto3
import botocore.exceptions

from f5kb.storage.base import StorageBackend


class S3Storage(StorageBackend):
    """S3-backed storage.  Credentials come from the Lambda execution role;
    no explicit key/secret is ever stored in code."""

    def __init__(self, bucket: str, prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._s3 = boto3.client("s3")

    def _key(self, key: str) -> str:
        k = key.lstrip("/")
        return f"{self._prefix}/{k}" if self._prefix else k

    def _strip_prefix(self, full_key: str) -> str:
        if self._prefix and full_key.startswith(self._prefix + "/"):
            return full_key[len(self._prefix) + 1:]
        return full_key

    # ── JSON ─────────────────────────────────────────────────────────────────

    def get(self, key: str) -> Any:
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=self._key(key))
            return json.loads(resp["Body"].read().decode("utf-8"))
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise KeyError(key) from e
            raise

    def put(self, key: str, data: Any) -> None:
        body = (json.dumps(data, indent=2) + "\n").encode("utf-8")
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._key(key),
            Body=body,
            ContentType="application/json",
        )

    # ── Bytes ────────────────────────────────────────────────────────────────

    def get_bytes(self, key: str) -> bytes:
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=self._key(key))
            return resp["Body"].read()
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise KeyError(key) from e
            raise

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._key(key),
            Body=data,
            ContentType=content_type,
        )

    # ── Existence / deletion ─────────────────────────────────────────────────

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._key(key))
            return True
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def delete(self, key: str) -> None:
        # delete_object is idempotent — no error if key absent
        self._s3.delete_object(Bucket=self._bucket, Key=self._key(key))

    # ── Listing ──────────────────────────────────────────────────────────────

    def list_prefix(self, prefix: str) -> list[str]:
        """Return sorted storage-relative keys under prefix using the paginator."""
        full_prefix = self._key(prefix)
        paginator = self._s3.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                keys.append(self._strip_prefix(obj["Key"]))
        return sorted(keys)

    # ── Move / markers ───────────────────────────────────────────────────────

    def move(self, src: str, dst: str) -> None:
        """S3 has no rename — copy then delete."""
        self.copy(src, dst)
        self.delete(src)

    def copy(self, src: str, dst: str) -> None:
        """Server-side copy; src is left in place. Raises KeyError if src absent."""
        try:
            self._s3.copy_object(
                Bucket=self._bucket,
                CopySource={"Bucket": self._bucket, "Key": self._key(src)},
                Key=self._key(dst),
            )
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise KeyError(src) from e
            raise

    def put_conditional(self, key: str, data: bytes) -> bool:
        """Create-if-absent via If-None-Match: *.  Returns True if this writer
        created the object, False on 412 PreconditionFailed (already existed)."""
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=self._key(key),
                Body=data,
                IfNoneMatch="*",
            )
            return True
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("PreconditionFailed", "412"):
                return False
            raise

    def put_marker(self, key: str) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._key(key),
            Body=b"",
            ContentType="application/octet-stream",
        )

    # ── JSONL ────────────────────────────────────────────────────────────────

    def append_jsonl(self, key: str, entry: dict) -> None:
        """Single-writer: get existing bytes + append line + put."""
        try:
            existing = self.get_bytes(key).decode("utf-8")
        except KeyError:
            existing = ""
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        self.put_bytes(key, (existing + line).encode("utf-8"), content_type="application/x-ndjson")

    # ── Hash index ───────────────────────────────────────────────────────────

    def load_hash_index(self, key: str) -> dict[str, str]:
        try:
            compressed = self.get_bytes(key)
            return json.loads(gzip.decompress(compressed).decode("utf-8"))
        except KeyError:
            return {}

    def save_hash_index(self, index: dict[str, str], key: str) -> None:
        data = json.dumps(index, separators=(",", ":")).encode("utf-8")
        self.put_bytes(key, gzip.compress(data), content_type="application/gzip")
