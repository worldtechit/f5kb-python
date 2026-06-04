// `f5kb dump` — dumps full metadata + content for F5 KB articles, one JSON per
// article, split by document type, driven by config.yaml. Behavior reference:
// dump_articles.ts main(). File formats (per-article JSON, _catalogue.*,
// _index.json) and the exit code (1 if any type FAILED) are preserved exactly.
//
// Flags:
//   --days=N | --all     (one required) window; --all = entire corpus
//   --out=DIR            REQUIRED output directory
//   --config=FILE        config YAML (default config.yaml)
//   --fields-doc=FILE    DEPRECATED — descriptions now come from config.yaml;
//                        if given, still loaded + merged over config descriptions
//   --types="A,B"        subset of config type keys
//   --page-size=N        results per call (default 200, max 500)
//   --limit=N            cap articles per type (testing)

import { type ParsedArgs } from "../lib/args.ts";
import { type Logger } from "../lib/logger.ts";
import { flagNum, flagStr } from "../lib/args.ts";
import { loadConfig, loadFieldDescriptionsFile } from "../lib/config/loader.ts";
import { normalizeType, type TypeConfig } from "../lib/config/types.ts";
import { CoveoClient, type CoveoResult } from "../lib/coveo/client.ts";
import { fetchCoveoConfig, refreshConfig } from "../lib/coveo/aura.ts";
import { dateAq, modMsOf } from "../lib/coveo/dates.ts";
import { fetchTypeSince } from "../lib/coveo/paging.ts";
import {
  type CatalogueEntry,
  flattenFieldsSafe,
  splitEntry,
  updateCatalogue,
  writeCatalogue,
} from "../lib/coveo/fields.ts";
import { idOf, sanitizeName } from "../lib/fsutil.ts";
import { makeProgress } from "../lib/progress.ts";

interface TypeStatus {
  typeKey: string;
  documentType: string;
  dir: string;
  status: "ok" | "partial" | "failed";
  expected: number | null;
  fetched: number;
  written: number;
  writeErrors: number;
  error?: string;
}

export async function run(args: ParsedArgs, logger: Logger): Promise<number> {
  const flags = args.flags;

  const allTime = "all" in flags;
  const daysRaw = flagStr(flags, "days");
  const days = Number(daysRaw);
  if (!allTime && (!daysRaw || !Number.isFinite(days) || days <= 0)) {
    logger.error("provide --all or --days=N (a positive number)");
    return 1;
  }

  const outDir = flagStr(flags, "out");
  if (!outDir) {
    logger.error("--out (output directory) is required");
    return 1;
  }

  const configPath = flagStr(flags, "config", "config.yaml")!;
  const fieldsDocPath = flagStr(flags, "fields-doc");
  const pageSize = Math.min(flagNum(flags, "page-size", 200)!, 500);
  const limit = flags.limit ? parseInt(String(flags.limit)) : Infinity;
  const typeKeyFilter = typeof flags.types === "string"
    ? flags.types.split(",").map((s) => s.trim()).filter(Boolean)
    : null;

  // ---- config ----
  let config;
  try {
    config = await loadConfig(configPath);
  } catch (e) {
    logger.error(`could not read/parse config ${configPath}: ${(e as Error).message}`);
    return 1;
  }
  const typeConfigs = config.types;
  let typeKeys = Object.keys(typeConfigs);
  if (!typeKeys.length) {
    logger.error(`config ${configPath} has no types`);
    return 1;
  }

  if (typeKeyFilter) {
    const unknown = typeKeyFilter.filter((k) => !typeKeys.includes(k));
    if (unknown.length) logger.warn(`type key(s) not in config ignored: ${unknown.join(", ")}`);
    typeKeys = typeKeys.filter((k) => typeKeyFilter.includes(k));
    if (!typeKeys.length) {
      logger.error("no valid type keys selected");
      return 1;
    }
  }

  // Field descriptions: from config.yaml, with the deprecated --fields-doc merged on top.
  let descriptions = { ...config.fieldDescriptions };
  if (fieldsDocPath) {
    logger.warn(
      "--fields-doc is DEPRECATED: field descriptions now come from config.yaml. " +
        `Merging ${fieldsDocPath} over the config descriptions.`,
    );
    const extra = await loadFieldDescriptionsFile(fieldsDocPath);
    descriptions = { ...descriptions, ...extra };
  }

  // ---- token / client ----
  logger.info("Fetching Coveo configuration from F5 portal...");
  const coveoConfig = await fetchCoveoConfig();
  logger.info(`Organization ID: ${coveoConfig.organizationId}`);
  const client = new CoveoClient(coveoConfig, {
    logger: logger.child("coveo"),
    refresh: (c) => refreshConfig(c),
  });

  logger.info(`Field descriptions loaded: ${Object.keys(descriptions).length}`);

  const nowMs = Date.now();
  const cutoffMs = allTime ? Date.UTC(2000, 0, 1) : nowMs - days * 86400000;
  const endMs = nowMs + 86400000; // slightly future so newest items are never clipped

  logger.info(
    allTime
      ? "Window: entire corpus (--all, no lower date bound)"
      : `Window: articles modified since ${new Date(cutoffMs).toISOString().slice(0, 10)} ` +
        `(last ${days} day${days === 1 ? "" : "s"})`,
  );

  await Deno.mkdir(outDir, { recursive: true });

  const manifest: TypeStatus[] = [];

  for (const typeKey of typeKeys) {
    const cfg: TypeConfig = normalizeType({
      documentType: typeConfigs[typeKey]?.documentType,
      metadata: typeConfigs[typeKey]?.metadata,
      content: typeConfigs[typeKey]?.content,
    });
    const dir = sanitizeName(typeKey);
    if (!cfg.documentType) {
      logger.warn(`Skipping "${typeKey}": no documentType in config`);
      manifest.push({
        typeKey,
        documentType: "",
        dir,
        status: "failed",
        expected: null,
        fetched: 0,
        written: 0,
        writeErrors: 0,
        error: "no documentType in config",
      });
      continue;
    }

    const st: TypeStatus = {
      typeKey,
      documentType: cfg.documentType,
      dir,
      status: "ok",
      expected: null,
      fetched: 0,
      written: 0,
      writeErrors: 0,
    };
    const progress = makeProgress(logger);
    try {
      // Server-side count over the window — the target to validate against.
      const expectAq = allTime
        ? `@f5_document_type=="${cfg.documentType}"`
        : `@f5_document_type=="${cfg.documentType}" ${dateAq(cutoffMs, endMs)}`.trim();
      st.expected = await client.getCount(expectAq);

      progress.start(typeKey, st.expected ?? undefined);
      const results = await fetchTypeSince(
        client,
        cfg.documentType,
        cutoffMs,
        endMs,
        pageSize,
        limit,
        (n) => progress.update(n),
        !allTime,
      );
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
        try {
          await Deno.writeTextFile(`${typeDir}/${id}.json`, JSON.stringify(entry, null, 2));
          st.written++;
        } catch (e) {
          st.writeErrors++;
          if (st.writeErrors <= 3) logger.warn(`write failed for ${id}: ${(e as Error).message}`);
        }
      }

      await writeCatalogue(typeDir, typeKey, cfg.documentType, catalogue, results.length, cfg);

      // Undercount=partial only under --all (see dump_articles.ts rationale).
      const undercount = allTime && st.expected !== null && limit === Infinity &&
        st.written < st.expected;
      if (st.writeErrors > 0 || undercount) st.status = "partial";

      const flag = st.status === "ok" ? "" : `  [${st.status.toUpperCase()}]`;
      const exp = st.expected !== null ? `/${st.expected}` : "";
      progress.done(
        `${st.written}${exp} article${st.written === 1 ? "" : "s"} -> ${typeDir}/${flag}`,
      );
    } catch (e) {
      st.status = "failed";
      st.error = (e as Error).message;
      progress.done(`FAILED: ${st.error}`);
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

  logger.info(
    `Done. ${total} article${total === 1 ? "" : "s"} across ${manifest.length} type(s) ` +
      `written to ${outDir}/ (manifest: ${outDir}/_index.json)`,
  );
  if (partial.length) {
    logger.warn(
      `PARTIAL (${partial.length}): ` +
        partial.map((m) =>
          `${m.typeKey} (${m.written}/${m.expected ?? "?"}, writeErr=${m.writeErrors})`
        )
          .join(", "),
    );
  }
  if (failed.length) {
    logger.error(
      `FAILED (${failed.length}): ` +
        failed.map((m) => `${m.typeKey}: ${m.error}`).join("; "),
    );
    logger.error(`Re-run just these with --types="${failed.map((m) => m.typeKey).join(",")}"`);
    return 1;
  }
  return 0;
}
