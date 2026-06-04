// `f5kb track` — indexes a dump into the SQLite overview and reports
// new/changed/unchanged/removed. Behavior reference: track_articles.ts main().
// The DB schema/upsert come from lib/track/db.ts (byte-identical). Returns 0.
//
// Flags:
//   --dump=DIR     dump directory (default outputs/dump)
//   --db=FILE      SQLite file (default <dump>/../articles.db)
//   --types="A,B"  only index these type subdirs
//   --run-id=ID    label for this run (default current ISO timestamp)
//   --json         print the run summary as JSON to STDOUT

import { type ParsedArgs } from "../lib/args.ts";
import { flagBool, flagStr } from "../lib/args.ts";
import { type Logger, makeLogger } from "../lib/logger.ts";
import { trackDump } from "../lib/track/db.ts";

export async function run(args: ParsedArgs, logger: Logger): Promise<number> {
  const flags = args.flags;

  const dump = flagStr(flags, "dump", "outputs/dump")!;
  const db = flagStr(flags, "db");
  const types = typeof flags.types === "string"
    ? flags.types.split(",").map((s) => s.trim()).filter(Boolean)
    : null;
  const runId = flagStr(flags, "run-id");
  const asJson = flagBool(flags, "json");

  // In --json mode, floor the logger at warn so the human info summary is dropped
  // (the JSON on STDOUT is the only payload) while skip-unreadable warnings still
  // surface on STDERR. Otherwise log the full human summary.
  const trackLogger = asJson ? makeLogger({ level: "warn", json: false, scope: "track" }) : logger;

  const summary = await trackDump({ dump, db, types, runId, logger: trackLogger });

  if (asJson) {
    console.log(JSON.stringify(summary, null, 2));
  }
  return 0;
}
