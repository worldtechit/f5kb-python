# CLAUDE.md

Project guide for Claude Code working in this repo. Read the pointers below before
making changes.

## What this is

A toolkit that builds and maintains a local, full-fidelity index of **F5 Knowledge
Base articles** (metadata + full body text) for every document type, with **no
login**. F5's support portal (my.f5.com) has no public REST API; the only public
path is the **Coveo guest-token search backend**, accessed via a token fetched at
runtime from a Salesforce Aura endpoint.

## Runtime & dependencies

- **Deno 2.x** (`deno run …` / `deno task …`). Not Node. Code is `.ts`; type-check
  with `deno task check`.
- No package manifest. Deps are declared in `deno.json` imports and fetched by URL:
  `jsr:@b-fuze/deno-dom` (HTML parsing), `jsr:@std/yaml`, `jsr:@std/assert`,
  `jsr:@std/testing`. `node:sqlite` is built into Deno (no install).
- Typical perms: `--allow-net --allow-read --allow-write` (+ `--allow-env` for
  `f5kb enrich` to read `GITHUB_TOKEN`). `deno task` bakes these in per subcommand.

## The CLI

Everything is one entry point, `f5kb.ts`, with subcommands. `f5kb --help` lists
them; `f5kb <sub> --help` shows that subcommand's flags. Subcommands: `dump`,
`enrich`, `track`, `sync`, `reconcile`, `approve`, `status`, `fetch`, `recent`,
`list-types`, `list-products`, `discover`. Global flags:
`--verbose`/`--debug`/`--quiet`/`--json-logs`/`--help`/
`--version`. Logs/progress go to STDERR; any `--json` payload goes to STDOUT.

`deno task` shortcuts (see `deno.json`): `dump`, `enrich`, `track`, `sync`,
`reconcile`, `approve`, `status`, `discover`, `check`, `test`, `test:live`, `fmt`,
`lint`.

## The pipeline (run in order)

```
f5kb dump  →  f5kb enrich  →  f5kb track     (then f5kb status for a health report)
```
1. **`f5kb dump`** (reads `config.yaml`) — one JSON per article under
   `outputs/dump/<Type>/<id>.json`, fields split into `metadata`/`content`. Use
   `--all` (full corpus) or `--days=N`; `--out`, `--types`.
2. **`f5kb enrich`** — fills `content` for the 5 types the Coveo index leaves
   empty (Bug_Tracker, Manual, Release_Note, Supplemental_Document, F5_GitHub).
   `--dump`, `--types`, `--refetch-errors`, `--concurrency`.
3. **`f5kb track`** — SQLite master overview (`outputs/articles.db`): per-article
   dates + metadata/content hashes; new/changed/unchanged/removed across runs.

**Incremental refresh: `f5kb sync`** runs all three steps but only rewrites/
re-enriches articles whose `metadata_hash` changed (skips unchanged), and under
`--all` DETECTS + reports upstream deletions (never removes). **`f5kb reconcile`**
is the only command that deletes on our side: report-only unless `--apply`
(threshold guard + DB backup + soft-delete to `_deleted/`, or `--purge`). Any
mutating op takes `--changelog[=FILE]` to append a JSONL change record (format in
README.md "CHANGELOG FORMAT"); `sync` writes one by default.

**Overwrite protection (the approval gate).** `sync`/`dump`/`enrich` never silently
overwrite a live article that already holds good data: an EDIT to an existing
article is staged to `<dump>/_pending/<type>/<id>.json` (live untouched) and recorded
in `_pending/_manifest.json`; new articles write directly, unchanged are skipped.
**`f5kb approve`** promotes staged edits (archiving each replaced file to
`<dump>/_replaced/`, then reindexing the DB) and HOLDS BACK edits flagged risky (body
would be dropped/errored) unless `--include-risky`; `--list` previews, `--reject`
discards. Pass `--yes` to sync/dump/enrich to bypass the gate (overwrite in place,
still archiving to `_replaced/`). Code: `lib/staging.ts` + `lib/approve.ts`; the
day-to-day workflow is in HOWTO.txt.

Exploratory subcommands (predate the pipeline): `fetch`, `recent`, `list-types`,
`list-products`, `discover`.

## Where the docs live (don't duplicate; update the right one)

- **README.md** — usage: every subcommand, its flags, examples, output layout.
- **FINDINGS.txt** — discoveries about the *scraped system* (Coveo token flow, API
  limits, field meanings, counts, deprecation/lifecycle). Appendix A is the full
  field inventory; the my.f5.com sitemap notes + gap analysis are in its "Sitemap"
  section.
- **OUTLINE.txt** — our *code*: module tree, the dump→enrich→track flow, the
  network-injection design, strategies, decisions, obstacles overcome.
- **HOWTO.txt** — task-oriented USER guide: quick start + the common workflows (full
  build, incremental refresh, reviewing/approving changes, deletions) with examples.
- **TODO.txt** — open work (sitemap-gap follow-up incl. the 47 IDs) + a log of
  shipped work.
- Machine-read config (not docs): `config.yaml` — three sections: `types:`
  (per-type field keep-lists, read by `f5kb dump`), `field_descriptions:`
  (field → description, annotates the catalogue), `products:` (read-only discovered-
  product snapshot; `f5kb discover` writes `discovered_products.yaml` to copy in).

## Documentation file conventions (.txt rules)

Most human docs are plain-text `.txt`. The two Markdown exceptions are `CLAUDE.md`
(the harness expects that name) and `README.md` (the user's entry-point doc —
GitHub renders it). README.md uses normal Markdown (`#`/`##` headings, fenced code,
tables, `**bold**`); keep it that way going forward. The remaining docs
(`FINDINGS.txt`, `OUTLINE.txt`, `HOWTO.txt`, `TODO.txt`) stay `.txt` and follow the
rules below — keep them readable for humans AND unambiguous for LLMs. Apply to every
`.txt` now and going forward:

- **Filename**: `CAPITAL_LETTERS.txt` (UPPER snake-case), e.g. `FINDINGS.txt`.
  `CLAUDE.md` and `README.md` are the Markdown exceptions.
- **Title**: first line is the title; underline it with `=` the same width; then a
  blank line and a 1–2 line purpose statement.
- **Sections (H2)**: `UPPERCASE NAME` underlined with `-` (full width), one blank
  line before. **Subsections (H3)**: `Title Case` underlined with `~`.
- **No Markdown syntax**: no leading `#`, no ``` fences, no `**bold**`/`*italic*`.
  Set off code/commands/examples by indenting 4 spaces between blank lines. Inline
  backticks for literals (field names, files, flags) are allowed (monospace-safe).
- **Tables**: a header row, a dashed separator line, then rows; `|` separators OK.
- **Lists**: `  - item` (2-space indent, hyphen); nest with deeper indent.
- **Wrap** prose to ~88 cols; one blank line between paragraphs; never >1 blank line.
- **Cross-reference** other docs by filename (e.g. "see FINDINGS.txt").
- If a `.txt`'s content is also needed by code, keep the machine-read copy in a
  YAML/JSON config and have the code read that — never make code parse prose.
  (Example: `config.yaml`'s `field_descriptions:` section is the machine copy of
  FINDINGS.txt Appendix A.)

## Conventions & gotchas

- **`outputs/` is gitignored** (large regenerable data: dumps + `articles.db`). Commit
  code (`f5kb.ts`, `cmd/`, `lib/`, `test/`), curated config (`config.yaml`), and
  docs. `.claude/settings.local.json` and `.claude/*.lock` are also ignored.
- **No headless browser.** Every page's body is reachable via plain fetch — JS-rendered
  sites embed it in JSON (`__NEXT_DATA__`) or render it server-side. Don't add Puppeteer.
- **Beating Coveo's 5,000-offset cap:** `--all` uses **keyset pagination by `@rowid`**
  (the only sortable/unique field; `@date` is 1-second-resolution and misses
  null/out-of-window docs). See OUTLINE.txt §4.
- **Live corpus drifts** — counts change between runs; that's expected. The dump
  validates written-vs-server and marks `partial`/`failed` in `_index.json`; re-run
  shortfalls with `--types=`. Enrichment failures land in `_enrich_report.json`; fix
  the host rule / parser then `--refetch-errors`.
- **Trust via classification:** when a body can't be extracted, record a descriptive
  `content.bodyError` — never capture nav/landing/404 text as the body.
- Subcommands are resumable and idempotent; never assume a clean restart is required.
- **Network is dependency-injected.** `CoveoClient` (`lib/coveo/client.ts`) and
  `HttpClient` (`lib/http/fetcher.ts`) each take a `fetch` fn; tests pass a mock
  (`test/_helpers/mock_fetch.ts`), which is why the 125-test suite runs offline.
  Don't reach for the global `fetch` directly in lib code.
- **Incremental skip hinges on one string.** `dbKey(documentType,id)` in
  `lib/dump.ts` must produce the EXACT same `"<document_type> <id>"` key that
  `loadHashIndex` builds from the DB — a separator mismatch makes every lookup miss,
  silently disabling skip-unchanged (every article looks new). Covered by
  `test/integration/sync_cmd_test.ts` (a real DB round-trip, not a self-built map).
- **`listTypeDirs` skips `_`-prefixed dirs.** The gate/reconcile add `_pending/`,
  `_replaced/`, `_deleted/` under the dump; `lib/fsutil.ts listTypeDirs` excludes any
  dir starting with `_` so `track`/`status` never index them as article types (a real
  type dir is a sanitized type key, which never starts with `_`).
- **The gate stages; `approve` applies.** A staged edit is NOT in the DB and NOT
  logged as applied to the changelog until `approve` promotes it. `approve` recomputes,
  fresh from the live-vs-pending files, both the risk flags (holds body-dropped/
  body-error unless `--include-risky`) and the `changed` parts (`metadata` and/or
  `content`). Its changelog is ON by default (like `sync`) — promotions log
  op="edited", source="approve", with `changed` + a metadata-only/metadata+content
  `detail`; `--no-changelog` opts out. (The DB `changes` table also records the
  metadata/content split via `diffFields`.)
- **`config.yaml` is curated — excluded from `deno fmt`.** `deno fmt` reformats YAML;
  `config.yaml` is in the fmt `exclude` so a bare `deno fmt` won't rewrite it. Don't
  reformat it; hand-edit only.

## Git

- `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` on commits.
- Commit only when work is in a coherent state (the user often says when). Use a
  file-based commit message (`git commit -F`) for multi-line bodies so backticks
  aren't mangled by the shell.

## Credentials (no secrets stored)

Coveo org `f5networksproduction5vkhn00h`; guest token fetched at runtime via
`HeadlessController.getHeadlessConfiguration` (no auth needed). Optional
`GITHUB_TOKEN` env raises the GitHub API limit for F5_GitHub enrichment. Full
token/credential details in FINDINGS.txt.
