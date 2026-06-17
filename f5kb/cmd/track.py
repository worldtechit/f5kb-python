"""f5kb track subcommand."""

from __future__ import annotations

import json
import sys

import click

from f5kb.lib.changelog import Changelog, changelog_path_from_flag
from f5kb.track.db import track_dump


@click.command()
@click.option("--dump", "dump_dir", default="outputs/dump", show_default=True)
@click.option("--db", default=None)
@click.option("--types", default=None)
@click.option("--exclude-types", default=None)
@click.option("--run-id", default=None)
@click.option("--changelog", "changelog_flag", default=None, is_flag=False, flag_value="")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def track_cmd(ctx, dump_dir, db, types, exclude_types, run_id, changelog_flag, as_json) -> None:
    """Index a dump into the SQLite overview; report new/changed/removed."""
    from f5kb.lib.logger import make_logger
    log = ctx.obj["logger"]
    if as_json:
        log = make_logger(level="warn", json_mode=False, scope="track")
    include_types = [t.strip() for t in types.split(",")] if types else None
    excl_types = [t.strip() for t in exclude_types.split(",")] if exclude_types else None
    import datetime
    run = run_id or datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    cl_path = changelog_path_from_flag(changelog_flag, dump_dir)
    changelog = Changelog(cl_path, run)

    summary = track_dump(
        dump_dir, db_path=db, types=include_types, exclude_types=excl_types,
        run_id=run, logger=log, changelog=changelog,
    )
    changelog.flush()
    if as_json:
        import dataclasses
        click.echo(json.dumps(dataclasses.asdict(summary), indent=2))
    elif cl_path:
        log.info(f"Changelog: {cl_path}")
