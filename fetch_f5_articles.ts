/**
 * Fetches F5 KB articles (BIG-IP, Support Solution) from the F5 Coveo search API.
 *
 * Usage:
 *   deno run --allow-net --allow-write fetch_f5_articles.ts [--limit=500] [--output=articles.json]
 *
 * Options:
 *   --limit=N      Stop after N articles (default: all)
 *   --output=FILE  Write JSON output to FILE (default: f5_articles.json)
 *   --csv=FILE     Also write CSV output to FILE
 *   --page-size=N  Results per API call (default: 100, max: 1000)
 *
 * API limits handled automatically:
 *   - Coveo rejects firstResult + numberOfResults > 5000. When the total result
 *     count exceeds this, the script switches to recursive date-range chunking.
 *   - Coveo rejects responses larger than 20 MB. Mitigated by fieldsToInclude,
 *     which restricts each result to only the fields this script uses.
 */

// Coveo enforces: firstResult + numberOfResults <= 5000
const COVEO_MAX_OFFSET = 5000;
// Wide enough date range to cover all F5 articles
const EPOCH_START_MS = new Date("2000-01-01").getTime();
const EPOCH_END_MS = new Date(`${new Date().getFullYear() + 2}-01-01`).getTime();
// Only fetch raw fields this script uses; keeps responses well under the 20 MB limit
const FIELDS_TO_INCLUDE = [
  "clickableuri",
  "f5_original_published_date",
  "f5_updated_published_date",
  "sffirstpublisheddate",
  "sflastmodifieddate",
  "date",
];

const AURA_URL =
  "https://my.f5.com/manage/s/sfsites/aura?r=7";
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

interface Article {
  name: string;
  link: string;
  summary: string;
  publicationDate: string;
  modificationDate: string;
}

async function fetchCoveoConfig(): Promise<CoveoConfig> {
  console.log("Fetching Coveo configuration from F5 portal...");
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
  // Response is plain JSON, or wrapped as */JSON/* for error responses
  let jsonText = text;
  const jsonMatch = text.match(/^\*\/(.+?)\/\*(?:ERROR\*\/)?$/s);
  if (jsonMatch) jsonText = jsonMatch[1];
  const data = JSON.parse(jsonText);

  if (data.actions[0].state !== "SUCCESS") {
    throw new Error(`Aura action failed: ${JSON.stringify(data.actions[0].error)}`);
  }

  const config: CoveoConfig = JSON.parse(data.actions[0].returnValue.returnValue);
  console.log(`Organization ID: ${config.organizationId}`);
  return config;
}

// Coveo date filter format: YYYY/MM/DD@HH:MM:SS (UTC)
function toCoveoDate(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${d.getUTCFullYear()}/${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}` +
    `@${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

function formatDate(tsMs: number | undefined): string {
  if (!tsMs) return "";
  return new Date(tsMs).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

async function fetchPage(
  config: CoveoConfig,
  aq: string,
  firstResult: number,
  pageSize: number,
): Promise<{ results: Article[]; total: number }> {
  const res = await fetch(
    `${config.platformUrl}/rest/search/v2?organizationId=${config.organizationId}`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${config.accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        q: "",
        aq,
        numberOfResults: pageSize,
        firstResult,
        searchHub: "myF5",
        sortCriteria: "date descending",
        fieldsToInclude: FIELDS_TO_INCLUDE,
      }),
    },
  );

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Coveo API error ${res.status}: ${text.slice(0, 300)}`);
  }

  const data = await res.json();
  const total: number = data.totalCountFiltered ?? data.totalCount ?? 0;

  const results: Article[] = (data.results ?? []).map((r: Record<string, unknown>) => {
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
  });

  return { results, total };
}

function toCSV(articles: Article[]): string {
  const escape = (s: string) => `"${s.replace(/"/g, '""').replace(/\n/g, " ")}"`;
  const header = "Name,Link,Summary,Publication Date,Modification Date";
  const rows = articles.map((a) =>
    [a.name, a.link, a.summary, a.publicationDate, a.modificationDate]
      .map(escape)
      .join(",")
  );
  return [header, ...rows].join("\n");
}

// Parse CLI args
const args = Object.fromEntries(
  Deno.args.filter((a) => a.startsWith("--")).map((a) => {
    const [k, v] = a.slice(2).split("=");
    return [k, v ?? "true"];
  }),
);

const limit = args.limit ? parseInt(args.limit) : Infinity;
const pageSize = Math.min(parseInt(args["page-size"] ?? "100"), 1000);
const jsonOutput = args.output ?? "f5_articles.json";
const csvOutput = args.csv;

const config = await fetchCoveoConfig();

const BASE_AQ = '@f5_document_type=="Support Solution" @f5_version=="BIG-IP"';

// Pages through results for a single aq. Caller is responsible for ensuring
// the total for this aq is <= COVEO_MAX_OFFSET.
async function fetchAllPaged(
  aq: string,
  maxResults: number,
  grandTarget: number,
  soFar: number,
): Promise<Article[]> {
  const articles: Article[] = [];
  let firstResult = 0;
  while (articles.length < maxResults) {
    const toFetch = Math.min(
      pageSize,
      maxResults - articles.length,
      COVEO_MAX_OFFSET - firstResult,
    );
    if (toFetch <= 0) break;
    const { results } = await fetchPage(config, aq, firstResult, toFetch);
    articles.push(...results);
    firstResult += results.length;
    const pct = Math.round(((soFar + articles.length) / grandTarget) * 100);
    console.log(`  Fetched ${soFar + articles.length} / ${grandTarget} (${pct}%)`);
    if (results.length < toFetch) break;
    if (firstResult >= COVEO_MAX_OFFSET) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  return articles;
}

// Recursively splits a date range until each chunk fits within COVEO_MAX_OFFSET,
// then pages through each chunk normally.
async function fetchChunked(
  baseAq: string,
  startMs: number,
  endMs: number,
  grandTarget: number,
  collected: Article[],
  depth = 0,
): Promise<void> {
  if (collected.length >= grandTarget) return;
  const aq = `${baseAq} @date>=${toCoveoDate(startMs)} @date<${toCoveoDate(endMs)}`;
  const { total } = await fetchPage(config, aq, 0, 1);
  if (total === 0) return;
  if (total <= COVEO_MAX_OFFSET || depth >= 25) {
    const remaining = grandTarget - collected.length;
    const batch = await fetchAllPaged(aq, Math.min(total, remaining), grandTarget, collected.length);
    collected.push(...batch);
    return;
  }
  const midMs = Math.floor((startMs + endMs) / 2);
  if (midMs === startMs) {
    const batch = await fetchAllPaged(aq, grandTarget - collected.length, grandTarget, collected.length);
    collected.push(...batch);
    return;
  }
  await fetchChunked(baseAq, startMs, midMs, grandTarget, collected, depth + 1);
  await fetchChunked(baseAq, midMs, endMs, grandTarget, collected, depth + 1);
}

// Get total and choose fetch strategy
const { total: grandTotal } = await fetchPage(config, BASE_AQ, 0, 1);
const target = isFinite(limit) ? Math.min(limit, grandTotal) : grandTotal;
console.log(`Total articles available: ${grandTotal}`);
if (isFinite(limit)) console.log(`Fetching up to ${target} articles...`);
else console.log(`Fetching all ${grandTotal} articles...`);

const allArticles: Article[] = [];

if (target <= COVEO_MAX_OFFSET) {
  const batch = await fetchAllPaged(BASE_AQ, target, target, 0);
  allArticles.push(...batch);
} else {
  console.log("(total exceeds 5,000 — using date-range chunking)");
  await fetchChunked(BASE_AQ, EPOCH_START_MS, EPOCH_END_MS, target, allArticles);
}

// Deduplicate by link (chunk boundaries can overlap by one result)
const seen = new Set<string>();
const deduped = allArticles.filter((a) => {
  if (seen.has(a.link)) return false;
  seen.add(a.link);
  return true;
});
if (deduped.length !== allArticles.length) {
  console.log(`After deduplication: ${deduped.length} articles`);
  allArticles.length = 0;
  allArticles.push(...deduped);
}

console.log(`\nTotal fetched: ${allArticles.length} articles`);

// Write JSON
await Deno.writeTextFile(jsonOutput, JSON.stringify(allArticles, null, 2));
console.log(`JSON written to: ${jsonOutput}`);

// Write CSV if requested
if (csvOutput) {
  await Deno.writeTextFile(csvOutput, toCSV(allArticles));
  console.log(`CSV written to: ${csvOutput}`);
}

// Print a sample table to stdout
console.log("\n--- Sample (first 5) ---");
const sample = allArticles.slice(0, 5);
for (const a of sample) {
  console.log(`\nName:              ${a.name}`);
  console.log(`Link:              ${a.link}`);
  console.log(`Summary:           ${a.summary.slice(0, 120)}...`);
  console.log(`Publication Date:  ${a.publicationDate}`);
  console.log(`Modification Date: ${a.modificationDate}`);
}
