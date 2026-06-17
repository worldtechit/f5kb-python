"""f5kb sync subcommand."""

from __future__ import annotations

import json
import os
import sys
import time

import click

from f5kb.lib.changelog import changelog_path_from_flag
from f5kb.config.loader import load_config
from f5kb.coveo.aura import fetch_coveo_config, refresh_config
from f5kb.coveo.client import CoveoClient
from f5kb.http.fetcher import HttpClient
from f5kb.track.db import load_last_run_at
from f5kb.lib.sync import sync_dump


@click.command()
@click.option("--all", "all_time", is_flag=True)
@click.option("--days", "days_str", default=None)
@click.option("--since-last-run", is_flag=True)
@click.option("--types", default=None)
@click.option("--exclude-types", default=None)
@click.option("--out", "out_dir", default="outputs/dump", show_default=True)
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--db", default=None)
@click.option("--no-enrich", is_flag=True)
@click.option("--changelog", "changelog_flag", default="", is_flag=False, flag_value="")
@click.option("--no-changelog", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--yes", is_flag=True)
@click.option("--page-size", type=int, default=200, show_default=True)
@click.option("--limit", type=int, default=0)
@click.option("--concurrency", type=int, default=4, show_default=True)
@click.option("--delay-ms", type=int, default=200, show_default=True)
@click.pass_context
def sync_cmd(ctx, all_time, days_str, since_last_run, types, exclude_types, out_dir,
             config_path, db, no_enrich, changelog_flag, no_changelog, dry_run, yes,
             page_size, limit, concurrency, delay_ms) -> None:
    """Incremental update: dump+enrich+track only changed; detect deletions."""
    log = ctx.obj["logger"]
    mode_count = sum([all_time, bool(days_str), since_last_run])
    if mode_count == 0:
        log.error("provide one of --all, --days=N, or --since-last-run")
        sys.exit(1)
    if mode_count > 1:
        log.error("--all, --days=N and --since-last-run are mutually exclusive")
        sys.exit(1)

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

    now_ms = int(time.time() * 1000)
    db_path = db or f"{out_dir.rstrip('/')}/../articles.db"

    if since_last_run:
        last = load_last_run_at(db_path)
        if not last:
            log.error("--since-last-run: no prior run in DB; use --all or --days=N first")
            sys.exit(1)
        cutoff_ms = last["ran_at_ms"] or (now_ms - 7 * 86400000)
        mode = "since-last-run"
    elif days_str:
        try:
            d = float(days_str)
            assert d > 0
        except Exception:
            log.error("--days must be a positive number")
            sys.exit(1)
        cutoff_ms = int(now_ms - d * 86400000)
        mode = f"days={d}"
    else:
        cutoff_ms = 946684800000
        mode = "all"

    end_ms = now_ms + 86400000
    cl_path = None if no_changelog else changelog_path_from_flag(changelog_flag, out_dir)

    log.info("Fetching Coveo configuration from F5 portal...")
    try:
        coveo_config = fetch_coveo_config()
    except Exception as e:
        log.error(f"failed to fetch Coveo config: {e}")
        sys.exit(1)
    client = CoveoClient(coveo_config, logger=log.child("coveo"), refresh=lambda c: refresh_config(c))
    http = HttpClient(logger=log.child("http"))
    github_token = os.environ.get("GITHUB_TOKEN")

    result = sync_dump(
        client=client,
        http=http,
        type_configs=type_configs,
        type_keys=type_keys,
        descriptions=dict(config.field_descriptions),
        out_dir=out_dir,
        db=db_path,
        mode=mode,
        all_time=all_time,
        cutoff_ms=cutoff_ms,
        end_ms=end_ms,
        now_ms=now_ms,
        page_size=min(page_size, 500),
        limit=limit,
        config_path=config_path,
        enrich=not no_enrich,
        github_token=github_token,
        concurrency=max(1, concurrency),
        delay_ms=max(0, delay_ms),
        changelog_path=cl_path,
        dry_run=dry_run,
        approval=not yes,
        archive_on_overwrite=yes,
        logger=log,
    )

    log.info(
        f"Sync {'(dry-run) ' if dry_run else ''}done. "
        f"written={result.written} skipped={result.skipped} "
        f"added={result.added} edited={result.edited} staged={result.staged} "
        f"deletions_detected={result.deletions_detected}"
    )
    if result.changelog_path:
        log.info(f"Changelog: {result.changelog_path}")
