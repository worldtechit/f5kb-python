// `f5kb fetch` — fetch articles by product/type into a flat JSON array (+ CSV).
// Behavior reference: fetch_f5_articles_flex.ts FETCH mode (not the list/discover
// modes — those are `list-types` / `list-products` / `discover`). Output files
// (JSON array of {name,link,summary,publicationDate,modificationDate}, optional
// CSV) are byte-identical.
//
// Flags:
//   --product=NAME   filter by product (f5_version)
//   --type=NAME      filter by document type
//   --limit=N        stop after N articles (default: all)
//   --output=FILE    JSON output (default auto-named f5_<slug>.json)
//   --csv=FILE       also write a CSV file
//   --page-size=N    results per call (default 100, max 1000)

import { type ParsedArgs } from "../lib/args.ts";
import { flagNum, flagStr } from "../lib/args.ts";
import { type Logger } from "../lib/logger.ts";
import { CoveoClient } from "../lib/coveo/client.ts";
import { fetchCoveoConfig, refreshConfig } from "../lib/coveo/aura.ts";
import {
  buildAq,
  COVEO_MAX_OFFSET,
  fetchFlatChunked,
  fetchFlatPaged,
  type FlatArticle,
  toCSV,
} from "../lib/coveo/flat.ts";
import { sanitizeName } from "../lib/fsutil.ts";

// Cover all possible articles (matches the flex EPOCH_* constants).
const EPOCH_START_MS = new Date("2000-01-01").getTime();
const EPOCH_END_MS = new Date(`${new Date().getFullYear() + 2}-01-01`).getTime();

export async function run(args: ParsedArgs, logger: Logger): Promise<number> {
  const flags = args.flags;

  const product = flagStr(flags, "product");
  const type = flagStr(flags, "type");
  const limit = flags.limit ? parseInt(String(flags.limit)) : Infinity;
  const pageSize = Math.min(flagNum(flags, "page-size", 100)!, 1000);

  const slug = [
    product ? sanitizeName(product) : "",
    type ? sanitizeName(type) : "",
  ].filter(Boolean).join("_") || "all";
  const jsonOutput = flagStr(flags, "output", `f5_${slug}.json`)!;
  const csvOutput = flagStr(flags, "csv");

  logger.info("Fetching Coveo configuration from F5 portal...");
  const coveoConfig = await fetchCoveoConfig();
  logger.info(`Organization ID: ${coveoConfig.organizationId}`);
  const client = new CoveoClient(coveoConfig, {
    logger: logger.child("coveo"),
    refresh: (c) => refreshConfig(c),
  });

  const baseAq = buildAq(product, type);
  if (!baseAq) {
    logger.info("No --product or --type specified — fetching all articles.");
  } else {
    logger.info(`Filter: ${baseAq}`);
  }

  const total = await client.getCount(baseAq);
  const target = isFinite(limit) ? Math.min(limit, total) : total;
  logger.info(`Total matching articles: ${total.toLocaleString()}`);
  logger.info(`Fetching ${target.toLocaleString()} articles...`);

  if (total > COVEO_MAX_OFFSET && isFinite(limit) && limit <= COVEO_MAX_OFFSET) {
    logger.info("(limit is within Coveo's range, using standard pagination)");
  }

  let lastReported = 0;
  const onProgress = (n: number) => {
    if (n - lastReported >= 500 || n >= target) {
      const pct = target ? Math.round((n / target) * 100) : 100;
      logger.info(`  Fetched ${n.toLocaleString()} / ${target.toLocaleString()} (${pct}%)`);
      lastReported = n;
    }
  };

  const allArticles: FlatArticle[] = [];
  if (total <= COVEO_MAX_OFFSET || (isFinite(limit) && limit <= COVEO_MAX_OFFSET)) {
    const batch = await fetchFlatPaged(client, baseAq, pageSize, target, onProgress);
    allArticles.push(...batch);
  } else {
    logger.info("(total exceeds 5,000 — using date-range chunking)");
    await fetchFlatChunked(
      client,
      baseAq,
      EPOCH_START_MS,
      EPOCH_END_MS,
      pageSize,
      target,
      onProgress,
      allArticles,
    );
  }

  logger.info(`Total fetched: ${allArticles.length.toLocaleString()} articles`);

  // Deduplicate by link (chunking can produce duplicates at chunk boundaries).
  const seen = new Set<string>();
  const deduped = allArticles.filter((a) => {
    if (seen.has(a.link)) return false;
    seen.add(a.link);
    return true;
  });
  if (deduped.length !== allArticles.length) {
    logger.info(`After deduplication: ${deduped.length.toLocaleString()} articles`);
  }

  // Strip the internal modMs before writing (flat output is the 5 stable fields).
  const out = deduped.map(({ name, link, summary, publicationDate, modificationDate }) => ({
    name,
    link,
    summary,
    publicationDate,
    modificationDate,
  }));

  await Deno.writeTextFile(jsonOutput, JSON.stringify(out, null, 2));
  logger.info(`JSON written to: ${jsonOutput}`);

  if (csvOutput) {
    await Deno.writeTextFile(csvOutput, toCSV(deduped));
    logger.info(`CSV written to: ${csvOutput}`);
  }
  return 0;
}
