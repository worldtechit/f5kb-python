// ===========================================================================
// TEST: read-only status report aggregation + rendering.
// CATEGORY: unit
// COVERS: lib/status.ts (fns: computeStatus, renderStatus, classifyError)
// FIXTURES: none (a temp dump dir + temp SQLite DB seeded in-test)
// NETWORK: none (mocked)
// ASSERTS:
//   - computeStatus reads DB article/bodied counts + last run + per-type disk/index
//   - health is OK for a complete dump (bodyErrors don't degrade it); PARTIAL when a
//     type is partial/failed or fetched fewer than the server count
//   - error classes are tallied from body_error via classifyError
//   - renderStatus returns a non-empty multi-line string
// ===========================================================================

import { assertEquals, assertStringIncludes } from "@std/assert";
import { DatabaseSync } from "node:sqlite";
import { classifyError, computeStatus, renderStatus } from "../../lib/status.ts";
import { initDb } from "../../lib/track/db.ts";

// Build a temp dump dir with one type dir, an _index.json, and a seeded DB.
async function seed(opts: { withError?: boolean; partial?: boolean } = {}) {
  const root = await Deno.makeTempDir();
  const dump = `${root}/dump`;
  await Deno.mkdir(`${dump}/Knowledge`, { recursive: true });
  // Two on-disk article files (underscore files are ignored by the counter).
  await Deno.writeTextFile(`${dump}/Knowledge/K1.json`, "{}");
  await Deno.writeTextFile(`${dump}/Knowledge/K2.json`, "{}");
  await Deno.writeTextFile(`${dump}/Knowledge/_catalogue.json`, "{}");

  const nowIso = new Date().toISOString();
  await Deno.writeTextFile(
    `${dump}/_index.json`,
    JSON.stringify({
      types: [
        opts.partial
          ? { typeKey: "Knowledge", status: "partial", expected: 5, written: 2 }
          : { typeKey: "Knowledge", status: "ok", expected: 2, written: 2 },
      ],
    }),
  );
  if (opts.withError) {
    await Deno.writeTextFile(
      `${dump}/_enrich_report.json`,
      JSON.stringify({ types: [{ typeKey: "Knowledge", enriched: 1, failed: 1, skipped: 0 }] }),
    );
  }

  const dbPath = `${root}/articles.db`;
  const db = new DatabaseSync(dbPath);
  initDb(db);
  const ins = db.prepare(
    `INSERT INTO articles (document_type,id,title,link,created_ms,original_published_ms,
      updated_published_ms,modified_ms,captured_at,metadata_hash,content_hash,has_body,
      body_error,first_seen_run,last_seen_run,last_changed_run)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  );
  ins.run(
    "Knowledge",
    "K1",
    "t1",
    "l1",
    null,
    null,
    null,
    null,
    nowIso,
    "m1",
    "c1",
    1,
    null,
    "R1",
    "R1",
    "R1",
  );
  ins.run(
    "Knowledge",
    "K2",
    "t2",
    "l2",
    null,
    null,
    null,
    null,
    nowIso,
    "m2",
    "c2",
    0,
    opts.withError ? "HTTP 404 Not Found" : null,
    "R1",
    "R1",
    "R1",
  );
  db.prepare(
    "INSERT INTO runs (run_id,ran_at,dump_dir,types,scanned,new,changed,unchanged,removed) VALUES (?,?,?,?,?,?,?,?,?)",
  ).run("R1", nowIso, dump, "Knowledge", 2, 2, 0, 0, 0);
  db.close();

  return { root, dump, dbPath };
}

Deno.test("classifyError: coarse buckets", () => {
  assertEquals(classifyError("HTTP 404 Not Found"), "not-found");
  assertEquals(classifyError("403 Forbidden"), "forbidden");
  assertEquals(classifyError("request timeout"), "timeout");
  assertEquals(classifyError("HTTP 503"), "server-error");
  assertEquals(classifyError("extracted body too short"), "parse/empty");
  assertEquals(classifyError("something weird"), "other");
});

Deno.test("computeStatus: healthy complete dump -> OK with correct counts", async () => {
  const { root, dump, dbPath } = await seed();
  try {
    const rep = await computeStatus({ dump, db: dbPath });
    assertEquals(rep.dbPresent, true);
    assertEquals(rep.overall.totalArticles, 2);
    assertEquals(rep.overall.bodied, 1);
    assertEquals(rep.overall.health, "OK");
    assertEquals(rep.overall.lastRun?.runId, "R1");
    const kn = rep.perType.find((t) => t.typeKey === "Knowledge")!;
    assertEquals(kn.diskCount, 2); // _catalogue.json excluded
    assertEquals(kn.expected, 2);
    assertEquals(kn.written, 2);
    assertEquals(kn.status, "ok");
  } finally {
    await Deno.remove(root, { recursive: true });
  }
});

Deno.test("computeStatus: per-article bodyErrors are tallied but keep health OK", async () => {
  // bodyErrors (404s, moved pages, empty stubs) are an expected enrichment-coverage
  // detail — they populate the ERR column + error-class breakdown but do NOT degrade
  // health, because the DUMP itself is complete.
  const { root, dump, dbPath } = await seed({ withError: true });
  try {
    const rep = await computeStatus({ dump, db: dbPath });
    assertEquals(rep.overall.health, "OK");
    assertEquals(rep.errorClasses.find((e) => e.klass === "not-found")?.count, 1);
    const kn = rep.perType.find((t) => t.typeKey === "Knowledge")!;
    assertEquals(kn.errors, 1); // from the DB grouping
    assertEquals(kn.bodied, 1);
  } finally {
    await Deno.remove(root, { recursive: true });
  }
});

Deno.test("computeStatus: an incomplete dump (written<expected) -> PARTIAL", async () => {
  const { root, dump, dbPath } = await seed({ partial: true });
  try {
    const rep = await computeStatus({ dump, db: dbPath });
    assertEquals(rep.overall.health, "PARTIAL");
  } finally {
    await Deno.remove(root, { recursive: true });
  }
});

Deno.test("renderStatus: non-empty multi-line report", async () => {
  const { root, dump, dbPath } = await seed();
  try {
    const rep = await computeStatus({ dump, db: dbPath });
    const text = renderStatus(rep);
    assertEquals(text.length > 0, true);
    assertStringIncludes(text, "Status: OK");
    assertStringIncludes(text, "Knowledge");
    assertEquals(text.split("\n").length > 3, true);
  } finally {
    await Deno.remove(root, { recursive: true });
  }
});
