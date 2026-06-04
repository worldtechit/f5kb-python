// `f5kb enrich` — fills article bodies for the document types the search index
// leaves empty. Behavior reference: enrich_bodies.ts main(). The driver writes
// _enrich_report.json and prints the per-type DONE summaries / GitHub-auth note.
// Returns 0 normally.
//
// Flags:
//   --dump=DIR        dump directory (default outputs/dump)
//   --types="A,B"     subset of implemented type keys
//   --concurrency=N   parallel fetches (default 4)
//   --delay-ms=N      min delay per worker between requests (default 200)
//   --limit=N         cap articles per type (testing)
//   --refetch         re-fetch even if a body / error already present
//   --refetch-errors  re-process only previously-errored articles

import { type ParsedArgs } from "../lib/args.ts";
import { flagBool, flagNum, flagStr } from "../lib/args.ts";
import { type Logger } from "../lib/logger.ts";
import { HttpClient } from "../lib/http/fetcher.ts";
import { enrichDump } from "../lib/enrich/driver.ts";

export async function run(args: ParsedArgs, logger: Logger): Promise<number> {
  const flags = args.flags;

  const dump = flagStr(flags, "dump", "outputs/dump")!;
  const types = typeof flags.types === "string"
    ? flags.types.split(",").map((s) => s.trim()).filter(Boolean)
    : null;
  const concurrency = Math.max(1, flagNum(flags, "concurrency", 4) || 4);
  const delayMs = Math.max(0, flagNum(flags, "delay-ms", 200) ?? 200);
  const limit = flags.limit ? (parseInt(String(flags.limit), 10) || null) : null;
  const refetch = flagBool(flags, "refetch");
  const refetchErrors = flagBool(flags, "refetch-errors");

  let githubToken: string | undefined;
  try {
    githubToken = Deno.env.get("GITHUB_TOKEN") || undefined;
  } catch {
    // --allow-env not granted; GitHub enrichment runs unauthenticated.
    githubToken = undefined;
  }

  const http = new HttpClient({ logger: logger.child("http") });

  await enrichDump({
    dump,
    types,
    concurrency,
    delayMs,
    limit,
    refetch,
    refetchErrors,
    http,
    githubToken,
    logger,
  });

  return 0;
}
