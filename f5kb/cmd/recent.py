"""f5kb recent subcommand."""

from __future__ import annotations

import sys
import time

import click

from f5kb.coveo.aura import fetch_coveo_config, refresh_config
from f5kb.coveo.client import CoveoClient
from f5kb.config.loader import load_config
from f5kb.lib.dump import dump_types


@click.command()
@click.option("--days", required=True, type=float)
@click.option("--out", required=True)
@click.option("--types", default=None)
@click.option("--exclude-types", default=None)
@click.option("--page-size", type=int, default=500, show_default=True)
@click.option("--limit", type=int, default=0)
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.pass_context
def recent_cmd(ctx, days, out, types, exclude_types, page_size, limit, config_path) -> None:
    """Fetch articles modified in the last N days, one JSON per type."""
    log = ctx.obj["logger"]
    try:
        config = load_config(config_path)
    except Exception as e:
        log.error(f"could not read/parse config {config_path}: {e}")
        sys.exit(1)
    type_configs = {k: {"documentType": v.document_type, "metadata": v.metadata, "content": v.content}
                   for k, v in config.types.items()}
    all_type_keys = list(type_configs.keys())
    include_types = [t.strip() for t in types.split(",")] if types else None
    excl_types = [t.strip() for t in exclude_types.split(",")] if exclude_types else None
    type_keys = [t for t in all_type_keys if
                 (not include_types or t in include_types) and
                 (not excl_types or t not in excl_types)]
    if not type_keys:
        log.error("no type keys selected")
        sys.exit(1)

    log.info("Fetching Coveo configuration from F5 portal...")
    try:
        coveo_config = fetch_coveo_config()
    except Exception as e:
        log.error(f"failed to fetch Coveo config: {e}")
        sys.exit(1)
    client = CoveoClient(coveo_config, logger=log.child("coveo"), refresh=lambda c: refresh_config(c))
    now_ms = int(time.time() * 1000)
    cutoff_ms = int(now_ms - days * 86400000)

    result = dump_types(
        client,
        type_configs=type_configs,
        type_keys=type_keys,
        descriptions=dict(config.field_descriptions),
        out_dir=out,
        all_time=False,
        mode=f"days={days}",
        cutoff_ms=cutoff_ms,
        end_ms=now_ms + 86400000,
        now_ms=now_ms,
        page_size=min(page_size, 500),
        limit=limit,
        config_path=config_path,
        logger=log,
    )
    log.info(f"Done. {result.total} articles -> {out}/")
