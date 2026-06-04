// `f5kb status` — read-only health report for a dump + its tracking DB.
// Backed by lib/status.ts computeStatus (never writes). With --json the report
// is printed as JSON to STDOUT; otherwise the rendered table is printed to STDOUT.
//
// Flags:
//   --dump=DIR   dump directory (default outputs/dump)
//   --db=FILE    SQLite file (default <dump>/../articles.db)
//   --json       print the report as JSON

import { type ParsedArgs } from "../lib/args.ts";
import { flagBool, flagStr } from "../lib/args.ts";
import { type Logger } from "../lib/logger.ts";
import { computeStatus, renderStatus } from "../lib/status.ts";

export async function run(args: ParsedArgs, _logger: Logger): Promise<number> {
  const flags = args.flags;
  const dump = flagStr(flags, "dump", "outputs/dump")!;
  const db = flagStr(flags, "db");
  const asJson = flagBool(flags, "json");

  const report = await computeStatus({ dump, db });
  if (asJson) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(renderStatus(report));
  }
  return 0;
}
