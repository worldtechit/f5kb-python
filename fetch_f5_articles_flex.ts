/**
 * Fetches F5 KB articles from the F5 Coveo search API with flexible
 * product and content type filtering.
 *
 * Handles Coveo's 5000-result pagination limit automatically by splitting
 * queries into date-range chunks when needed.
 *
 * Usage:
 *   deno run --allow-net --allow-write fetch_f5_articles_flex.ts [options]
 *
 * Options:
 *   --product=NAME    Filter by product name (e.g. "BIG-IP", "NGINX Plus")
 *   --type=NAME       Filter by document type (e.g. "Support Solution", "Release Note")
 *   --limit=N         Stop after N articles (default: all)
 *   --output=FILE     JSON output file (default: auto-named from filters)
 *   --csv=FILE        Also write a CSV output file
 *   --page-size=N     Results per API call (default: 100, max: 1000)
 *   --list-products        Print products known to the global facet (fast, ~30 API calls)
 *   --list-types           Print all available document types with counts and exit
 *   --discover-products    Deep scan: queries every document type to surface products
 *                          hidden from the global facet. Writes supplemental_products.json
 *                          and prints a summary. Takes ~3-4 minutes (~250 API calls).
 *
 * Examples:
 *   deno run --allow-net --allow-write fetch_f5_articles_flex.ts \
 *       --product="NGINX Plus" --type="Security Advisory" --csv=out.csv
 *
 *   deno run --allow-net --allow-write fetch_f5_articles_flex.ts \
 *       --product="BIG-IP" --type="Support Solution" --output=all.json --csv=all.csv
 *
 *   deno run --allow-net --allow-write fetch_f5_articles_flex.ts --list-types
 *   deno run --allow-net --allow-write fetch_f5_articles_flex.ts --discover-products
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

// Coveo enforces a hard limit: firstResult + numberOfResults <= 5000
const COVEO_MAX_OFFSET = 5000;

// Date range to cover all possible articles
const EPOCH_START_MS = new Date("2000-01-01").getTime();
const EPOCH_END_MS = new Date(`${new Date().getFullYear() + 2}-01-01`).getTime();

interface CoveoConfig {
  platformUrl: string;
  accessToken: string;
  organizationId: string;
}

interface Article {
  name: string;
  link: string;
  summary: string;
  publicationDate: string;
  modificationDate: string;
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
// Helpers
// ---------------------------------------------------------------------------

function buildAq(
  product: string | undefined,
  type: string | undefined,
  dateStartMs?: number,
  dateEndMs?: number,
): string {
  const parts: string[] = [];
  if (type) parts.push(`@f5_document_type=="${type}"`);
  if (product) parts.push(`@f5_version=="${product}"`);
  if (dateStartMs !== undefined) parts.push(`@date>=${toCoveoDate(dateStartMs)}`);
  if (dateEndMs !== undefined) parts.push(`@date<${toCoveoDate(dateEndMs)}`);
  return parts.join(" ");
}

// Coveo date filter format: YYYY/MM/DD@HH:MM:SS (UTC)
function toCoveoDate(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return [
    `${d.getUTCFullYear()}/${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}`,
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`,
  ].join("@");
}

function formatDate(tsMs: number | undefined): string {
  if (!tsMs) return "";
  return new Date(tsMs).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function sanitizeName(s: string): string {
  return s.replace(/[^a-zA-Z0-9_-]/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "");
}

// ---------------------------------------------------------------------------
// Coveo API calls
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

async function getCount(config: CoveoConfig, aq: string): Promise<number> {
  const data = await coveoPost(config, {
    q: "",
    aq: aq || undefined,
    numberOfResults: 0,
    searchHub: "myF5",
  });
  return ((data.totalCountFiltered ?? data.totalCount) as number) ?? 0;
}

async function listFacetValues(
  config: CoveoConfig,
  field: string,
  filterAq?: string,
): Promise<Array<{ value: string; count: number }>> {
  const data = await coveoPost(config, {
    q: "",
    ...(filterAq ? { aq: filterAq } : {}),
    numberOfResults: 0,
    searchHub: "myF5",
    // 5000 is needed for f5_version: the field has ~2,769 values total, most of
    // which are versioned hierarchy entries (e.g. "BIG-IP LTM|16|16.1|16.1.0").
    // 500 fills up with versioned entries before all top-level product names are
    // returned, so less-common products silently disappear from --list-products.
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

interface ProductEntry {
  product: string;
  count: number;
  source: "global_facet" | "type_filtered_facet";
  hiddenFromGlobalFacet: boolean;
  discoveredViaTypes?: string[];
}

const enc = new TextEncoder();
function writeInline(s: string) {
  Deno.stdout.writeSync(enc.encode(s));
}

async function discoverProducts(
  config: CoveoConfig,
  outputFile = "supplemental_products.json",
): Promise<void> {
  // Step 1: All document types
  console.log("Getting all document types...");
  const docTypeValues = await listFacetValues(config, "f5_document_type");
  const docTypes = docTypeValues
    .filter((v) => !v.value.includes("|"))
    .sort((a, b) => b.count - a.count);
  console.log(`  Found ${docTypes.length} document types`);
  await new Promise((r) => setTimeout(r, 200));

  // Step 2: Global f5_version facet — standalone top-level entries only
  console.log("Getting global product facet (baseline)...");
  const globalValues = await listFacetValues(config, "f5_version");
  const globalStandalone = new Map<string, number>(
    globalValues
      .filter((v) => !v.value.includes("|"))
      .map((v) => [v.value, v.count]),
  );
  console.log(`  Global facet: ${globalStandalone.size} top-level products`);
  await new Promise((r) => setTimeout(r, 200));

  // Step 3: Type-filtered facet for each document type
  // Products excluded from the global facet surface when the facet is
  // computed over the narrower set of documents matching a specific type.
  console.log("Running type-filtered facet queries...");
  const hiddenFoundIn = new Map<string, Set<string>>();

  for (const { value: docType, count: typeCount } of docTypes) {
    const aq = `@f5_document_type=="${docType}"`;
    writeInline(`  [${docType}] (${typeCount.toLocaleString()} docs) ... `);

    const values = await listFacetValues(config, "f5_version", aq);

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

    console.log(`${values.length} values, ${newFound} new hidden products`);
    await new Promise((r) => setTimeout(r, 200));
  }

  console.log(`\n  Total hidden products discovered: ${hiddenFoundIn.size}`);

  // Step 4: Fetch total counts for hidden products
  const hiddenCounts = new Map<string, number>();
  if (hiddenFoundIn.size > 0) {
    console.log("Fetching total counts for hidden products...");
    for (const [name] of hiddenFoundIn.entries()) {
      writeInline(`  Counting "${name}" ... `);
      const count = await getCount(config, `@f5_version=="${name}"`);
      hiddenCounts.set(name, count);
      console.log(count.toLocaleString());
      await new Promise((r) => setTimeout(r, 200));
    }
  }

  // Step 5: Build and write output
  const output: ProductEntry[] = [
    ...Array.from(globalStandalone.entries()).map(([product, count]) => ({
      product,
      count,
      source: "global_facet" as const,
      hiddenFromGlobalFacet: false,
    })),
    ...Array.from(hiddenFoundIn.entries()).map(([product, types]) => ({
      product,
      count: hiddenCounts.get(product) ?? 0,
      source: "type_filtered_facet" as const,
      hiddenFromGlobalFacet: true,
      discoveredViaTypes: [...types].sort(),
    })),
  ].sort((a, b) => b.count - a.count);

  await Deno.writeTextFile(outputFile, JSON.stringify(output, null, 2));

  const hiddenCount = output.filter((p) => p.hiddenFromGlobalFacet).length;
  console.log(`\n${output.length} total products written to ${outputFile}`);
  console.log(`  From global facet:       ${globalStandalone.size}`);
  console.log(`  Hidden (type-filtered):  ${hiddenCount}`);
  console.log("\nTop 20 by total count:");
  for (const { product, count, hiddenFromGlobalFacet } of output.slice(0, 20)) {
    const flag = hiddenFromGlobalFacet ? " [HIDDEN]" : "";
    console.log(`  ${product.padEnd(45)} ${count.toLocaleString()}${flag}`);
  }
}

function parseResult(r: Record<string, unknown>): Article {
  const raw = r.raw as Record<string, unknown>;
  return {
    name: (r.title as string) ?? "",
    link: (r.clickUri as string) ?? (raw?.clickableuri as string) ?? "",
    summary: (r.excerpt as string) ?? "",
    publicationDate: formatDate(
      (raw?.f5_original_published_date as number) ??
        (raw?.sffirstpublisheddate as number),
    ),
    modificationDate: formatDate(
      (raw?.f5_updated_published_date as number) ??
        (raw?.sflastmodifieddate as number) ??
        (raw?.date as number),
    ),
  };
}

// Only fetch the raw fields we actually use, keeping response sizes small.
const FIELDS_TO_INCLUDE = [
  "clickableuri",
  "f5_original_published_date",
  "f5_updated_published_date",
  "sffirstpublisheddate",
  "sflastmodifieddate",
  "date",
];

// Fetch up to COVEO_MAX_OFFSET results for a single aq using standard pagination.
async function fetchPaged(
  config: CoveoConfig,
  aq: string,
  pageSize: number,
  maxResults: number,
  onProgress?: (n: number) => void,
): Promise<Article[]> {
  const articles: Article[] = [];
  let firstResult = 0;

  while (articles.length < maxResults) {
    const toFetch = Math.min(pageSize, maxResults - articles.length, COVEO_MAX_OFFSET - firstResult);
    if (toFetch <= 0) break;

    const data = await coveoPost(config, {
      q: "",
      aq: aq || undefined,
      numberOfResults: toFetch,
      firstResult,
      searchHub: "myF5",
      sortCriteria: "date descending",
      fieldsToInclude: FIELDS_TO_INCLUDE,
    });

    const batch = ((data.results as Array<Record<string, unknown>>) ?? []).map(parseResult);
    articles.push(...batch);
    firstResult += batch.length;
    onProgress?.(articles.length);

    if (batch.length < toFetch) break; // last page
    if (firstResult >= COVEO_MAX_OFFSET) break; // hit Coveo limit

    await new Promise((r) => setTimeout(r, 150));
  }

  return articles;
}

// Recursively split a date range until each chunk fits within COVEO_MAX_OFFSET.
// Depth guard prevents infinite recursion if articles share the exact same timestamp.
async function fetchChunked(
  config: CoveoConfig,
  baseAq: string,
  startMs: number,
  endMs: number,
  pageSize: number,
  maxResults: number,
  onProgress: (n: number) => void,
  collected: Article[],
  depth = 0,
): Promise<void> {
  if (collected.length >= maxResults) return;

  const aq = buildAq(undefined, undefined, startMs, endMs)
    ? `${baseAq} ${buildAq(undefined, undefined, startMs, endMs)}`.trim()
    : baseAq;

  const total = await getCount(config, aq);
  if (total === 0) return;

  if (total <= COVEO_MAX_OFFSET || depth >= 25) {
    // Safe to page through directly
    const remaining = maxResults - collected.length;
    const batch = await fetchPaged(
      config,
      aq,
      pageSize,
      Math.min(total, remaining),
      (n) => onProgress(collected.length + n),
    );
    collected.push(...batch);
    return;
  }

  // Split the range in half and recurse
  const midMs = Math.floor((startMs + endMs) / 2);
  if (midMs === startMs) {
    // Range can't be split further — fetch what we can
    const batch = await fetchPaged(config, aq, pageSize, maxResults - collected.length);
    collected.push(...batch);
    return;
  }

  await fetchChunked(config, baseAq, startMs, midMs, pageSize, maxResults, onProgress, collected, depth + 1);
  await fetchChunked(config, baseAq, midMs, endMs, pageSize, maxResults, onProgress, collected, depth + 1);
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

function toCSV(articles: Article[]): string {
  const esc = (s: string) => `"${s.replace(/"/g, '""').replace(/\n/g, " ")}"`;
  const header = "Name,Link,Summary,Publication Date,Modification Date";
  const rows = articles.map((a) =>
    [a.name, a.link, a.summary, a.publicationDate, a.modificationDate].map(esc).join(",")
  );
  return [header, ...rows].join("\n");
}

function printSample(articles: Article[], n = 5) {
  console.log(`\n--- Sample (first ${Math.min(n, articles.length)}) ---`);
  for (const a of articles.slice(0, n)) {
    console.log(`\nName:              ${a.name}`);
    console.log(`Link:              ${a.link}`);
    console.log(`Summary:           ${a.summary.slice(0, 120)}...`);
    console.log(`Publication Date:  ${a.publicationDate}`);
    console.log(`Modification Date: ${a.modificationDate}`);
  }
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

const args: Record<string, string> = {};
for (const a of Deno.args) {
  if (!a.startsWith("--")) continue;
  const eq = a.indexOf("=");
  if (eq === -1) args[a.slice(2)] = "true";
  else args[a.slice(2, eq)] = a.slice(eq + 1);
}

const product = args.product;
const type = args.type;
const limit = args.limit ? parseInt(args.limit) : Infinity;
const pageSize = Math.min(parseInt(args["page-size"] ?? "100"), 1000);

const slug = [
  product ? sanitizeName(product) : "",
  type ? sanitizeName(type) : "",
].filter(Boolean).join("_") || "all";

const jsonOutput = args.output ?? `f5_${slug}.json`;
const csvOutput = args.csv;

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

console.log("Fetching Coveo configuration from F5 portal...");
const config = await fetchCoveoConfig();
console.log(`Organization ID: ${config.organizationId}`);

// --discover-products / --list-products / --list-types modes
if ("discover-products" in args) {
  const outFile = args.output ?? "supplemental_products.json";
  await discoverProducts(config, outFile);
  Deno.exit(0);
}

if ("list-products" in args || "list-types" in args) {
  if ("list-types" in args) {
    console.log("\nAvailable document types:");
    const types = await listFacetValues(config, "f5_document_type");
    for (const { value, count } of types.sort((a, b) => b.count - a.count)) {
      console.log(`  ${value.padEnd(35)} ${count.toLocaleString()}`);
    }
  }
  if ("list-products" in args) {
    console.log("\nAvailable products (top-level only, global facet):");
    const products = await listFacetValues(config, "f5_version");
    const topLevel = products
      .filter(({ value }) => !value.includes("|"))
      .sort((a, b) => b.count - a.count);
    for (const { value, count } of topLevel) {
      console.log(`  ${value.padEnd(45)} ${count.toLocaleString()}`);
    }
    console.log("\n  Note: some products are hidden from the global facet.");
    console.log("  Use --discover-products for the complete list.");
  }
  Deno.exit(0);
}

const baseAq = buildAq(product, type);
if (!baseAq) {
  console.log("No --product or --type specified — fetching all articles.");
} else {
  console.log(`Filter: ${baseAq}`);
}

// Check total and decide strategy
const total = await getCount(config, baseAq);
const target = isFinite(limit) ? Math.min(limit, total) : total;
console.log(`Total matching articles: ${total.toLocaleString()}`);
console.log(`Fetching ${target.toLocaleString()} articles...`);

if (total > COVEO_MAX_OFFSET && isFinite(limit) && limit <= COVEO_MAX_OFFSET) {
  console.log("(limit is within Coveo's range, using standard pagination)");
}

let lastReported = 0;
const allArticles: Article[] = [];

function onProgress(n: number) {
  // Report at every 500 or at completion
  if (n - lastReported >= 500 || n >= target) {
    const pct = Math.round((n / target) * 100);
    console.log(`  Fetched ${n.toLocaleString()} / ${target.toLocaleString()} (${pct}%)`);
    lastReported = n;
  }
}

if (total <= COVEO_MAX_OFFSET || (isFinite(limit) && limit <= COVEO_MAX_OFFSET)) {
  // Standard pagination — no chunking needed
  const batch = await fetchPaged(config, baseAq, pageSize, target, onProgress);
  allArticles.push(...batch);
} else {
  // Chunked fetch to work around Coveo's 5000-result limit
  console.log("(total exceeds 5,000 — using date-range chunking)");
  await fetchChunked(
    config,
    baseAq,
    EPOCH_START_MS,
    EPOCH_END_MS,
    pageSize,
    target,
    onProgress,
    allArticles,
  );
}

console.log(`\nTotal fetched: ${allArticles.length.toLocaleString()} articles`);

// Deduplicate by link (chunking can produce duplicates at chunk boundaries)
const seen = new Set<string>();
const deduped = allArticles.filter((a) => {
  if (seen.has(a.link)) return false;
  seen.add(a.link);
  return true;
});

if (deduped.length !== allArticles.length) {
  console.log(`After deduplication: ${deduped.length.toLocaleString()} articles`);
}

await Deno.writeTextFile(jsonOutput, JSON.stringify(deduped, null, 2));
console.log(`JSON written to: ${jsonOutput}`);

if (csvOutput) {
  await Deno.writeTextFile(csvOutput, toCSV(deduped));
  console.log(`CSV written to: ${csvOutput}`);
}

printSample(deduped);
