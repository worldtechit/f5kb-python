// ===========================================================================
// TEST: `f5kb approve` cmd wrapper — changelog ON by default + DB reindex
// CATEGORY: integration
// COVERS: cmd/approve.ts (run), lib/approve.ts, lib/staging.ts, lib/track/db.ts
// FIXTURES: none — builds a dump + _pending + manifest inline
// NETWORK: none (mocked)
// ASSERTS:
//   - a plain `approve` (NO --changelog) still writes <dump>/_changelog.jsonl
//     (regression: approve used to be opt-in, so promotions went unlogged)
//   - the logged record is op="edited", source="approve", with the changed parts
//   - --no-changelog suppresses the file
//   - promotion reindexes the DB (the live row reflects the new content)
// ===========================================================================

import { assertEquals } from "@std/assert";
import { DatabaseSync } from "node:sqlite";
import { run as approveCmd } from "../../cmd/approve.ts";
import { parseFlags } from "../../lib/args.ts";
import { makeLogger } from "../../lib/logger.ts";
import { livePath, mergePending, pendingPath } from "../../lib/staging.ts";

const NOW = Date.UTC(2026, 5, 4);
const quietLog = makeLogger({ level: "error", json: false, scope: "test" });

async function seedStaged(out: string) {
  // a live Manual article + a staged replacement (metadata + body both changed)
  const lp = livePath(out, "Manual", "K1");
  await Deno.mkdir(lp.slice(0, lp.lastIndexOf("/")), { recursive: true });
  await Deno.writeTextFile(
    lp,
    JSON.stringify({
      id: "K1",
      documentType: "Manual",
      metadata: { v: 1 },
      content: { body_text: "old" },
    }),
  );
  const pp = pendingPath(out, "Manual", "K1");
  await Deno.mkdir(pp.slice(0, pp.lastIndexOf("/")), { recursive: true });
  await Deno.writeTextFile(
    pp,
    JSON.stringify({
      id: "K1",
      documentType: "Manual",
      metadata: { v: 2 },
      content: { body_text: "brand new body" },
    }),
  );
  await mergePending(out, [{
    typeKey: "Manual",
    id: "K1",
    op: "edited",
    source: "sync",
    stagedAt: "t",
  }], "r");
}

Deno.test("approve cmd: logs to _changelog.jsonl by DEFAULT (no --changelog)", async () => {
  const root = await Deno.makeTempDir();
  const out = `${root}/dump`;
  const db = `${root}/articles.db`;
  try {
    await seedStaged(out);
    const code = await approveCmd(
      parseFlags([`--dump=${out}`, `--db=${db}`]),
      quietLog,
      { nowMs: NOW },
    );
    assertEquals(code, 0);

    // Default ON: the changelog file exists with the promotion record.
    const rec = JSON.parse((await Deno.readTextFile(`${out}/_changelog.jsonl`)).trim());
    assertEquals(rec.op, "edited");
    assertEquals(rec.source, "approve");
    assertEquals(rec.changed, ["metadata", "content"]);

    // The promotion reindexed the DB (live row now has a body).
    const sdb = new DatabaseSync(db);
    const row = sdb.prepare(
      "SELECT has_body FROM articles WHERE document_type='Manual' AND id='K1'",
    )
      .get() as { has_body: number } | undefined;
    sdb.close();
    assertEquals(row?.has_body, 1);

    // Live file holds the new body; pending cleared.
    assertEquals(
      JSON.parse(await Deno.readTextFile(livePath(out, "Manual", "K1"))).content.body_text,
      "brand new body",
    );
  } finally {
    await Deno.remove(root, { recursive: true });
  }
});

Deno.test("approve cmd: --no-changelog suppresses the file", async () => {
  const root = await Deno.makeTempDir();
  const out = `${root}/dump`;
  try {
    await seedStaged(out);
    await approveCmd(
      parseFlags([`--dump=${out}`, `--db=${root}/db`, "--no-changelog"]),
      quietLog,
      { nowMs: NOW },
    );
    let present = true;
    try {
      Deno.statSync(`${out}/_changelog.jsonl`);
    } catch {
      present = false;
    }
    assertEquals(present, false);
  } finally {
    await Deno.remove(root, { recursive: true });
  }
});
