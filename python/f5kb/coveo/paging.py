"""Pagination strategies over the Coveo search API."""

from __future__ import annotations

import time
from typing import Any, Callable

from f5kb.coveo.client import CoveoClient, CoveoResult
from f5kb.coveo.dates import date_aq, mod_ms_of, to_coveo_date

COVEO_MAX_OFFSET = 5000
# Python int is arbitrary precision — no BigInt needed
CURSOR_MARGIN = 4096


def fetch_paged(
    client: CoveoClient,
    aq: str,
    page_size: int,
    max_results: int,
    on_progress: Callable[[int], None] | None = None,
) -> list[CoveoResult]:
    """Standard offset pagination (safe when total <= COVEO_MAX_OFFSET)."""
    out: list[CoveoResult] = []
    first_result = 0
    eff = page_size

    while len(out) < max_results:
        to_fetch = min(eff, max_results - len(out), COVEO_MAX_OFFSET - first_result)
        if to_fetch <= 0:
            break
        try:
            data = client.post({
                "q": "",
                "aq": aq or None,
                "numberOfResults": to_fetch,
                "firstResult": first_result,
                "searchHub": "myF5",
                "sortCriteria": "date descending",
            })
        except RuntimeError as e:
            if eff > 1 and _is_size_error(e):
                eff = max(1, eff // 2)
                continue
            raise
        batch: list[CoveoResult] = data.get("results") or []
        out.extend(batch)
        first_result += len(batch)
        if on_progress:
            on_progress(len(out))
        if len(batch) < to_fetch:
            break
        if first_result >= COVEO_MAX_OFFSET:
            break
        time.sleep(0.12)

    return out


def fetch_keyset(
    client: CoveoClient,
    aq: str,
    page_size: int,
    max_results: int | float,
    on_progress: Callable[[int], None] | None = None,
    fields: list[str] | None = None,
) -> list[CoveoResult]:
    """Cursor pagination by @rowid — no 5k offset cap."""
    out: list[CoveoResult] = []
    seen: set[str] = set()
    cursor: int | None = None
    eff = page_size

    while len(out) < max_results:
        to_fetch = min(eff, int(max_results) - len(out)) if max_results != float("inf") else eff
        if to_fetch <= 0:
            break
        cursor_aq = aq if cursor is None else f"{aq} @rowid>={cursor}"
        body: dict[str, Any] = {
            "q": "",
            "aq": cursor_aq or None,
            "numberOfResults": to_fetch,
            "searchHub": "myF5",
            "sortCriteria": "@rowid ascending",
        }
        if fields:
            body["fieldsToInclude"] = fields
        try:
            data = client.post(body)
        except RuntimeError as e:
            if eff > 1 and _is_size_error(e):
                eff = max(1, eff // 2)
                continue
            raise

        batch: list[CoveoResult] = data.get("results") or []
        if not batch:
            break

        last_raw = batch[-1].get("raw") or {}
        last_row = last_raw.get("rowid")
        if last_row is None:
            raise RuntimeError("keyset paging: result missing @rowid")

        added = 0
        for r in batch:
            raw = r.get("raw") or {}
            pid = raw.get("permanentid") or r.get("uniqueId") or ""
            if pid and pid in seen:
                continue
            if pid:
                seen.add(pid)
            out.append(r)
            added += 1

        if on_progress:
            on_progress(len(out))

        next_cursor = int(last_row) - CURSOR_MARGIN
        if added == 0 and cursor is not None and next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < to_fetch:
            break
        time.sleep(0.12)

    return out


def fetch_chunked(
    client: CoveoClient,
    base_aq: str,
    start_ms: int,
    end_ms: int,
    page_size: int,
    max_results: int,
    on_progress: Callable[[int], None],
    collected: list[CoveoResult],
    depth: int = 0,
) -> None:
    """Recursively split date window until each chunk fits within COVEO_MAX_OFFSET."""
    if len(collected) >= max_results:
        return
    window = date_aq(start_ms, end_ms)
    aq = f"{base_aq} {window}".strip() if window else base_aq
    total = client.get_count(aq)
    if total == 0:
        return

    if total <= COVEO_MAX_OFFSET:
        remaining = max_results - len(collected)
        batch = fetch_paged(
            client, aq, page_size, min(total, remaining),
            lambda n: on_progress(len(collected) + n),
        )
        collected.extend(batch)
        return

    mid_ms = (start_ms + end_ms) // 2
    if to_coveo_date(start_ms) == to_coveo_date(mid_ms) or depth >= 50:
        batch = fetch_keyset(
            client, aq, page_size, max_results - len(collected),
            lambda n: on_progress(len(collected) + n),
        )
        collected.extend(batch)
        return

    fetch_chunked(client, base_aq, start_ms, mid_ms, page_size, max_results, on_progress, collected, depth + 1)
    fetch_chunked(client, base_aq, mid_ms, end_ms, page_size, max_results, on_progress, collected, depth + 1)


def fetch_ids(
    client: CoveoClient,
    document_type: str,
    page_size: int = 2000,
    on_progress: Callable[[int], None] | None = None,
) -> list[CoveoResult]:
    """IDs-only sweep of an entire type (for deletion reconcile)."""
    base_aq = f'@f5_document_type=="{document_type}"'
    return fetch_keyset(
        client, base_aq, page_size, float("inf"), on_progress,
        fields=["rowid", "permanentid", "f5_kb_id"],
    )


def fetch_type_since(
    client: CoveoClient,
    document_type: str,
    cutoff_ms: int,
    end_ms: int,
    page_size: int,
    limit: int,
    on_progress: Callable[[int], None],
    apply_mod_filter: bool = True,
) -> list[CoveoResult]:
    base_aq = f'@f5_document_type=="{document_type}"'
    # limit=0 means "no cap"; normalize before passing to paging helpers
    max_r: int | float = float("inf") if limit == 0 else limit
    if not apply_mod_filter:
        return fetch_keyset(client, base_aq, page_size, max_r, on_progress)
    collected: list[CoveoResult] = []
    # fetch_chunked takes int; use large sentinel for unlimited
    chunked_max = int(max_r) if max_r != float("inf") else 10_000_000
    fetch_chunked(client, base_aq, cutoff_ms, end_ms, page_size, chunked_max, on_progress, collected)
    return [
        r for r in collected
        if (m := mod_ms_of(r.get("raw"))) is None or m >= cutoff_ms
    ]


def _is_size_error(e: Exception) -> bool:
    import re
    return bool(re.search(r"maximum size|ResponseExceededMaximumSize", str(e), re.I))
