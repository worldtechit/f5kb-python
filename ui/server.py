"""f5kb console — local web server for the pipeline.

Read (and optionally drive) everything the pipeline produces: runs, the live
corpus, pending edits, held approvals, archives, audit history, queues, and
errors — against the deployed S3 stage or a local tree.

Run:
    uv run --group ui python ui/server.py --target staging
    uv run --group ui python ui/server.py --target staging --allow-writes
    uv run --group ui python ui/server.py --target local

Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import concurrent.futures
import pathlib
import re
import threading
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from readers import (
    BROWSABLE_PREFIXES,
    REPO,
    Reader,
    list_targets,
    load_target,
    page_articles,
    structured_diff,
)
from runview import build_run_detail, run_summary


class ReaderRef:
    """Mutable indirection so /api/actions/switch-target can swap the backing
    reader in place — every route closes over this proxy, and attribute access
    forwards to the current reader."""

    def __init__(self, reader: Reader) -> None:
        object.__setattr__(self, "_r", reader)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_r"), name)

    def switch(self, reader: Reader) -> None:
        object.__setattr__(self, "_r", reader)

HERE = pathlib.Path(__file__).resolve().parent

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_TYPE_RE = re.compile(r"^[A-Za-z0-9_]{1,60}$")


def _check(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not pattern.match(value or ""):
        raise HTTPException(400, f"invalid {what}: {value!r}")
    return value


# ══════════════════════════════════════════════════════════════════════════════
#  Docs — the built-in playbook + the repo's own documentation, rendered
# ══════════════════════════════════════════════════════════════════════════════
DOCS: list[dict[str, str]] = [
    {"name": "playbook", "title": "Console Playbook", "group": "Operate", "path": "ui/playbook.md"},
    {"name": "consumer-guide", "title": "Consumer Guide (SNS/S3 contract)", "group": "Operate", "path": "docs/CONSUMER_GUIDE.md"},
    {"name": "master-pipeline", "title": "Pipeline Master Reference (P1)", "group": "Operate", "path": "docs/MASTER_PIPELINE_DOC.md"},
    {"name": "deployments", "title": "Deployments (AWS stages)", "group": "Operate", "path": "docs/DEPLOYMENTS.md"},
    {"name": "howto", "title": "HOWTO (CLI workflows)", "group": "Toolkit", "path": "docs/HOWTO.md"},
    {"name": "readme", "title": "README (CLI reference)", "group": "Toolkit", "path": "README.md"},
    {"name": "outline", "title": "OUTLINE (architecture)", "group": "Toolkit", "path": "docs/OUTLINE.md"},
    {"name": "findings", "title": "FINDINGS (scraped system)", "group": "Toolkit", "path": "docs/FINDINGS.md"},
    {"name": "memories", "title": "MEMORIES (project memory)", "group": "Toolkit", "path": "docs/MEMORIES.md"},
    {"name": "test-guide", "title": "TEST_GUIDE (running tests)", "group": "Toolkit", "path": "docs/TEST_GUIDE.md"},
    {"name": "todo", "title": "TODO (open work)", "group": "Toolkit", "path": "docs/TODO.md"},
    {"name": "ui-readme", "title": "Console README", "group": "Operate", "path": "ui/README.md"},
]


def render_markdown(text: str) -> str:
    try:
        import markdown
        return markdown.markdown(
            text, extensions=["fenced_code", "tables", "sane_lists", "toc"])
    except ImportError:
        import html
        return f"<pre>{html.escape(text)}</pre>"


# ══════════════════════════════════════════════════════════════════════════════
#  App
# ══════════════════════════════════════════════════════════════════════════════
def make_app(reader: Reader, original_target: str = "",
             allow_writes: bool = False) -> FastAPI:
    app = FastAPI(title="f5kb console", docs_url=None, redoc_url=None)
    current = {"target": original_target}

    def _require_writes() -> None:
        if not reader.writable:
            raise HTTPException(status_code=403,
                                detail="server is read-only — restart with --allow-writes")

    # ── identity / overview ──────────────────────────────────────────────────
    @app.get("/api/config")
    def config() -> dict:
        return reader.label()

    def _summaries(dates: list[str]) -> list[dict]:
        """run_summary does 2-3 store round-trips per date — run them concurrently."""
        if not dates:
            return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(dates))) as ex:
            rows = ex.map(lambda d: {"run_date": d, **run_summary(reader, d)}, dates)
        return list(rows)

    @app.get("/api/overview")
    def overview() -> dict:
        dates = reader.list_run_dates(limit=8)
        runs = _summaries(dates)
        latest: dict[str, Any] | None = None
        if runs:
            latest = dict(runs[0])
            held = reader.get_json(f"lambda/state/{dates[0]}/approve_held.json") or {}
            latest["held"] = held.get("remaining", len(held.get("entries") or []))
        corpus = reader.corpus_counts()
        pending = reader.pending_entries(cap=1)
        return {
            "label": reader.label(),
            "latest": latest,
            "runs": runs,
            "corpus": corpus,
            "corpus_total": sum(corpus.values()),
            "pending_total": pending["total"],
            "dlqs": reader.dlq_depths(),
            "hash_index": reader.hash_index_stats(),
        }

    # ── runs ─────────────────────────────────────────────────────────────────
    @app.get("/api/runs")
    def runs(limit: int = 20) -> list[dict]:
        return _summaries(reader.list_run_dates(limit))

    @app.get("/api/runs/{date}")
    def run_detail(date: str) -> dict:
        _check(_ID_RE, date, "run date")
        d = build_run_detail(reader, date)
        if d is None:
            raise HTTPException(404, f"run {date} not found — deleted or never ran")
        return d

    # ── corpus / articles ────────────────────────────────────────────────────
    @app.get("/api/corpus")
    def corpus(refresh: bool = False) -> dict:
        counts = reader.corpus_counts(refresh=refresh)
        return {"types": reader.corpus_types(), "counts": counts,
                "total": sum(counts.values())}

    @app.get("/api/articles/{type_key}")
    def articles(type_key: str, q: str = "", page: int = 1,
                 size: int = Query(default=25, le=100)) -> dict:
        _check(_TYPE_RE, type_key, "type key")
        return page_articles(reader, type_key, q, page, size)

    @app.get("/api/find")
    def find(id: str) -> dict:
        _check(_ID_RE, id, "article id")
        return {"id": id, "matches": reader.find_article(id)}

    @app.get("/api/article/{type_key}/{art_id}")
    def article(type_key: str, art_id: str) -> dict:
        _check(_TYPE_RE, type_key, "type key")
        _check(_ID_RE, art_id, "article id")
        live = reader.get_article(type_key, art_id)
        pending = reader.get_pending(type_key, art_id)
        if live is None and pending is None:
            raise HTTPException(404, f"article {type_key}/{art_id} not found")
        months = reader.changelog_months()[:3]
        history: list[dict] = []
        for m in months:
            history += [r for r in reader.changelog(month=m, limit=5000)
                        if r.get("id") == art_id]
        return {
            "type_key": type_key, "id": art_id,
            "live": live, "pending": pending,
            "archive": reader.archive_versions(type_key, art_id),
            "history": history[:50],
        }

    @app.get("/api/article/{type_key}/{art_id}/diff")
    def article_diff(type_key: str, art_id: str, archive_key: str = "") -> dict:
        _check(_TYPE_RE, type_key, "type key")
        _check(_ID_RE, art_id, "article id")
        live = reader.get_article(type_key, art_id)
        if archive_key:
            if not archive_key.startswith("archive/"):
                raise HTTPException(400, "archive_key must start with archive/")
            old = reader.get_json(archive_key)
            if old is None:
                raise HTTPException(404, f"no such archive version: {archive_key}")
            return {"kind": "archive-vs-live", "old_label": archive_key,
                    "new_label": "live", **structured_diff(old, live)}
        pending = reader.get_pending(type_key, art_id)
        if pending is None:
            raise HTTPException(404, "no pending version staged for this article")
        return {"kind": "live-vs-pending", "old_label": "live", "new_label": "pending",
                **structured_diff(live, pending)}

    # ── review / pending / history ───────────────────────────────────────────
    @app.get("/api/pending")
    def pending(cap: int = 2000) -> dict:
        return reader.pending_entries(cap=cap)

    @app.get("/api/changelog")
    def changelog(month: str = "", limit: int = 500) -> dict:
        return {"months": reader.changelog_months(),
                "rows": reader.changelog(month or None, limit)}

    @app.get("/api/decisions")
    def decisions(month: str = "", limit: int = 500) -> dict:
        return {"months": reader.changelog_months(),
                "rows": reader.decisions(month or None, limit)}

    # ── generic object browse (read-only, whitelisted prefixes) ─────────────
    @app.get("/api/object")
    def get_object(key: str) -> Any:
        if ".." in key or not key.startswith(BROWSABLE_PREFIXES):
            raise HTTPException(400, f"key must start with one of {BROWSABLE_PREFIXES}")
        data = reader.get_json(key)
        if data is None:
            text = reader.read_text(key)
            if text is None:
                raise HTTPException(404, f"no such key: {key}")
            return JSONResponse({"_raw": text})
        return data

    @app.get("/api/keys")
    def list_keys(prefix: str, limit: int = Query(default=500, le=5000)) -> dict:
        if ".." in prefix or not prefix.startswith(BROWSABLE_PREFIXES):
            raise HTTPException(400, f"prefix must start with one of {BROWSABLE_PREFIXES}")
        keys = reader.list_keys(prefix)
        return {"total": len(keys), "keys": keys[:limit], "capped": len(keys) > limit}

    # ── health ───────────────────────────────────────────────────────────────
    @app.get("/api/dlqs")
    def dlqs() -> dict:
        return reader.dlq_depths()

    @app.get("/api/dlq/{queue}/messages")
    def dlq_messages(queue: str) -> list[dict]:
        _check(_ID_RE, queue, "queue name")
        try:
            return reader.dlq_messages(queue)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/errors")
    def errors(minutes: int = 1440) -> list[dict]:
        return reader.recent_errors(minutes)

    @app.get("/api/logs")
    def logs(fn: str = "all", minutes: int = Query(default=180, le=10080),
             level: str = "all", q: str = "",
             limit: int = Query(default=300, le=1000)) -> list[dict]:
        if fn != "all":
            _check(_ID_RE, fn, "function name")
        if level not in ("all", "info", "error"):
            raise HTTPException(400, "level must be all|info|error")
        return reader.logs(fn=fn, minutes=minutes, level=level, query=q, limit=limit)

    @app.get("/api/pipeline")
    def pipeline() -> dict:
        return reader.pipeline_state()

    @app.get("/api/integrations")
    def integrations() -> dict:
        return reader.integrations()

    @app.get("/api/health")
    def health() -> list[dict]:
        return reader.health_check()

    @app.get("/api/costs")
    def costs(minutes: int = Query(default=1440, le=10080)) -> dict:
        return reader.cost_report(minutes)

    @app.get("/api/targets")
    def targets() -> dict:
        return {"targets": list_targets(), "current": current["target"],
                "original": original_target}

    @app.post("/api/actions/switch-target")
    def switch_target(body: dict) -> dict:
        name = (body or {}).get("target") or ""
        if name not in list_targets():
            raise HTTPException(400, f"unknown target: {name}")
        if not isinstance(reader, ReaderRef):
            raise HTTPException(400, "target switching unavailable on this server")
        # Safety: only the target the server was STARTED against keeps write
        # access — a switched-to target is always read-only.
        writable = allow_writes and name == original_target
        try:
            new_reader = load_target(name, writable)
        except SystemExit as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            raise HTTPException(502, f"could not connect to target {name}: {e}") from e
        reader.switch(new_reader)
        current["target"] = name
        return {"status": "switched", "target": name,
                "forced_read_only": bool(allow_writes and not writable),
                **new_reader.label()}

    # ── docs ─────────────────────────────────────────────────────────────────
    @app.get("/api/docs")
    def docs_list() -> list[dict]:
        out = []
        for d in DOCS:
            if (REPO / d["path"]).is_file():
                out.append({"name": d["name"], "title": d["title"], "group": d["group"]})
        return out

    @app.get("/api/docs/{name}")
    def doc(name: str) -> dict:
        entry = next((d for d in DOCS if d["name"] == name), None)
        if entry is None:
            raise HTTPException(404, f"unknown doc: {name}")
        p = REPO / entry["path"]
        if not p.is_file():
            raise HTTPException(404, f"{entry['path']} missing on disk")
        return {"name": name, "title": entry["title"],
                "html": render_markdown(p.read_text("utf-8"))}

    # ── actions (mutations) ──────────────────────────────────────────────────
    def _act(fn: Any, *args: Any) -> dict:
        """Run a reader mutation; unsupported-on-this-target → clean 400."""
        try:
            return fn(*args)
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/actions/trigger")
    def trigger(body: dict) -> dict:
        _require_writes()
        mode = (body or {}).get("mode", "incremental")
        if mode not in ("incremental", "full"):
            raise HTTPException(400, "mode must be incremental|full")
        return _act(reader.trigger_run, mode)

    @app.post("/api/actions/approve")
    def approve(body: dict) -> dict:
        _require_writes()
        b = body or {}
        action = b.get("action")
        if action not in ("approve", "reject", "approve_all", "reject_all"):
            raise HTTPException(400, "bad action")
        run_date = _check(_ID_RE, b.get("run_date") or "", "run date")
        return _act(reader.approve_action, action, run_date, b.get("type_key"),
                    b.get("id") or b.get("art_id"), b.get("actor", "dashboard"))

    @app.post("/api/actions/backfill")
    def backfill(body: dict) -> dict:
        _require_writes()
        b = body or {}
        for field in ("run_date", "manifest_key"):
            if not b.get(field):
                raise HTTPException(400, f"{field} is required")
        return _act(reader.backfill, b["run_date"], b["manifest_key"],
                    int(b.get("article_count", 0)), b.get("mode", "incremental"))

    @app.post("/api/actions/restore")
    def restore(body: dict) -> dict:
        _require_writes()
        b = body or {}
        type_key = _check(_TYPE_RE, b.get("type_key") or "", "type key")
        art_id = _check(_ID_RE, b.get("art_id") or b.get("id") or "", "article id")
        archive_key = b.get("archive_key") or ""
        if not archive_key.startswith("archive/"):
            raise HTTPException(400, "archive_key must start with archive/")
        return _act(reader.restore_article, type_key, art_id, archive_key,
                    b.get("actor", "dashboard"))

    @app.post("/api/actions/redrive")
    def redrive(body: dict) -> dict:
        _require_writes()
        b = body or {}
        queue = _check(_ID_RE, b.get("queue") or "", "queue name")
        try:
            return _act(reader.redrive_dlq, queue, b.get("message_id") or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/actions/pipeline")
    def pipeline_action(body: dict) -> dict:
        _require_writes()
        action = (body or {}).get("action")
        if action == "pause":
            return _act(reader.set_pipeline_enabled, False)
        if action == "resume":
            return _act(reader.set_pipeline_enabled, True)
        if action == "purge_queues":
            return _act(reader.purge_queues)
        raise HTTPException(400, "action must be pause|resume|purge_queues")

    @app.post("/api/actions/pending")
    def pending_action(body: dict) -> dict:
        _require_writes()
        b = body or {}
        action = b.get("action")
        if action not in ("approve", "reject"):
            raise HTTPException(400, "action must be approve|reject")
        # Whole-type mode: {action, type_key} with no items — acts on EVERY
        # pending object of the type (bulk-optimised path).
        if b.get("type_key") and not b.get("items"):
            type_key = _check(_TYPE_RE, b.get("type_key") or "", "type key")
            return _act(reader.resolve_pending_type, action, type_key,
                        b.get("actor", "dashboard"))
        items = b.get("items")
        if not isinstance(items, list) or not items:
            raise HTTPException(400, "items must be a non-empty list (or pass type_key)")
        if len(items) > 500:
            raise HTTPException(400, "max 500 items per request")
        for it in items:
            _check(_TYPE_RE, (it or {}).get("type_key") or "", "type key")
            _check(_ID_RE, (it or {}).get("id") or "", "article id")
        return _act(reader.resolve_pending, action, items, b.get("actor", "dashboard"))

    @app.post("/api/actions/delete-run")
    def delete_run(body: dict) -> dict:
        b = body or {}
        run_date = _check(_ID_RE, b.get("run_date") or "", "run date")
        dry_run = bool(b.get("dry_run", True))
        if not dry_run:
            _require_writes()
        return _act(reader.delete_run, run_date,
                    bool(b.get("include_pending", False)), dry_run,
                    b.get("actor", "dashboard"))

    @app.post("/api/actions/save-article")
    def save_article(body: dict) -> dict:
        _require_writes()
        b = body or {}
        type_key = _check(_TYPE_RE, b.get("type_key") or "", "type key")
        art_id = _check(_ID_RE, b.get("id") or "", "article id")
        article = b.get("article")
        if not isinstance(article, dict) or not article:
            raise HTTPException(400, "article must be a non-empty JSON object")
        return _act(reader.save_article, type_key, art_id, article,
                    b.get("actor", "dashboard"))

    app.mount("/", StaticFiles(directory=str(HERE / "static"), html=True), name="static")
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="f5kb console")
    ap.add_argument("--target", default="staging", help="config target: local|staging|prod")
    ap.add_argument("--allow-writes", action="store_true",
                    help="enable mutations (trigger/approve/restore/backfill/edit)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    reader = ReaderRef(load_target(args.target, args.allow_writes))
    lbl = reader.label()
    mode_note = "READ-WRITE" if lbl.get("writable") else "read-only"
    print(f"f5kb console — target={args.target} mode={lbl['mode']} "
          f"layout={lbl.get('layout')} ({mode_note})")
    if lbl.get("bucket"):
        print(f"  bucket: {lbl['bucket']}  region: {lbl.get('region')}")
    if lbl.get("root"):
        print(f"  root: {lbl['root']}")
    print(f"  open http://{args.host}:{args.port}")

    def warm() -> None:
        # Pre-populate the expensive listings so the first page load doesn't
        # pay the cold sweep; after this the SWR caches refresh in background.
        for load in (reader.list_run_dates, reader.corpus_counts,
                     lambda: reader.pending_entries(cap=1), reader.dlq_depths):
            try:
                load()
            except Exception:
                pass

    threading.Thread(target=warm, daemon=True).start()
    uvicorn.run(make_app(reader, original_target=args.target,
                         allow_writes=args.allow_writes),
                host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
