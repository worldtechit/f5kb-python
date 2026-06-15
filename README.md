# F5 KB Article Index Toolkit — Python Edition

Usage guide for `f5kb`, a single-command CLI that builds and maintains a local,
full-fidelity index of F5 Knowledge Base articles (metadata + full body text) for
every document type, with no login.

Everything is one entry point with subcommands. There is no public REST API for
my.f5.com; everything runs over the Coveo guest-token search backend (a token is
fetched at runtime — no key, no login required).

## Requirements

- Python 3.11+ (<https://python.org>)
- [uv](https://docs.astral.sh/uv/) (recommended) or `pip` for dependency management
- Internet access to my.f5.com and f5networksproduction5vkhn00h.org.coveo.com
- No login or API key required — a guest token is fetched automatically and refreshed
  if it expires mid-run
- Optional `GITHUB_TOKEN` env raises the GitHub API limit (60 → 5,000 req/hr) for
  `f5kb enrich` on F5 GitHub articles

The Python package lives in the `python/` subdirectory.

## Installation

```
cd python
uv sync          # creates .venv and installs all deps (recommended)
# or:
pip install -e .
```

After `uv sync`, commands run with `uv run f5kb <sub>` from inside `python/`.
After `pip install -e .`, run `f5kb <sub>` directly.

All examples below use `uv run f5kb`. Substitute `f5kb` if installed via pip.

## Quick start — the pipeline

The production flow is three subcommands in order: dump (metadata + indexed body) →
enrich (fill bodies the index omits) → track (master overview + change tracking). Each
reads the dump directory, so enrich and track can re-run anytime without re-hitting the
search API.

```
cd python
uv run f5kb dump --all --out=outputs/dump
uv run f5kb enrich --dump=outputs/dump
uv run f5kb track --dump=outputs/dump
uv run f5kb status
```

Generated data lives under `outputs/` (e.g. `outputs/dump/` and the SQLite
`outputs/articles.db`) and is git-ignored. Pass `--out` / `--dump` / `--db` to
override paths.

After the first build, refresh incrementally with `f5kb sync` (see below). Run
`f5kb --help` for the subcommand list and global flags; `f5kb <sub> --help` prints
that subcommand's flag synopsis.

## Running tests

```
cd python
uv run pytest tests/ -q           # all 275 offline tests
uv run pytest tests/unit/ -q      # unit tests only
uv run pytest tests/integration/  # CLI smoke tests
```

No network required — all tests use injected transports and local fixtures.

## Selecting types (`--types` / `--exclude-types`)

Every type-aware subcommand (`dump`, `enrich`, `track`, `sync`, `reconcile`, `approve`,
`recent`) accepts two complementary filters:

- `--types=A,B` — INCLUDE only these types (a subset of the configured/known types).
- `--exclude-types=A,B` — EXCLUDE these types from the working set.

They compose: include is applied first, then exclude removes from the result (so if a
type appears in both, exclude wins). With neither flag, all configured types are used.
Unknown keys are warned about and ignored.

```
# everything except the two big/low-value types (the usual production selection)
uv run f5kb sync --all --exclude-types=Community,F5_GitHub

# just two types
uv run f5kb dump --all --out=outputs/dump --types=Manual,Release_Note

# the enrichable types except F5 GitHub
uv run f5kb enrich --dump=outputs/dump --exclude-types=F5_GitHub
```

## Global flags

These apply to every subcommand:

- `--verbose` — debug-level logging
- `--debug` — trace-level logging
- `--quiet` — warn-level logging only
- `--json-logs` — emit logs as NDJSON (one JSON object per line)
- `--help`, `-h` — show usage (bare) or the subcommand's flag synopsis
- `--version` — print the version and exit

Output discipline: human-readable progress and logs go to STDERR; machine output (any
`--json` payload) goes to STDOUT, so `f5kb track --json > out.json` captures only the JSON.

## Overwrite protection (the approval gate)

`sync`, `dump`, and `enrich` can REWRITE an article that already holds good data. If an
upstream change makes the new version worse (a reformatted page that now extracts to an
empty body, say), an unguarded rewrite would silently replace good data with bad. To
prevent that, those three commands run an approval gate BY DEFAULT:

- a NEW article (no existing file) is written straight to the live dump;
- an UNCHANGED article is left exactly as-is (not rewritten);
- an EDIT that would OVERWRITE an existing file is written to
  `<dump>/_pending/<type>/<id>.json` instead — the live file is untouched — and recorded
  in `<dump>/_pending/_manifest.json`.

You then review the staged files and run `f5kb approve` to apply them. On approval each
replaced live file is first archived to `<dump>/_replaced/<type>/<id>.<timestamp>.json`
(recoverable), then the pending version is moved into place and the tracking DB is
updated. `approve` HOLDS BACK any edit flagged risky (body would be dropped or errored)
unless you pass `--include-risky`.

Bypass: pass `--yes` to sync/dump/enrich to skip staging and overwrite in place (each
replaced file is still archived to `_replaced/` first). Use for unattended runs where
you trust the source.

Layout the gate adds under the dump dir (all gitignored with the rest of `outputs/`):

```
_pending/_manifest.json          index of staged edits (what + why)
_pending/<type>/<id>.json        the staged new version (review vs live)
_replaced/<type>/<id>.<ts>.json  archived previous version after an approval
```

## Subcommand — dump

Full-fidelity dumper. Writes ONE JSON file per article, grouped by document type,
splitting each article's fields into `metadata` vs `content` objects per config.yaml.
Also emits a per-type field catalogue. Protected by the approval gate: new articles are
written, unchanged ones are skipped, and edits to existing articles are staged to
`_pending/` for `f5kb approve` rather than overwriting in place. Pass `--yes` to
overwrite in place.

Flags:

- `--days=N` — Only dump articles modified in the last N days.
- `--all` — Dump the entire corpus (no lower date bound). Provide exactly one of `--days`
  or `--all`. With `--all`, the written count is validated against the server count per
  type and shortfalls are flagged.
- `--out=DIR` — REQUIRED. Output directory (created if missing).
- `--config=FILE` — Config YAML (default: config.yaml).
- `--types="A,B"` — Subset of config type keys (default: all in config).
- `--exclude-types="A,B"` — Exclude these config type keys (applied after `--types`).
- `--page-size=N` — Results per API call (default: 200, max: 500).
- `--limit=N` — Cap articles per type (default: 0 = none). For testing.
- `--db=FILE` — Tracking DB consulted to classify changed vs unchanged (default:
  `<out>/../articles.db`).
- `--changelog[=FILE]` — Record each written article as added/edited to a JSONL
  changelog (default `<out>/_changelog.jsonl`). See **Changelog format**.
- `--yes` — Bypass the approval gate: overwrite edited articles in place (after archiving
  to `_replaced/`) instead of staging to `_pending/`.

Example — dump the entire corpus:

```
uv run f5kb dump --all --out=outputs/dump
```

Example — last 7 days, one type only:

```
uv run f5kb dump --days=7 --out=outputs/dump --types=Support_Solution
```

Output layout:

```
dump/
  _index.json                  manifest (window, counts, per-type status)
  Support_Solution/
    _catalogue.json            every field seen + source/type/coverage/sample
    _catalogue.md              same, as a readable table
    K000161535.json            one file per article (named by KB id)
    ...
```

Each per-article file:

```json
{
  "id": "K000161535",
  "documentType": "Support Solution",
  "title": "K000161535: ...",
  "link": "https://my.f5.com/manage/s/article/K000161535",
  "modifiedMs": 1780430428000,
  "modified": "2026-06-02T20:00:28.000Z",
  "capturedAt": "2026-06-02T...Z",
  "metadata": { "...selected fields...": "..." },
  "content":  { "sfdetails__c": "<full HTML body>" }
}
```

## Subcommand — enrich

Post-processes a dump directory to fill in article BODIES for the five types whose body
is absent from the Coveo search index: Bug_Tracker, F5_GitHub, Manual, Release_Note,
Supplemental_Document. Fetches each article's public page and extracts ONLY the body,
writing `content.sections` + `content.body_text` (plus `content.bodySource` and
`content.fetchedAt`) back into the per-article JSON.

Flags:

- `--dump=DIR` — Dump directory to enrich (default: outputs/dump).
- `--types="A,B"` — Subset of the enrichable types (default: all five).
- `--exclude-types="A,B"` — Exclude these types (e.g. `--exclude-types=F5_GitHub`).
- `--concurrency=N` — Parallel fetches (default: 4).
- `--delay-ms=N` — Per-worker delay between fetches in ms (default: 200).
- `--limit=N` — Cap articles per type (testing).
- `--refetch` — Re-fetch even articles that already have a body or bodyError.
- `--refetch-errors` — Re-process only articles that recorded a `content.bodyError`.
  Use after mapping a new host or fixing a parser.
- `--changelog[=FILE]` — Record body-added / body-changed / body-error events to a
  JSONL changelog (default `<dump>/_changelog.jsonl`).
- `--yes` — Bypass the approval gate: a `--refetch` that would overwrite an existing
  body overwrites in place instead of staging to `_pending/`.

Env: `GITHUB_TOKEN` raises the GitHub API rate limit from 60 to 5,000 req/hr for
F5_GitHub enrichment.

Example:

```
uv run f5kb enrich --dump=outputs/dump --types=Bug_Tracker,Manual
```

How bodies are recovered, by type:

- **Bug_Tracker**: cdn.f5.com page; two templates handled (standard + security/CVE).
- **F5_GitHub**: GitHub REST API (issues, pulls, repo-root README, `/blob/` raw files).
- **Manual / Release_Note / Supplemental_Document**: doc-page scrape driven by a
  host→selector map (clouddocs.f5.com, techdocs.f5.com, docs.nginx.com, nginx.org,
  unit.nginx.org) with a generic fallback. docs.cloud.f5.com (Next.js) reads its body
  from the embedded `<script id="__NEXT_DATA__">` JSON — no headless browser needed.

Edge cases are recorded as `content.bodyError` (never captured as a body). Resumable:
an article is skipped if it already has `body_text` or a `bodyError`. Each run writes
`outputs/dump/_enrich_report.json` (per-type enriched/failed/skipped). If a
`<dump>/_pending/` tree exists, enrich also fills the bodies of staged articles so a
reviewer sees the complete new version before approving.

## Subcommand — track

Maintains a master overview of every dumped article in an embedded SQLite DB (default
`outputs/articles.db`): one row per article with its dates and a hash of the metadata
and content/body. On each run it classifies articles new/changed/unchanged/removed vs
the prior run and logs every change. Run after enrich so bodies are included in the
content hash.

Flags:

- `--dump=DIR` — Dump directory to index (default: outputs/dump).
- `--db=FILE` — SQLite file (default: `<dump>/../articles.db`).
- `--types="A,B"` — Subset of document types (scopes "removed" detection too).
- `--exclude-types="A,B"` — Exclude these types (applied after `--types`).
- `--run-id=ID` — Label for this run (default: a timestamp).
- `--changelog[=FILE]` — Record new→added / changed→edited to a JSONL changelog
  (default `<dump>/_changelog.jsonl`).
- `--json` — Emit the run summary as JSON on STDOUT.

Example:

```
uv run f5kb track --dump=outputs/dump
```

Per-article DB row: id, document_type, title, link; the dates created_ms /
original_published_ms / updated_published_ms / modified_ms / captured_at;
metadata_hash and content_hash (SHA-256 over canonicalized JSON, with volatile keys
bodySource/fetchedAt excluded so a re-fetch isn't a "change"); has_body; body_error;
first_seen_run / last_seen_run / last_changed_run. Changes are logged to a `changes`
table and a per-run summary to a `runs` table. Removed articles are logged, not deleted.

## Subcommand — sync

Incremental update of a dump + its tracking DB. Instead of re-dumping and re-enriching
everything in the window, sync runs the dump/enrich/track pipeline but only TOUCHES what
actually changed:

1. Loads the prior metadata_hash of every article from the tracking DB.
2. Dumps the window, but SKIPS rewriting any article whose metadata_hash is unchanged.
   An article that CHANGED is staged to `_pending/` for review; a brand-new article is
   written directly.
3. Enriches new/staged files (already-bodied files are skipped), including staged ones
   so a reviewer sees the complete new article before approving.
4. Updates the tracking DB from the LIVE dump (staged edits excluded until approved).
5. Under `--all` only, DETECTS upstream deletions (DB ids no longer present in the live
   Coveo id set) and REPORTS them. Sync NEVER removes anything; use `reconcile --apply`
   to act on detected deletions.

Flags:

- `--all` — Full corpus. REQUIRED for deletion detection.
- `--days=N` — Only articles modified in the last N days (no deletion detection).
- `--since-last-run` — Window starting at the tracking DB's most recent run time
  (falls back to `--days=7` if there is no prior run).
- `--types="A,B"` — Subset of config type keys.
- `--exclude-types="A,B"` — Exclude these config type keys, e.g.
  `--exclude-types=Community,F5_GitHub`.
- `--out=DIR` — Dump directory (default: outputs/dump).
- `--config=FILE` — Config YAML (default: config.yaml).
- `--db=FILE` — SQLite file (default: `<out>/../articles.db`).
- `--no-enrich` — Skip the body-enrichment step.
- `--changelog[=FILE]` — Changelog path (default `<out>/_changelog.jsonl`). ON by
  default for sync.
- `--no-changelog` — Disable the changelog.
- `--dry-run` — Classify + report only: write no files, DB rows, or changelog.
- `--yes` — Bypass the approval gate: overwrite edited articles in place (after
  archiving each to `_replaced/`) instead of staging to `_pending/`.
- `--page-size=N` — Results per call (default: 200, max: 500).
- `--limit=N` — Cap articles per type (testing).
- `--concurrency=N` — Enrich parallelism (default: 4).
- `--delay-ms=N` — Enrich min delay per worker in ms (default: 200).

Example — nightly incremental over everything except two low-value types:

```
uv run f5kb sync --all --exclude-types=Community,F5_GitHub
```

Example — quick catch-up since the last run, no deletion scan:

```
uv run f5kb sync --since-last-run
```

## Subcommand — reconcile

Removes articles that exist in our dump/DB but no longer exist upstream in Coveo. This
is the execution counterpart to sync's detect-and-report: reconcile is the only command
that deletes on our side, and is report-only unless you pass `--apply`. Detection is a
cheap IDs-only keyset sweep of each type's current Coveo id set, diffed against the DB.

Safety when `--apply` is given:

- a deletion-threshold guard aborts if a type's deletions exceed `--max-delete-pct` of
  its DB rows, or total deletions exceed `--max-deletes`;
- the tracking DB is copied to `<db>.bak-<timestamp>` before any change;
- soft-delete by default: the article file is MOVED to `<dump>/_deleted/<type>/`
  (recoverable) and its DB row dropped;
- `--purge` hard-removes the file instead of archiving.

Flags:

- `--types="A,B"` — Subset of config type keys (default: all in config).
- `--exclude-types="A,B"` — Exclude these config type keys (applied after `--types`).
- `--dump=DIR` — Dump directory (default: outputs/dump).
- `--config=FILE` — Config YAML (default: config.yaml).
- `--db=FILE` — SQLite file (default: `<dump>/../articles.db`).
- `--apply` — Actually remove. Without it, reconcile only reports.
- `--purge` — Hard-remove files instead of archiving to `_deleted/`.
- `--max-delete-pct=N` — Abort if a type's deletions exceed N% of its DB rows
  (default: 10).
- `--max-deletes=N` — Abort if total deletions exceed N (optional absolute cap).
- `--changelog[=FILE]` — Record deletions to a JSONL changelog (default
  `<dump>/_changelog.jsonl`).
- `--page-size=N` — IDs-only sweep page size (default: 2000).
- `--json` — Emit the result as JSON on STDOUT.

Example — see what would be removed (safe, no changes):

```
uv run f5kb reconcile 2>&1 | tail
```

Example — apply, archiving removed articles, with a changelog:

```
uv run f5kb reconcile --apply --changelog
```

## Subcommand — approve

The human checkpoint for the approval gate. A gated sync/dump/enrich stages
would-overwrite edits under `<dump>/_pending/`; `approve` applies them to the live dump.
For each promoted edit the replaced live file is archived to
`<dump>/_replaced/<type>/<id>.<timestamp>.json`, the pending version is moved into place,
and the tracking DB is reindexed.

Safety default: an edit flagged risky (body-dropped or body-error) is HELD BACK and
reported; pass `--include-risky` to apply those too. Both the risk flags AND the changed
parts (metadata / content) are recomputed fresh from the actual files at approve time.
Each promoted edit is logged to the changelog with `changed` = the parts that differ
(`["metadata"]`, `["content"]`, or both).

The changelog is ON by default for approve — it writes to `<dump>/_changelog.jsonl`.
Use `--no-changelog` to disable.

Flags:

- `--dump=DIR` — Dump directory (default: outputs/dump).
- `--db=FILE` — SQLite file (default: `<dump>/../articles.db`).
- `--types="A,B"` — Only act on these type dirs.
- `--exclude-types="A,B"` — Exclude these type dirs (applied after `--types`).
- `--ids="K1,K2"` — Only act on these article ids.
- `--list` — Preview: show each pending edit + its change kind + risk flags, change
  nothing.
- `--reject` — Discard the staged files instead of promoting them.
- `--include-risky` — Also promote edits flagged risky (default: hold them back).
- `--no-archive` — Don't keep a `_replaced/` copy of overwritten files.
- `--changelog[=FILE]` — Changelog path (default `<dump>/_changelog.jsonl`).
- `--no-changelog` — Disable the changelog.
- `--json` — Print the result as JSON on STDOUT.

Example — review what is staged, then apply the safe ones:

```
uv run f5kb approve --list
uv run f5kb approve
```

Example — a body-dropped edit you have verified is correct anyway:

```
uv run f5kb approve --include-risky --ids=K12345
```

Example — throw away the staged changes (keep the live versions):

```
uv run f5kb approve --reject
```

## Changelog format

Mutating subcommands (sync, dump, enrich, track, reconcile, approve) can append a
structured changelog via `--changelog[=FILE]`. It is JSONL: one JSON object per line,
append-only (default file: `<dump>/_changelog.jsonl`). sync writes it by default; the
others opt in.

Each line:

| Key            | Type   | Meaning                                                              |
| -------------- | ------ | -------------------------------------------------------------------- |
| `runId`        | string | ISO timestamp shared with the runs/changes tables                    |
| `ts`           | string | ISO timestamp when the record was written                            |
| `op`           | string | `added` \| `edited` \| `deleted` \| `body-added` \| `body-changed` \| `body-error` |
| `documentType` | string | e.g. "Bug Tracker"                                                   |
| `id`           | string | article id (matches the per-article filename)                        |
| `title`        | string | (optional) article title                                             |
| `changed`      | array  | (optional) `["metadata"]`, `["content"]`, or both                   |
| `hashOld`      | string | (optional) prior metadata_hash                                       |
| `hashNew`      | string | (optional) new metadata_hash                                         |
| `source`       | string | (optional) dump \| enrich \| track \| reconcile \| sync \| approve  |
| `detail`       | string | (optional) free text                                                 |

Example lines:

```json
{"runId":"2026-06-04T00:00:00.000Z","ts":"2026-06-04T00:00:01.2Z","op":"added","documentType":"Policy","id":"K12345","source":"dump"}
{"runId":"2026-06-04T16:13:32.685Z","ts":"2026-06-04T16:13:40.1Z","op":"edited","documentType":"Manual","id":"K321","changed":["metadata","content"],"source":"approve","detail":"metadata+content; replaced file archived"}
{"runId":"2026-06-04T00:00:00.000Z","ts":"2026-06-04T00:00:09.8Z","op":"deleted","documentType":"Manual","id":"K98","source":"reconcile","detail":"archived to _deleted/Manual/"}
```

## Subcommand — status

Read-only health report for a dump and its tracking DB. Aggregates: `<dump>/_index.json`
(per-type expected/written/status), `<dump>/_enrich_report.json` (per-type
enriched/failed/skipped), on-disk per-type file counts, and the tracking DB. If a
`<dump>/_changelog.jsonl` exists it is surfaced too, with the most recent run's per-op
tally. Never writes.

Flags:

- `--dump=DIR` — Dump directory (default: outputs/dump).
- `--db=FILE` — SQLite file (default: `<dump>/../articles.db`).
- `--json` — Emit the report as JSON on STDOUT instead of a table.

Example:

```
uv run f5kb status
```

## Subcommand — fetch

Lighter-weight exploratory fetch. Fetches articles by product and/or type into a flat
JSON array, optionally also a CSV. Each article: name, link, summary, publicationDate,
modificationDate.

Flags:

- `--product=NAME` — Filter by product (e.g. "BIG-IP", "NGINX Plus", "F5OS").
- `--type=NAME` — Filter by document type (e.g. "Support Solution").
- `--limit=N` — Stop after N articles (default: all).
- `--output=FILE` — JSON output (default: auto-named, e.g. f5_NGINX_Plus_...json).
- `--csv=FILE` — Also write a CSV file (optional).
- `--page-size=N` — Results per call (default: 100, max: 1000).

Example — NGINX Plus Security Advisories, JSON + CSV:

```
uv run f5kb fetch \
    --product="NGINX Plus" --type="Security Advisory" \
    --csv=nginx_security.csv
```

## Subcommand — recent

Fetches articles modified within the last N days and writes one JSON file per document
type into a chosen directory, plus an `_index.json` manifest. Exploratory; prefer
`dump --days=N` for the full-fidelity pipeline.

Flags:

- `--days=N` — REQUIRED. Window size: articles modified in the last N days.
- `--out=DIR` — REQUIRED. Output directory (created if missing).
- `--types="A,B"` — Subset of document types (default: all).
- `--exclude-types="A,B"` — Exclude these document types (applied after `--types`).
- `--page-size=N` — Results per call (default: 500, max: 1000).
- `--limit=N` — Cap articles per type (testing).

Example:

```
uv run f5kb recent --days=7 --out=last_week
```

## Subcommand — list-types

Prints all document types with their article counts (from the global facet).

```
uv run f5kb list-types
uv run f5kb list-types --json
```

## Subcommand — list-products

Prints the products known to the global Coveo facet (~73) with counts. The global facet
is incomplete — many valid products are hidden from it; use `f5kb discover` for the full
set.

```
uv run f5kb list-products
uv run f5kb list-products --json
```

## Subcommand — discover

Deep product discovery. The Coveo global facet returns only ~73 top-level products; ~247
more valid product names are hidden but remain queryable. This subcommand surfaces them
by running a type-filtered facet query per document type, then a count query for each
hidden product. Takes ~3–4 minutes (~250 API calls).

Flags:

- `--out=FILE` — Output file (default: discovered_products.yaml).
- `--format=yaml|json` — Output format (default: yaml).

```
uv run f5kb discover
uv run f5kb discover --format=json --out=products.json
```

## config.yaml

`config.yaml` is the single source of truth, with three sections:

- **`types:`** — one entry per document type, read by `f5kb dump`. Each entry:
  - `documentType` — Exact Coveo f5_document_type value (what the API filters on).
  - `metadata` — Fields routed to the entry's `metadata` object. Accepts `"*"` (all
    fields) or a `[a, b, c]` keep-list.
  - `content` — Fields routed to the entry's `content` object. Same syntax. Manual /
    Release Note / Supplemental Document / F5 GitHub / Bug Tracker use `content: []`
    because the index returns no body — those are filled by `f5kb enrich`.

- **`field_descriptions:`** — field-name → short description, used to annotate the dump
  catalogue.

- **`products:`** — a read-only snapshot of discovered products. The pipeline does NOT
  read this section; it is a reference for valid `--product` values. Refresh it via
  `f5kb discover` (writes discovered_products.yaml; copy its `products:` block in).

## Coveo API limits (handled automatically)

1. `firstResult + numberOfResults` cannot exceed 5,000. When a result set is larger,
   `dump --all` uses keyset pagination by @rowid; `dump --days`, `fetch`, and `recent`
   use recursive date-range chunking.
2. A single response cannot exceed 20 MB. The dump halves the page size for any request
   that exceeds it and retries automatically.

Other notes:

- The guest token is valid ~24h and is auto-refreshed mid-run on 401/419.
- The live corpus drifts between runs; counts changing is expected.
- Field availability varies by document type. See `FINDINGS.txt` for the full field
  inventory.

## Documentation map

- **README.md** — this file: CLI usage guide (subcommands, flags, examples, outputs,
  config, API limits).
- **python/** — the Python package (source, tests, pyproject.toml).
- **FINDINGS.txt** — discoveries about the scraped system (Coveo token flow, API limits,
  field meanings, counts). Appendix A is the full field inventory.
- **OUTLINE.txt** — the code: module tree, the dump→enrich→track flow, the
  dependency-injection design, pagination strategy, and decisions.
- **MEMORIES.md** — durable project memory & handoff: current state, credentials/token
  flow, gotchas, and data layout.
- **config.yaml** — machine config the CLI reads (`types:` + `field_descriptions:` +
  `products:`).
