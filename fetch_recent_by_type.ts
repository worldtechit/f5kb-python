/**
 * Fetches recently-modified F5 KB articles from the Coveo search API and writes
 * one JSON file per document type into a user-specified output directory.
 *
 * For every document type, it collects all articles modified within the last
 * --days days and writes { name, link, summary, publicationDate, modificationDate }
 * for each to <outdir>/<Type>.json.
 *
 * "Modified within X days" is filtered server-side on Coveo's @date field. That
 * field is the index date, but the F5 index re-indexes an article whenever it
 * changes, so @date tracks the modification date and is the only date field
 * present on all 15 document types (the sf and f5_updated_published_date fields
 * exist only on Salesforce Knowledge + Bug Tracker types). Verified 2026-06-02:
 * for recent items date == f5_updated_published_date == sflastmodifieddate.
 * The per-record modificationDate is still derived from the most specific field
 * available (f5_updated_published_date -> sflastmodifieddate -> date).
 *
 * Coveo's 5,000-result offset cap is handled automatically via recursive
 * date-range chunking (Manual alone exceeds 5,000 in a 30-day window).
 *
 * Usage:
 *   deno run --allow-net --allow-write fetch_recent_by_type.ts \
 *       --days=30 --out=recent_articles
 *
 * Options:
 *   --days=N         REQUIRED. Window size: articles modified in the last N days.
 *   --out=DIR        REQUIRED. Output directory for the per-type JSON files
 *                    (created if it does not exist).
 *   --types="A,B"    Comma-separated subset of document types to fetch.
 *                    Default: all types present in the index.
 *   --page-size=N    Results per API call (default: 500, max: 1000).
 *   --limit=N        Cap articles per type (default: no cap). For testing.
 *
 * Examples:
 *   deno run --allow-net --allow-write fetch_recent_by_type.ts --days=7 --out=last_week
 *   deno run --allow-net --allow-write fetch_recent_by_type.ts \
 *       --days=30 --out=out --types="Support Solution,Release Note,Security Advisory"
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
  // Raw modification timestamp (ms) used for exact client-side date filtering.
  // Internal only — stripped before the JSON is written.
  modMs?: number;
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

// Coveo date filter format: YYYY/MM/DD@HH:MM:SS (UTC)
function toCoveoDate(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return [
    `${d.getUTCFullYear()}/${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}`,
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`,
  ].join("@");
}

// aq fragment for a @date window. Either bound is optional.
function dateAq(startMs?: number, endMs?: number): string {
  const parts: string[] = [];
  if (startMs !== undefined) parts.push(`@date>=${toCoveoDate(startMs)}`);
  if (endMs !== undefined) parts.push(`@date<${toCoveoDate(endMs)}`);
  return parts.join(" ");
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

// Most specific available modification timestamp (ms). For Salesforce Knowledge
// and Bug Tracker types this is the real content-modified date; for the other
// types only the index date is available (which tracks modification anyway).
function modMsOf(raw: Record<string, unknown>): number | undefined {
  return (raw?.f5_updated_published_date as number) ??
    (raw?.sflastmodifieddate as number) ??
    (raw?.date as number);
}

function parseResult(r: Record<string, unknown>): Article {
  const raw = r.raw as Record<string, unknown>;
  const modMs = modMsOf(raw);
  return {
    name: (r.title as string) ?? "",
    link: (r.clickUri as string) ?? (raw?.clickableuri as string) ?? "",
    summary: (r.excerpt as string) ?? "",
    publicationDate: formatDate(
      (raw?.f5_original_published_date as number) ??
        (raw?.sffirstpublisheddate as number),
    ),
    modificationDate: formatDate(modMs),
    modMs,
  };
}

// Only fetch the raw fields we actually use, keeping response sizes below the
// Coveo 20 MB per-response cap so larger page sizes stay safe.
const FIELDS_TO_INCLUDE = [
  "clickableuri",
  "f5_original_published_date",
  "f5_updated_published_date",
  "sffirstpublisheddate",
  "sflastmodifieddate",
  "date",
];

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

// All document types currently present in the index, with counts.
async function listDocumentTypes(
  config: CoveoConfig,
): Promise<Array<{ value: string; count: number }>> {
  const data = await coveoPost(config, {
    q: "",
    numberOfResults: 0,
    searchHub: "myF5",
    facets: [{ field: "f5_document_type", numberOfValues: 5000, type: "specific" }],
  });
  const facets = (data.facets as Array<Record<string, unknown>>) ?? [];
  const facet = facets.find((f) => f.field === "f5_document_type");
  return ((facet?.values as Array<Record<string, unknown>>) ?? [])
    .map((v) => ({ value: v.value as string, count: v.numberOfResults as number }))
    .filter((v) => !v.value.includes("|"));
}

// Page through a single aq using standard pagination (safe only when the total
// for that aq is <= COVEO_MAX_OFFSET).
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

    await new Promise((r) => setTimeout(r, 120));
  }

  return articles;
}

// Recursively split a date window until each chunk fits within COVEO_MAX_OFFSET,
// then page each leaf. Depth guard prevents infinite recursion when many
// articles share the same timestamp.
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

  const window = dateAq(startMs, endMs);
  const aq = window ? `${baseAq} ${window}`.trim() : baseAq;

  const total = await getCount(config, aq);
  if (total === 0) return;

  if (total <= COVEO_MAX_OFFSET || depth >= 25) {
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

  const midMs = Math.floor((startMs + endMs) / 2);
  if (midMs === startMs) {
    const batch = await fetchPaged(config, aq, pageSize, maxResults - collected.length);
    collected.push(...batch);
    return;
  }

  await fetchChunked(config, baseAq, startMs, midMs, pageSize, maxResults, onProgress, collected, depth + 1);
  await fetchChunked(config, baseAq, midMs, endMs, pageSize, maxResults, onProgress, collected, depth + 1);
}

// Fetch all articles of one type modified since cutoffMs.
async function fetchTypeSince(
  config: CoveoConfig,
  type: string,
  cutoffMs: number,
  endMs: number,
  pageSize: number,
  limit: number,
): Promise<Article[]> {
  const baseAq = `@f5_document_type=="${type}"`;
  const collected: Article[] = [];
  await fetchChunked(
    config,
    baseAq,
    cutoffMs,
    endMs,
    pageSize,
    limit,
    () => {},
    collected,
  );
  // The @date server-side filter is a superset: @date (re-index date) is always
  // >= the content modification date, so a few items re-indexed recently can
  // carry an older modification date. Filter to the exact window here.
  return collected.filter((a) => a.modMs === undefined || a.modMs >= cutoffMs);
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

function usage(msg?: string): never {
  if (msg) console.error(`Error: ${msg}\n`);
  console.error(
    "Usage: deno run --allow-net --allow-write fetch_recent_by_type.ts \\\n" +
      "         --days=N --out=DIR [--types=\"A,B\"] [--page-size=N] [--limit=N]",
  );
  Deno.exit(msg ? 1 : 0);
}

if ("help" in args) usage();

const days = Number(args.days);
if (!args.days || !Number.isFinite(days) || days <= 0) {
  usage("--days must be a positive number");
}

const outDir = args.out;
if (!outDir) usage("--out (output directory) is required");

const pageSize = Math.min(parseInt(args["page-size"] ?? "500"), 1000);
const limit = args.limit ? parseInt(args.limit) : Infinity;
const typesFilter = args.types
  ? args.types.split(",").map((s) => s.trim()).filter(Boolean)
  : null;

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const nowMs = Date.now();
const cutoffMs = nowMs - days * 86400000;
// End the window slightly in the future so the newest items are never clipped.
const endMs = nowMs + 86400000;

console.log("Fetching Coveo configuration from F5 portal...");
const config = await fetchCoveoConfig();
console.log(`Organization ID: ${config.organizationId}`);
console.log(
  `Window: articles modified since ${new Date(cutoffMs).toISOString().slice(0, 10)} ` +
    `(last ${days} day${days === 1 ? "" : "s"})\n`,
);

const allTypes = await listDocumentTypes(config);
const allTypeNames = new Set(allTypes.map((t) => t.value));

let selected: string[];
if (typesFilter) {
  const unknown = typesFilter.filter((t) => !allTypeNames.has(t));
  if (unknown.length) {
    console.warn(`Warning: unknown type(s) ignored: ${unknown.join(", ")}`);
    console.warn(`Known types: ${[...allTypeNames].join(", ")}\n`);
  }
  selected = typesFilter.filter((t) => allTypeNames.has(t));
  if (!selected.length) usage("no valid types selected");
} else {
  selected = allTypes.map((t) => t.value);
}

await Deno.mkdir(outDir, { recursive: true });

const summary: Array<{ type: string; count: number; file: string }> = [];

for (const type of selected) {
  Deno.stdout.writeSync(new TextEncoder().encode(`${type.padEnd(24)} ... `));
  const articles = await fetchTypeSince(config, type, cutoffMs, endMs, pageSize, limit);

  const fileName = `${sanitizeName(type)}.json`;
  const filePath = `${outDir}/${fileName}`;
  const payload = {
    documentType: type,
    days,
    cutoff: new Date(cutoffMs).toISOString(),
    generatedAt: new Date(nowMs).toISOString(),
    count: articles.length,
    articles: articles.map(({ name, link, summary, publicationDate, modificationDate }) => ({
      name,
      link,
      summary,
      publicationDate,
      modificationDate,
    })),
  };
  await Deno.writeTextFile(filePath, JSON.stringify(payload, null, 2));

  summary.push({ type, count: articles.length, file: fileName });
  console.log(`${articles.length} article${articles.length === 1 ? "" : "s"} -> ${filePath}`);
}

// Write a manifest index across all types.
const manifestPath = `${outDir}/_index.json`;
await Deno.writeTextFile(
  manifestPath,
  JSON.stringify(
    {
      days,
      cutoff: new Date(cutoffMs).toISOString(),
      generatedAt: new Date(nowMs).toISOString(),
      totalArticles: summary.reduce((a, s) => a + s.count, 0),
      types: summary,
    },
    null,
    2,
  ),
);

const total = summary.reduce((a, s) => a + s.count, 0);
console.log(
  `\nDone. ${total} article${total === 1 ? "" : "s"} across ${selected.length} type(s) ` +
    `written to ${outDir}/ (manifest: ${manifestPath})`,
);
