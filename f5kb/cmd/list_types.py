"""f5kb list-types subcommand."""

import json
import sys
import click

from f5kb.coveo.aura import fetch_coveo_config, refresh_config
from f5kb.coveo.client import CoveoClient


@click.command()
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_types_cmd(ctx, as_json) -> None:
    """Print all document types with counts."""
    log = ctx.obj["logger"]
    log.info("Fetching Coveo configuration from F5 portal...")
    try:
        coveo_config = fetch_coveo_config()
    except Exception as e:
        log.error(f"failed to fetch Coveo config: {e}")
        sys.exit(1)
    client = CoveoClient(coveo_config, logger=log.child("coveo"), refresh=lambda c: refresh_config(c))
    values = client.list_facet_values("@f5_document_type")
    if as_json:
        click.echo(json.dumps(values, indent=2))
    else:
        for v in values:
            click.echo(f"{v.get('value', '?'):<40}  {v.get('numberOfResults', 0):>7,}")
