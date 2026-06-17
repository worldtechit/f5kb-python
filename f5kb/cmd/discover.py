"""f5kb discover subcommand."""

from __future__ import annotations

import json
import sys
import click

from f5kb.coveo.aura import fetch_coveo_config, refresh_config
from f5kb.coveo.client import CoveoClient
from f5kb.config.loader import load_config


@click.command()
@click.option("--out", default="discovered_products.yaml", show_default=True)
@click.option("--format", "fmt", default="yaml", type=click.Choice(["yaml", "json"]), show_default=True)
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.pass_context
def discover_cmd(ctx, out, fmt, config_path) -> None:
    """Deep product discovery; write discovered_products.yaml."""
    log = ctx.obj["logger"]
    log.info("Fetching Coveo configuration from F5 portal...")
    try:
        coveo_config = fetch_coveo_config()
    except Exception as e:
        log.error(f"failed to fetch Coveo config: {e}")
        sys.exit(1)
    client = CoveoClient(coveo_config, logger=log.child("coveo"), refresh=lambda c: refresh_config(c))

    log.info("Discovering products via facet sweep...")
    values = client.list_facet_values("@f5_version")
    products = [{"name": v.get("value", ""), "count": v.get("numberOfResults", 0)} for v in values]

    if fmt == "json":
        data = json.dumps(products, indent=2)
    else:
        import yaml
        data = yaml.dump({"products": products}, default_flow_style=False, allow_unicode=True)

    with open(out, "w", encoding="utf-8") as f:
        f.write(data)
    log.info(f"Wrote {len(products)} products -> {out}")
