"""f5kb enrich subcommand."""

from __future__ import annotations

import os
import sys

import click

from f5kb.lib.changelog import Changelog, changelog_path_from_flag
from f5kb.http.fetcher import HttpClient
from f5kb.enrich.driver import enrich_dump


@click.command()
@click.option("--dump", "dump_dir", default="outputs/dump", show_default=True)
@click.option("--types", default=None, help="Comma-separated type keys.")
@click.option("--exclude-types", default=None)
@click.option("--concurrency", type=int, default=4, show_default=True)
@click.option("--delay-ms", type=int, default=200, show_default=True)
@click.option("--limit", type=int, default=0, help="Cap per type (0=all).")
@click.option("--refetch", is_flag=True)
@click.option("--refetch-errors", is_flag=True)
@click.option("--changelog", "changelog_flag", default=None, is_flag=False, flag_value="")
@click.option("--yes", is_flag=True, help="Bypass gate: overwrite existing bodies.")
@click.pass_context
def enrich_cmd(ctx, dump_dir, types, exclude_types, concurrency, delay_ms, limit,
               refetch, refetch_errors, changelog_flag, yes) -> None:
    """Fetch article bodies for types the search index leaves empty."""
    log = ctx.obj["logger"]
    include_types = [t.strip() for t in types.split(",")] if types else None
    excl_types = [t.strip() for t in exclude_types.split(",")] if exclude_types else None
    github_token = os.environ.get("GITHUB_TOKEN")
    http = HttpClient(logger=log.child("http"))
    cl_path = changelog_path_from_flag(changelog_flag, dump_dir)
    import datetime
    run_id = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    changelog = Changelog(cl_path, run_id)

    reports = enrich_dump(
        dump_dir,
        types=include_types,
        exclude_types=excl_types,
        http=http,
        github_token=github_token,
        concurrency=max(1, concurrency),
        delay_ms=max(0, delay_ms),
        limit=limit or None,
        refetch=refetch,
        refetch_errors=refetch_errors,
        logger=log,
        changelog=changelog,
        approval=not yes,
    )
    changelog.flush()
    if cl_path:
        log.info(f"Changelog: {cl_path}")
    total_staged = sum(r.staged for r in reports)
    if total_staged:
        log.warn(f"{total_staged} re-fetched body(ies) STAGED to {dump_dir}/_pending/")
