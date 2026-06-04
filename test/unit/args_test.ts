// ===========================================================================
// TEST: CLI argument parsing + typed flag accessors + log-level mapping.
// CATEGORY: unit
// COVERS: lib/args.ts (fns: parseFlags, flagNum, flagBool, flagList, logLevelFromFlags)
// FIXTURES: none
// NETWORK: none (mocked)
// ASSERTS:
//   - parseFlags: --k=v, bare --flag (=true), and positionals (order preserved)
//   - flagNum: numeric coercion, default fallback, bare-flag -> default
//   - flagBool: presence true; "--k=false" -> false
//   - flagList: comma split + trim + drop-empties; null when absent
//   - logLevelFromFlags: quiet/verbose/debug precedence + json-logs
// ===========================================================================

import { assertEquals } from "@std/assert";
import { flagBool, flagList, flagNum, logLevelFromFlags, parseFlags } from "../../lib/args.ts";

Deno.test("parseFlags: --k=v, bare --flag, positionals", () => {
  const { positionals, flags } = parseFlags([
    "dump",
    "--days=30",
    "--all",
    "sub",
    "--out=/tmp/x",
  ]);
  assertEquals(positionals, ["dump", "sub"]);
  assertEquals(flags.days, "30");
  assertEquals(flags.all, true);
  assertEquals(flags.out, "/tmp/x");
});

Deno.test("flagNum: coercion, default, and bare flag", () => {
  const { flags } = parseFlags(["--n=42", "--bad=abc", "--bare"]);
  assertEquals(flagNum(flags, "n"), 42);
  assertEquals(flagNum(flags, "bad", 7), 7); // non-numeric -> default
  assertEquals(flagNum(flags, "bare", 5), 5); // bare flag (true) -> default
  assertEquals(flagNum(flags, "missing", 9), 9);
});

Deno.test("flagBool: presence + explicit false", () => {
  const { flags } = parseFlags(["--on", "--off=false", "--str=hi"]);
  assertEquals(flagBool(flags, "on"), true);
  assertEquals(flagBool(flags, "off"), false);
  assertEquals(flagBool(flags, "str"), true);
  assertEquals(flagBool(flags, "missing"), false);
});

Deno.test("flagList: split/trim/drop-empties; null when absent or bare", () => {
  const { flags } = parseFlags(["--types=Manual, Release_Note ,,Knowledge", "--bare"]);
  assertEquals(flagList(flags, "types"), ["Manual", "Release_Note", "Knowledge"]);
  assertEquals(flagList(flags, "missing"), null);
  assertEquals(flagList(flags, "bare"), null); // bare flag is boolean true, not a list
});

Deno.test("logLevelFromFlags: precedence + json-logs", () => {
  assertEquals(logLevelFromFlags(parseFlags([]).flags), { level: "info", json: false });
  assertEquals(logLevelFromFlags(parseFlags(["--quiet"]).flags).level, "warn");
  assertEquals(logLevelFromFlags(parseFlags(["--verbose"]).flags).level, "debug");
  assertEquals(logLevelFromFlags(parseFlags(["--debug"]).flags).level, "trace");
  // debug overrides verbose/quiet (applied last).
  assertEquals(
    logLevelFromFlags(parseFlags(["--quiet", "--verbose", "--debug"]).flags).level,
    "trace",
  );
  assertEquals(logLevelFromFlags(parseFlags(["--json-logs"]).flags).json, true);
});
