"""SQLite schema + walk/upsert/classify flow for change tracking.

Schema and upsert SQL are byte-identical to the TypeScript implementation so the
existing outputs/articles.db remains valid across runs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from f5kb.lib.logger import Logger, NULL_LOGGER
from f5kb.lib.fsutil import iso_now, list_type_dirs, path_exists, read_json, walk_article_files
from f5kb.track.hashing import Record_, to_record


INIT_SQL = """
    CREATE TABLE IF NOT EXISTS articles (
      document_type TEXT NOT NULL,
      id TEXT NOT NULL,
      title TEXT, link TEXT,
      created_ms INTEGER, original_published_ms INTEGER,
      updated_published_ms INTEGER, modified_ms INTEGER,
      captured_at TEXT,
      metadata_hash TEXT, content_hash TEXT,
      has_body INTEGER, body_error TEXT,
      first_seen_run TEXT, last_seen_run TEXT, last_changed_run TEXT,
      PRIMARY KEY (document_type, id)
    );
    CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY,
      ran_at TEXT, dump_dir TEXT, types TEXT,
      scanned INTEGER, new INTEGER, changed INTEGER, unchanged INTEGER, removed INTEGER
    );
    CREATE TABLE IF NOT EXISTS changes (
      run_id TEXT, document_type TEXT, id TEXT, change_type TEXT, detail TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
    CREATE INDEX IF NOT EXISTS idx_articles_seen ON articles(last_seen_run);
"""

UPSERT_SQL = """
    INSERT INTO articles (
      document_type, id, title, link, created_ms, original_published_ms,
      updated_published_ms, modified_ms, captured_at, metadata_hash, content_hash,
      has_body, body_error, first_seen_run, last_seen_run, last_changed_run
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(document_type, id) DO UPDATE SET
      title=excluded.title, link=excluded.link, created_ms=excluded.created_ms,
      original_published_ms=excluded.original_published_ms,
      updated_published_ms=excluded.updated_published_ms,
      modified_ms=excluded.modified_ms,
      captured_at=excluded.captured_at, metadata_hash=excluded.metadata_hash,
      content_hash=excluded.content_hash, has_body=excluded.has_body,
      body_error=excluded.body_error, last_seen_run=excluded.last_seen_run,
      last_changed_run=excluded.last_changed_run
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(INIT_SQL)


def diff_fields(prev: dict, rec: Record_) -> list[str]:
    changed = []
    if prev.get("metadata_hash") != rec.metadata_hash:
        changed.append("metadata")
    if prev.get("content_hash") != rec.content_hash:
        changed.append("content")
    if prev.get("updated_published_ms") != rec.updated_published_ms:
        changed.append("updated_published")
    if prev.get("modified_ms") != rec.modified_ms:
        changed.append("modified")
    if (prev.get("body_error") or None) != (rec.body_error or None):
        changed.append("body_error")
    return changed


def load_hash_index(db_path: str) -> dict[str, str]:
    """Load {' <id>': metadata_hash} map for incremental skip."""
    out: dict[str, str] = {}
    if not path_exists(db_path):
        return out
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        with conn:
            rows = conn.execute(
                "SELECT document_type AS dt, id, metadata_hash AS mh FROM articles"
            ).fetchall()
        for r in rows:
            out[f"{r['dt']} {r['id']}"] = r["mh"]
        conn.close()
    except Exception:
        pass
    return out


def load_last_run_at(db_path: str) -> dict | None:
    """Return {run_id, ran_at_ms} for the most recent run, or None."""
    if not path_exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT run_id, ran_at FROM runs ORDER BY ran_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return None
        import datetime
        try:
            dt = datetime.datetime.fromisoformat(row["ran_at"].replace("Z", "+00:00"))
            ran_at_ms = int(dt.timestamp() * 1000)
        except Exception:
            ran_at_ms = None
        return {"run_id": row["run_id"], "ran_at_ms": ran_at_ms}
    except Exception:
        return None


def load_ids_by_type(db_path: str, document_types: list[str]) -> dict[str, list[str]]:
    """Return {document_type: [id, ...]} for reconcile diffing."""
    out = {dt: [] for dt in document_types}
    if not path_exists(db_path):
        return out
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for dt in document_types:
            rows = conn.execute(
                "SELECT id FROM articles WHERE document_type=?", (dt,)
            ).fetchall()
            out[dt] = [r["id"] for r in rows]
        conn.close()
    except Exception:
        pass
    return out


def delete_rows(db_path: str, rows: list[dict[str, str]]) -> int:
    """Delete article rows by (document_type, id). Returns count removed."""
    if not rows:
        return 0
    conn = sqlite3.connect(db_path)
    n = 0
    try:
        with conn:
            for r in rows:
                cur = conn.execute(
                    "DELETE FROM articles WHERE document_type=? AND id=?",
                    (r["documentType"], r["id"]),
                )
                n += cur.rowcount
    finally:
        conn.close()
    return n


@dataclass
class PerTypeStat:
    scanned: int = 0
    new: int = 0
    changed: int = 0


@dataclass
class TrackSummary:
    run_id: str
    db: str
    dump: str
    types: int
    scanned: int
    new: int
    changed: int
    unchanged: int
    removed: int
    per_type: dict[str, PerTypeStat] = field(default_factory=dict)


def track_dump(
    dump: str,
    db_path: str | None = None,
    types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    run_id: str | None = None,
    logger: Logger = NULL_LOGGER,
    changelog=None,
) -> TrackSummary:
    log = logger
    dump_path = str(dump).rstrip("/")
    db_file = db_path or str(Path(dump_path).parent / "articles.db")
    run = run_id or iso_now()
    exclude = set(exclude_types or [])

    try:
        type_keys = list_type_dirs(dump_path)
    except Exception as e:
        raise RuntimeError(f"Cannot read dump dir {dump_path}: {e}")

    if types:
        type_keys = [t for t in type_keys if t in types]
    if exclude:
        type_keys = [t for t in type_keys if t not in exclude]
    if not type_keys:
        raise RuntimeError(f"No type directories to index under {dump_path}.")

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    scanned = n_new = n_changed = n_unchanged = 0
    per_type: dict[str, PerTypeStat] = {}

    with conn:
        for type_key in type_keys:
            type_dir = f"{dump_path}/{type_key}"
            per_type[type_key] = PerTypeStat()
            for file_path in walk_article_files(type_dir):
                try:
                    a = read_json(file_path)
                except Exception as e:
                    log.warn(f"skip unreadable {file_path}: {e}")
                    continue
                rec = to_record(a)
                if not rec.id:
                    continue
                scanned += 1
                per_type[type_key].scanned += 1

                prev = conn.execute(
                    "SELECT * FROM articles WHERE document_type=? AND id=?",
                    (rec.document_type, rec.id),
                ).fetchone()

                if prev is None:
                    last_changed = run
                    n_new += 1
                    per_type[type_key].new += 1
                    conn.execute(
                        "INSERT INTO changes (run_id,document_type,id,change_type,detail) VALUES (?,?,?,?,?)",
                        (run, rec.document_type, rec.id, "new", ""),
                    )
                    if changelog:
                        changelog.record("added", rec.document_type, rec.id, title=rec.title, source="track")
                    first_seen = run
                else:
                    prev_dict = dict(prev)
                    diff = diff_fields(prev_dict, rec)
                    if diff:
                        last_changed = run
                        n_changed += 1
                        per_type[type_key].changed += 1
                        conn.execute(
                            "INSERT INTO changes (run_id,document_type,id,change_type,detail) VALUES (?,?,?,?,?)",
                            (run, rec.document_type, rec.id, "changed", ",".join(diff)),
                        )
                        if changelog:
                            changelog.record("edited", rec.document_type, rec.id, title=rec.title, changed=diff, source="track")
                    else:
                        last_changed = prev_dict.get("last_changed_run") or run
                        n_unchanged += 1
                    first_seen = prev_dict.get("first_seen_run") or run

                conn.execute(UPSERT_SQL, (
                    rec.document_type, rec.id, rec.title, rec.link,
                    rec.created_ms, rec.original_published_ms,
                    rec.updated_published_ms, rec.modified_ms, rec.captured_at,
                    rec.metadata_hash, rec.content_hash, rec.has_body, rec.body_error,
                    first_seen, run, last_changed,
                ))

        # Detect removed: rows in scanned types not touched this run
        placeholders = ",".join("?" * len(type_keys))
        removed_rows = conn.execute(
            f"SELECT document_type, id FROM articles WHERE document_type IN ({placeholders}) AND last_seen_run!=?",
            (*type_keys, run),
        ).fetchall()
        for r in removed_rows:
            conn.execute(
                "INSERT INTO changes (run_id,document_type,id,change_type,detail) VALUES (?,?,?,?,?)",
                (run, r["document_type"], r["id"], "removed", ""),
            )
        removed = len(removed_rows)

        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id,ran_at,dump_dir,types,scanned,new,changed,unchanged,removed) VALUES (?,?,?,?,?,?,?,?,?)",
            (run, iso_now(), dump_path, ",".join(type_keys), scanned, n_new, n_changed, n_unchanged, removed),
        )

    conn.close()

    log.info(f"Indexed {scanned} articles across {len(type_keys)} type(s) -> {db_file}")
    log.info(f"  new={n_new} changed={n_changed} unchanged={n_unchanged} removed={removed} (run {run})")

    return TrackSummary(
        run_id=run,
        db=db_file,
        dump=dump_path,
        types=len(type_keys),
        scanned=scanned,
        new=n_new,
        changed=n_changed,
        unchanged=n_unchanged,
        removed=removed,
        per_type=per_type,
    )
