// `f5kb recent` — fetch articles modified in the last N days, one JSON per type,
// plus an _index.json manifest. Behavior reference: fetch_recent_by_type.ts.
// The per-type wrapped JSON {documentType, days, cutoff, generatedAt, count,
// articles[]} and _index.json are byte-identical (client-side modMs filter).
//
// Flags:
//   --days=N         REQUIRED window size
//   --out=DIR        REQUIRED output directory
//   --types="A,B"    subset of document types
//   --page-size=N    results per call (default 500, max 1000)
//   --limit=N        cap articles per type (testing)

import { type ParsedArgs } from "../lib/args.ts";
import { flagNum, flagStr } from "../lib/args.ts";
import { type Logger } from "../lib/logger.ts";
import { CoveoClient } from "../lib/coveo/client.ts";
import { fetchCoveoConfig, refreshConfig } from "../lib/coveo/aura.ts";
import { fetchFlatChunked, type FlatArticle } from "../lib/coveo/flat.ts";
import { sanitizeName } from "../lib/fsutil.ts";

// fetch_recent_by_type.ts paged with a 120ms inter-page pause (vs flex's 150ms).
const RECENT_PAUSE_MS = 120;

async function listDocumentTypes(
  client: CoveoClient,
): Promise<Array<{ value: string; count: number }>> {
  const values = await client.listFacetValues("f5_document_type");
  return values.filter((v) => !v.value.includes("|"));
}

async function fetchTypeSince(
  client: CoveoClient,
  type: string,
  cutoffMs: number,
  endMs: number,
  pageSize: number,
  limit: number,
): Promise<FlatArticle[]> {
  const baseAq = `@f5_document_type=="${type}"`;
  const collected: FlatArticle[] = [];
  await fetchFlatChunked(
    client,
    baseAq,
    cutoffMs,
    endMs,
    pageSize,
    limit,
    () => {},
    collected,
    RECENT_PAUSE_MS,
  );
  // @date is a superset of the content-mod window; refine to the exact window.
  return collected.filter((a) => a.modMs === undefined || a.modMs >= cutoffMs);
}

export async function run(args: ParsedArgs, logger: Logger): Promise<number> {
  const flags = args.flags;

  const daysRaw = flagStr(flags, "days");
  const days = Number(daysRaw);
  if (!daysRaw || !Number.isFinite(days) || days <= 0) {
    logger.error("--days must be a positive number");
    return 1;
  }
  const outDir = flagStr(flags, "out");
  if (!outDir) {
    logger.error("--out (output directory) is required");
    return 1;
  }
  const pageSize = Math.min(flagNum(flags, "page-size", 500)!, 1000);
  const limit = flags.limit ? parseInt(String(flags.limit)) : Infinity;
  const typesFilter = typeof flags.types === "string"
    ? flags.types.split(",").map((s) => s.trim()).filter(Boolean)
    : null;

  const nowMs = Date.now();
  const cutoffMs = nowMs - days * 86400000;
  const endMs = nowMs + 86400000;

  logger.info("Fetching Coveo configuration from F5 portal...");
  const coveoConfig = await fetchCoveoConfig();
  logger.info(`Organization ID: ${coveoConfig.organizationId}`);
  const client = new CoveoClient(coveoConfig, {
    logger: logger.child("coveo"),
    refresh: (c) => refreshConfig(c),
  });
  logger.info(
    `Window: articles modified since ${new Date(cutoffMs).toISOString().slice(0, 10)} ` +
      `(last ${days} day${days === 1 ? "" : "s"})`,
  );

  const allTypes = await listDocumentTypes(client);
  const allTypeNames = new Set(allTypes.map((t) => t.value));

  let selected: string[];
  if (typesFilter) {
    const unknown = typesFilter.filter((t) => !allTypeNames.has(t));
    if (unknown.length) {
      logger.warn(`unknown type(s) ignored: ${unknown.join(", ")}`);
      logger.warn(`Known types: ${[...allTypeNames].join(", ")}`);
    }
    selected = typesFilter.filter((t) => allTypeNames.has(t));
    if (!selected.length) {
      logger.error("no valid types selected");
      return 1;
    }
  } else {
    selected = allTypes.map((t) => t.value);
  }

  await Deno.mkdir(outDir, { recursive: true });

  const summary: Array<{ type: string; count: number; file: string }> = [];

  for (const type of selected) {
    const articles = await fetchTypeSince(client, type, cutoffMs, endMs, pageSize, limit);

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
    logger.info(
      `${type}: ${articles.length} article${articles.length === 1 ? "" : "s"} -> ${filePath}`,
    );
  }

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
  logger.info(
    `Done. ${total} article${total === 1 ? "" : "s"} across ${selected.length} type(s) ` +
      `written to ${outDir}/ (manifest: ${manifestPath})`,
  );
  return 0;
}
