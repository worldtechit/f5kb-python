// Tiny CLI arg parser shared by the entry point and all subcommands. Supports
// `--key=value`, bare `--flag` (boolean true), and positional args. Matches the
// `--k=v` style every original script used.

export interface ParsedArgs {
  /** Positional (non---flag) args, in order. */
  positionals: string[];
  flags: Record<string, string | boolean>;
}

export function parseFlags(argv: string[]): ParsedArgs {
  const positionals: string[] = [];
  const flags: Record<string, string | boolean> = {};
  for (const a of argv) {
    if (!a.startsWith("--")) {
      positionals.push(a);
      continue;
    }
    const body = a.slice(2);
    const eq = body.indexOf("=");
    if (eq === -1) flags[body] = true;
    else flags[body.slice(0, eq)] = body.slice(eq + 1);
  }
  return { positionals, flags };
}

export function flagStr(
  flags: Record<string, string | boolean>,
  key: string,
  dflt?: string,
): string | undefined {
  const v = flags[key];
  if (v === undefined) return dflt;
  return typeof v === "string" ? v : String(v);
}

export function flagNum(
  flags: Record<string, string | boolean>,
  key: string,
  dflt?: number,
): number | undefined {
  const v = flags[key];
  if (v === undefined || v === true) return dflt;
  const n = Number(v);
  return Number.isFinite(n) ? n : dflt;
}

export function flagBool(flags: Record<string, string | boolean>, key: string): boolean {
  return key in flags && flags[key] !== "false";
}

export function flagList(flags: Record<string, string | boolean>, key: string): string[] | null {
  const v = flags[key];
  if (typeof v !== "string") return null;
  return v.split(",").map((s) => s.trim()).filter(Boolean);
}

// Map global logging flags (--quiet/--verbose/--debug/--json-logs) to logger opts.
export function logLevelFromFlags(flags: Record<string, string | boolean>): {
  level: "error" | "warn" | "info" | "debug" | "trace";
  json: boolean;
} {
  let level: "error" | "warn" | "info" | "debug" | "trace" = "info";
  if (flagBool(flags, "quiet")) level = "warn";
  if (flagBool(flags, "verbose")) level = "debug";
  if (flagBool(flags, "debug")) level = "trace";
  return { level, json: flagBool(flags, "json-logs") };
}
