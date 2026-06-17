"""f5kb status subcommand."""

import json
import dataclasses
import click

from f5kb.lib.status import compute_status, render_status


@click.command()
@click.option("--dump", "dump_dir", default="outputs/dump", show_default=True)
@click.option("--db", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def status_cmd(ctx, dump_dir, db, as_json) -> None:
    """Read-only health report for a dump + its tracking DB."""
    report = compute_status(dump_dir, db=db)
    if as_json:
        click.echo(json.dumps(dataclasses.asdict(report), indent=2))
    else:
        click.echo(render_status(report))
