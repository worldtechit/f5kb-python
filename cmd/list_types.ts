// `f5kb list-types` — print all document types with counts (to STDOUT).
// Behavior reference: fetch_f5_articles_flex.ts --list-types.

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

  console.log("\nAvailable document types:");
  const types = await client.listFacetValues("f5_document_type");
  for (const { value, count } of types.sort((a, b) => b.count - a.count)) {
    console.log(`  ${value.padEnd(35)} ${count.toLocaleString()}`);
  }
  return 0;
}
