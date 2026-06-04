// ===========================================================================
// TEST: change-tracking canonicalization / hashing / record mapping.
// CATEGORY: unit
// COVERS: lib/track/hashing.ts (fns: canonical, sha256, contentForHash, hasBody,
//   toRecord) + lib/track/db.ts (fn: diffFields)
// FIXTURES: dump_mini/Knowledge/K14448.json (real article shape as input)
// NETWORK: none (mocked)
// ASSERTS:
//   - sha256 is key-order independent (canonical sort) and deterministic
//   - contentForHash drops bodySource/fetchedAt (re-fetch never looks like change)
//   - hasBody ignores volatile keys + bodyError; true for real body content
//   - diffFields reports metadata/content/updated_published/modified/body_error
// ===========================================================================

import { assertEquals } from "@std/assert";
import {
  type Article,
  canonical,
  contentForHash,
  hasBody,
  type Record_,
  sha256,
  toRecord,
} from "../../lib/track/hashing.ts";
import { diffFields } from "../../lib/track/db.ts";
import { loadJsonFixture } from "../_helpers/fixtures.ts";

Deno.test("canonical: recursively sorts object keys", () => {
  const c = canonical({ b: 1, a: { d: 4, c: 3 } }) as Record<string, unknown>;
  assertEquals(JSON.stringify(c), JSON.stringify({ a: { c: 3, d: 4 }, b: 1 }));
});

Deno.test("sha256: stable across key order, deterministic", async () => {
  const h1 = await sha256({ a: 1, b: [1, 2], c: { x: 1, y: 2 } });
  const h2 = await sha256({ c: { y: 2, x: 1 }, b: [1, 2], a: 1 });
  assertEquals(h1, h2);
  assertEquals(h1, await sha256({ a: 1, b: [1, 2], c: { x: 1, y: 2 } }));
  assertEquals(h1.length, 64); // hex sha-256
  // Array order is significant.
  const ha = await sha256([1, 2]);
  const hb = await sha256([2, 1]);
  assertEquals(ha === hb, false);
});

Deno.test("contentForHash: excludes bodySource + fetchedAt", () => {
  const out = contentForHash({
    body_text: "hi",
    bodySource: "http://x",
    fetchedAt: "2026-01-01",
  });
  assertEquals(out, { body_text: "hi" });
});

Deno.test("hasBody: ignores volatile keys + bodyError", () => {
  assertEquals(hasBody({ bodySource: "x", fetchedAt: "t" }), false);
  assertEquals(hasBody({ bodyError: "404" }), false);
  assertEquals(hasBody({ body_text: "   " }), false); // whitespace-only
  assertEquals(hasBody({ body_text: "real content" }), true);
  assertEquals(hasBody({ sfdetails__c: "x" }), true);
  assertEquals(hasBody(undefined), false);
});

Deno.test("toRecord: maps a real dump article to a tracked record", async () => {
  const a = loadJsonFixture<Article>("dump_mini/Knowledge/K14448.json");
  const rec = await toRecord(a);
  assertEquals(rec.id, "K14448");
  assertEquals(rec.document_type, "Knowledge");
  assertEquals(rec.has_body, 1); // sfdetails__c body present
  assertEquals(rec.body_error, null);
  assertEquals(typeof rec.metadata_hash, "string");
  assertEquals(rec.metadata_hash.length, 64);
  assertEquals(rec.updated_published_ms, 1677008488000);
});

function baseRec(): Record_ {
  return {
    id: "K1",
    document_type: "Knowledge",
    title: "t",
    link: "l",
    created_ms: null,
    original_published_ms: null,
    updated_published_ms: 100,
    modified_ms: 200,
    captured_at: "t",
    metadata_hash: "M",
    content_hash: "C",
    has_body: 1,
    body_error: null,
  };
}

Deno.test("diffFields: categorizes each changed dimension", () => {
  const prev = baseRec();
  assertEquals(diffFields(prev, baseRec()), []); // identical
  assertEquals(diffFields(prev, { ...baseRec(), metadata_hash: "M2" }), ["metadata"]);
  assertEquals(diffFields(prev, { ...baseRec(), content_hash: "C2" }), ["content"]);
  assertEquals(
    diffFields(prev, { ...baseRec(), updated_published_ms: 999 }),
    ["updated_published"],
  );
  assertEquals(diffFields(prev, { ...baseRec(), modified_ms: 999 }), ["modified"]);
  assertEquals(diffFields(prev, { ...baseRec(), body_error: "404" }), ["body_error"]);
});
