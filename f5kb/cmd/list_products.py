"""f5kb list-products subcommand."""

import json
import sys
import click

from f5kb.coveo.aura import fetch_coveo_config, refresh_config
from f5kb.coveo.client import CoveoClient


@click.command()
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_products_cmd(ctx, as_json) -> None:
    """Print products known to the global facet, with counts."""
    log = ctx.obj["logger"]
    log.info("Fetching Coveo configuration from F5 portal...")
    try:
        coveo_config = fetch_coveo_config()
    except Exception as e:
        log.error(f"failed to fetch Coveo config: {e}")
        sys.exit(1)
    client = CoveoClient(coveo_config, logger=log.child("coveo"), refresh=lambda c: refresh_config(c))
    values = client.list_facet_values("@f5_version")
    if as_json:
        click.echo(json.dumps(values, indent=2))
    else:
        for v in values:
            click.echo(f"{v.get('value', '?'):<50}  {v.get('numberOfResults', 0):>7,}")
