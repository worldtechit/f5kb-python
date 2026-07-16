"""Run assembly — compose a run's live status from the per-stage S3 files.

Reads exactly the keys the Lambda handlers write (see docs/MEMORIES.md "Cloud (S3)
data layout" + the handlers themselves):

  lambda/state/{date}/orchestrator.json     types + mode for the run
  lambda/state/{date}/dump-{t}.json         in-flight dump cursor (deleted on done)
  lambda/state/{date}/enrich-{t}.json       in-flight enrich cursor (deleted on done)
  lambda/state/{date}/approve_held.json     held articles awaiting a decision
  runs/{date}/status.json                   phase + history + errors
  runs/{date}/dump/{t}/_index.json|_done    per-type dump result
  runs/{date}/enrich/{t}/_report.json|_done per-type enrich result
  runs/{date}/manifest/{t}.jsonl            staged-article manifest (grows live)
  runs/{date}/track/summary.json|progress.json|_done
  runs/{date}/approve/changed_ids*.jsonl|_done
"""

from __future__ import annotations

import concurrent.futures
from typing import Any

from readers import ALL_TYPES, ENRICHABLE, Reader

PHASES = ["scrape", "track", "approve", "done"]


def run_summary(reader: Reader, date: str) -> dict:
    """Lightweight per-run row for the list view."""
    status = reader.get_json(f"runs/{date}/status.json") or {}
    keys = set(reader.list_keys(f"runs/{date}/approve/"))
    done = f"runs/{date}/approve/_done" in keys
    phase = status.get("phase") or ("done" if done else "running")
    mode = status.get("mode")
    if not mode:
        orch = reader.get_json(f"lambda/state/{date}/orchestrator.json") or {}
        mode = orch.get("mode")
    return {"phase": phase, "mode": mode,
            "updated_at": status.get("last_updated") or status.get("updated_at"),
            "closed": done}


def build_run_detail(reader: Reader, date: str) -> dict | None:
    """Compose the run view; None when the run has no trace in the store
    (deleted or never ran) — otherwise a bogus URL renders a ghost skeleton."""
    orch = reader.get_json(f"lambda/state/{date}/orchestrator.json") or {}
    status = reader.get_json(f"runs/{date}/status.json") or {}
    done_keys = set(reader.list_keys(f"runs/{date}/"))
    if not orch and not status and not done_keys:
        return None

    types = orch.get("types") or ALL_TYPES
    enrichable = set(orch.get("enrichable") or (ENRICHABLE & set(types)))

    def has(k: str) -> bool:
        return f"runs/{date}/{k}" in done_keys

    # One parallel batch for every per-type state file (4 keys x 13 types plus
    # the run-level ones) — serially these small GETs dominated page latency.
    fetched = reader.get_json_many(
        [f"runs/{date}/dump/{t}/_index.json" for t in types]
        + [f"runs/{date}/enrich/{t}/_report.json" for t in types]
        + [f"lambda/state/{date}/dump-{t}.json" for t in types]
        + [f"lambda/state/{date}/enrich-{t}.json" for t in types]
        + [f"runs/{date}/track/summary.json",
           f"runs/{date}/track/progress.json",
           f"lambda/state/{date}/approve_held.json"])

    # Manifests are only needed for types whose final index isn't written yet
    # (i.e. still dumping); they grow to tens of thousands of lines, so fetch
    # the few we need concurrently instead of one multi-MB GET per type.
    need_manifest = [
        t for t in types
        if (fetched.get(f"runs/{date}/dump/{t}/_index.json") or {}).get("count_written") is None]
    manifest_counts: dict[str, int] = {}
    if need_manifest:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(need_manifest))) as ex:
            counts = ex.map(
                lambda t: reader.count_jsonl_lines(f"runs/{date}/manifest/{t}.jsonl"),
                need_manifest)
        manifest_counts = dict(zip(need_manifest, counts))

    per_type = []
    for t in types:
        dump_idx = fetched.get(f"runs/{date}/dump/{t}/_index.json") or {}
        enrich_rep = fetched.get(f"runs/{date}/enrich/{t}/_report.json") or {}
        dump_cur = fetched.get(f"lambda/state/{date}/dump-{t}.json") or {}
        enrich_cur = fetched.get(f"lambda/state/{date}/enrich-{t}.json") or {}
        is_enrich = t in enrichable
        dump_done = has(f"dump/{t}/_done")
        enrich_done = has(f"enrich/{t}/_done")
        terminal = enrich_done if is_enrich else dump_done

        # Live staged count: prefer the final index, else the growing manifest,
        # else the resume cursor.
        staged = dump_idx.get("count_written")
        if staged is None:
            staged = manifest_counts.get(t, 0) or dump_cur.get("written", 0)
        server_total = dump_idx.get("count_server") or dump_cur.get("count_server") or 0

        if terminal:
            state = "done"
        elif is_enrich and dump_done and not enrich_done:
            state = "enriching" + (" (resumed)" if enrich_cur else "")
        elif not dump_done:
            if dump_cur:
                state = "dumping (resumed)"
            elif staged:
                state = "dumping"
            else:
                state = "queued"
        else:
            state = "…"

        per_type.append({
            "type_key": t,
            "enrichable": is_enrich,
            "dump_done": dump_done,
            "dump_count": staged,
            "server_total": server_total,
            "enrich_done": enrich_done,
            "enriched": enrich_rep.get("enriched", enrich_cur.get("enriched", 0)),
            "enrich_failed": enrich_rep.get("failed", enrich_cur.get("failed", 0)),
            "enrich_offset": enrich_cur.get("manifest_offset"),
            "terminal": terminal,
            "state": state,
            "resumed": bool(dump_cur or enrich_cur),
        })

    # Track: final summary if written, else the live progress counters.
    track = fetched.get(f"runs/{date}/track/summary.json") or {}
    progress = fetched.get(f"runs/{date}/track/progress.json") or {}
    risk = track.get("risk_breakdown") or progress.get("counts") or {}
    track_view = {
        "new": track.get("new", progress.get("counts", {}).get("new", 0)),
        "changed": track.get("changed", progress.get("counts", {}).get("changed", 0)),
        "unchanged": track.get("unchanged", progress.get("counts", {}).get("unchanged", 0)),
        "body_shrank": risk.get("body_shrank", 0),
        "body_dropped": risk.get("body_dropped", 0),
        "body_error": risk.get("body_error", 0),
    }

    held = fetched.get(f"lambda/state/{date}/approve_held.json") or {}
    held_entries = held.get("entries") or []

    markers = {
        "scrape_done": has("scrape/_done"),
        "track_done": has("track/_done"),
        "approve_started": has("approve/started.json"),
        "approve_done": has("approve/_done"),
    }
    phase = status.get("phase")
    if not phase:
        if markers["approve_done"]:
            phase = "done"
        elif markers["track_done"]:
            phase = "approve"
        elif markers["scrape_done"]:
            phase = "track"
        elif any(pt["dump_done"] for pt in per_type):
            phase = "scrape"
        else:
            phase = "unknown"

    # Alerts: surface "why is it stuck?" without the CLI hunt.
    dlqs = reader.dlq_depths()
    alerts: list[dict[str, Any]] = []
    dump_dlq = dlqs.get(f"f5kb-dump-dlq-{reader.stage}", 0)
    enrich_dlq = dlqs.get(f"f5kb-enrich-dlq-{reader.stage}", 0)
    if dump_dlq and dump_dlq > 0:
        alerts.append({"level": "error",
                       "msg": f"{dump_dlq} msg(s) in dump DLQ — a type failed 3 retries; not scraped."})
    if enrich_dlq and enrich_dlq > 0:
        alerts.append({"level": "error",
                       "msg": f"{enrich_dlq} msg(s) in enrich DLQ — a type failed all retries."})
    for pt in per_type:
        if pt["resumed"] and not pt["terminal"]:
            prog = (f'{pt["dump_count"]}/{pt["server_total"]}'
                    if pt["server_total"] else str(pt["dump_count"]))
            alerts.append({"level": "info",
                           "msg": f'{pt["type_key"]} resuming after timeout '
                                  f'({pt["state"]}, {prog}) — normal for large types.'})
    if phase not in ("done",) and not per_type:
        alerts.append({"level": "warn", "msg": "no per-type data yet — orchestrator may still be fanning out."})
    if markers["approve_started"] and not markers["approve_done"] and held_entries:
        alerts.append({"level": "warn",
                       "msg": f"{len(held_entries)} article(s) held for review — run stays open until resolved."})
    for err in (status.get("errors") or [])[-5:]:
        alerts.append({"level": "error",
                       "msg": f'{err.get("by", "?")}: {err.get("message", "")} ({err.get("at", "")})'})

    return {
        "run_date": date,
        "phase": phase,
        "phases": PHASES,
        "mode": orch.get("mode") or status.get("mode"),
        "mode_source": orch.get("mode_source"),
        "started_at": orch.get("started_at") or status.get("started_at"),
        "updated_at": status.get("last_updated") or status.get("updated_at"),
        "types_total": len(types),
        "per_type": per_type,
        "markers": markers,
        "alerts": alerts,
        "queues": dlqs,
        "track": track_view,
        "approve": {
            "auto": reader.count_jsonl_lines(f"runs/{date}/approve/changed_ids.jsonl"),
            "holds": reader.count_jsonl_lines(f"runs/{date}/approve/changed_ids-holds.jsonl"),
        },
        "held": [
            {"id": e.get("id"), "type_key": e.get("type_key"),
             "op": e.get("op"), "risk": e.get("risk") or [],
             "changed": e.get("changed") or [],
             "live_chars": e.get("live_chars"), "pending_chars": e.get("pending_chars"),
             "error_msg": e.get("error_msg"), "article_url": e.get("article_url"),
             "live_excerpt": e.get("live_excerpt")}
            for e in held_entries
        ] if isinstance(held_entries, list) else [],
        "held_remaining": held.get("remaining", len(held_entries) if isinstance(held_entries, list) else 0),
    }
