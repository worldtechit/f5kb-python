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
  load: () => Promise<{ run: CmdRunner }>;
}

const COMMANDS: Record<string, CmdDef> = {
  dump: {
    desc: "Dump full metadata + content per article, one JSON per type.",
    load: () => import("./cmd/dump.ts"),
  },
  enrich: {
    desc: "Fetch article bodies for types the search index leaves empty.",
    load: () => import("./cmd/enrich.ts"),
  },
  track: {
    desc: "Index a dump into the SQLite overview; report new/changed/removed.",
    load: () => import("./cmd/track.ts"),
  },
  status: {
    desc: "Read-only health report for a dump + its tracking DB.",
    load: () => import("./cmd/status.ts"),
  },
  fetch: {
    desc: "Fetch articles by product/type into a flat JSON (+ optional CSV).",
    load: () => import("./cmd/fetch.ts"),
  },
  recent: {
    desc: "Fetch articles modified in the last N days, one JSON per type.",
    load: () => import("./cmd/recent.ts"),
  },
  "list-types": {
    desc: "Print all document types with counts.",
    load: () => import("./cmd/list_types.ts"),
  },
  "list-products": {
    desc: "Print products known to the global facet, with counts.",
    load: () => import("./cmd/list_products.ts"),
  },
  discover: {
    desc: "Deep product discovery; write discovered_products.yaml.",
    load: () => import("./cmd/discover.ts"),
  },
};

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

  // Subcommand help: print usage and exit 0 (the cmd modules document their own
  // flags in comments; here we just point back to the top-level usage).
  if ("help" in parsed.flags || "h" in parsed.flags) {
    usage();
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
