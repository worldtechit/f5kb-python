# Outline — Code Layout, Flows & Decisions

How f5kb is built: the module tree, the dump→enrich→track flow through the
modules, the design that makes offline testing possible, and the strategies (and
war stories) for getting complete/correct data out of a hostile API.

This file is about our code and approach. For discoveries about the thing we scrape
(my.f5.com / Coveo internals, field meanings, counts), see FINDINGS.md. For usage
(subcommands, flags, examples), see README.md.

## 1. THE BIG PICTURE

The goal: build and maintain a local, full-fidelity index of F5 Knowledge Base
articles — metadata and full body text — for every document type, with no login,
and track changes over time.

There is no public REST API. The only public path is the Coveo guest-token search
backend that powers my.f5.com (see FINDINGS.md → Token / Credential Discovery).
Everything flows from a token fetched at runtime from a Salesforce Aura endpoint.

The whole toolkit is one Click CLI (`python -m f5kb` / `uv run f5kb`), dispatching
to thin per-subcommand wrappers in `f5kb/cmd/`; all heavy logic lives in `f5kb/lib/`
and related subdirectories. The production pipeline is three subcommands in sequence:

```
f5kb dump    →   f5kb enrich    →   f5kb track
(metadata +       (fill bodies the     (master overview +
 indexed body,     index doesn't have)  change tracking)
 per article)            |                     |
          |      outputs/dump/<Type>/<id>.json  outputs/articles.db
```

`f5kb status` is a read-only health report over a dump + its DB. The remaining
subcommands (fetch, recent, list-types, list-products, discover) are lighter-weight
/ exploratory tools that predate the pipeline; they share the same token + Coveo
plumbing in `f5kb/coveo/`.

## 2. MODULE TREE

```
f5kb/
  __main__.py             entry: Click group + 12 subcommand registrations
  cmd/                    one thin wrapper per subcommand (parse flags, call lib):
    dump.py enrich.py track.py sync.py reconcile.py approve.py status.py
    fetch.py recent.py list_types.py list_products.py discover.py
  coveo/
    aura.py               fetch the guest token from the Aura endpoint
                          (double-parse JSON-in-JSON); CoveoConfig + refresh_config.
    client.py             CoveoClient: post() (retry/backoff/refresh/timeout),
                          get_count(), list_facet_values(). Takes an injected
                          httpx.Client.
    paging.py             fetch_paged (offset), fetch_keyset (@rowid cursor),
                          fetch_chunked (date-window split), fetch_ids (cheap
                          IDs-only keyset sweep for deletion reconcile). The §4
                          strategy.
    dates.py              @date ↔ ms helpers (to_coveo_date, the 1s resolution).
    fields.py             split a Coveo result into metadata/content per config;
                          build the per-type field catalogue.
    flat.py               the 5-field flat shape, build_aq, fetch_flat_paged /
                          fetch_flat_chunked, to_csv (for fetch/recent).
  http/
    fetcher.py            HttpClient: plain-page GET with retry/backoff/timeout
                          and a descriptive User-Agent. Takes an injected httpx.Client.
    github.py             GitHub REST helpers (issue/pull/README/raw file).
  html/
    serialize.py          DOM subtree → markdown (headings/lists/code/links).
    bugtracker.py         Bug Tracker page → labelled sections (2 templates).
    docpage.py            doc-site host→selector map + fallbacks; soft-404 and
                          landing-redirect detection.
    nextdata.py           read a body from embedded __NEXT_DATA__ JSON.
  enrich/
    enrichers.py          per-type enricher registry (Bug Tracker, F5 GitHub,
                          Manual/Release Note/Supplemental via docpage).
    driver.py             walk the dump, resumability gate, dispatch, write
                          content + _enrich_report.json.
  track/
    hashing.py            canonicalize + SHA-256 metadata/content hashes
                          (volatile keys excluded). Canonical has_body().
    db.py                 sqlite3 schema + upsert + classify + runs/changes;
                          load_hash_index (prior metadata_hash map for incremental
                          skip), load_ids_by_type + delete_rows (reconcile),
                          load_last_run_at (--since-last-run).
  config/
    loader.py             load + normalize config.yaml; refresh_config helper.
    types.py              TypeConfig / ProductEntry / config shapes.
  lib/
    dump.py               dump orchestration (count → page → split → write
                          per-article + catalogue + _index.json). Optional
                          incremental mode: skip rewriting unchanged articles
                          (prior metadata_hash match) + classify added/edited.
    changelog.py          Changelog: append-only JSONL recorder of every change
                          a mutating op makes (added/edited/deleted/body-*);
                          no-op when no path.
    sync.py               incremental orchestrator: load_hash_index → dump_types
                          (incremental) → enrich_dump (changed-only) → track_dump
                          → detect + report upstream deletions. Never removes.
    reconcile.py          deletion executor: fetch_ids vs DB diff; report-only
                          unless apply; threshold guard + DB backup + soft-delete
                          (archive to _deleted/) or --purge.
    staging.py            overwrite-protection primitive: _pending/_replaced paths,
                          the pending manifest (load/merge/save), compute_risk
                          (body-dropped/body-error/body-shrank), archive_replaced.
    approve.py            promote/reject staged overwrites: archive replaced live
                          file → _replaced/, move _pending → live; HOLD risky
                          edits unless include_risky; records op="edited".
    status.py             aggregate _index.json + _enrich_report.json + on-disk
                          counts + DB + _changelog.jsonl into a StatusReport.
    logger.py             levelled logger → STDERR (text or NDJSON).
    progress.py           throttled TTY/non-TTY progress reporter.
    args.py               apply_type_filters + warn_unknown_types (shared
                          --types / --exclude-types resolution).
    fsutil.py             read_json/write_json, list_type_dirs, sanitize_name,
                          exists, iso_now (UTC timestamp).
    version.py            VERSION constant.
config.yaml               types: + field_descriptions: + products: (see README.md).
tests/                    unit / integration / regression + fixtures (see §7).
```

## 3. THE PIPELINE FLOW THROUGH THE MODULES

### f5kb dump (cmd/dump.py → lib/dump.py)

1. Fetch a Coveo guest token via `coveo/aura.py` (Aura
   `HeadlessController.getHeadlessConfiguration`; double-parse the JSON-in-JSON).
2. Load `config.yaml` via `config/loader.py` (per-type documentType + metadata
   keep-list + content keep-list; `field_descriptions:` annotates the catalogue).
3. For each requested type, via a `CoveoClient` (`coveo/client.py`):
   - Get the server count (validation target).
   - Fetch every article — see §4 for `--days` (date-chunk) vs `--all` (@rowid
     keyset), in `coveo/paging.py`.
   - Split each result into metadata/content per the config (`coveo/fields.py`);
     write one JSON per article to `outputs/dump/<TypeKey>/<id>.json`.
   - Write the per-type field catalogue (`_catalogue.json`/`.md`).
4. Write `_index.json` (per-type status ok/partial/failed + written-vs-server
   counts). Exit non-zero if any type failed.

### f5kb enrich (cmd/enrich.py → enrich/driver.py)

The Coveo index returns no body for Manual / Release Note / Supplemental Document /
Bug Tracker / F5 GitHub (content left empty). This post-processor fills content
from each article's public page.

1. Walk `outputs/dump/<Type>/*.json` for the requested types.
2. Resumability gate: skip an article that already has `body_text` or a recorded
   `bodyError` (unless `--refetch`, or `--refetch-errors` to retry only the errored).
3. Dispatch per type via the enricher registry (`enrich/enrichers.py`):
   - Bug Tracker → deterministic cdn.f5.com page; `html/bugtracker.py` extracts the
     labelled sections (standard + security/CVE templates).
   - Manual / Release Note / Supplemental Document → `html/docpage.py`
     (host→selector map + generic + `<pre>`/`<body>` fallback);
     docs.cloud.f5.com (Next.js) reads from `__NEXT_DATA__` via `html/nextdata.py`.
   - F5 GitHub → GitHub REST API (`http/github.py`), not HTML.
4. Extract only the body as markdown (`html/serialize.py`; links absolutized),
   strip chrome and metadata duplication. Write `content.body_text` (+ sections),
   `bodySource`, `fetchedAt`, or `content.bodyError` on failure.
5. Write `_enrich_report.json` (per-type enriched/failed/skipped + errored items).

### f5kb track (cmd/track.py → track/db.py)

1. Walk the dump; for each article compute identity, the several dates
   (created/original-published/updated-published/modified/captured), a metadata
   hash and a content hash (`track/hashing.py`: SHA-256 over canonicalized JSON,
   with volatile `bodySource`/`fetchedAt` excluded so a re-fetch isn't a "change"),
   `has_body`, `body_error`.
2. Upsert into `outputs/articles.db` (sqlite3). Compare to the stored row →
   classify new / changed / unchanged; log every change to a `changes` table and a
   per-run summary to a `runs` table. Removed = rows in the scanned types absent
   from this dump (logged, not deleted).

### f5kb sync (cmd/sync.py → lib/sync.py)

Incremental update — the same dump/enrich/track building blocks, wired to touch
only what changed:

1. `load_hash_index(db)` → a `{"<document_type> <id>": metadata_hash}` map (empty
   on first run). The map key is `db_key()`; it MUST match `load_hash_index`'s
   `"<dt> <id>"` format exactly.
2. `dump_types({incremental=True, prior_hashes, changelog, approval})` — recompute
   sha256(metadata) per article; equal to the prior hash → SKIP; absent → NEW →
   write live; differs → EDIT. With the approval gate on (default), an EDIT is
   staged to `_pending/<type>/<id>.json` (live untouched); with `--yes` it overwrites
   in place after archiving the old file.
3. `enrich_dump` over the enrichable types — resumability skips already-bodied files,
   so enrich auto-limits to new/rewritten ones; it ALSO fills the bodies of staged
   `_pending/` articles so a reviewer sees the complete new version.
4. `merge_pending` records the staged edits in `_pending/_manifest.json`.
5. `track_dump` updates the DB from the LIVE dump only (`list_type_dirs` skips
   `_`-prefixed dirs, so `_pending/_replaced/_deleted` are never indexed). Staged
   edits stay out of the DB until approved.
6. Deletion DETECTION (only under `--all`): DB ids per document_type absent from
   `current_ids` are recorded as changelog `op="deleted"` source="sync" and
   reported — never removed.
7. `--dry-run` classifies + reports but writes nothing. `--changelog` is ON by default.

### f5kb reconcile (cmd/reconcile.py → lib/reconcile.py)

The execution counterpart to sync's detect-and-report — the ONLY command that
deletes on our side. `fetch_ids` (cheap IDs-only `@rowid` keyset) gives the live
Coveo id set per type; diff vs the DB = deletions. Report-only unless `--apply`.
On apply: a threshold guard aborts on suspicious mass-deletion; the DB is copied to
`<db>.bak-<stamp>`; then each article is soft-deleted (file moved to `_deleted/`,
DB row dropped, changelog `op="deleted"`) or hard-removed with `--purge`.

### f5kb approve (cmd/approve.py → lib/approve.py)

The human checkpoint behind the approval gate. Reads `_pending/_manifest.json`
and, for each selected entry, recomputes risk fresh from the actual files
(`compute_risk`: body-dropped / body-error / body-shrank). `--list` previews;
`--reject` deletes the pending file; otherwise PROMOTE: archive the live file to
`_replaced/`, move the pending file into place, record changelog `op="edited"`
source="approve". Edits with a risk flag are HELD (left pending) unless
`--include-risky`. `approve` then runs `track_dump` so the DB matches.

### Cloud: Approve Lambda (handlers/approve.py)

Cloud counterpart to `f5kb approve`. Two invocation modes:

1. **S3 event** on `runs/{date}/track/_done` → automatic approval pass over all
   pending articles for that run.
2. **API Gateway POST** `/approve` → Slack Block Kit button callback for held articles.

**Risk routing** (from `MANUAL_FLAGS = {"body-dropped", "body-error"}`):

| Risk flag | Action |
|---|---|
| `[]` (none) | Auto-approve → `live/` immediately |
| `body-shrank-{pct}%` | Auto-approve → `live/`, Slack text notify |
| `body-dropped` | Hold → Slack buttons (approve / reject) |
| `body-error` | Hold → Slack buttons (approve / reject) |

**Archive-before-overwrite.** Before any live article is overwritten (auto or
manual), the current live version is copied to
`archive/{type_key}/{art_id}/{timestamp}.json`. This is the source for restores.
The copy is best-effort — a failure to archive never blocks promotion.

**Dependency updates on every approve:**
- `live/{type_key}/{art_id}.json` — receives the promoted version
- `hash-index/current.json.gz` — updated with the new metadata hash so the next
  incremental run skips unchanged articles correctly
- `changed_ids.jsonl` — appended with `{op, id, type, s3_key, run_date, approved_by}`
- `decisions.jsonl` — appended with `{id, type, op="approved", actor, ts}`

**Held articles** are written to `lambda/state/{run_date}/approve_held.json`.
When `remaining` reaches 0 (all decided via Slack), `runs/{date}/approve/_done` is
written to trigger downstream consumers.

### Cloud: Restore Lambda (handlers/restore.py)

Reverts an article to any previously archived version. Invoked manually:

```
aws lambda invoke \
  --function-name f5kb-restore-prod \
  --payload '{"type_key":"Solution","art_id":"K12345",
              "archive_key":"archive/Solution/K12345/2026-07-01T02-00-00Z.json",
              "actor":"operator"}' \
  response.json
```

To list available archived versions for an article:

```
aws s3 ls s3://<bucket>/archive/<type_key>/<art_id>/
```

**What restore updates (all four must stay consistent):**

1. `live/{type_key}/{art_id}.json` — overwritten with the archived version
2. `archive/{type_key}/{art_id}/{ts}.json` — current live is archived first (so
   the displacement is itself reversible)
3. `hash-index/current.json.gz` — recomputed from the restored version's metadata;
   without this update, the next incremental run would see the restored article as
   "changed" and re-stage the current upstream version, undoing the restore
4. `changed_ids.jsonl` + `decisions.jsonl` — appended with `op="restored"`,
   `restored_from`, `displaced_to`, `actor`, `ts` for full audit trail

The restore Lambda **does not** re-run track/enrich. If the restored version differs
materially from what the DB tracks, run `f5kb track` (local) or manually invoke the
Track Lambda after restore to re-align the DB.

### f5kb status (cmd/status.py → lib/status.py)

Read-only. Aggregates `_index.json`, `_enrich_report.json`, on-disk per-type counts,
the DB (articles/runs), the changelog (last-run tally), and the count of edits staged
in `_pending/` into a `StatusReport`; renders a table or, with `--json`, a JSON payload.

## 4. PAGINATION STRATEGY (THE CORE OBSTACLE)

Lives in `coveo/paging.py`. Coveo enforces a hard cap:
`firstResult + numberOfResults ≤ 5,000`. Two strategies, chosen by mode:

- **`--days=N`** (and fetch/recent over a window) → recursive date-window chunking
  (`fetch_chunked`). Split on `@date>=...@date<...`, recursively halving any window
  whose count exceeds 5,000, then offset-page each leaf. Then refine client-side to
  the exact content-mod window. Good for recency.
- **`--all`** → keyset (cursor) pagination by `@rowid` (`fetch_keyset`). No date
  window at all. Sort `@rowid` ascending, page with `@rowid>=cursor`. No offset cap,
  and it captures articles a date window would miss.

**Why keyset was necessary (war story).** A full-corpus dump came up short on the
two largest types, and the built-in count-validation flagged it. Root causes:

- `@date` filtering is only 1-second resolution; a bulk re-index stamped 12,992
  Manual articles with the identical `@date` second → irreducible by date → the
  5,000 cap silently dropped ~8k.
- Date-range queries silently exclude null / out-of-window `@date` docs
  (Release_Note: bare 757 vs windowed 726).

Keyset by `@rowid` fixes both. Picking the cursor field mattered:
`@permanentid` / `@urihash` / `@f5_kb_id` are not sortable (InvalidSortField); only
`@rowid` (a.k.a. `@sysrowid`) is sortable, unique, and monotonic. Gotcha: `@rowid`
(~1.8e18) exceeds Python's `float` precision in JSON — the cursor backs off a margin
and uses `>=` + `permanentid` dedup so a boundary doc is re-fetched-and-deduped
rather than skipped.

Other Coveo limits handled here: the 20 MB response cap (auto-halve the page size
and retry that page) and the 5,000-offset cap (above).

## 5. THE DEPENDENCY-INJECTION DESIGN (= OFFLINE TESTS)

Both network surfaces are dependency-injected:

- `CoveoClient` (`coveo/client.py`) takes an optional `httpx.Client`; in production
  it creates one internally; in tests a `_ScriptedTransport(httpx.BaseTransport)` is
  passed that returns scripted `httpx.Response` objects.
- `HttpClient` (`http/fetcher.py`) similarly takes an `httpx.Client`.

This is what makes the whole suite run offline and deterministically. The
retry/backoff/refresh/timeout logic, the pagination strategies, the HTML/JSON parsers,
and the full subcommand wrappers are all exercised without a network — the scripted
transport returns per-call responses (simulating 401-then-200 refresh, 503 retries,
20 MB-size errors, multi-page keyset cursors, etc.).

Live tests are opt-in: `uv run pytest -m live` and are the only ones that touch the
real API.

## 6. CROSS-CUTTING STRATEGIES

- **Token management** (`coveo/aura.py` + `client.py`). Fetched once at start;
  `CoveoClient.post` auto-refreshes on 401/419 in place (a full dump can outlive
  the ~24h guest JWT).
- **Retry/backoff.** Transient network errors + HTTP 429/5xx retried with
  exponential backoff. HTTP 4xx (gone/restricted) are terminal.
- **Per-request timeout (no-hang).** httpx has a configurable timeout; `CoveoClient`
  passes it through so a socket that dies mid-request becomes a recoverable error.
- **Per-type error isolation.** In the dump, one type failing never aborts the run;
  status is recorded per type and the operator re-runs just the failures via `--types=`.
- **Count validation.** The dump compares written-vs-server per type and marks
  `partial`/`failed`; this is the tripwire that caught every completeness bug.
- **Resumability everywhere.** All output is per-article files + idempotent upserts.
  Enrichment skips already-done articles; `--refetch-errors` retries only failures.
  Nothing needs a clean restart.
- **Error classification over silent junk.** When a page can't yield a real body, we
  record a descriptive `content.bodyError` rather than capture nav/landing/404 text.
  This keeps the data trustworthy and makes gaps visible and re-runnable.
- **Politeness.** Concurrency pool + per-worker delay + descriptive User-Agent.
- **Output discipline.** Logs/progress → STDERR (`lib/logger.py`); machine output
  (`--json`) → STDOUT.
- **Overwrite protection** (`lib/staging.py` + `approve.py`). sync/dump/enrich never
  silently replace a good live article: an edit is staged to `_pending/` for review
  and applied only by `f5kb approve`, which archives the replaced file to `_replaced/`
  and HOLDS edits whose body would regress unless `--include-risky`. `--yes` bypasses.

## 7. TESTING APPROACH

488 tests, all offline by default (`addopts = "-m 'not live'"`), under `tests/` in
three categories:

- **unit** — one module's functions in isolation (paging, client, aura/config, dates,
  fields, HTML parsers, hashing, args, logger, status, github, fsutil; the incremental
  layer: changelog, dump-incremental, reconcile, staging, approve; plus dump_types,
  sync_dump, reconcile, Progress class).
- **integration** — full subcommand wrapper / orchestrator end-to-end against the
  scripted transport and a temp dir (dump, enrich, track, status, flat fetch, sync,
  approval gate, approve cmd). CLI smoke tests use `subprocess` / `python -m f5kb`.
- **regression** — lock the on-disk contracts: the 9-key article envelope, the
  catalogue row schema + markdown table, the SQLite schema (tables/indexes/columns/PK),
  the enrich output shape, and the changelog JSONL line schema.

Live tests (5) are in `tests/integration/test_live.py`, all marked `@pytest.mark.live`.
Run with `uv run pytest -m live`.

Fixtures live in `tests/fixtures/` (Aura token response, Coveo facet/search/count
JSON, a 25-article mini dump across 5 types). The `noop_sleep` fixture and
`_ScriptedTransport` helpers live in `tests/conftest.py` and test files respectively.

## 8. KEY DECISION POINTS (AND WHY)

- **Python + uv over seven scripts.** Migrated from Deno/TypeScript. Python gives
  a richer HTML ecosystem (BeautifulSoup + lxml), type hints via dataclasses, better
  async optionality, and wider operator familiarity. uv provides fast, reproducible
  installs. Output formats and flag semantics are byte-identical.
- **httpx over requests.** Type-safe, supports both sync and async, has a clean
  transport DI interface (`BaseTransport`) that makes offline testing easy. The
  `_ScriptedTransport` pattern replaces the Deno mock-fetch approach.
- **No headless browser — ever.** Two sites looked client-rendered
  (docs.cloud.f5.com, newer techdocs.f5.com). The body is always reachable without
  a browser: docs.cloud embeds it in `__NEXT_DATA__` JSON; techdocs renders the real
  topic body server-side.
- **`@rowid` keyset as the canonical full-corpus pager** (over date-chunking) — see §4.
- **SQLite (built-in sqlite3) for the master overview.** Upserting 100k+ rows and
  querying "what changed since run X" is far cheaper than JSON, and sqlite3 ships
  with Python (no extra dependency).
- **Extract only the body; never duplicate metadata.** Bug Tracker header block,
  doc-site nav/header/footer, Sphinx permalinks, etc. are stripped.
- **Consolidate generated data under outputs/ and gitignore it.** Code + curated
  config + docs are versioned; large regenerable data is not.

## 9. OBSTACLES OVERCOME (INDEX)

| Obstacle                                          | Resolution                                           |
| ------------------------------------------------- | ---------------------------------------------------- |
| 5,000-result offset cap                           | date-window chunking (--days) / @rowid keyset (--all) |
| 20 MB response cap                                | auto-halve page size and retry the page              |
| Dense same-second @date cluster (>5,000 in 1s)    | keyset by @rowid                                     |
| Null / out-of-window @date docs                   | --all keyset whole type (no date filter)             |
| Guest token expiry mid-run                        | refresh on 401/419                                   |
| Hung socket after sleep / connectivity drop       | httpx timeout parameter                              |
| --all mod-date filter dropping pre-2000 articles  | skip the filter under --all                          |
| Coveo body absent for 5 doc types                 | off-API enrichment (f5kb enrich)                     |
| Multiple doc-site templates                       | host→selector map + generic + `<pre>`/`<body>` fallback |
| docs.cloud.f5.com JS-rendered                     | parse embedded `__NEXT_DATA__` JSON                  |
| Bug Tracker 2nd (CVE) template                    | labelled-field fallback parser                       |
| Soft-404 pages (HTTP 200)                         | detect by body signature → bodyError                 |
| Moved-to-landing redirects                        | detect file→dir-root / section change → bodyError    |
| docs.nginx.com → F5 KB migration                  | record K-id cross-reference                          |
| GitHub 60/hr rate limit                           | GITHUB_TOKEN → 5,000/hr                             |

## 10. OPERATING IT

Full refresh:

```
uv run f5kb dump --all --out=outputs/dump --types="..."
uv run f5kb enrich --dump=outputs/dump --types="Bug_Tracker,Manual,Release_Note,Supplemental_Document"
uv run f5kb track --dump=outputs/dump
uv run f5kb status
```

React to issues: check `_index.json` (re-run failed/partial types with `--types=`)
and `_enrich_report.json` (fix the host rule / parser, then `enrich --refetch-errors`).
Live corpus counts drift, so re-run any type whose dump shows a shortfall; `f5kb track`
records the delta as new/changed/removed.

## 11. THE WEB CONSOLE (ui/)

A local FastAPI server + no-build ES-module frontend over everything the
pipeline produces. Not part of the CLI or the Lambda deployment — a dev-machine
tool (`uv sync --group ui`, never shipped to Lambda).

```
ui/
  server.py     routes only — validation, 403 read-only guard, docs rendering
  readers.py    the data layer. One Reader interface, three sources:
                  AwsReader          S3 bucket + SQS/CloudWatch/Lambda/SNS
                  LocalReader        an S3-mirror tree on disk (live/, runs/, …)
                  CliOutputsReader   the classic outputs/dump/<Type> layout
                Mutations (edit/restore) are implemented ONCE on the
                StorageBackend-backed base class: archive-before-overwrite →
                write live → refresh hash-index (db_key format!) → append
                audit/{YYYY-MM}/changed_ids.jsonl + decisions.jsonl.
  runview.py    composes a run's live status from the per-stage S3 files
                (orchestrator state, dump/enrich cursors + sentinels,
                track summary/progress, approve manifests, approve_held.json)
  playbook.md   operator playbook, rendered in-app (daily loop, held-article
                review, restore, incident runbook)
  static/       app.js (hash router/shell) · pages.js (7 pages) · ui.js
                (components) · api.js · style.css — no build step, no CDN
```

Design decisions: reads are byte-exact against the keys the handlers write
(see MEMORIES.md S3 layout); AWS restores go through the Restore Lambda so the
run-open refusal and SNS handoff stay in one place; local restores/edits reuse
the same protocol via LocalStorage; the article-list endpoint fetches titles
only for the visible page (bounded ThreadPool) since S3 List doesn't return
them; expensive listings sit behind a small TTL cache.
