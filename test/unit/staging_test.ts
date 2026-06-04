// ===========================================================================
// TEST: overwrite-protection staging primitives
// CATEGORY: unit
// COVERS: lib/staging.ts (paths, mergePending upsert, computeRisk, archiveReplaced)
// FIXTURES: none
// NETWORK: none (mocked)
// ASSERTS:
//   - mergePending writes _pending/_manifest.json and upserts by typeKey+id
//   - computeRisk flags body-dropped / body-error / body-shrank, [] when safe/no-live
//   - archiveReplaced moves the live file to _replaced/ and returns the path
// ===========================================================================

import { assertEquals, assertExists } from "@std/assert";
import {
  archiveReplaced,
  computeRisk,
  livePath,
  loadPendingManifest,
  mergePending,
  pendingPath,
} from "../../lib/staging.ts";
import type { Article } from "../../lib/track/hashing.ts";

Deno.test("mergePending: writes manifest, upserts by typeKey+id", async () => {
  const out = await Deno.makeTempDir();
  try {
    await mergePending(out, [
      { typeKey: "Policy", id: "K1", op: "edited", source: "dump", stagedAt: "t1" },
      { typeKey: "Policy", id: "K2", op: "edited", source: "dump", stagedAt: "t1" },
    ], "run-1");
    let m = await loadPendingManifest(out);
    assertEquals(m.entries.length, 2);

    // Re-stage K1 (newer) + add K3 -> K1 replaced in place, not duplicated.
    await mergePending(out, [
      { typeKey: "Policy", id: "K1", op: "edited", source: "enrich", stagedAt: "t2" },
      { typeKey: "Policy", id: "K3", op: "edited", source: "dump", stagedAt: "t2" },
    ], "run-2");
    m = await loadPendingManifest(out);
    assertEquals(m.entries.length, 3);
    const k1 = m.entries.find((e) => e.id === "K1")!;
    assertEquals(k1.source, "enrich"); // upserted
    assertEquals(k1.stagedAt, "t2");
  } finally {
    await Deno.remove(out, { recursive: true });
  }
});

Deno.test("computeRisk: flags body regressions", () => {
  const withBody = (t: string): Article => ({ content: { body_text: t } });
  const empty: Article = { content: {} };
  const errored: Article = { content: { bodyError: "404 not found" } };

  // safe: no live counterpart
  assertEquals(computeRisk(null, withBody("x".repeat(100))), []);
  // safe: comparable bodies
  assertEquals(computeRisk(withBody("x".repeat(100)), withBody("y".repeat(100))), []);
  // body dropped: live had a body, pending has none
  assertEquals(computeRisk(withBody("x".repeat(100)), empty), ["body-dropped"]);
  // body error: pending recorded an error (and lost the body)
  const r = computeRisk(withBody("x".repeat(100)), errored);
  assertEquals(r.includes("body-error"), true);
  assertEquals(r.includes("body-dropped"), true);
  // body shrank >50%
  const shrank = computeRisk(withBody("x".repeat(100)), withBody("y".repeat(20)));
  assertEquals(shrank, ["body-shrank-80%"]);
});

Deno.test("archiveReplaced: moves the live file into _replaced/", async () => {
  const out = await Deno.makeTempDir();
  try {
    const lp = livePath(out, "Policy", "K1");
    await Deno.mkdir(lp.slice(0, lp.lastIndexOf("/")), { recursive: true });
    await Deno.writeTextFile(lp, '{"id":"K1"}');

    const dest = await archiveReplaced(out, "Policy", "K1", "2026-06-04T00-00-00-000Z");
    assertExists(dest);
    assertEquals(dest!.includes("/_replaced/Policy/"), true);
    assertEquals(await Deno.readTextFile(dest!), '{"id":"K1"}');
    // live file moved out
    let live = false;
    try {
      Deno.statSync(lp);
      live = true;
    } catch { /* expected */ }
    assertEquals(live, false);

    // no live file -> null, no throw
    assertEquals(await archiveReplaced(out, "Policy", "missing", "x"), null);
    // pendingPath is just a path builder (no IO)
    assertEquals(pendingPath(out, "Policy", "K1"), `${out}/_pending/Policy/K1.json`);
  } finally {
    await Deno.remove(out, { recursive: true });
  }
});

Deno.test("diffParts / changeKind: metadata vs content", async () => {
  const { diffParts, changeKind } = await import("../../lib/staging.ts");
  const A = (meta: Record<string, unknown>, body: string) => ({
    metadata: meta,
    content: { body_text: body },
  });
  // metadata changed only
  assertEquals(await diffParts(A({ v: 1 }, "x"), A({ v: 2 }, "x")), ["metadata"]);
  // content changed only
  assertEquals(await diffParts(A({ v: 1 }, "x"), A({ v: 1 }, "y")), ["content"]);
  // both
  assertEquals(await diffParts(A({ v: 1 }, "x"), A({ v: 2 }, "y")), ["metadata", "content"]);
  // identical -> none
  assertEquals(await diffParts(A({ v: 1 }, "x"), A({ v: 1 }, "x")), []);
  // volatile content keys are ignored (a re-fetch stamp is not a content change)
  assertEquals(
    await diffParts(
      { metadata: { v: 1 }, content: { body_text: "x", fetchedAt: "t1" } },
      { metadata: { v: 1 }, content: { body_text: "x", fetchedAt: "t2" } },
    ),
    [],
  );
  // no live counterpart -> []
  assertEquals(await diffParts(null, A({ v: 1 }, "x")), []);

  assertEquals(changeKind(["metadata", "content"]), "metadata+content");
  assertEquals(changeKind(["metadata"]), "metadata-only");
  assertEquals(changeKind(["content"]), "content-only");
  assertEquals(changeKind([]), "no-op");
});
