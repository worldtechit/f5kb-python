/**
 * Discovers all valid @f5_version filter values, including those excluded
 * from the global Coveo facet by the F5 admin.
 *
 * Strategy:
 *   1. Get all document types from the global f5_document_type facet.
 *   2. Get the global f5_version facet (baseline 73 products, with counts).
 *   3. For each document type, re-run the f5_version facet scoped to that
 *      type. Products excluded from the global facet become prominent when
 *      the facet is computed over a narrower document set.
 *   4. Collect every unique top-level name (no pipe) found across all queries.
 *   5. For names absent from the global facet, run a count query to get their
 *      true total count across all document types.
 *   6. Write supplemental_products.json with every discovered product.
 *
 * Usage:
 *   deno run --allow-net --allow-write discover_products.ts
 *
 * Output: supplemental_products.json
 */

const AURA_URL = "https://my.f5.com/manage/s/sfsites/aura?r=7";
const AURA_CONTEXT = JSON.stringify({
  mode: "PROD",
  fwuid:
    "ZkJhOVpLN2NZQkJrd2NWd3pMcnFOdzJEa1N5enhOU3R5QWl2VzNveFZTbGcxMy4tMjE0NzQ4MzY0OC4xMzEwNzIwMA",
  app: "siteforce:communityApp",
  loaded: {
    "APPLICATION@markup://siteforce:communityApp": "1547_6p-2GBd9IQWZ4UXs1Im3BQ",
  },
  dn: [],
  globals: {},
  uad: false,
});

interface CoveoConfig {
  platformUrl: string;
  accessToken: string;
  organizationId: string;
}

interface FacetValue {
  value: string;
  count: number;
}

interface ProductEntry {
  product: string;
  count: number;
  source: "global_facet" | "type_filtered_facet";
  hiddenFromGlobalFacet: boolean;
  discoveredViaTypes?: string[];
}

// ---------------------------------------------------------------------------
// Token
// ---------------------------------------------------------------------------

async function fetchCoveoConfig(): Promise<CoveoConfig> {
  const body = new URLSearchParams({
    message: JSON.stringify({
      actions: [
        {
          id: "1",
          descriptor: "aura://ApexActionController/ACTION$execute",
          callingDescriptor: "UNKNOWN",
          params: {
            classname: "HeadlessController",
            method: "getHeadlessConfiguration",
            params: {},
            cacheable: false,
            isContinuation: false,
          },
        },
      ],
    }),
    "aura.context": AURA_CONTEXT,
    "aura.pageURI": "/manage/s/global-search/%40uri",
    "aura.token": "null",
  });

  const res = await fetch(AURA_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });

  const text = await res.text();
  let jsonText = text;
  const wrapped = text.match(/^\*\/(.+?)\/\*(?:ERROR\*\/)?$/s);
  if (wrapped) jsonText = wrapped[1];
  const data = JSON.parse(jsonText);

  if (data.actions[0].state !== "SUCCESS") {
    throw new Error(`Aura action failed: ${JSON.stringify(data.actions[0].error)}`);
  }

  return JSON.parse(data.actions[0].returnValue.returnValue) as CoveoConfig;
}

// ---------------------------------------------------------------------------
// Coveo helpers
// ---------------------------------------------------------------------------

async function coveoPost(
  config: CoveoConfig,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const res = await fetch(
    `${config.platformUrl}/rest/search/v2?organizationId=${config.organizationId}`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${config.accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Coveo API error ${res.status}: ${text.slice(0, 300)}`);
  }
  return await res.json();
}

async function getFacetValues(
  config: CoveoConfig,
  field: string,
  filterAq?: string,
): Promise<FacetValue[]> {
  const data = await coveoPost(config, {
    q: "",
    ...(filterAq ? { aq: filterAq } : {}),
    numberOfResults: 0,
    searchHub: "myF5",
    facets: [{ field, numberOfValues: 5000, type: "specific" }],
  });

  const facets = (data.facets as Array<Record<string, unknown>>) ?? [];
  const facet = facets.find((f) => f.field === field);
  if (!facet) return [];

  return ((facet.values as Array<Record<string, unknown>>) ?? []).map((v) => ({
    value: v.value as string,
    count: v.numberOfResults as number,
  }));
}

async function getCount(config: CoveoConfig, aq: string): Promise<number> {
  const data = await coveoPost(config, {
    q: "",
    aq,
    numberOfResults: 0,
    searchHub: "myF5",
  });
  return ((data.totalCountFiltered ?? data.totalCount) as number) ?? 0;
}

function delay(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

const enc = new TextEncoder();
function write(s: string) {
  Deno.stdout.writeSync(enc.encode(s));
}

// Extract the top-level name from a pipe-delimited hierarchy value.
// "BIG-IP LTM|16|16.1|16.1.0" → "BIG-IP LTM"
function topLevelName(value: string): string {
  return value.split("|")[0];
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

console.log("Fetching Coveo configuration...");
const config = await fetchCoveoConfig();
console.log(`Organization ID: ${config.organizationId}`);

// Step 1: All document types
console.log("\nStep 1: Getting all document types...");
const docTypeValues = await getFacetValues(config, "f5_document_type");
const docTypes = docTypeValues
  .filter((v) => !v.value.includes("|"))
  .sort((a, b) => b.count - a.count);
console.log(`  Found ${docTypes.length} document types`);
await delay(200);

// Step 2: Global f5_version facet — baseline products
console.log("\nStep 2: Getting global f5_version facet (baseline)...");
const globalValues = await getFacetValues(config, "f5_version");

// Build a map of top-level name → count from the global facet.
// Note: a name may appear both as a standalone entry AND embedded in a
// pipe-delimited entry (e.g., "BIG-IP" appears alone AND as "BIG-IP|...").
// We use the standalone entry's count (the more accurate total).
const globalTopLevel = new Map<string, number>();
for (const { value, count } of globalValues) {
  const name = topLevelName(value);
  if (!value.includes("|")) {
    // Standalone top-level entry — use its count directly
    globalTopLevel.set(name, count);
  } else if (!globalTopLevel.has(name)) {
    // Pipe-delimited: record it as a placeholder if no standalone exists yet
    globalTopLevel.set(name, count);
  }
}

// Keep only standalone (no-pipe) entries as the definitive global list
const globalStandalone = new Map<string, number>(
  globalValues
    .filter((v) => !v.value.includes("|"))
    .map((v) => [v.value, v.count]),
);

console.log(`  Global facet returned ${globalStandalone.size} top-level products`);
await delay(200);

// Step 3: Type-filtered facet for each document type
// For each doc type, run f5_version facet scoped to @f5_document_type=="<type>"
// This surfaces products excluded from the global facet.
console.log("\nStep 3: Running type-filtered facet queries...");

// Map: product name → set of doc types it was found in (for hidden products)
const hiddenFoundIn = new Map<string, Set<string>>();

for (const { value: docType, count: typeCount } of docTypes) {
  const aq = `@f5_document_type=="${docType}"`;
  write(`  [${docType}] (${typeCount.toLocaleString()} docs) ... `);

  const values = await getFacetValues(config, "f5_version", aq);

  let newFound = 0;
  for (const { value } of values) {
    const name = topLevelName(value);
    if (!globalStandalone.has(name)) {
      if (!hiddenFoundIn.has(name)) {
        hiddenFoundIn.set(name, new Set());
        newFound++;
      }
      hiddenFoundIn.get(name)!.add(docType);
    }
  }

  console.log(`${values.length} values returned, ${newFound} new hidden products found`);
  await delay(200);
}

console.log(`\n  Total hidden products discovered: ${hiddenFoundIn.size}`);
if (hiddenFoundIn.size > 0) {
  console.log("  Hidden products:");
  for (const [name, types] of hiddenFoundIn.entries()) {
    console.log(`    "${name}" — found via: ${[...types].join(", ")}`);
  }
}

// Step 4: Get total counts for hidden products
console.log("\nStep 4: Fetching total counts for hidden products...");
const hiddenCounts = new Map<string, number>();

for (const [name] of hiddenFoundIn.entries()) {
  write(`  Counting "${name}" ... `);
  const count = await getCount(config, `@f5_version=="${name}"`);
  hiddenCounts.set(name, count);
  console.log(count.toLocaleString());
  await delay(200);
}

// Step 5: Build and write output
console.log("\nStep 5: Writing supplemental_products.json...");

const output: ProductEntry[] = [
  // Products from the global facet
  ...Array.from(globalStandalone.entries()).map(([product, count]) => ({
    product,
    count,
    source: "global_facet" as const,
    hiddenFromGlobalFacet: false,
  })),
  // Products only discoverable via type-filtered queries
  ...Array.from(hiddenFoundIn.entries()).map(([product, types]) => ({
    product,
    count: hiddenCounts.get(product) ?? 0,
    source: "type_filtered_facet" as const,
    hiddenFromGlobalFacet: true,
    discoveredViaTypes: [...types].sort(),
  })),
].sort((a, b) => b.count - a.count);

await Deno.writeTextFile("supplemental_products.json", JSON.stringify(output, null, 2));

console.log(`\nDone. ${output.length} total products written to supplemental_products.json`);
console.log(`  From global facet:         ${globalStandalone.size}`);
console.log(`  Hidden (type-filtered):    ${hiddenFoundIn.size}`);
console.log("\nTop 20 by total count:");
for (const { product, count, hiddenFromGlobalFacet } of output.slice(0, 20)) {
  const flag = hiddenFromGlobalFacet ? " [HIDDEN]" : "";
  console.log(`  ${product.padEnd(45)} ${count.toLocaleString()}${flag}`);
}

