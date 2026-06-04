// Enrichment driver: walks a dump, runs the per-type enrichers with a bounded
// concurrency pool, and writes _enrich_report.json. Logic moved from
// enrich_bodies.ts (the resumability gate, stale-key clearing, per-type counters,
// progress logging) with two injection changes: an HttpClient is passed in (so
// the GitHub token is read in the cmd layer, not here) and a Logger replaces the
// bare console.* calls. Behavior is otherwise unchanged.

import { type Logger, NULL_LOGGER } from "../logger.ts";
import { HttpClient } from "../http/fetcher.ts";
import { readJson, walkArticleFiles, writeJson } from "../fsutil.ts";
import {
  type Article,
  type EnricherDeps,
  type EnrichResult,
  hasBody,
  TYPE_ENRICHERS,
} from "./enrichers.ts";

export interface TypeReport {
  typeKey: string;
  files: number;
  enriched: number;
  failed: number;
  skipped: number;
  missingDir?: boolean;
  errors: Array<{ id: string; link: string; error: string }>;
}

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// Run async tasks with a fixed concurrency, returning when all are done.
export async function runPool<T>(
  items: T[],
  concurrency: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  let idx = 0;
  const runners = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (idx < items.length) {
      const i = idx++;
      await worker(items[i]);
    }
  });
  await Promise.all(runners);
}

export interface EnrichTypeOpts {
  typeKey: string;
  dump: string;
  http: HttpClient;
  githubToken?: string;
  concurrency: number;
  delayMs: number;
  limit: number | null;
  refetch: boolean;
  refetchErrors: boolean;
  logger?: Logger;
  sleep?: (ms: number) => Promise<void>;
}

async function listArticleFiles(dir: string): Promise<string[]> {
  const files: string[] = [];
  for await (const file of walkArticleFiles(dir)) files.push(file);
  files.sort();
  return files;
}

export async function enrichType(opts: EnrichTypeOpts): Promise<TypeReport> {
  const logger = opts.logger ?? NULL_LOGGER;
  const sleep = opts.sleep ?? defaultSleep;
  const { typeKey } = opts;
  const report: TypeReport = { typeKey, files: 0, enriched: 0, failed: 0, skipped: 0, errors: [] };
  const enricher = TYPE_ENRICHERS[typeKey];
  const deps: EnricherDeps = { http: opts.http, githubToken: opts.githubToken };
  const dir = `${opts.dump}/${typeKey}`;
  let files: string[];
  try {
    files = await listArticleFiles(dir);
  } catch {
    logger.info(`  [${typeKey}] no directory ${dir} — skipping`);
    report.missingDir = true;
    return report;
  }
  if (opts.limit) files = files.slice(0, opts.limit);
  report.files = files.length;

  let done = 0, skipped = 0, ok = 0, failed = 0;
  const nowIso = new Date().toISOString();

  await runPool(files, opts.concurrency, async (file) => {
    const article = await readJson<Article>(file);
    const hadError = typeof article.content?.bodyError === "string";
    // Skip already-done articles unless forced. --refetch-errors re-processes
    // only those that previously errored (e.g. after mapping a new host).
    if (!opts.refetch && !(opts.refetchErrors && hadError) && hasBody(article.content)) {
      skipped++;
      return;
    }
    let result: EnrichResult;
    try {
      result = await enricher(article, nowIso, deps);
      ok++;
    } catch (e) {
      const error = (e as Error).message;
      result = { bodySource: article.link ?? "", fetchedAt: nowIso, bodyError: error };
      failed++;
      report.errors.push({ id: article.id ?? "", link: article.link ?? "", error });
    }
    // Clear any keys a prior run set so a re-enrich (e.g. after fixing a
    // JS-rendered host) doesn't leave a stale bodyError/body_text behind.
    const base = { ...(article.content ?? {}) };
    for (const k of ["sections", "body_text", "bodyError", "bodySource", "fetchedAt"]) {
      delete (base as Record<string, unknown>)[k];
    }
    article.content = { ...base, ...result };
    await writeJson(file, article);
    done++;
    if ((done + skipped) % 25 === 0) {
      logger.info(
        `  [${typeKey}] ${
          done + skipped
        }/${files.length} (ok=${ok} fail=${failed} skip=${skipped})`,
      );
    }
    if (opts.delayMs) await sleep(opts.delayMs);
  });

  report.enriched = ok;
  report.failed = failed;
  report.skipped = skipped;
  logger.info(
    `  [${typeKey}] DONE: ${files.length} files — enriched=${ok} failed=${failed} skipped=${skipped}`,
  );
  return report;
}

export interface EnrichDumpOpts {
  dump: string;
  types?: string[] | null;
  http: HttpClient;
  /** Read in the cmd layer (from env), never here; passed through to the github enricher. */
  githubToken?: string;
  concurrency: number;
  delayMs: number;
  limit: number | null;
  refetch: boolean;
  refetchErrors: boolean;
  logger?: Logger;
  sleep?: (ms: number) => Promise<void>;
}

export async function enrichDump(opts: EnrichDumpOpts): Promise<TypeReport[]> {
  const logger = opts.logger ?? NULL_LOGGER;
  const requested = opts.types ?? Object.keys(TYPE_ENRICHERS);
  const toRun = requested.filter((t) => {
    if (!TYPE_ENRICHERS[t]) {
      logger.error(`  [${t}] no enricher implemented — skipping`);
      return false;
    }
    return true;
  });
  if (toRun.length === 0) {
    throw new Error("Nothing to do. Implemented types: " + Object.keys(TYPE_ENRICHERS).join(", "));
  }
  logger.info(`Enriching bodies in ${opts.dump} for: ${toRun.join(", ")}`);
  logger.info(
    `(concurrency=${opts.concurrency}, delay=${opts.delayMs}ms, refetch=${opts.refetch})`,
  );
  if (toRun.includes("F5_GitHub")) {
    logger.info(
      `GitHub auth: ${
        opts.githubToken
          ? "token present (5000/hr)"
          : "UNAUTHENTICATED (60/hr) — set GITHUB_TOKEN to raise"
      }`,
    );
  }
  const reports: TypeReport[] = [];
  for (const t of toRun) {
    reports.push(
      await enrichType({
        typeKey: t,
        dump: opts.dump,
        http: opts.http,
        githubToken: opts.githubToken,
        concurrency: opts.concurrency,
        delayMs: opts.delayMs,
        limit: opts.limit,
        refetch: opts.refetch,
        refetchErrors: opts.refetchErrors,
        logger: opts.logger,
        sleep: opts.sleep,
      }),
    );
  }

  // Write a machine-readable report so a long driven run can be inspected and
  // re-run (re-process the failures with --refetch-errors).
  const reportPath = `${opts.dump}/_enrich_report.json`;
  const totalFailed = reports.reduce((a, r) => a + r.failed, 0);
  try {
    await writeJson(reportPath, { generatedAt: new Date().toISOString(), types: reports });
    logger.info(`\nReport: ${reportPath}`);
  } catch (e) {
    logger.warn(`Could not write report: ${(e as Error).message}`);
  }
  if (totalFailed) {
    logger.warn(
      `\nFAILURES (${totalFailed}): ` +
        reports.filter((r) => r.failed).map((r) => `${r.typeKey}=${r.failed}`).join(", ") +
        ` — see ${reportPath}; re-run with --refetch-errors after fixing.`,
    );
  }
  logger.info("All done.");
  return reports;
}
