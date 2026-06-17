"""f5kb dump subcommand."""

from __future__ import annotations

import sys
import time
import click

from f5kb.lib.logger import make_logger
from f5kb.lib.changelog import Changelog, changelog_path_from_flag
from f5kb.config.loader import load_config, load_field_descriptions_file
from f5kb.coveo.aura import fetch_coveo_config, refresh_config
from f5kb.coveo.client import CoveoClient
from f5kb.lib.dump import dump_types
from f5kb.lib.staging import merge_pending
from f5kb.track.db import load_hash_index


@click.command()
@click.option("--all", "all_time", is_flag=True, help="Full corpus (no lower date bound).")
@click.option("--days", type=str, default=None, help="Window: last N days.")
@click.option("--out", required=True, help="Output directory.")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--fields-doc", default=None, help="DEPRECATED: extra field descriptions file.")
@click.option("--types", default=None, help="Comma-separated type keys to dump.")
@click.option("--exclude-types", default=None)
@click.option("--page-size", type=int, default=200, show_default=True)
@click.option("--limit", type=int, default=0, help="Cap articles per type (0=all).")
@click.option("--db", default=None, help="SQLite file (default <out>/../articles.db).")
@click.option("--changelog", "changelog_flag", default=None, is_flag=False, flag_value="", help="Changelog file.")
@click.option("--yes", is_flag=True, help="Bypass approval gate: overwrite in place.")
@click.pass_context
def dump_cmd(ctx, all_time, days, out, config_path, fields_doc, types, exclude_types,
             page_size, limit, db, changelog_flag, yes) -> None:
    """Dump full metadata + content for F5 KB articles (one JSON per article)."""
    log = ctx.obj["logger"]
    include_types = [t.strip() for t in types.split(",")] if types else None
    excl_types = [t.strip() for t in exclude_types.split(",")] if exclude_types else None
    page_size = min(page_size, 500)

    if not all_time and not days:
        log.error("provide --all or --days=N")
        sys.exit(1)
    if days is not None:
        try:
            days_n = float(days)
            assert days_n > 0
        except Exception:
            log.error("--days must be a positive number")
            sys.exit(1)
    else:
        days_n = 0

    try:
        config = load_config(config_path)
    except Exception as e:
        log.error(f"could not read/parse config {config_path}: {e}")
        sys.exit(1)
    type_configs = {k: {"documentType": v.document_type, "metadata": v.metadata, "content": v.content}
                   for k, v in config.types.items()}
    all_type_keys = list(type_configs.keys())
    if not all_type_keys:
        log.error(f"config {config_path} has no types")
        sys.exit(1)
    if include_types:
        unknown = [t for t in include_types if t not in all_type_keys]
        if unknown:
            log.warn(f"unknown --types: {', '.join(unknown)}")
    if excl_types:
        unknown = [t for t in excl_types if t not in all_type_keys]
        if unknown:
            log.warn(f"unknown --exclude-types: {', '.join(unknown)}")
    type_keys = [t for t in all_type_keys if
                 (not include_types or t in include_types) and
                 (not excl_types or t not in excl_types)]
    if not type_keys:
        log.error("no type keys selected")
        sys.exit(1)

    descriptions = dict(config.field_descriptions)
    if fields_doc:
        log.warn("--fields-doc is DEPRECATED: descriptions come from config.yaml.")
        try:
            descriptions.update(load_field_descriptions_file(fields_doc))
        except Exception as e:
            log.warn(f"could not load fields-doc: {e}")

    log.info("Fetching Coveo configuration from F5 portal...")
    try:
        coveo_config = fetch_coveo_config()
    except Exception as e:
        log.error(f"failed to fetch Coveo config: {e}")
        sys.exit(1)
    log.info(f"Organization ID: {coveo_config.organization_id}")
    client = CoveoClient(coveo_config, logger=log.child("coveo"),
                          refresh=lambda c: refresh_config(c))

    now_ms = int(time.time() * 1000)
    cutoff_ms = int(now_ms - days_n * 86400000) if not all_time else 946684800000  # 2000-01-01
    end_ms = now_ms + 86400000

    db_path = db or f"{out.rstrip('/')}/../articles.db"
    cl_path = changelog_path_from_flag(changelog_flag, out)
    run_id = __import__("datetime").datetime.fromtimestamp(now_ms / 1000, tz=__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")
    changelog = Changelog(cl_path, run_id)
    prior_hashes = load_hash_index(db_path)

    result = dump_types(
        client,
        type_configs=type_configs,
        type_keys=type_keys,
        descriptions=descriptions,
        out_dir=out,
        all_time=all_time,
        mode="all" if all_time else f"days={days_n}",
        cutoff_ms=cutoff_ms,
        end_ms=end_ms,
        now_ms=now_ms,
        page_size=page_size,
        limit=limit,
        config_path=config_path,
        logger=log,
        prior_hashes=prior_hashes,
        changelog=changelog,
        approval=not yes,
        archive_on_overwrite=yes,
    )
    changelog.flush()
    if cl_path:
        log.info(f"Changelog: {cl_path}")
    if result.pending:
        import datetime
        merge_pending(out, result.pending, datetime.datetime.fromtimestamp(now_ms / 1000, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z"))
        log.warn(f"{len(result.pending)} edited article(s) STAGED to {out}/_pending/")

    failed = [m for m in result.manifest if m.status == "failed"]
    partial = [m for m in result.manifest if m.status == "partial"]
    log.info(f"Done. {result.total} articles across {len(result.manifest)} type(s) -> {out}/")
    if partial:
        log.warn(f"PARTIAL ({len(partial)}): " + ", ".join(f"{m.type_key} ({m.written}/{m.expected or '?'})" for m in partial))
    if failed:
        log.error(f"FAILED ({len(failed)}): " + "; ".join(f"{m.type_key}: {m.error}" for m in failed))
        sys.exit(1)
