// `f5kb list-products` — print products known to the global facet, with counts
// (to STDOUT). Behavior reference: fetch_f5_articles_flex.ts --list-products
// (top-level / no-pipe values only).

import { type ParsedArgs } from "../lib/args.ts";
import { type Logger } from "../lib/logger.ts";
import { CoveoClient } from "../lib/coveo/client.ts";
import { fetchCoveoConfig, refreshConfig } from "../lib/coveo/aura.ts";

export async function run(_args: ParsedArgs, logger: Logger): Promise<number> {
  logger.info("Fetching Coveo configuration from F5 portal...");
  const coveoConfig = await fetchCoveoConfig();
  logger.info(`Organization ID: ${coveoConfig.organizationId}`);
  const client = new CoveoClient(coveoConfig, {
    logger: logger.child("coveo"),
    refresh: (c) => refreshConfig(c),
  });

  console.log("\nAvailable products (top-level only, global facet):");
  const products = await client.listFacetValues("f5_version");
  const topLevel = products
    .filter(({ value }) => !value.includes("|"))
    .sort((a, b) => b.count - a.count);
  for (const { value, count } of topLevel) {
    console.log(`  ${value.padEnd(45)} ${count.toLocaleString()}`);
  }
  console.log("\n  Note: some products are hidden from the global facet.");
  console.log("  Use `f5kb discover` for the complete list.");
  return 0;
}
