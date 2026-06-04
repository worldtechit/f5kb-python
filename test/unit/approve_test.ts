// ===========================================================================
// TEST: approve — promote/hold/reject staged overwrites
// CATEGORY: unit (integration-ish; real files, no network)
// COVERS: lib/approve.ts (approve), lib/staging.ts
// FIXTURES: none — builds a dump with live + _pending files inline
// NETWORK: none (mocked)
// ASSERTS:
//   - a clean edit is promoted: live file archived to _replaced/, pending moved in
//   - a risky edit (body-dropped) is HELD by default, promoted with includeRisky
//   - --reject discards the pending file, leaves live untouched
//   - --list (dryRun) previews + changes nothing
//   - the changelog records op="edited" source="approve" for promotions only
// ===========================================================================

import { assertEquals, assertExists } from "@std/assert";
import { approve } from "../../lib/approve.ts";
import { Changelog } from "../../lib/changelog.ts";
import { livePath, mergePending, pendingPath } from "../../lib/staging.ts";

// Seed a live article + a staged pending replacement for it.
async function stage(
  out: string,
  typeKey: string,
  id: string,
  liveContent: Record<string, unknown>,
  pendingContent: Record<string, unknown>,
) {
  const lp = livePath(out, typeKey, id);
  await Deno.mkdir(lp.slice(0, lp.lastIndexOf("/")), { recursive: true });
  await Deno.writeTextFile(lp, JSON.stringify({ id, documentType: typeKey, content: liveContent }));
  const pp = pendingPath(out, typeKey, id);
  await Deno.mkdir(pp.slice(0, pp.lastIndexOf("/")), { recursive: true });
  await Deno.writeTextFile(
    pp,
    JSON.stringify({ id, documentType: typeKey, content: pendingContent }),
  );
}

const NOW = Date.UTC(2026, 5, 4);

Deno.test("approve: promotes a clean edit, archives the replaced live file", async () => {
  const out = await Deno.makeTempDir();
  try {
    await stage(out, "Policy", "K1", { body_text: "old" }, { body_text: "new and improved" });
    await mergePending(out, [{
      typeKey: "Policy",
      id: "K1",
      op: "edited",
      source: "sync",
      stagedAt: "t",
    }], "r");

    const cl = new Changelog(null, "r");
    const res = await approve({ dump: out, nowMs: NOW, changelog: cl });
    assertEquals(res.promoted, 1);
    assertEquals(res.heldRisky, 0);
    assertEquals(res.remaining, 0);

    // live now holds the new body; pending gone; replaced archived.
    const live = JSON.parse(await Deno.readTextFile(livePath(out, "Policy", "K1")));
    assertEquals(live.content.body_text, "new and improved");
    let pend = true;
    try {
      Deno.statSync(pendingPath(out, "Policy", "K1"));
      pend = true;
    } catch {
      pend = false;
    }
    assertEquals(pend, false);
    // a _replaced/ copy of the old body exists
    let replacedFound = false;
    for await (const e of Deno.readDir(`${out}/_replaced/Policy`)) {
      if (e.name.startsWith("K1.")) replacedFound = true;
    }
    assertEquals(replacedFound, true);
    assertEquals(cl.byOp(), { edited: 1 });
  } finally {
    await Deno.remove(out, { recursive: true });
  }
});

Deno.test("approve: HOLDS a risky edit (body-dropped) until --include-risky", async () => {
  const out = await Deno.makeTempDir();
  try {
    // pending would drop the live body (empty content).
    await stage(out, "Manual", "K9", { body_text: "good body".repeat(20) }, {});
    await mergePending(out, [{
      typeKey: "Manual",
      id: "K9",
      op: "edited",
      source: "sync",
      stagedAt: "t",
    }], "r");

    // default: held, live untouched, still pending.
    let res = await approve({ dump: out, nowMs: NOW });
    assertEquals(res.promoted, 0);
    assertEquals(res.heldRisky, 1);
    assertEquals(res.remaining, 1);
    assertEquals(res.items[0].risk.includes("body-dropped"), true);
    assertEquals(
      JSON.parse(await Deno.readTextFile(livePath(out, "Manual", "K9"))).content.body_text.length >
        0,
      true,
    );

    // explicit override promotes it.
    res = await approve({ dump: out, nowMs: NOW, includeRisky: true });
    assertEquals(res.promoted, 1);
    assertEquals(res.remaining, 0);
    assertEquals(
      JSON.parse(await Deno.readTextFile(livePath(out, "Manual", "K9"))).content.body_text,
      undefined,
    );
  } finally {
    await Deno.remove(out, { recursive: true });
  }
});

Deno.test("approve --reject: discards pending, leaves live intact", async () => {
  const out = await Deno.makeTempDir();
  try {
    await stage(out, "Policy", "K2", { body_text: "keep me" }, { body_text: "discard me" });
    await mergePending(out, [{
      typeKey: "Policy",
      id: "K2",
      op: "edited",
      source: "sync",
      stagedAt: "t",
    }], "r");

    const res = await approve({ dump: out, nowMs: NOW, reject: true });
    assertEquals(res.rejected, 1);
    assertEquals(res.remaining, 0);
    assertEquals(
      JSON.parse(await Deno.readTextFile(livePath(out, "Policy", "K2"))).content.body_text,
      "keep me",
    );
    let pend = true;
    try {
      Deno.statSync(pendingPath(out, "Policy", "K2"));
    } catch {
      pend = false;
    }
    assertEquals(pend, false);
  } finally {
    await Deno.remove(out, { recursive: true });
  }
});

Deno.test("approve --list (dry-run): previews, changes nothing", async () => {
  const out = await Deno.makeTempDir();
  try {
    await stage(out, "Policy", "K3", { body_text: "old" }, { body_text: "new" });
    await mergePending(out, [{
      typeKey: "Policy",
      id: "K3",
      op: "edited",
      source: "sync",
      stagedAt: "t",
    }], "r");

    const res = await approve({ dump: out, nowMs: NOW, dryRun: true });
    assertEquals(res.promoted, 0);
    assertEquals(res.remaining, 1);
    assertEquals(res.items[0].action, "preview");
    // pending still there, live unchanged.
    assertExists(Deno.statSync(pendingPath(out, "Policy", "K3")));
    assertEquals(
      JSON.parse(await Deno.readTextFile(livePath(out, "Policy", "K3"))).content.body_text,
      "old",
    );
  } finally {
    await Deno.remove(out, { recursive: true });
  }
});

Deno.test("approve: --exclude-types skips that type's staged edits", async () => {
  const out = await Deno.makeTempDir();
  try {
    await stage(out, "Policy", "P1", { body_text: "old" }, { body_text: "new" });
    await stage(out, "Manual", "M1", { body_text: "old" }, { body_text: "new" });
    await mergePending(out, [
      { typeKey: "Policy", id: "P1", op: "edited", source: "sync", stagedAt: "t" },
      { typeKey: "Manual", id: "M1", op: "edited", source: "sync", stagedAt: "t" },
    ], "r");

    const res = await approve({ dump: out, nowMs: NOW, excludeTypeKeys: ["Manual"] });
    assertEquals(res.promoted, 1); // only Policy
    assertEquals(res.remaining, 1); // Manual still pending
    assertEquals(res.items.map((i) => `${i.typeKey}/${i.id}`), ["Policy/P1"]);
    // Manual's pending file untouched, Policy's promoted.
    assertExists(Deno.statSync(pendingPath(out, "Manual", "M1")));
    assertEquals(
      JSON.parse(await Deno.readTextFile(livePath(out, "Policy", "P1"))).content.body_text,
      "new",
    );
  } finally {
    await Deno.remove(out, { recursive: true });
  }
});

Deno.test("approve: changelog records metadata/content via `changed` + detail", async () => {
  const out = await Deno.makeTempDir();
  const clPath = `${out}/_changelog.jsonl`;
  try {
    // live vs pending differ in BOTH metadata and content (body).
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
        content: { body_text: "a much longer new body" },
      }),
    );
    await mergePending(out, [{
      typeKey: "Manual",
      id: "K1",
      op: "edited",
      source: "sync",
      stagedAt: "t",
    }], "r");

    const cl = new Changelog(clPath, "2026-06-04T00:00:00.000Z");
    const res = await approve({ dump: out, nowMs: NOW, changelog: cl });
    await cl.flush();

    assertEquals(res.promoted, 1);
    assertEquals(res.items[0].changed, ["metadata", "content"]);

    const rec = JSON.parse((await Deno.readTextFile(clPath)).trim());
    assertEquals(rec.op, "edited");
    assertEquals(rec.source, "approve");
    assertEquals(rec.changed, ["metadata", "content"]);
    assertEquals(rec.detail.includes("metadata+content"), true);
  } finally {
    await Deno.remove(out, { recursive: true });
  }
});
