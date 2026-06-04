// Structured, leveled logger. All output goes to STDERR so that machine-readable
// command output (e.g. `--json` payloads) stays clean on STDOUT.
//
// Levels (most to least severe): error < warn < info < debug < trace.
// CLI flags map to levels: --quiet=warn, default=info, --verbose=debug, --debug=trace.
// `--json-logs` emits one NDJSON object per line (good for the driver to grep).

export type Level = "error" | "warn" | "info" | "debug" | "trace";

const ORDER: Record<Level, number> = { error: 0, warn: 1, info: 2, debug: 3, trace: 4 };

export interface Logger {
  error(msg: string, meta?: Record<string, unknown>): void;
  warn(msg: string, meta?: Record<string, unknown>): void;
  info(msg: string, meta?: Record<string, unknown>): void;
  debug(msg: string, meta?: Record<string, unknown>): void;
  trace(msg: string, meta?: Record<string, unknown>): void;
  /** Start a timer; the returned fn logs "<label> (Nms)" at debug level when called. */
  timer(label: string): () => void;
  /** Derived logger that prefixes every line with "[scope]". */
  child(scope: string): Logger;
  readonly level: Level;
}

export interface LoggerOpts {
  level?: Level;
  json?: boolean;
  scope?: string;
  /** Override the sink (defaults to stderr). Used by tests. */
  write?: (line: string) => void;
}

export function makeLogger(opts: LoggerOpts = {}): Logger {
  const level: Level = opts.level ?? "info";
  const json = opts.json ?? false;
  const scope = opts.scope ?? "";
  const write = opts.write ??
    ((line: string) => Deno.stderr.writeSync(new TextEncoder().encode(line + "\n")));
  const threshold = ORDER[level];

  const emit = (lvl: Level, msg: string, meta?: Record<string, unknown>) => {
    if (ORDER[lvl] > threshold) return;
    if (json) {
      write(
        JSON.stringify({
          ts: new Date().toISOString(),
          level: lvl,
          scope: scope || undefined,
          msg,
          ...meta,
        }),
      );
    } else {
      const prefix = scope ? `[${scope}] ` : "";
      const metaStr = meta && Object.keys(meta).length ? "  " + JSON.stringify(meta) : "";
      write(`${lvl.toUpperCase().padEnd(5)} ${prefix}${msg}${metaStr}`);
    }
  };

  const logger: Logger = {
    level,
    error: (m, meta) => emit("error", m, meta),
    warn: (m, meta) => emit("warn", m, meta),
    info: (m, meta) => emit("info", m, meta),
    debug: (m, meta) => emit("debug", m, meta),
    trace: (m, meta) => emit("trace", m, meta),
    timer(label) {
      const start = performance.now();
      return () => emit("debug", `${label} (${Math.round(performance.now() - start)}ms)`);
    },
    child(childScope) {
      return makeLogger({
        level,
        json,
        scope: scope ? `${scope}:${childScope}` : childScope,
        write,
      });
    },
  };
  return logger;
}

/** A logger that drops everything — handy as a default in libs and tests. */
export const NULL_LOGGER: Logger = makeLogger({ level: "error", write: () => {} });
