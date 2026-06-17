"""f5kb approve subcommand."""

from __future__ import annotations

import dataclasses
import json
import time

import click

from f5kb.lib.changelog import Changelog, changelog_path_from_flag
from f5kb.lib.approve import approve
from f5kb.track.db import track_dump


@click.command()
@click.option("--dump", "dump_dir", default="outputs/dump", show_default=True)
@click.option("--db", default=None)
@click.option("--types", default=None)
@click.option("--exclude-types", default=None)
@click.option("--ids", default=None)
@click.option("--list", "list_only", is_flag=True)
@click.option("--reject", is_flag=True)
@click.option("--include-risky", is_flag=True)
@click.option("--no-archive", is_flag=True)
@click.option("--changelog", "changelog_flag", default="", is_flag=False, flag_value="")
@click.option("--no-changelog", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def approve_cmd(ctx, dump_dir, db, types, exclude_types, ids, list_only, reject,
                include_risky, no_archive, changelog_flag, no_changelog, as_json) -> None:
    """Review + apply (or reject) overwrites staged in _pending/ by the gate."""
    log = ctx.obj["logger"]
    type_keys = [t.strip() for t in types.split(",")] if types else None
    excl_type_keys = [t.strip() for t in exclude_types.split(",")] if exclude_types else None
    id_list = [i.strip() for i in ids.split(",")] if ids else None
    db_path = db or f"{dump_dir.rstrip('/')}/../articles.db"
    now_ms = int(time.time() * 1000)

    cl_path = None if no_changelog else (None if (list_only or reject) else changelog_path_from_flag(changelog_flag if changelog_flag != "" else True, dump_dir))
    import datetime
    run_id = datetime.datetime.fromtimestamp(now_ms / 1000, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    changelog = Changelog(cl_path, run_id)

    result = approve(
        dump_dir,
        reject=reject,
        type_keys=type_keys,
        exclude_type_keys=excl_type_keys,
        ids=id_list,
        archive=not no_archive,
        include_risky=include_risky,
        dry_run=list_only,
        changelog=changelog,
        now_ms=now_ms,
        logger=log,
    )
    changelog.flush()

    if not list_only and not reject and result.promoted > 0:
        track_dump(dump_dir, db_path=db_path, run_id=run_id, logger=log)

    if as_json:
        click.echo(json.dumps(dataclasses.asdict(result), indent=2))
        return

    for item in result.items:
        kind = f"  ({'+'.join(item.changed)})" if item.changed else ""
        risk = f"  [risk: {', '.join(item.risk)}]" if item.risk else ""
        log.info(f"  {item.action:<15} {item.type_key}/{item.id}{kind}{risk}")

    if list_only:
        log.info(f"{len(result.items)} pending edit(s). Run `f5kb approve` to apply.")
    elif reject:
        log.info(f"Rejected {result.rejected} staged edit(s); {result.remaining} still pending.")
    else:
        risky_note = f"; HELD {result.held_risky} risky" if result.held_risky else ""
        log.info(f"Promoted {result.promoted} edit(s){risky_note}; {result.remaining} still pending.")
