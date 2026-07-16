# F5 KB Toolkit — TODO

Open work and a log of what's been shipped. See OUTLINE.md for how the code works
and FINDINGS.md for what we learned about the scraped system.


## OPEN: SITEMAP GAP (47 COVEO-UNINDEXED K-ARTICLES)

The my.f5.com sitemap lists 47 K-article IDs that are absent from the Coveo index
(so the pipeline can't reach them) — see FINDINGS.md "Sitemap" section for the
method and analysis. ~2 are recent (likely indexing lag → re-check on a future dump;
if they appear in Coveo, no action). The other ~45 are old (2023–early 2026) and only
reachable by scraping the my.f5.com Salesforce SPA per-article.

TODO:
- Periodically re-derive the gap (re-run the sitemap diff) and confirm the recent
  ones get picked up by Coveo over time.
- DECIDE whether the ~45 old ones are worth a per-article my.f5.com SPA scrape
  (likely superseded/unpublished; low value). If yes, build a small targeted scraper
  (parse the article page's embedded data, not headless) for just these.
- Also re-check whether the sitemap's Security-Advisory under-listing matters for
  any external cross-checks (our dump has 252 SAs the sitemap omits).

The 47 IDs (id and sitemap lastmod date):

```
K000130602   2023-03-02    K000132222   2023-03-02    K000092630   2023-03-20
K000091342   2023-03-20    K000133545   2023-04-17    K000133617   2023-04-21
K000133767   2023-05-03    K000133455   2023-06-20    K000135570   2023-07-22
K000135481   2023-07-27    K000135713   2023-08-04    K000135540   2023-10-10
K000137165   2023-10-26    K000137438   2023-11-03    K000137983   2023-12-20
K000138204   2024-01-16    K000138116   2024-01-29    K000138580   2024-02-14
K000138860   2024-03-08    K000138939   2024-03-19    K000139023   2024-03-27
K000139671   2024-05-29    K000139855   2024-06-04    K000137213   2024-07-12
K000140692   2024-08-14    K000140750   2024-09-03    K000141072   2024-09-17
K000141424   2024-10-16    K000148237   2024-10-24    K000148468   2024-11-11
K000148834   2024-12-03    K000149522   2025-01-28    K000149624   2025-02-05
K000151148   2025-05-07    K000151676   2025-06-03    K000152918   2025-08-13
K000156974   2025-10-09    K000157017   2025-10-17    K000159585   2026-01-20
K000159835   2026-02-04    K000159836   2026-02-06    K000159943   2026-02-09
K000160047   2026-02-18    K000160425   2026-03-24    K000160490   2026-03-27
K000161231   2026-05-13    K000161322   2026-05-22
```


## OPEN: PRODUCTS DRIFT

The last sync flagged one live product not in `config.yaml`: **"BIG-IP Next CNF"**.
Run `uv run f5kb discover` and copy the refreshed `products:` block into `config.yaml`
if you want it captured (the pipeline does NOT read `products:`, so this is
reference-only).


## OPEN: POSSIBLE FUTURE GUARDS

- A `--max-staged` abort for a sync that would stage an enormous `_pending/` (mirrors
  reconcile's threshold guard).
- A combined "sync then reconcile --apply" wrapper, if the report-then-execute
  two-step ever proves tedious in practice.
- Body-only upstream changes that bump no date are not detected by `metadata_hash`
  (rare). `enrich --refetch` remains the escape hatch; could be automated by adding
  a periodic forced re-enrich.


---


## DONE 2026-07-13: O(n^2) JSONL APPEND FIX — TRACK + DUMP BATCH WRITES

Track died in an infinite restart loop on the 07-10 full run (105k articles):
`_process_entry` called `append_jsonl(changes.jsonl)` PER ARTICLE, and each S3
append re-uploads the whole growing file — O(n^2) bytes (~1 TB of PUT traffic
per full run). One type outlasted the 900s Lambda limit, progress.json only
checkpoints at type boundaries, so every retry restarted from zero. Fix:
`append_jsonl_many` on the storage backends (S3: one get + one put for N
lines; local: one open-append), track batches each type's records into ONE
write with per-article pending/live GETs parallelized (ThreadPool 16), and a
fresh start resets changes.jsonl so partial lines from crashed attempts can't
duplicate. Dump had the identical bug on the per-type manifest (~155 GB for a
47k type) — now stages each page with concurrent envelope puts + ONE manifest
append (page order preserved; enrich consumes by line offset). Regression
tests pin one-write-per-type (track), one-write-per-page (dump), and the
stale-partial reset. Requires redeploy.

---


## DONE 2026-07-13: WATCHDOG STALL AUTO-REDRIVE (SELF-HEALING RUNS)

Root cause of the 07-10 run stalling 2.5 days at 97%: Manual's enrich
self-requeue message failed 3 deliveries while the pipeline triggers were
paused (Pause caught it mid-retry), landed in the enrich DLQ, and nothing ever
redrives a DLQ. Fix: the watchdog (now hourly, was daily 06:00) gained a stall
sweep. For each DLQ message it applies `stall_decision` (pure, unit-tested):
run OPEN + work queue EMPTY + cursor STALE (>STALL_AGE_H, default 1h) or
absent + under the redrive cap → re-send the body to the work queue (with a
`watchdog_redrives` counter bumped) and delete from the DLQ; the type resumes
from its saved cursor. Bounds against runaway compute: max WATCHDOG_MAX_REDRIVES
(3) per message then escalate-only (~$0.15 worst-case per stuck type), one
attempt per hourly pass, never touches closed-run orphans or busy queues.
Action alerts (redrive/cap/escalation) email any hour; the outstanding-holds
digest stays once daily (06:00) so hourly cron doesn't spam. Template: DLQ arns
added to the PipelineQueues IAM Sid, queue URL env vars on the watchdog,
schedule rate(1 hour). NOTE: staging ScheduleState=DISABLED — enable the
f5kb-watchdog-staging schedule manually for staging self-healing. Moto tests:
redrive moves message + bumps counter, cap leaves it, orphan/busy untouched.

---


## DONE 2026-07-10: CONSOLE OPS SUITE — HEALTH, REDRIVE, COSTS, TAIL, TARGET SWITCH

Six console features. (1) Health checks on Operations: Coveo token + one live
search, bucket read, queue reachability, Lambda deployment — failing rows carry
a what-to-check hint (`/api/health`). (2) DLQ redrive: per-message or redrive-
all buttons in the DLQ peek modal — body re-sent to the work queue, message
deleted from the DLQ (`/api/actions/redrive`, writes-gated). (3) Compute cost +
duration panel: REPORT platform lines parsed per lambda into invocations,
billed GB-seconds, peak duration/memory, estimated dollars (`/api/costs`;
`parse_report_line` unit-tested). (4) Live log tail toggle (5s auto-reload,
self-cleans when leaving the page). (5) Delete-run now also counts (dry run) /
deletes (real run) DLQ messages whose body references the run_date. (6) Target
switcher dropdown in the topbar: hot-swaps the backing reader via a ReaderRef
proxy (`/api/targets` + `/api/actions/switch-target`); switched-to targets are
FORCED read-only — only the startup target ever keeps writes; undeployed prod
loads gracefully (health page names what's missing). Whole-type bulk
approve/reject also landed on Review (`resolve_pending_type`, bulk-optimised:
one hash-index save + batched audit appends instead of per-article O(n^2)),
plus dismissible run alerts (per-browser) and a 404 for deleted/unknown runs.

---


## DONE 2026-07-10: REVIEW BULK ACTIONS + LOG PAGINATION + AI-WALKABLE LAMBDA LOGGING

(1) Review page: checkbox selection (per-type select-all) + bulk approve/reject
on pending staged articles via `/api/actions/pending` (chunked 500/request).
Approve runs the console-side full protocol (`resolve_pending` →
archive-before-overwrite + hash-index + audit); NO P2 handoff SNS is published —
backfill afterwards if P2 must receive them. Held articles still route through
the Approve Lambda. (2) Log viewer paginated (50/page, fetch-size selector).
(3) Logging overhaul across all 8 Lambda handlers for AI-assisted root cause:
new `f5kb/lib/logutil.exc_fields()` adds err_type/err_msg/trimmed-traceback to
every error; every handler wraps its body so uncaught crashes emit a structured
`invocation_failed` record with a `hint` field (what to check next) instead of
a raw non-JSON runtime traceback; previously-silent paths now log (track's
missing-pending skip, approve's archive-before-overwrite failure, Slack webhook
failures, orchestrator sweep case B resume, SSM token fetch failures, malformed
manifest lines); terminal gates log WHICH types they wait on; invocation
entry/exit records carry remaining_ms/elapsed_ms/counts/next_step. NOTE: the
handler logging lands only after `sam build && sam deploy`.

---


## DONE 2026-07-10: CONSOLE OPS EXPANSION — LOGS, RUN CONTROLS, INTEGRATIONS

Four console additions. (1) Fixed the "DLQ shows N messages but click shows
nothing" bug: `dlq_messages` used a short poll (WaitTimeSeconds=0) which samples
a subset of SQS servers and misses sparse queues — now long-polls with retries
until the approximate depth is gathered. (2) Full log viewer on Operations:
CloudWatch logs for all 8 lambdas, INFO/ERROR/platform lines, filterable by
function/level/window/free text, row click shows the structured record
(`/api/logs`). (3) Run controls on the run detail page + Operations (writes
only): pause/resume the dump+enrich SQS triggers, stop a run (pause + purge
work queues, DLQs untouched), and delete a run's tracking data (`runs/{date}/`
+ `lambda/state/{date}/`, optional pending/ cleanup resolved from the run's own
manifests) with a dry-run preview, hard guard against non-run-scoped keys, and
an audit record. Added `delete_many` batch delete to the storage backends.
(4) New Integrations tab: every f5kb SNS topic, its subscribers, and each
subscriber queue's visible/in-flight/delayed backlog = live downstream
ingestion status. Tests in tests/unit/test_ui_readers.py (delete-run scoping).

---


## DONE 2026-07-10: CONSOLE PERFORMANCE — HASH-INDEX CORPUS + SWR CACHING

The console against AWS was taking ~30s per page: corpus counts swept every
`live/<Type>/` prefix with sequential `list_objects_v2` pagination, `pending/`
(68k keys during a stalled run) was re-listed uncached on every poll, run detail
issued ~50 serial GETs plus multi-MB manifest downloads per 15s refresh, and the
~3MB hash index was re-downloaded per overview poll. Fixed without a database:
corpus counts + per-type key lists now derive from `hash-index/current.json.gz`
(one GET; `?refresh=true` still LISTs as ground truth), all expensive listings
sit behind a stale-while-revalidate cache (`_TTLCache.swr`) so polls never block,
per-type run state is fetched in parallel batches (`get_json_many`), manifest
line counts are short-TTL cached, DLQ depths/queue URLs are cached + parallel,
and the server pre-warms caches at startup. Warm loads: overview ~0.2s, run
detail ~0.5s, corpus instant. Tests: `tests/unit/test_ui_readers.py`.

---


## DONE 2026-07-08: WEB CONSOLE (ui/) — FULL REDESIGN

Rebuilt the dashboard into a full console (FastAPI + no-build ES-module frontend):
Overview / Runs (live per-type progress, held queue with diff + approve/reject) /
Review / Corpus (browse + search every type, article view with body/metadata/JSON
tabs) / History (audit trails) / Operations (DLQs, errors, trigger, backfill,
restore, raw key browser) / Playbook & Docs (built-in operator playbook
`ui/playbook.md` + all repo docs rendered in-app). Mutations require
`--allow-writes`, confirm, follow archive-before-overwrite + hash-index +
audit-trail protocol, and work on both AWS stages and local trees
(`readers.py` auto-detects S3-mirror vs CLI `outputs/` layouts). See ui/README.md.


## DONE 2026-06-17: PYTHON PORT MIGRATION

Migrated from Deno/TypeScript to Python 3.11+ / uv. Full history:

- Code moved from `python/` subdirectory to repo root via `git mv`.
- All Deno/TypeScript artifacts removed (`f5kb.ts`, `deno.json`, `deno.lock`,
  `cmd/` (TS), `lib/` (TS), `test/` (TS)).
- Package manager changed to uv (hatchling build system); `uv sync` replaces
  `deno cache`. All pip/venv references removed.
- Documentation rewritten: CLAUDE.md, README.md, MEMORIES.md, OUTLINE.md,
  HOWTO.md (was HOWTO.txt), FINDINGS.md (was FINDINGS.txt), TODO.md (was TODO.txt).
- Code modernization: `__import__()` anti-patterns fixed; `import re` moved to
  module top; `has_body()` kept in two places (different semantics — not
  consolidated); `iso_now()` consolidated into `lib/fsutil.py`; `-> None` type hints
  added to all 12 cmd functions; `HttpClient.get()` public method added so
  `github.py` no longer accesses `._client` directly.
- Test coverage: new unit tests for `dump_types()`, `sync_dump()`, `reconcile()`,
  `Progress` class; live integration tests in `tests/integration/test_live.py`.
- All 488 offline tests pass. Remote: `worldtechit/f5kb-pythonport`, branch `main`.


## DONE 2026-06-04: INCREMENTAL SYNC + DELETION RECONCILE + CHANGELOG

`f5kb sync` loads each article's prior `metadata_hash` from the DB and SKIPS
rewriting unchanged articles; enrich's existing resumability then auto-limits body
fetches to the rewritten (changed) files. Modes: `--all`, `--days=N`,
`--since-last-run`. Detection of upstream deletions runs under `--all` only (needs
the full live id set) and is REPORTED, never executed.

`f5kb reconcile` is the explicit, guarded executor for deletions (report-only unless
`--apply`; threshold guard + DB backup; soft-delete to `_deleted/` or `--purge`).
It is the only command that removes data.

`--changelog[=FILE]` on every mutating op appends JSONL change records; sync writes
one by default.


## DONE 2026-06-04: OVERWRITE PROTECTION (APPROVAL GATE) + approve COMMAND

sync/dump/enrich no longer silently overwrite a live article that already holds good
data. An EDIT is STAGED to `<dump>/_pending/<type>/<id>.json` (live untouched) and
recorded in `_pending/_manifest.json`; new articles are written directly, unchanged
are skipped.

`f5kb approve` promotes staged edits: archive the replaced live file to `_replaced/`,
move pending → live, reindex the DB. Edits flagged risky (`body-dropped` / `body-error`,
recomputed fresh from the files) are HELD unless `--include-risky`; `--list` previews,
`--reject` discards.

`--yes` on sync/dump/enrich bypasses the gate (overwrite in place, still archiving
to `_replaced/`). `list_type_dirs` skips `_`-prefixed dirs so `_pending/_replaced/
_deleted` are never indexed as article types.


## DONE 2026-06-04: FULL-CORPUS DUMP (13 types, all except Community + F5 GitHub)

106,042 articles. `f5kb dump --all` → `f5kb enrich` → `f5kb track`. All 13 types'
on-disk counts == live Coveo counts (verified). Body coverage:
Bug_Tracker/Release_Note/Supplemental 100%, Manual 99% (the ~239 no-body cases are
all legitimate: soft-404 dead links, moved-to-landing redirects, image-only/empty
stub pages, 1 KB cross-reference). Tracking DB: `outputs/articles.db`.


## DONE: BODY ENRICHMENT FOR 5 EMPTY-INDEX TYPES

Bug Tracker, F5 GitHub, Manual, Release Note, Supplemental Document — see
FINDINGS.md "Body recovery" and README.md. No headless browser needed (docs.cloud.f5.com
bodies come from the page's embedded `__NEXT_DATA__` JSON).


## DONE: PIPELINE HARDENING

Dump path gained `--all` (full corpus), guest-token refresh on 401/419, per-type
error isolation, written-vs-server count validation, and a richer `_index.json` +
non-zero exit on failures. Enrich path gained `--refetch-errors` and writes
`_enrich_report.json`. SQLite master overview added (`f5kb track`).
