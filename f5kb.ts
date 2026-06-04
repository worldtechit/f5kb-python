#!/usr/bin/env -S deno run --allow-net --allow-read --allow-write --allow-env
// f5kb — single-entry CLI for the F5 KB indexing toolkit.
//
// Dispatches to thin subcommand wrappers in cmd/*.ts; all heavy logic lives in
// lib/. Global conventions:
//   - human-readable progress/log lines -> STDERR (via the logger)
//   - machine output (a --json payload)  -> STDOUT
//   - generated FILES are unchanged from the original scripts
//
// Global flags: --verbose(debug) / --debug(trace) / --quiet(warn) / --json-logs
// (logger build), --help/-h (usage), --version. Unknown subcommand -> usage,
// exit 2. A top-level try/catch logs the error and exits 1.

import { type Logger, makeLogger } from "./lib/logger.ts";
import { logLevelFromFlags, parseFlags } from "./lib/args.ts";
import { VERSION } from "./lib/version.ts";

type CmdRunner = (
  args: ReturnType<typeof parseFlags>,
  logger: Logger,
) => Promise<number>;

interface CmdDef {
  desc: string;
  /** Flag synopsis shown by `f5kb <sub> --help`. */
  flags: string;
  load: () => Promise<{ run: CmdRunner }>;
}

const COMMANDS: Record<string, CmdDef> = {
  dump: {
    desc: "Dump full metadata + content per article, one JSON per type.",
    flags:
      "(--days=N | --all)  --out=DIR  [--config=config.yaml] [--types=A,B] [--exclude-types=A,B] [--page-size=200] [--limit=N] [--db=FILE] [--changelog[=FILE]] [--yes] [--fields-doc=FILE(deprecated)]",
    load: () => import("./cmd/dump.ts"),
  },
  enrich: {
    desc: "Fetch article bodies for types the search index leaves empty.",
    flags:
      "[--dump=outputs/dump] [--types=A,B] [--exclude-types=A,B] [--concurrency=4] [--delay-ms=200] [--limit=N] [--refetch] [--refetch-errors] [--changelog[=FILE]] [--yes]   (env: GITHUB_TOKEN)",
    load: () => import("./cmd/enrich.ts"),
  },
  track: {
    desc: "Index a dump into the SQLite overview; report new/changed/removed.",
    flags:
      "[--dump=outputs/dump] [--db=FILE] [--types=A,B] [--exclude-types=A,B] [--run-id=ID] [--changelog[=FILE]] [--json]",
    load: () => import("./cmd/track.ts"),
  },
  sync: {
    desc: "Incremental update: dump+enrich+track only changed; detect deletions.",
    flags:
      "(--all | --days=N | --since-last-run)  [--types=A,B] [--exclude-types=A,B] [--out=outputs/dump] [--config=config.yaml] [--db=FILE] [--no-enrich] [--changelog[=FILE]] [--no-changelog] [--dry-run] [--yes] [--page-size=200] [--limit=N] [--concurrency=4] [--delay-ms=200]",
    load: () => import("./cmd/sync.ts"),
  },
  reconcile: {
    desc: "Remove articles deleted upstream (report-only unless --apply).",
    flags:
      "[--types=A,B] [--exclude-types=A,B] [--dump=outputs/dump] [--config=config.yaml] [--db=FILE] [--apply] [--purge] [--max-delete-pct=10] [--max-deletes=N] [--changelog[=FILE]] [--page-size=2000] [--json]",
    load: () => import("./cmd/reconcile.ts"),
  },
  approve: {
    desc: "Review + apply (or reject) overwrites staged in _pending/ by the gate.",
    flags:
      "[--dump=outputs/dump] [--db=FILE] [--types=A,B] [--exclude-types=A,B] [--ids=K1,K2] [--list] [--reject] [--include-risky] [--no-archive] [--changelog[=FILE]] [--no-changelog] [--json]",
    load: () => import("./cmd/approve.ts"),
  },
  status: {
    desc: "Read-only health report for a dump + its tracking DB.",
    flags: "[--dump=outputs/dump] [--db=FILE] [--json]",
    load: () => import("./cmd/status.ts"),
  },
  fetch: {
    desc: "Fetch articles by product/type into a flat JSON (+ optional CSV).",
    flags:
      "[--product=NAME] [--type=NAME] [--limit=N] [--output=FILE] [--csv=FILE] [--page-size=100]",
    load: () => import("./cmd/fetch.ts"),
  },
  recent: {
    desc: "Fetch articles modified in the last N days, one JSON per type.",
    flags: "--days=N  --out=DIR  [--types=A,B] [--exclude-types=A,B] [--page-size=500] [--limit=N]",
    load: () => import("./cmd/recent.ts"),
  },
  "list-types": {
    desc: "Print all document types with counts.",
    flags: "(no flags)",
    load: () => import("./cmd/list_types.ts"),
  },
  "list-products": {
    desc: "Print products known to the global facet, with counts.",
    flags: "(no flags)",
    load: () => import("./cmd/list_products.ts"),
  },
  discover: {
    desc: "Deep product discovery; write discovered_products.yaml.",
    flags: "[--out=discovered_products.yaml] [--format=yaml|json]",
    load: () => import("./cmd/discover.ts"),
  },
};

function subUsage(sub: string, def: CmdDef): void {
  const lines = [
    `f5kb ${sub} — ${def.desc}`,
    "",
    `Usage: f5kb ${sub} ${def.flags}`,
    "",
    "Plus global flags: --verbose / --debug / --quiet / --json-logs.",
  ];
  Deno.stderr.writeSync(new TextEncoder().encode(lines.join("\n") + "\n"));
}

function usage(): void {
  const lines = [
    `f5kb ${VERSION} — F5 Knowledge Base indexing toolkit`,
    "",
    "Usage: f5kb <subcommand> [flags]",
    "",
    "Subcommands:",
  ];
  const width = Math.max(...Object.keys(COMMANDS).map((k) => k.length));
  for (const [name, def] of Object.entries(COMMANDS)) {
    lines.push(`  ${name.padEnd(width)}  ${def.desc}`);
  }
  lines.push(
    "",
    "Global flags:",
    "  --verbose      debug-level logging",
    "  --debug        trace-level logging",
    "  --quiet        warn-level logging only",
    "  --json-logs    emit logs as NDJSON",
    "  --help, -h     show this help",
    "  --version      print version",
    "",
    "Run `f5kb <subcommand> --help` for subcommand flags.",
  );
  // Usage goes to stderr (it is human-readable, not machine output).
  Deno.stderr.writeSync(new TextEncoder().encode(lines.join("\n") + "\n"));
}

async function main(): Promise<number> {
  const argv = [...Deno.args];
  const sub = argv[0];

  // --version / bare --help / -h before a subcommand.
  if (sub === "--version") {
    console.log(VERSION);
    return 0;
  }
  if (!sub || sub === "--help" || sub === "-h") {
    usage();
    return 0;
  }

  const def = COMMANDS[sub];
  if (!def) {
    Deno.stderr.writeSync(
      new TextEncoder().encode(`Unknown subcommand: ${sub}\n\n`),
    );
    usage();
    return 2;
  }

  // Parse the remaining args (everything after the subcommand).
  const parsed = parseFlags(argv.slice(1));

  // `f5kb <sub> --help` prints that subcommand's flag synopsis.
  if ("help" in parsed.flags || "h" in parsed.flags) {
    subUsage(sub, def);
    return 0;
  }

  const { level, json } = logLevelFromFlags(parsed.flags);
  const logger = makeLogger({ level, json, scope: sub });

  const mod = await def.load();
  return await mod.run(parsed, logger);
}

if (import.meta.main) {
  try {
    const code = await main();
    Deno.exit(code);
  } catch (e) {
    // Last-resort error handler: log and exit 1.
    const logger = makeLogger();
    logger.error(`fatal: ${(e as Error).message}`);
    if ((e as Error).stack) logger.debug((e as Error).stack!);
    Deno.exit(1);
  }
}
