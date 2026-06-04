// Flat-article helpers shared by the `fetch` and `recent` subcommands. These
// reproduce the small per-result helpers that lived in fetch_f5_articles_flex.ts
// and fetch_recent_by_type.ts (buildAq / parseResult / toCSV), so both commands
// emit byte-identical output to the old scripts. Kept separate from the heavier
// pagination logic in paging.ts because these are flat (5-field) projections.

import { CoveoClient, CoveoResult } from "./client.ts";

// Coveo enforces a hard limit: firstResult + numberOfResults <= 5000.
export const COVEO_MAX_OFFSET = 5000;

// The flat article shape the flex/recent exports write.
export interface FlatArticle {
  name: string;
  link: string;
  summary: string;
  publicationDate: string;
  modificationDate: string;
  // Raw modification timestamp (ms), used by `recent` for exact client-side
  // date filtering. Internal only — stripped before the JSON is written.
  modMs?: number;
}

// Coveo date filter format: YYYY/MM/DD@HH:MM:SS (UTC). Duplicated from dates.ts
// to keep buildAq self-contained (matches the old flex script verbatim).
function toCoveoDate(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return [
    `${d.getUTCFullYear()}/${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}`,
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`,
  ].join("@");
}

// "en-US" short date ("Mon D, YYYY"), matching fetch_f5_articles_flex.ts /
// fetch_recent_by_type.ts formatDate (NOT the dates.ts formatDate, which is UTC).
function formatDate(tsMs: number | undefined): string {
  if (!tsMs) return "";
  return new Date(tsMs).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

// Most specific available modification timestamp (ms) on a Coveo `raw` bag.
function modMsOf(raw: Record<string, unknown> | undefined): number | undefined {
  return (raw?.f5_updated_published_date as number) ??
    (raw?.sflastmodifieddate as number) ??
    (raw?.date as number);
}

// Build an aq from product/type filters (+ optional @date window). Verbatim from
// fetch_f5_articles_flex.ts buildAq.
export function buildAq(
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

// Flatten one Coveo result into the flat 5-field article (plus internal modMs).
// Verbatim from fetch_recent_by_type.ts parseResult (the superset of the flex
// version — flex omits modMs, which is harmless extra data dropped on write).
export function parseResult(r: CoveoResult): FlatArticle {
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

// CSV serialization, verbatim from fetch_f5_articles_flex.ts toCSV.
export function toCSV(articles: FlatArticle[]): string {
  const esc = (s: string) => `"${s.replace(/"/g, '""').replace(/\n/g, " ")}"`;
  const header = "Name,Link,Summary,Publication Date,Modification Date";
  const rows = articles.map((a) =>
    [a.name, a.link, a.summary, a.publicationDate, a.modificationDate].map(esc).join(",")
  );
  return [header, ...rows].join("\n");
}

// The flat-projection raw fields the old scripts requested (keeps responses small).
export const FLAT_FIELDS_TO_INCLUDE = [
  "clickableuri",
  "f5_original_published_date",
  "f5_updated_published_date",
  "sffirstpublisheddate",
  "sflastmodifieddate",
  "date",
];

// Flat standard pagination (<= COVEO_MAX_OFFSET). Reproduces the flex/recent
// fetchPaged: sortCriteria "date descending", FLAT_FIELDS_TO_INCLUDE, 150ms pause.
export async function fetchFlatPaged(
  client: CoveoClient,
  aq: string,
  pageSize: number,
  maxResults: number,
  onProgress?: (n: number) => void,
  pauseMs = 150,
): Promise<FlatArticle[]> {
  const articles: FlatArticle[] = [];
  let firstResult = 0;
  while (articles.length < maxResults) {
    const toFetch = Math.min(
      pageSize,
      maxResults - articles.length,
      COVEO_MAX_OFFSET - firstResult,
    );
    if (toFetch <= 0) break;
    const data = await client.post({
      q: "",
      aq: aq || undefined,
      numberOfResults: toFetch,
      firstResult,
      searchHub: "myF5",
      sortCriteria: "date descending",
      fieldsToInclude: FLAT_FIELDS_TO_INCLUDE,
    });
    const batch = ((data.results as CoveoResult[]) ?? []).map(parseResult);
    articles.push(...batch);
    firstResult += batch.length;
    onProgress?.(articles.length);
    if (batch.length < toFetch) break; // last page
    if (firstResult >= COVEO_MAX_OFFSET) break; // hit Coveo limit
    await new Promise((r) => setTimeout(r, pauseMs));
  }
  return articles;
}

// Recursive date-range chunking with a depth>=25 fallback to direct paging.
// Reproduces the flex/recent fetchChunked behavior (NOT the dump's keyset path).
export async function fetchFlatChunked(
  client: CoveoClient,
  baseAq: string,
  startMs: number,
  endMs: number,
  pageSize: number,
  maxResults: number,
  onProgress: (n: number) => void,
  collected: FlatArticle[],
  pauseMs = 150,
  depth = 0,
): Promise<void> {
  if (collected.length >= maxResults) return;
  const window = buildAq(undefined, undefined, startMs, endMs);
  const aq = window ? `${baseAq} ${window}`.trim() : baseAq;
  const total = await client.getCount(aq);
  if (total === 0) return;

  if (total <= COVEO_MAX_OFFSET || depth >= 25) {
    const remaining = maxResults - collected.length;
    const batch = await fetchFlatPaged(
      client,
      aq,
      pageSize,
      Math.min(total, remaining),
      (n) => onProgress(collected.length + n),
      pauseMs,
    );
    collected.push(...batch);
    return;
  }

  const midMs = Math.floor((startMs + endMs) / 2);
  if (midMs === startMs) {
    const batch = await fetchFlatPaged(
      client,
      aq,
      pageSize,
      maxResults - collected.length,
      undefined,
      pauseMs,
    );
    collected.push(...batch);
    return;
  }
  await fetchFlatChunked(
    client,
    baseAq,
    startMs,
    midMs,
    pageSize,
    maxResults,
    onProgress,
    collected,
    pauseMs,
    depth + 1,
  );
  await fetchFlatChunked(
    client,
    baseAq,
    midMs,
    endMs,
    pageSize,
    maxResults,
    onProgress,
    collected,
    pauseMs,
    depth + 1,
  );
}
