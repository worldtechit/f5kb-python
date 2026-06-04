// Pagination strategies over the Coveo search API. Moved verbatim from
// dump_articles.ts; the only change is dependency-injection — each function takes
// a CoveoClient (its post/getCount) instead of reaching for module-global
// config/coveoPost. Algorithm, sort criteria, cursor margin, page-halving retry,
// and the 120ms inter-page pause are unchanged.

import { CoveoClient, CoveoResult } from "./client.ts";
import { dateAq, modMsOf, toCoveoDate } from "./dates.ts";

// Coveo enforces a hard limit: firstResult + numberOfResults <= 5000
export const COVEO_MAX_OFFSET = 5000;

// @rowid values (~1.8e18) exceed JS's safe-integer range, so JSON.parse rounds
// them (ULP ~256). A strict `@rowid>roundedCursor` could therefore skip a doc
// near a page boundary. Instead, back the cursor off by a margin well above the
// rounding error and use `>=`, then dedup by permanentid — boundary docs get
// re-fetched (and dropped) rather than skipped. The margin is tiny next to the
// per-doc rowid spacing, so each page still makes progress.
export const CURSOR_MARGIN = 4096n;

// Page through one aq using standard pagination (safe only when that aq's total
// is <= COVEO_MAX_OFFSET). Returns full Coveo result objects (all fields).
export async function fetchPaged(
  client: CoveoClient,
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
      data = await client.post({
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

// Keyset (cursor) pagination by @rowid — the one sortable, unique, monotonic
// system field. Unlike offset paging it has NO 5,000-result cap, so it can page
// a window of any size. This is how we get past Coveo's offset limit when a date
// window can't be split below 5,000 (e.g. a bulk re-index that stamped >5,000
// articles with the SAME @date second — @date filtering is only 1s-resolution,
// so such a second is irreducible). Returns full result objects (all fields).
export async function fetchKeyset(
  client: CoveoClient,
  aq: string,
  pageSize: number,
  maxResults: number,
  onProgress?: (n: number) => void,
): Promise<CoveoResult[]> {
  const out: CoveoResult[] = [];
  const seen = new Set<string>(); // dedup the small overlap the safety margin re-fetches
  let cursor: bigint | null = null;
  let eff = pageSize;

  while (out.length < maxResults) {
    const toFetch = Math.min(eff, maxResults - out.length);
    if (toFetch <= 0) break;
    const cursorAq = cursor === null ? aq : `${aq} @rowid>=${cursor}`;

    let data: Record<string, unknown>;
    try {
      data = await client.post({
        q: "",
        aq: cursorAq || undefined,
        numberOfResults: toFetch,
        searchHub: "myF5",
        sortCriteria: "@rowid ascending",
        // No fieldsToInclude -> every field is returned.
      });
    } catch (e) {
      if (eff > 1 && /maximum size|ResponseExceededMaximumSize/i.test((e as Error).message)) {
        eff = Math.max(1, Math.floor(eff / 2));
        continue;
      }
      throw e;
    }

    const batch = (data.results as CoveoResult[]) ?? [];
    if (batch.length === 0) break;

    const lastRow = (batch[batch.length - 1].raw as CoveoResult)?.rowid;
    if (lastRow == null) throw new Error("keyset paging: result missing @rowid");

    let added = 0;
    for (const r of batch) {
      const pid = ((r.raw as CoveoResult)?.permanentid as string) ?? (r.uniqueId as string) ?? "";
      if (pid && seen.has(pid)) continue; // overlap re-fetched by the safety margin
      if (pid) seen.add(pid);
      out.push(r);
      added++;
    }
    onProgress?.(out.length);

    const nextCursor = BigInt(Math.trunc(lastRow as number)) - CURSOR_MARGIN;
    // Stop if a full page yielded nothing new (would otherwise spin in place).
    if (added === 0 && cursor !== null && nextCursor <= cursor) break;
    cursor = nextCursor;
    if (batch.length < toFetch) break; // last page
    await new Promise((r) => setTimeout(r, 120));
  }

  return out;
}

// Recursively split a date window until each chunk fits within COVEO_MAX_OFFSET,
// then page each leaf.
export async function fetchChunked(
  client: CoveoClient,
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

  const total = await client.getCount(aq);
  if (total === 0) return;

  if (total <= COVEO_MAX_OFFSET) {
    const remaining = maxResults - collected.length;
    const batch = await fetchPaged(
      client,
      aq,
      pageSize,
      Math.min(total, remaining),
      (n) => onProgress(collected.length + n),
    );
    collected.push(...batch);
    return;
  }

  // total > 5,000: try to split the date window further. Coveo's @date filter is
  // only 1-second resolution, so once a window is within a single second it can
  // no longer be reduced by date — fall back to keyset paging (no offset cap),
  // which handles a dense same-second cluster of any size. depth>=50 is a
  // pathological-recursion backstop that also defers to keyset (never lossy).
  const midMs = Math.floor((startMs + endMs) / 2);
  if (toCoveoDate(startMs) === toCoveoDate(midMs) || depth >= 50) {
    const batch = await fetchKeyset(
      client,
      aq,
      pageSize,
      maxResults - collected.length,
      (n) => onProgress(collected.length + n),
    );
    collected.push(...batch);
    return;
  }

  await fetchChunked(
    client,
    baseAq,
    startMs,
    midMs,
    pageSize,
    maxResults,
    onProgress,
    collected,
    depth + 1,
  );
  await fetchChunked(
    client,
    baseAq,
    midMs,
    endMs,
    pageSize,
    maxResults,
    onProgress,
    collected,
    depth + 1,
  );
}

export async function fetchTypeSince(
  client: CoveoClient,
  documentType: string,
  cutoffMs: number,
  endMs: number,
  pageSize: number,
  limit: number,
  onProgress: (n: number) => void,
  applyModFilter = true,
): Promise<CoveoResult[]> {
  const baseAq = `@f5_document_type=="${documentType}"`;
  // --all (applyModFilter=false): page the ENTIRE type by @rowid keyset, with no
  // @date window. @rowid is on every document, so this captures articles whose
  // @date is null or outside any date window (date-range queries silently drop
  // those) and has no 5,000-offset cap or dense-second blind spot. This is the
  // robust full-corpus path; fetchKeyset dedups by permanentid.
  if (!applyModFilter) {
    return await fetchKeyset(client, baseAq, pageSize, limit, onProgress);
  }
  // --days: date-window the @date superset, then refine to the exact content-mod
  // window (the @date re-index date is always >= the content modification date).
  const collected: CoveoResult[] = [];
  await fetchChunked(client, baseAq, cutoffMs, endMs, pageSize, limit, onProgress, collected);
  return collected.filter((r) => {
    const m = modMsOf(r.raw as CoveoResult);
    return m === undefined || m >= cutoffMs;
  });
}
