"""f5kb fetch subcommand."""

from __future__ import annotations

import json
import sys

import click

from f5kb.coveo.aura import fetch_coveo_config, refresh_config
from f5kb.coveo.client import CoveoClient
from f5kb.coveo.flat import build_aq, fetch_flat_chunked, fetch_flat_paged, to_csv, COVEO_MAX_OFFSET
from f5kb.lib.fsutil import sanitize_name

EPOCH_START_MS = 946684800000  # 2000-01-01
EPOCH_END_MS = 1893456000000   # 2030-01-01


@click.command()
@click.option("--product", default=None)
@click.option("--type", "doc_type", default=None)
@click.option("--limit", type=int, default=0)
@click.option("--output", default=None)
@click.option("--csv", "csv_file", default=None)
@click.option("--page-size", type=int, default=100, show_default=True)
@click.pass_context
def fetch_cmd(ctx, product, doc_type, limit, output, csv_file, page_size) -> None:
    """Fetch articles by product/type into a flat JSON (+ optional CSV)."""
    log = ctx.obj["logger"]
    page_size = min(page_size, 1000)
    slug = "_".join(filter(None, [
        sanitize_name(product) if product else "",
        sanitize_name(doc_type) if doc_type else "",
    ])) or "all"
    json_output = output or f"f5_{slug}.json"

    log.info("Fetching Coveo configuration from F5 portal...")
    try:
        coveo_config = fetch_coveo_config()
    except Exception as e:
        log.error(f"failed to fetch Coveo config: {e}")
        sys.exit(1)
    client = CoveoClient(coveo_config, logger=log.child("coveo"), refresh=lambda c: refresh_config(c))

    base_aq = build_aq(product, doc_type)
    total = client.get_count(base_aq)
    target = min(limit, total) if limit else total
    log.info(f"Total matching articles: {total:,}")
    log.info(f"Fetching {target:,} articles...")

    articles = []
    last_reported = 0

    def on_progress(n: int):
        nonlocal last_reported
        if n - last_reported >= 500 or n >= target:
            pct = round(n / target * 100) if target else 100
            log.info(f"  Fetched {n:,} / {target:,} ({pct}%)")
            last_reported = n

    if total <= COVEO_MAX_OFFSET or (limit and limit <= COVEO_MAX_OFFSET):
        articles = fetch_flat_paged(client, base_aq, page_size, target, on_progress)
    else:
        articles = fetch_flat_chunked(client, base_aq, page_size, target, on_progress)

    with open(json_output, "w", encoding="utf-8") as f:
        json.dump([a.__dict__ for a in articles], f, indent=2)
    log.info(f"Wrote {len(articles)} articles -> {json_output}")

    if csv_file:
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write(to_csv(articles))
        log.info(f"Wrote CSV -> {csv_file}")
