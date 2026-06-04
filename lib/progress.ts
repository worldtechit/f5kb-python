// Throttled progress reporter.
//
// Writes to STDERR only (never STDOUT, so machine-readable --json output stays
// clean). When stderr is a TTY it rewrites a single line in place at <= ~4 Hz;
// otherwise (piped/redirected) it emits a plain periodic line every ~2s so logs
// stay readable. Always safe when not a TTY.

import type { Logger } from "./logger.ts";

const TTY_INTERVAL_MS = 250; // <= ~4 Hz
const PLAIN_INTERVAL_MS = 2000; // every ~2s when not a TTY

function fmtDuration(sec: number): string {
  if (sec < 60) return `${sec.toFixed(0)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return `${m}m${s.toString().padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h${(m % 60).toString().padStart(2, "0")}m`;
}

export interface ProgressOpts {
  /** Override TTY detection (tests). */
  isTty?: boolean;
  /** Override the sink (defaults to Deno.stderr.writeSync). Tests only. */
  write?: (s: string) => void;
  /** Optional logger to mirror the final .done() summary at info level. */
  logger?: Logger;
}

export class Progress {
  private label = "";
  private total: number | undefined;
  private n = 0;
  private startMs = 0;
  private lastEmitMs = 0;
  private lastLineLen = 0;
  private active = false;
  private readonly isTty: boolean;
  private readonly write: (s: string) => void;
  private readonly logger?: Logger;

  constructor(opts: ProgressOpts = {}) {
    this.isTty = opts.isTty ?? safeIsTerminal();
    this.write = opts.write ??
      ((s: string) => {
        try {
          Deno.stderr.writeSync(new TextEncoder().encode(s));
        } catch { /* sink closed; ignore */ }
      });
    this.logger = opts.logger;
  }

  start(label: string, total?: number): void {
    this.label = label;
    this.total = total;
    this.n = 0;
    this.startMs = performance.now();
    this.lastEmitMs = 0;
    this.lastLineLen = 0;
    this.active = true;
  }

  /** Set the absolute count (not a delta) and maybe render. */
  update(n: number, extra?: string): void {
    if (!this.active) return;
    this.n = n;
    const now = performance.now();
    const interval = this.isTty ? TTY_INTERVAL_MS : PLAIN_INTERVAL_MS;
    if (now - this.lastEmitMs < interval) return;
    this.lastEmitMs = now;
    this.render(this.buildLine(extra), false);
  }

  done(extra?: string): void {
    if (!this.active) return;
    this.active = false;
    const line = this.buildLine(extra);
    this.render(line, true);
    if (this.logger) this.logger.info(line.trim());
  }

  private buildLine(extra?: string): string {
    const elapsedSec = (performance.now() - this.startMs) / 1000;
    const perSec = elapsedSec > 0 ? this.n / elapsedSec : 0;
    const count = this.total != null ? `${this.n}/${this.total}` : `${this.n}`;
    const parts = [
      `${this.label}: ${count}`,
      `${perSec.toFixed(1)}/s`,
      fmtDuration(elapsedSec),
    ];
    if (extra) parts.push(extra);
    return parts.join("  ");
  }

  private render(line: string, final: boolean): void {
    if (this.isTty) {
      // Rewrite the current line in place; pad to clear any leftover chars.
      const pad = Math.max(0, this.lastLineLen - line.length);
      this.write(`\r${line}${" ".repeat(pad)}`);
      this.lastLineLen = line.length;
      if (final) this.write("\n");
    } else {
      this.write(line + "\n");
    }
  }
}

function safeIsTerminal(): boolean {
  try {
    return Deno.stderr.isTerminal();
  } catch {
    return false;
  }
}

/** Convenience factory mirroring the makeLogger style. */
export function makeProgress(logger?: Logger): Progress {
  return new Progress({ logger });
}
