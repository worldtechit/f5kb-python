// `f5kb discover` — deep product discovery. Surfaces products hidden from the
// global Coveo facet by re-running the f5_version facet scoped to each document
// type. Behavior reference: discover_products.ts (and the flex --discover-products
// mode). Writes the result to discovered_products.yaml in the SAME `products:`
// schema as config.yaml (generatedAt + entries: list). MUST NOT touch config.yaml —
// the user copies the `products:` block over by hand.
//
// Flags:
//   --out=FILE       output file (default discovered_products.yaml)
//   --format=json    write JSON instead of YAML (parity with the old script)

import { stringify as stringifyYaml } from "@std/yaml";
import { type ParsedArgs } from "../lib/args.ts";
import { flagStr } from "../lib/args.ts";
import { type Logger } from "../lib/logger.ts";
import { CoveoClient } from "../lib/coveo/client.ts";
import { fetchCoveoConfig, refreshConfig } from "../lib/coveo/aura.ts";
import { type ProductEntry } from "../lib/config/types.ts";

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function run(args: ParsedArgs, logger: Logger): Promise<number> {
  const flags = args.flags;
  const format = flagStr(flags, "format", "yaml")!;
  const defaultOut = format === "json" ? "discovered_products.json" : "discovered_products.yaml";
  const outFile = flagStr(flags, "out", defaultOut)!;

  logger.info("Fetching Coveo configuration from F5 portal...");
  const coveoConfig = await fetchCoveoConfig();
  logger.info(`Organization ID: ${coveoConfig.organizationId}`);
  const client = new CoveoClient(coveoConfig, {
    logger: logger.child("coveo"),
    refresh: (c) => refreshConfig(c),
  });

  // Step 1: all document types (top-level / no-pipe), by count desc.
  logger.info("Getting all document types...");
  const docTypeValues = await client.listFacetValues("f5_document_type");
  const docTypes = docTypeValues
    .filter((v) => !v.value.includes("|"))
    .sort((a, b) => b.count - a.count);
  logger.info(`  Found ${docTypes.length} document types`);
  await delay(200);

  // Step 2: global f5_version facet — standalone (no-pipe) top-level entries.
  logger.info("Getting global product facet (baseline)...");
  const globalValues = await client.listFacetValues("f5_version");
  const globalStandalone = new Map<string, number>(
    globalValues.filter((v) => !v.value.includes("|")).map((v) => [v.value, v.count]),
  );
  logger.info(`  Global facet: ${globalStandalone.size} top-level products`);
  await delay(200);

  // Step 3: type-filtered facet per document type — surfaces hidden products.
  logger.info("Running type-filtered facet queries...");
  const hiddenFoundIn = new Map<string, Set<string>>();
  for (const { value: docType, count: typeCount } of docTypes) {
    const aq = `@f5_document_type=="${docType}"`;
    const values = await client.listFacetValues("f5_version", aq);
    let newFound = 0;
    for (const { value } of values) {
      const name = value.split("|")[0];
      if (!globalStandalone.has(name)) {
        if (!hiddenFoundIn.has(name)) {
          hiddenFoundIn.set(name, new Set());
          newFound++;
        }
        hiddenFoundIn.get(name)!.add(docType);
      }
    }
    logger.info(
      `  [${docType}] (${typeCount.toLocaleString()} docs) ${values.length} values, ${newFound} new hidden`,
    );
    await delay(200);
  }
  logger.info(`  Total hidden products discovered: ${hiddenFoundIn.size}`);

  // Step 4: total counts for hidden products.
  const hiddenCounts = new Map<string, number>();
  if (hiddenFoundIn.size > 0) {
    logger.info("Fetching total counts for hidden products...");
    for (const [name] of hiddenFoundIn.entries()) {
      const count = await client.getCount(`@f5_version=="${name}"`);
      hiddenCounts.set(name, count);
      logger.info(`  Counting "${name}" ... ${count.toLocaleString()}`);
      await delay(200);
    }
  }

  // Step 5: build the entries (same shape/sort as discover_products.ts output).
  const entries: ProductEntry[] = [
    ...Array.from(globalStandalone.entries()).map(([product, count]) => ({
      product,
      count,
      source: "global_facet",
      hiddenFromGlobalFacet: false,
    })),
    ...Array.from(hiddenFoundIn.entries()).map(([product, types]) => ({
      product,
      count: hiddenCounts.get(product) ?? 0,
      source: "type_filtered_facet",
      hiddenFromGlobalFacet: true,
      discoveredViaTypes: [...types].sort(),
    })),
  ].sort((a, b) => b.count - a.count);

  const productsBlock = {
    products: {
      generatedAt: new Date().toISOString().slice(0, 10),
      entries,
    },
  };

  if (format === "json") {
    await Deno.writeTextFile(outFile, JSON.stringify(productsBlock, null, 2));
  } else {
    await Deno.writeTextFile(outFile, stringifyYaml(productsBlock));
  }

  const hiddenCount = entries.filter((p) => p.hiddenFromGlobalFacet).length;
  logger.info(`${entries.length} total products written to ${outFile}`);
  logger.info(`  From global facet:       ${globalStandalone.size}`);
  logger.info(`  Hidden (type-filtered):  ${hiddenCount}`);
  logger.info(`NOTE: copy the products: block into config.yaml (this command never edits it).`);
  return 0;
}
