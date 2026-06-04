/**
 * Dumps full metadata + content for F5 KB articles, one file per article,
 * separated by document type, driven by a per-type config YAML.
 *
 * Scope: all 15 F5 document types are configured in dump_config.yaml, each with
 * a curated metadata keep-list and its correct body field. The script is generic
 * over types — only the types listed in the config are dumped. Note that the
 * TechComm/sitemap types (Manual, Release Note, Supplemental Document, F5 GitHub)
 * and Bug Tracker expose no body field via the search index, so their content
 * objects are empty (content: []).
 *
 * What it does:
 *   1. Fetches a guest Coveo token from the F5 portal (no login required).
 *   2. For each configured type, pulls every article modified in the last
 *      --days days (server-side @date window + recursive chunking to beat the
 *      5,000-offset cap, then an exact client-side modification-date filter).
 *   3. Requests ALL fields from Coveo (small page size to stay under the 20 MB
 *      per-response cap) so nothing is missed.
 *   4. Writes one JSON file per article to <out>/<TypeKey>/<id>.json, splitting
 *      fields into "metadata" and "content" objects per the config.
 *   5. Builds a field catalogue (_catalogue.json + _catalogue.md) per type:
 *      every field seen, its source (top-level vs raw), observed type(s),
 *      occurrence count, a sample value, and a description pulled from
 *      available_fields.txt when documented. Use it to refine the config YAML.
 *
 * Usage:
 *   deno run --allow-net --allow-read --allow-write dump_articles.ts \
 *       --days=30 --out=dump
 *
 * Options:
 *   --days=N         Only dump articles modified in the last N days.
 *   --all            Dump the entire corpus (no lower date bound). Use one of
 *                    --days or --all.
 *   --out=DIR        REQUIRED. Output directory (created if missing).
 *   --config=FILE    Config YAML (default: dump_config.yaml).
 *   --fields-doc=F   Field-description reference (default: available_fields.txt).
 *                    Used only to annotate the catalogue; optional.
 *   --types="A,B"    Subset of config type keys to dump (default: all in config).
 *   --page-size=N    Results per API call (default: 200, max: 500). Coveo caps
 *                    each response at 20 MB; if a page exceeds that, the script
 *                    automatically halves the page size for that request and
 *                    retries, so large content types degrade gracefully.
 *   --limit=N        Cap articles per type (default: no cap). For testing.
 */

import { parse as parseYaml } from "jsr:@std/yaml@^1";

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

type CoveoResult = Record<string, unknown>;

interface TypeConfig {
  documentType: string;
  metadata: "*" | string[];
  content: "*" | string[];
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

// Refresh an expired guest token in place (mutates the shared config object so
// every subsequent coveoPost uses the new token).
async function refreshConfig(config: CoveoConfig): Promise<void> {
  const fresh = await fetchCoveoConfig();
  config.accessToken = fresh.accessToken;
  config.platformUrl = fresh.platformUrl;
  config.organizationId = fresh.organizationId;
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

function dateAq(startMs?: number, endMs?: number): string {
  const parts: string[] = [];
  if (startMs !== undefined) parts.push(`@date>=${toCoveoDate(startMs)}`);
  if (endMs !== undefined) parts.push(`@date<${toCoveoDate(endMs)}`);
  return parts.join(" ");
}

function sanitizeName(s: string): string {
  return s.replace(/[^a-zA-Z0-9_-]/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "");
}

// Most specific available modification timestamp (ms).
function modMsOf(raw: CoveoResult | undefined): number | undefined {
  return (raw?.f5_updated_published_date as number) ??
    (raw?.sflastmodifieddate as number) ??
    (raw?.date as number);
}

// Stable, human-friendly id for the per-article filename.
function idOf(r: CoveoResult): string {
  const raw = (r.raw as CoveoResult) ?? {};
  const candidate = (raw.f5_kb_id as string) ||
    (raw.permanentid as string) ||
    (r.uniqueId as string) ||
    (r.title as string) ||
    "article";
  return sanitizeName(candidate).slice(0, 120);
}

// ---------------------------------------------------------------------------
// Field-description reference (available_fields.txt), best-effort parse
// ---------------------------------------------------------------------------

// Parses lines like:
//   title                    string   Full title including K-number prefix
//   sfdetails__c             string   [sf]  Full article HTML body (can be large).
// into { fieldName -> "description text" }. Templated names (containing "{")
// and non-matching lines are skipped. First occurrence wins.
async function loadFieldDescriptions(path: string): Promise<Record<string, string>> {
  const map: Record<string, string> = {};
  let text: string;
  try {
    text = await Deno.readTextFile(path);
  } catch {
    return map; // optional reference; absent is fine
  }
  const lineRe = /^\s{2,}([A-Za-z][A-Za-z0-9_]*(?:__c)?)\s{2,}(\w+)\s+(.+?)\s*$/;
  for (const line of text.split("\n")) {
    if (line.includes("{")) continue;
    const m = line.match(lineRe);
    if (!m) continue;
    const [, name, , desc] = m;
    if (!(name in map)) map[name] = desc;
  }
  return map;
}

// ---------------------------------------------------------------------------
// Coveo API calls
// ---------------------------------------------------------------------------

// Per-request wall-clock timeout. fetch() has no default timeout, so a socket
// that goes dead (e.g. the machine sleeps / loses connectivity mid-request)
// would otherwise hang forever. Aborting turns it into a rejection that the
// retry/backoff below handles.
const REQUEST_TIMEOUT_MS = 60_000;

async function coveoPost(
  config: CoveoConfig,
  body: Record<string, unknown>,
  attempt = 0,
): Promise<Record<string, unknown>> {
  const MAX_RETRIES = 5;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new Error("request timeout")), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(
      `${config.platformUrl}/rest/search/v2?organizationId=${config.organizationId}`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${config.accessToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      },
    );
    if (!res.ok) {
      const text = await res.text();
      // Expired/invalid guest token: refresh it in place and retry. The Coveo
      // JWT is ~24h, but a long full-corpus dump can outlive it.
      if ((res.status === 401 || res.status === 419) && attempt < MAX_RETRIES) {
        console.warn(`  (token rejected ${res.status} — refreshing Coveo token)`);
        await refreshConfig(config);
        await new Promise((r) => setTimeout(r, 250));
        return coveoPost(config, body, attempt + 1);
      }
      // Retry transient server-side statuses; surface everything else (incl.
      // the 400 response-size error, which fetchPaged handles by shrinking).
      if ((res.status >= 500 || res.status === 429) && attempt < MAX_RETRIES) {
        await new Promise((r) => setTimeout(r, 750 * 2 ** attempt));
        return coveoPost(config, body, attempt + 1);
      }
      throw new Error(`Coveo API error ${res.status}: ${text.slice(0, 300)}`);
    }
    return await res.json();
  } catch (e) {
    // Network-level failure (timeout, connection reset): retry with backoff.
    // Don't re-retry an HTTP error we already classified above.
    const msg = (e as Error).message ?? "";
    if (attempt < MAX_RETRIES && !/Coveo API error/.test(msg)) {
      await new Promise((r) => setTimeout(r, 750 * 2 ** attempt));
      return coveoPost(config, body, attempt + 1);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
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

// Page through one aq using standard pagination (safe only when that aq's total
// is <= COVEO_MAX_OFFSET). Returns full Coveo result objects (all fields).
async function fetchPaged(
  config: CoveoConfig,
  aq: string,
  pageSize: number,
  maxResults: number,
  onProgress?: (n: number) => void,
): Promise<CoveoResult[]> {
  const out: CoveoResult[] = [];
  let firstResult = 0;
  let eff = pageSize; // effective page size; shrinks if a response is too large

  while (out.length < maxResults) {
    const toFetch = Math.min(eff, maxResults - out.length, COVEO_MAX_OFFSET - firstResult);
    if (toFetch <= 0) break;

    let data: Record<string, unknown>;
    try {
      data = await coveoPost(config, {
        q: "",
        aq: aq || undefined,
        numberOfResults: toFetch,
        firstResult,
        searchHub: "myF5",
        sortCriteria: "date descending",
        // No fieldsToInclude -> every field is returned.
      });
    } catch (e) {
      // Coveo rejects responses over 20 MB. Halve the page and retry this page.
      if (eff > 1 && /maximum size|ResponseExceededMaximumSize/i.test((e as Error).message)) {
        eff = Math.max(1, Math.floor(eff / 2));
        continue;
      }
      throw e;
    }

    const batch = (data.results as CoveoResult[]) ?? [];
    out.push(...batch);
    firstResult += batch.length;
    onProgress?.(out.length);

    if (batch.length < toFetch) break; // last page
    if (firstResult >= COVEO_MAX_OFFSET) break; // hit Coveo limit
    await new Promise((r) => setTimeout(r, 120));
  }

  return out;
}

// Recursively split a date window until each chunk fits within COVEO_MAX_OFFSET,
// then page each leaf.
async function fetchChunked(
  config: CoveoConfig,
  baseAq: string,
  startMs: number,
  endMs: number,
  pageSize: number,
  maxResults: number,
  onProgress: (n: number) => void,
  collected: CoveoResult[],
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

async function fetchTypeSince(
  config: CoveoConfig,
  documentType: string,
  cutoffMs: number,
  endMs: number,
  pageSize: number,
  limit: number,
  onProgress: (n: number) => void,
): Promise<CoveoResult[]> {
  const baseAq = `@f5_document_type=="${documentType}"`;
  const collected: CoveoResult[] = [];
  await fetchChunked(config, baseAq, cutoffMs, endMs, pageSize, limit, onProgress, collected);
  // @date (re-index date) is always >= the content modification date, so the
  // server-side window is a superset. Filter to the exact window here.
  return collected.filter((r) => {
    const m = modMsOf(r.raw as CoveoResult);
    return m === undefined || m >= cutoffMs;
  });
}

// ---------------------------------------------------------------------------
// Field selection + catalogue
// ---------------------------------------------------------------------------

// A flat view of one article's fields: top-level keys (except `raw`) plus every
// raw.* key, each tagged by source. Used for both output splitting and the
// catalogue. Bare field name is the key; top-level wins on a clash.
function flattenFields(r: CoveoResult): Map<string, { source: "top" | "raw"; value: unknown }> {
  const fields = new Map<string, { source: "top" | "raw"; value: unknown }>();
  const raw = (r.raw as CoveoResult) ?? {};
  for (const [k, v] of Object.entries(raw)) fields.set(k, { source: "raw", value: v });
  for (const [k, v] of Object.entries(r)) {
    if (k === "raw") continue;
    fields.set(k, { source: "top", value: v }); // top-level overrides raw on clash
  }
  return fields;
}

function selects(sel: "*" | string[], name: string): boolean {
  return sel === "*" || sel.includes(name);
}

// Split an article's fields into { metadata, content } per the type config.
// "content" takes precedence: a field named in content never also appears in
// metadata (even when metadata is "*").
function splitEntry(
  fields: Map<string, { source: "top" | "raw"; value: unknown }>,
  cfg: TypeConfig,
): { metadata: Record<string, unknown>; content: Record<string, unknown> } {
  const metadata: Record<string, unknown> = {};
  const content: Record<string, unknown> = {};
  const contentSel = cfg.content;
  for (const [name, { value }] of fields) {
    const isContent = selects(contentSel, name);
    if (isContent) {
      content[name] = value;
    } else if (selects(cfg.metadata, name)) {
      metadata[name] = value;
    }
  }
  return { metadata, content };
}

interface CatalogueEntry {
  field: string;
  source: "top" | "raw";
  types: Set<string>;
  occurrences: number;
  sample: string;
  description: string;
}

function jsType(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "list";
  return typeof v;
}

function sampleOf(v: unknown): string {
  if (v === null || v === undefined) return "";
  let s: string;
  if (typeof v === "string") s = v;
  else if (Array.isArray(v)) s = JSON.stringify(v);
  else if (typeof v === "object") s = JSON.stringify(v);
  else s = String(v);
  s = s.replace(/\s+/g, " ").trim();
  return s.length > 200 ? s.slice(0, 200) + "…" : s;
}

function updateCatalogue(
  cat: Map<string, CatalogueEntry>,
  fields: Map<string, { source: "top" | "raw"; value: unknown }>,
  descriptions: Record<string, string>,
): void {
  for (const [name, { source, value }] of fields) {
    let e = cat.get(name);
    if (!e) {
      e = {
        field: name,
        source,
        types: new Set(),
        occurrences: 0,
        sample: "",
        description: descriptions[name] ?? "",
      };
      cat.set(name, e);
    }
    e.occurrences++;
    e.types.add(jsType(value));
    // Keep the first non-empty sample we encounter.
    if (!e.sample) {
      const s = sampleOf(value);
      if (s) e.sample = s;
    }
  }
}

function writeCatalogue(
  dir: string,
  typeKey: string,
  documentType: string,
  cat: Map<string, CatalogueEntry>,
  totalEntries: number,
  cfg: TypeConfig,
): Promise<void[]> {
  const rows = [...cat.values()]
    .map((e) => ({
      field: e.field,
      source: e.source,
      section: selects(cfg.content, e.field)
        ? "content"
        : (selects(cfg.metadata, e.field) ? "metadata" : "unselected"),
      types: [...e.types].sort(),
      occurrences: e.occurrences,
      coverage: totalEntries ? +(e.occurrences / totalEntries).toFixed(3) : 0,
      description: e.description,
      sample: e.sample,
    }))
    .sort((a, b) => a.field.localeCompare(b.field));

  const json = {
    typeKey,
    documentType,
    totalEntries,
    fieldCount: rows.length,
    note:
      "Every field returned by the API across the dumped entries. 'section' " +
      "reflects the current config. Replace metadata: \"*\" in the config with " +
      "an explicit list of the field names you want to keep.",
    fields: rows,
  };

  // Human-readable companion.
  const md: string[] = [
    `# Field catalogue — ${documentType} (${typeKey})`,
    "",
    `Entries surveyed: ${totalEntries}  •  Fields seen: ${rows.length}`,
    "",
    "| field | source | section | type(s) | coverage | description | sample |",
    "|-------|--------|---------|---------|----------|-------------|--------|",
  ];
  const esc = (s: string) => s.replace(/\|/g, "\\|").replace(/\n/g, " ");
  for (const r of rows) {
    md.push(
      `| \`${r.field}\` | ${r.source} | ${r.section} | ${r.types.join(", ")} | ` +
        `${(r.coverage * 100).toFixed(0)}% | ${esc(r.description)} | ${esc(r.sample)} |`,
    );
  }
  md.push("");

  return Promise.all([
    Deno.writeTextFile(`${dir}/_catalogue.json`, JSON.stringify(json, null, 2)),
    Deno.writeTextFile(`${dir}/_catalogue.md`, md.join("\n")),
  ]);
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
    "Usage: deno run --allow-net --allow-read --allow-write dump_articles.ts \\\n" +
      "         (--days=N | --all) --out=DIR [--config=dump_config.yaml] [--types=\"A,B\"] \\\n" +
      "         [--fields-doc=available_fields.txt] [--page-size=N] [--limit=N]\n\n" +
      "  --days=N   Only dump articles modified in the last N days.\n" +
      "  --all      Dump the entire corpus (no lower date bound). Use for a full dump.",
  );
  Deno.exit(msg ? 1 : 0);
}

if ("help" in args) usage();

const allTime = "all" in args;
const days = Number(args.days);
if (!allTime && (!args.days || !Number.isFinite(days) || days <= 0)) {
  usage("provide --all or --days=N (a positive number)");
}

const outDir = args.out;
if (!outDir) usage("--out (output directory) is required");

const configPath = args.config ?? "dump_config.yaml";
const fieldsDocPath = args["fields-doc"] ?? "available_fields.txt";
const pageSize = Math.min(parseInt(args["page-size"] ?? "200"), 500);
const limit = args.limit ? parseInt(args.limit) : Infinity;
const typeKeyFilter = args.types
  ? args.types.split(",").map((s) => s.trim()).filter(Boolean)
  : null;

// ---------------------------------------------------------------------------
// Load config
// ---------------------------------------------------------------------------

let configDoc: { types?: Record<string, TypeConfig> };
try {
  configDoc = parseYaml(await Deno.readTextFile(configPath)) as typeof configDoc;
} catch (e) {
  usage(`could not read/parse config ${configPath}: ${(e as Error).message}`);
}
const typeConfigs = configDoc.types ?? {};
let typeKeys = Object.keys(typeConfigs);
if (!typeKeys.length) usage(`config ${configPath} has no types`);

if (typeKeyFilter) {
  const unknown = typeKeyFilter.filter((k) => !typeKeys.includes(k));
  if (unknown.length) console.warn(`Warning: type key(s) not in config ignored: ${unknown.join(", ")}`);
  typeKeys = typeKeys.filter((k) => typeKeyFilter.includes(k));
  if (!typeKeys.length) usage("no valid type keys selected");
}

// Normalize each type config (default content to [] and metadata to "*").
function normalize(c: TypeConfig): TypeConfig {
  return {
    documentType: c.documentType,
    metadata: c.metadata ?? "*",
    content: c.content ?? [],
  };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const nowMs = Date.now();
// --all: no lower bound (F5 KB predates 2000, so this captures everything).
const cutoffMs = allTime ? Date.UTC(2000, 0, 1) : nowMs - days * 86400000;
const endMs = nowMs + 86400000; // slightly future so newest items are never clipped

console.log("Fetching Coveo configuration from F5 portal...");
const config = await fetchCoveoConfig();
console.log(`Organization ID: ${config.organizationId}`);

const descriptions = await loadFieldDescriptions(fieldsDocPath);
console.log(
  `Field descriptions loaded: ${Object.keys(descriptions).length} ` +
    `(from ${fieldsDocPath})`,
);
console.log(
  allTime
    ? `Window: entire corpus (--all, no lower date bound)\n`
    : `Window: articles modified since ${new Date(cutoffMs).toISOString().slice(0, 10)} ` +
      `(last ${days} day${days === 1 ? "" : "s"})\n`,
);

await Deno.mkdir(outDir, { recursive: true });

interface TypeStatus {
  typeKey: string;
  documentType: string;
  dir: string;
  status: "ok" | "partial" | "failed";
  expected: number | null; // server count over the window
  fetched: number; // results returned before write
  written: number; // files successfully written
  writeErrors: number;
  error?: string;
}

const manifest: TypeStatus[] = [];

for (const typeKey of typeKeys) {
  const cfg = normalize(typeConfigs[typeKey]);
  const dir = sanitizeName(typeKey);
  if (!cfg.documentType) {
    console.warn(`Skipping "${typeKey}": no documentType in config`);
    manifest.push({ typeKey, documentType: "", dir, status: "failed", expected: null, fetched: 0, written: 0, writeErrors: 0, error: "no documentType in config" });
    continue;
  }

  Deno.stdout.writeSync(new TextEncoder().encode(`${typeKey.padEnd(24)} ... `));

  const st: TypeStatus = { typeKey, documentType: cfg.documentType, dir, status: "ok", expected: null, fetched: 0, written: 0, writeErrors: 0 };
  try {
    // Server-side count over the window — the target to validate against.
    const windowAq = `@f5_document_type=="${cfg.documentType}" ${dateAq(cutoffMs, endMs)}`.trim();
    st.expected = await getCount(config, windowAq);

    const results = await fetchTypeSince(config, cfg.documentType, cutoffMs, endMs, pageSize, limit, () => {});
    st.fetched = results.length;

    const typeDir = `${outDir}/${dir}`;
    await Deno.mkdir(typeDir, { recursive: true });

    const catalogue = new Map<string, CatalogueEntry>();
    const seenIds = new Map<string, number>();

    for (const r of results) {
      const fields = flattenFieldsSafe(r);
      updateCatalogue(catalogue, fields, descriptions);

      const { metadata, content } = splitEntry(fields, cfg);
      const raw = (r.raw as CoveoResult) ?? {};

      // De-dupe filenames if two articles share an id.
      let id = idOf(r);
      const n = (seenIds.get(id) ?? 0) + 1;
      seenIds.set(id, n);
      if (n > 1) id = `${id}__${n}`;

      const modMs = modMsOf(raw);
      const entry = {
        id,
        documentType: cfg.documentType,
        title: (r.title as string) ?? "",
        link: (r.clickUri as string) ?? (raw.clickableuri as string) ?? "",
        modifiedMs: modMs ?? null,
        modified: modMs ? new Date(modMs).toISOString() : null,
        capturedAt: new Date(nowMs).toISOString(),
        metadata,
        content,
      };
      // A single bad write must not abort the whole type.
      try {
        await Deno.writeTextFile(`${typeDir}/${id}.json`, JSON.stringify(entry, null, 2));
        st.written++;
      } catch (e) {
        st.writeErrors++;
        if (st.writeErrors <= 3) console.warn(`\n  write failed for ${id}: ${(e as Error).message}`);
      }
    }

    await writeCatalogue(typeDir, typeKey, cfg.documentType, catalogue, results.length, cfg);

    // Classify partial so the driver notices and can re-run the type. Undercount
    // is only meaningful with --all: there the @date window covers the whole
    // corpus, so written should equal the server count. With --days, `expected`
    // is the @date (re-index) superset and the exact mod-date filter legitimately
    // drops some, so written < expected is normal and not flagged.
    const undercount = allTime && st.expected !== null && limit === Infinity && st.written < st.expected;
    if (st.writeErrors > 0 || undercount) st.status = "partial";

    const flag = st.status === "ok" ? "" : `  [${st.status.toUpperCase()}]`;
    const exp = st.expected !== null ? `/${st.expected}` : "";
    console.log(`${st.written}${exp} article${st.written === 1 ? "" : "s"} -> ${typeDir}/${flag}`);
  } catch (e) {
    st.status = "failed";
    st.error = (e as Error).message;
    console.log(`FAILED: ${st.error}`);
  }
  manifest.push(st);
}

const failed = manifest.filter((m) => m.status === "failed");
const partial = manifest.filter((m) => m.status === "partial");
const total = manifest.reduce((a, m) => a + m.written, 0);

await Deno.writeTextFile(
  `${outDir}/_index.json`,
  JSON.stringify(
    {
      mode: allTime ? "all" : `days=${days}`,
      cutoff: new Date(cutoffMs).toISOString(),
      generatedAt: new Date(nowMs).toISOString(),
      config: configPath,
      totalArticles: total,
      counts: {
        types: manifest.length,
        ok: manifest.filter((m) => m.status === "ok").length,
        partial: partial.length,
        failed: failed.length,
      },
      types: manifest,
    },
    null,
    2,
  ),
);

console.log(
  `\nDone. ${total} article${total === 1 ? "" : "s"} across ${manifest.length} type(s) ` +
    `written to ${outDir}/ (manifest: ${outDir}/_index.json)`,
);
if (partial.length) {
  console.warn(`\nPARTIAL (${partial.length}): ` +
    partial.map((m) => `${m.typeKey} (${m.written}/${m.expected ?? "?"}, writeErr=${m.writeErrors})`).join(", "));
}
if (failed.length) {
  console.error(`\nFAILED (${failed.length}): ` +
    failed.map((m) => `${m.typeKey}: ${m.error}`).join("; "));
  console.error(`Re-run just these with --types="${failed.map((m) => m.typeKey).join(",")}"`);
  Deno.exit(1);
}

// flattenFields is defined above; this guarded wrapper keeps a bad result from
// aborting the whole run.
function flattenFieldsSafe(r: CoveoResult) {
  try {
    return flattenFields(r);
  } catch {
    return new Map<string, { source: "top" | "raw"; value: unknown }>();
  }
}
