"""f5kb reconcile subcommand."""

from __future__ import annotations

import dataclasses
import json
import sys
import time

import click

from f5kb.lib.changelog import Changelog, changelog_path_from_flag
from f5kb.config.loader import load_config
from f5kb.coveo.aura import fetch_coveo_config, refresh_config
from f5kb.coveo.client import CoveoClient
from f5kb.lib.reconcile import reconcile


@click.command()
@click.option("--types", default=None)
@click.option("--exclude-types", default=None)
@click.option("--dump", "dump_dir", default="outputs/dump", show_default=True)
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--db", default=None)
@click.option("--apply", is_flag=True)
@click.option("--purge", is_flag=True)
@click.option("--max-delete-pct", type=float, default=10.0, show_default=True)
@click.option("--max-deletes", type=int, default=None)
@click.option("--changelog", "changelog_flag", default=None, is_flag=False, flag_value="")
@click.option("--page-size", type=int, default=2000, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def reconcile_cmd(ctx, types, exclude_types, dump_dir, config_path, db, apply, purge,
                  max_delete_pct, max_deletes, changelog_flag, page_size, as_json) -> None:
    """Remove articles deleted upstream (report-only unless --apply)."""
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

    db_path = db or f"{dump_dir.rstrip('/')}/../articles.db"
    cl_path = changelog_path_from_flag(changelog_flag, dump_dir)
    import datetime
    run_id = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    changelog = Changelog(cl_path, run_id) if cl_path else None

    log.info("Fetching Coveo configuration from F5 portal...")
    try:
        coveo_config = fetch_coveo_config()
    except Exception as e:
        log.error(f"failed to fetch Coveo config: {e}")
        sys.exit(1)
    client = CoveoClient(coveo_config, logger=log.child("coveo"), refresh=lambda c: refresh_config(c))

    result = reconcile(
        client,
        dump=dump_dir,
        db=db_path,
        type_configs=type_configs,
        type_keys=type_keys,
        apply=apply,
        purge=purge,
        max_delete_pct=max_delete_pct / 100.0,
        max_deletes=max_deletes,
        changelog=changelog,
        logger=log,
        page_size=page_size,
    )
    if changelog:
        changelog.flush()

    if as_json:
        click.echo(json.dumps(dataclasses.asdict(result), indent=2))
        return

    log.info(f"total_deletions={result.total_deletions} total_db={result.total_db} applied={result.applied}")
    if result.aborted:
        log.error(f"ABORTED: {result.aborted}")
        sys.exit(1)
