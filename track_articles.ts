/**
 * Maintains a master overview of every dumped article in a small embedded
 * SQLite database, so changes can be tracked across dump runs (new articles,
 * changed articles, removed articles).
 *
 * It is a post-processor: run it AFTER dump_articles.ts (and after
 * enrich_bodies.ts, so bodies are included in the content hash). It walks the
 * dump directory, and for each article records its identity, its several dates,
 * a hash of its metadata, and a hash of its content (body). On each run it
 * compares against the stored row and classifies the article as new / changed /
 * unchanged, logging every change.
 *
 * Why a DB (vs one JSON file): upserting ~100k+ rows and querying "what changed
 * since run X" is far cheaper against SQLite than rewriting a giant JSON each
 * run. node:sqlite is built into Deno 2.x — no external dependency.
 *
 * This is also the foundation for a future optimization (NOT implemented; see
 * TODO.txt): if an article's dates/metadata_hash are unchanged from the last
 * run, skip re-pulling its (expensive) body in enrich_bodies.ts.
 *
 * Usage:
 *   deno run --allow-read --allow-write track_articles.ts \
 *       --dump=outputs/dump [--db=outputs/articles.db] [--types="A,B"] \
 *       [--run-id=ID] [--json]
 *
 * Options:
 *   --dump=DIR     Dump directory to index (default: outputs/dump).
 *   --db=FILE      SQLite file (default: <dump>/../articles.db).
 *   --types="A,B"  Only index these type subdirs (default: all present).
 *   --run-id=ID    Label for this run (default: current ISO timestamp).
 *   --json         Print the run summary as JSON.
 *
 * Removed-article detection is scoped to the types actually scanned this run, so
 * indexing a subset never mislabels other types' articles as removed.
 */

import { DatabaseSync } from "node:sqlite";

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
const args: Record<string, string> = {};
for (const a of Deno.args) {
  if (!a.startsWith("--")) continue;
  const eq = a.indexOf("=");
  if (eq === -1) args[a.slice(2)] = "true";
  else args[a.slice(2, eq)] = a.slice(eq + 1);
}
const DUMP = args.dump ?? "outputs/dump";
const DB_PATH = args.db ?? `${DUMP.replace(/\/+$/, "")}/../articles.db`;
const RUN_ID = args["run-id"] ?? new Date().toISOString();
const TYPE_FILTER = args.types
  ? args.types.split(",").map((s) => s.trim()).filter(Boolean)
  : null;
const AS_JSON = "json" in args;

// ---------------------------------------------------------------------------
// Hashing / canonicalization
// ---------------------------------------------------------------------------
// Deterministic JSON: sort object keys recursively so logically-equal objects
// hash identically regardless of key order.
function canonical(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(canonical);
  if (v && typeof v === "object") {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(v as Record<string, unknown>).sort()) {
      out[k] = canonical((v as Record<string, unknown>)[k]);
    }
    return out;
  }
  return v;
}

async function sha256(obj: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(canonical(obj)));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------------------
// Article -> tracked record
// ---------------------------------------------------------------------------
interface Article {
  id?: string;
  documentType?: string;
  title?: string;
  link?: string;
  modifiedMs?: number | null;
  capturedAt?: string;
  metadata?: Record<string, unknown>;
  content?: Record<string, unknown>;
}

// content keys that are bookkeeping, not body — excluded from the content hash
// and the has-body test so a re-fetch timestamp never looks like a change.
const VOLATILE_CONTENT_KEYS = new Set(["bodySource", "fetchedAt"]);

function numMeta(meta: Record<string, unknown> | undefined, key: string): number | null {
  const v = meta?.[key];
  return typeof v === "number" ? v : null;
}

function contentForHash(content: Record<string, unknown> | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(content ?? {})) {
    if (!VOLATILE_CONTENT_KEYS.has(k)) out[k] = v;
  }
  return out;
}

function hasBody(content: Record<string, unknown> | undefined): boolean {
  for (const [k, v] of Object.entries(content ?? {})) {
    if (VOLATILE_CONTENT_KEYS.has(k) || k === "bodyError") continue;
    if (typeof v === "string" ? v.trim().length > 0 : v != null) return true;
  }
  return false;
}

interface Record_ {
  id: string;
  document_type: string;
  title: string;
  link: string;
  created_ms: number | null;
  original_published_ms: number | null;
  updated_published_ms: number | null;
  modified_ms: number | null;
  captured_at: string;
  metadata_hash: string;
  content_hash: string;
  has_body: number;
  body_error: string | null;
}

async function toRecord(a: Article): Promise<Record_> {
  return {
    id: a.id ?? "",
    document_type: a.documentType ?? "",
    title: a.title ?? "",
    link: a.link ?? "",
    created_ms: numMeta(a.metadata, "f5_created_date"),
    original_published_ms: numMeta(a.metadata, "f5_original_published_date"),
    updated_published_ms: numMeta(a.metadata, "f5_updated_published_date"),
    modified_ms: typeof a.modifiedMs === "number" ? a.modifiedMs : null,
    captured_at: a.capturedAt ?? "",
    metadata_hash: await sha256(a.metadata ?? {}),
    content_hash: await sha256(contentForHash(a.content)),
    has_body: hasBody(a.content) ? 1 : 0,
    body_error: typeof a.content?.bodyError === "string" ? a.content.bodyError : null,
  };
}

// ---------------------------------------------------------------------------
// DB schema
// ---------------------------------------------------------------------------
function initDb(db: DatabaseSync) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS articles (
      document_type TEXT NOT NULL,
      id TEXT NOT NULL,
      title TEXT, link TEXT,
      created_ms INTEGER, original_published_ms INTEGER,
      updated_published_ms INTEGER, modified_ms INTEGER,
      captured_at TEXT,
      metadata_hash TEXT, content_hash TEXT,
      has_body INTEGER, body_error TEXT,
      first_seen_run TEXT, last_seen_run TEXT, last_changed_run TEXT,
      PRIMARY KEY (document_type, id)
    );
    CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY,
      ran_at TEXT, dump_dir TEXT, types TEXT,
      scanned INTEGER, new INTEGER, changed INTEGER, unchanged INTEGER, removed INTEGER
    );
    CREATE TABLE IF NOT EXISTS changes (
      run_id TEXT, document_type TEXT, id TEXT, change_type TEXT, detail TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
    CREATE INDEX IF NOT EXISTS idx_articles_seen ON articles(last_seen_run);
  `);
}

// ---------------------------------------------------------------------------
// Walk + upsert
// ---------------------------------------------------------------------------
async function* articleFiles(typeDir: string): AsyncGenerator<string> {
  for await (const e of Deno.readDir(typeDir)) {
    if (e.isFile && e.name.endsWith(".json") && !e.name.startsWith("_")) {
      yield `${typeDir}/${e.name}`;
    }
  }
}

// What changed between the stored row and the new record.
function diffFields(prev: any, rec: Record_): string[] {
  const changed: string[] = [];
  if (prev.metadata_hash !== rec.metadata_hash) changed.push("metadata");
  if (prev.content_hash !== rec.content_hash) changed.push("content");
  if (prev.updated_published_ms !== rec.updated_published_ms) changed.push("updated_published");
  if (prev.modified_ms !== rec.modified_ms) changed.push("modified");
  if ((prev.body_error ?? null) !== (rec.body_error ?? null)) changed.push("body_error");
  return changed;
}

async function main() {
  // Discover type subdirs to index.
  let typeKeys: string[] = [];
  try {
    for await (const e of Deno.readDir(DUMP)) {
      if (e.isDirectory) typeKeys.push(e.name);
    }
  } catch (e) {
    console.error(`Cannot read dump dir ${DUMP}: ${(e as Error).message}`);
    Deno.exit(1);
  }
  typeKeys.sort();
  if (TYPE_FILTER) typeKeys = typeKeys.filter((t) => TYPE_FILTER.includes(t));
  if (!typeKeys.length) {
    console.error(`No type directories to index under ${DUMP}.`);
    Deno.exit(1);
  }

  const db = new DatabaseSync(DB_PATH);
  initDb(db);

  const sel = db.prepare("SELECT * FROM articles WHERE document_type=? AND id=?");
  const ins = db.prepare(`
    INSERT INTO articles (document_type,id,title,link,created_ms,original_published_ms,
      updated_published_ms,modified_ms,captured_at,metadata_hash,content_hash,has_body,
      body_error,first_seen_run,last_seen_run,last_changed_run)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(document_type,id) DO UPDATE SET
      title=excluded.title, link=excluded.link, created_ms=excluded.created_ms,
      original_published_ms=excluded.original_published_ms,
      updated_published_ms=excluded.updated_published_ms, modified_ms=excluded.modified_ms,
      captured_at=excluded.captured_at, metadata_hash=excluded.metadata_hash,
      content_hash=excluded.content_hash, has_body=excluded.has_body,
      body_error=excluded.body_error, last_seen_run=excluded.last_seen_run,
      last_changed_run=excluded.last_changed_run
  `);
  const logChange = db.prepare("INSERT INTO changes (run_id,document_type,id,change_type,detail) VALUES (?,?,?,?,?)");

  let scanned = 0, nNew = 0, nChanged = 0, nUnchanged = 0;
  const perType: Record<string, { scanned: number; new: number; changed: number }> = {};

  db.exec("BEGIN");
  for (const typeKey of typeKeys) {
    const typeDir = `${DUMP}/${typeKey}`;
    perType[typeKey] = { scanned: 0, new: 0, changed: 0 };
    for await (const file of articleFiles(typeDir)) {
      let a: Article;
      try {
        a = JSON.parse(await Deno.readTextFile(file));
      } catch (e) {
        console.warn(`  skip unreadable ${file}: ${(e as Error).message}`);
        continue;
      }
      const rec = await toRecord(a);
      if (!rec.id) continue;
      scanned++;
      perType[typeKey].scanned++;

      const prev = sel.get(rec.document_type, rec.id) as any;
      let changeType: "new" | "changed" | "unchanged";
      let lastChanged: string;
      if (!prev) {
        changeType = "new";
        lastChanged = RUN_ID;
        nNew++;
        perType[typeKey].new++;
        logChange.run(RUN_ID, rec.document_type, rec.id, "new", "");
      } else {
        const diff = diffFields(prev, rec);
        if (diff.length) {
          changeType = "changed";
          lastChanged = RUN_ID;
          nChanged++;
          perType[typeKey].changed++;
          logChange.run(RUN_ID, rec.document_type, rec.id, "changed", diff.join(","));
        } else {
          changeType = "unchanged";
          lastChanged = prev.last_changed_run ?? RUN_ID;
          nUnchanged++;
        }
      }
      const firstSeen = prev?.first_seen_run ?? RUN_ID;
      ins.run(
        rec.document_type, rec.id, rec.title, rec.link, rec.created_ms,
        rec.original_published_ms, rec.updated_published_ms, rec.modified_ms,
        rec.captured_at, rec.metadata_hash, rec.content_hash, rec.has_body,
        rec.body_error, firstSeen, RUN_ID, lastChanged,
      );
      void changeType;
    }
  }

  // Removed = previously-seen rows in the scanned types that this run did not touch.
  const placeholders = typeKeys.map(() => "?").join(",");
  const removedRows = db.prepare(
    `SELECT document_type,id FROM articles WHERE document_type IN (${placeholders}) AND last_seen_run!=?`,
  ).all(...typeKeys, RUN_ID) as any[];
  for (const r of removedRows) {
    logChange.run(RUN_ID, r.document_type, r.id, "removed", "");
  }
  const removed = removedRows.length;

  db.prepare(
    "INSERT OR REPLACE INTO runs (run_id,ran_at,dump_dir,types,scanned,new,changed,unchanged,removed) VALUES (?,?,?,?,?,?,?,?,?)",
  ).run(RUN_ID, new Date().toISOString(), DUMP, typeKeys.join(","), scanned, nNew, nChanged, nUnchanged, removed);
  db.exec("COMMIT");
  db.close();

  const summary = {
    runId: RUN_ID, db: DB_PATH, dump: DUMP, types: typeKeys.length,
    scanned, new: nNew, changed: nChanged, unchanged: nUnchanged, removed, perType,
  };
  if (AS_JSON) {
    console.log(JSON.stringify(summary, null, 2));
  } else {
    console.log(`Indexed ${scanned} articles across ${typeKeys.length} type(s) -> ${DB_PATH}`);
    console.log(`  new=${nNew} changed=${nChanged} unchanged=${nUnchanged} removed=${removed} (run ${RUN_ID})`);
    if (removed) console.log(`  (removed = rows in scanned types not present in this dump; not deleted)`);
  }
}

if (import.meta.main) await main();
