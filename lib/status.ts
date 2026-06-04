// Read-only status report for a dump + its tracking DB.
//
// Aggregates four independent sources, each handled gracefully when absent:
//   1. <dump>/_index.json          per-type expected/written/status (from the dump)
//   2. <dump>/_enrich_report.json  per-type enriched/failed/skipped (from enrich)
//   3. on-disk per-type file counts (count *.json minus _-prefixed)
//   4. <dump>/../articles.db (or opts.db) — articles/runs tables
//
// computeStatus() never writes; the DB is opened read-only when supported.

import { DatabaseSync } from "node:sqlite";
import { exists, listTypeDirs, readJson } from "./fsutil.ts";

export interface TypeStatus {
  typeKey: string;
  diskCount: number;
  expected: number | null;
  written: number | null;
  status: string | null; // dump status: ok/partial/failed
  bodied: number | null; // enriched bodies reported by enrich
  errors: number | null; // enrich failures
}

export interface ErrorClass {
  klass: string;
  count: number;
}

export interface StatusReport {
  dump: string;
  db: string;
  dbPresent: boolean;
  perType: TypeStatus[];
  overall: {
    totalArticles: number | null; // rows in DB
    bodied: number | null; // has_body=1 in DB
    health: "OK" | "PARTIAL" | "STALE";
    lastRun: LastRun | null;
    newestCapturedAt: string | null;
    stalenessMs: number | null; // now - newest(capturedAt, ran_at)
  };
  errorClasses: ErrorClass[];
  notes: string[];
}

export interface LastRun {
  runId: string;
  ranAt: string | null;
  dumpDir: string | null;
  scanned: number | null;
  new: number | null;
  changed: number | null;
  unchanged: number | null;
  removed: number | null;
}

interface IndexType {
  typeKey?: string;
  dir?: string;
  status?: string;
  expected?: number;
  written?: number;
}
interface IndexFile {
  types?: IndexType[];
}
interface EnrichType {
  typeKey?: string;
  enriched?: number;
  failed?: number;
  skipped?: number;
}
interface EnrichFile {
  types?: EnrichType[];
}

const STALE_MS = 7 * 24 * 60 * 60 * 1000; // a dump older than a week is "stale"

// Coarse classification of a bodyError message so we can group counts.
export function classifyError(msg: string): string {
  const m = msg.toLowerCase();
  if (m.includes("404") || m.includes("not found")) return "not-found";
  if (m.includes("403") || m.includes("forbidden") || m.includes("401") || m.includes("unauthor")) {
    return "forbidden";
  }
  if (m.includes("timeout") || m.includes("timed out")) return "timeout";
  if (m.includes("429") || m.includes("rate")) return "rate-limited";
  if (m.match(/\b5\d\d\b/) || m.includes("server error")) return "server-error";
  if (
    m.includes("parse") || m.includes("no body") || m.includes("empty") || m.includes("extract")
  ) {
    return "parse/empty";
  }
  if (m.includes("network") || m.includes("fetch") || m.includes("connection")) return "network";
  return "other";
}

async function countDiskJson(typeDir: string): Promise<number> {
  let n = 0;
  try {
    for await (const e of Deno.readDir(typeDir)) {
      if (e.isFile && e.name.endsWith(".json") && !e.name.startsWith("_")) n++;
    }
  } catch {
    // missing dir -> 0
  }
  return n;
}

function openReadOnly(path: string): DatabaseSync {
  // node:sqlite supports a readOnly open option; fall back to a default open if
  // the running Deno doesn't recognize it.
  try {
    return new DatabaseSync(path, { readOnly: true } as unknown as undefined);
  } catch {
    return new DatabaseSync(path);
  }
}

export async function computeStatus(
  opts: { dump: string; db?: string },
): Promise<StatusReport> {
  const dump = opts.dump.replace(/\/+$/, "");
  const dbPath = opts.db ?? `${dump}/../articles.db`;
  const notes: string[] = [];

  // --- _index.json ---
  const indexPath = `${dump}/_index.json`;
  const indexByType = new Map<string, IndexType>();
  if (await exists(indexPath)) {
    try {
      const idx = await readJson<IndexFile>(indexPath);
      for (const t of idx.types ?? []) {
        const key = t.typeKey ?? t.dir;
        if (key) indexByType.set(key, t);
      }
    } catch (e) {
      notes.push(`could not parse _index.json: ${(e as Error).message}`);
    }
  } else {
    notes.push("_index.json missing");
  }

  // --- _enrich_report.json ---
  const enrichPath = `${dump}/_enrich_report.json`;
  const enrichByType = new Map<string, EnrichType>();
  if (await exists(enrichPath)) {
    try {
      const er = await readJson<EnrichFile>(enrichPath);
      for (const t of er.types ?? []) {
        if (t.typeKey) enrichByType.set(t.typeKey, t);
      }
    } catch (e) {
      notes.push(`could not parse _enrich_report.json: ${(e as Error).message}`);
    }
  } else {
    notes.push("_enrich_report.json missing");
  }

  // --- on-disk per-type counts ---
  let typeKeys: string[] = [];
  try {
    typeKeys = await listTypeDirs(dump);
  } catch {
    notes.push(`dump dir ${dump} not readable`);
  }
  // Include any type known from _index even if its dir is absent on disk.
  for (const k of indexByType.keys()) if (!typeKeys.includes(k)) typeKeys.push(k);
  typeKeys.sort();

  const perType: TypeStatus[] = [];
  for (const typeKey of typeKeys) {
    const diskCount = await countDiskJson(`${dump}/${typeKey}`);
    const idx = indexByType.get(typeKey);
    const enr = enrichByType.get(typeKey);
    perType.push({
      typeKey,
      diskCount,
      expected: idx?.expected ?? null,
      written: idx?.written ?? null,
      status: idx?.status ?? null,
      bodied: enr?.enriched ?? null,
      errors: enr?.failed ?? null,
    });
  }

  // --- DB (read-only) ---
  let totalArticles: number | null = null;
  let bodied: number | null = null;
  let lastRun: LastRun | null = null;
  let newestCapturedAt: string | null = null;
  const errorClasses: ErrorClass[] = [];
  const dbPresent = await exists(dbPath);
  if (dbPresent) {
    let db: DatabaseSync | null = null;
    try {
      db = openReadOnly(dbPath);
      totalArticles = (db.prepare("SELECT COUNT(*) AS c FROM articles").get() as any)?.c ?? null;
      bodied =
        (db.prepare("SELECT COUNT(*) AS c FROM articles WHERE has_body=1").get() as any)?.c ?? null;

      const errRows = db.prepare(
        "SELECT body_error FROM articles WHERE body_error IS NOT NULL AND body_error != ''",
      ).all() as Array<{ body_error: string }>;
      const tally = new Map<string, number>();
      for (const r of errRows) {
        const k = classifyError(String(r.body_error));
        tally.set(k, (tally.get(k) ?? 0) + 1);
      }
      for (const [klass, count] of [...tally.entries()].sort((a, b) => b[1] - a[1])) {
        errorClasses.push({ klass, count });
      }

      const run = db.prepare(
        "SELECT * FROM runs ORDER BY ran_at DESC LIMIT 1",
      ).get() as any;
      if (run) {
        lastRun = {
          runId: run.run_id,
          ranAt: run.ran_at ?? null,
          dumpDir: run.dump_dir ?? null,
          scanned: run.scanned ?? null,
          new: run.new ?? null,
          changed: run.changed ?? null,
          unchanged: run.unchanged ?? null,
          removed: run.removed ?? null,
        };
      }
      const cap = db.prepare(
        "SELECT MAX(captured_at) AS m FROM articles",
      ).get() as any;
      newestCapturedAt = cap?.m ?? null;
    } catch (e) {
      notes.push(`could not read DB ${dbPath}: ${(e as Error).message}`);
    } finally {
      try {
        db?.close();
      } catch { /* ignore */ }
    }
  } else {
    notes.push(`DB ${dbPath} missing`);
  }

  // --- staleness + health ---
  const now = Date.now();
  const stamps: number[] = [];
  const capMs = newestCapturedAt ? Date.parse(newestCapturedAt) : NaN;
  if (!Number.isNaN(capMs)) stamps.push(capMs);
  const ranMs = lastRun?.ranAt ? Date.parse(lastRun.ranAt) : NaN;
  if (!Number.isNaN(ranMs)) stamps.push(ranMs);
  const newest = stamps.length ? Math.max(...stamps) : null;
  const stalenessMs = newest != null ? now - newest : null;

  const anyPartial = perType.some((t) => t.status && t.status !== "ok") ||
    perType.some((t) => (t.errors ?? 0) > 0) ||
    perType.some((t) => t.expected != null && t.written != null && t.written < t.expected);
  let health: StatusReport["overall"]["health"];
  if (stalenessMs != null && stalenessMs > STALE_MS) health = "STALE";
  else if (anyPartial || !dbPresent) health = "PARTIAL";
  else health = "OK";

  return {
    dump,
    db: dbPath,
    dbPresent,
    perType,
    overall: { totalArticles, bodied, health, lastRun, newestCapturedAt, stalenessMs },
    errorClasses,
    notes,
  };
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function fmtAge(ms: number | null): string {
  if (ms == null) return "?";
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(0)}s ago`;
  const min = sec / 60;
  if (min < 60) return `${min.toFixed(0)}m ago`;
  const hr = min / 60;
  if (hr < 24) return `${hr.toFixed(1)}h ago`;
  return `${(hr / 24).toFixed(1)}d ago`;
}

function pad(s: string, w: number): string {
  return s.length >= w ? s : s + " ".repeat(w - s.length);
}
function padL(s: string, w: number): string {
  return s.length >= w ? s : " ".repeat(w - s.length) + s;
}

export function renderStatus(report: StatusReport): string {
  const lines: string[] = [];
  const o = report.overall;
  lines.push(`Status: ${o.health}   dump=${report.dump}`);
  lines.push(
    `  DB: ${report.dbPresent ? report.db : "(missing)"}  ` +
      `articles=${o.totalArticles ?? "?"}  bodied=${o.bodied ?? "?"}`,
  );
  if (o.lastRun) {
    const r = o.lastRun;
    lines.push(
      `  last run ${r.runId} (${fmtAge(o.stalenessMs)}): ` +
        `scanned=${r.scanned ?? "?"} new=${r.new ?? "?"} changed=${r.changed ?? "?"} ` +
        `unchanged=${r.unchanged ?? "?"} removed=${r.removed ?? "?"}`,
    );
  } else {
    lines.push(`  last run: (none recorded)`);
  }
  if (o.newestCapturedAt) {
    lines.push(`  newest capturedAt: ${o.newestCapturedAt} (${fmtAge(o.stalenessMs)})`);
  }

  // Per-type table.
  const head = `  ${pad("TYPE", 26)} ${padL("DISK", 8)} ${padL("EXP", 8)} ${padL("WRIT", 8)} ${
    pad("STATUS", 9)
  } ${padL("BODIED", 8)} ${padL("ERR", 6)}`;
  lines.push("");
  lines.push(head);
  lines.push("  " + "-".repeat(head.length - 2));
  for (const t of report.perType) {
    lines.push(
      `  ${pad(t.typeKey, 26)} ${padL(String(t.diskCount), 8)} ` +
        `${padL(t.expected != null ? String(t.expected) : "-", 8)} ` +
        `${padL(t.written != null ? String(t.written) : "-", 8)} ` +
        `${pad(t.status ?? "-", 9)} ` +
        `${padL(t.bodied != null ? String(t.bodied) : "-", 8)} ` +
        `${padL(t.errors != null ? String(t.errors) : "-", 6)}`,
    );
  }

  if (report.errorClasses.length) {
    lines.push("");
    lines.push("  body errors by class:");
    for (const ec of report.errorClasses) lines.push(`    ${pad(ec.klass, 16)} ${ec.count}`);
  }
  if (report.notes.length) {
    lines.push("");
    lines.push("  notes:");
    for (const n of report.notes) lines.push(`    - ${n}`);
  }
  return lines.join("\n");
}
