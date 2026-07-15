# f5kb console

Local web UI to see — and, when explicitly enabled, drive — everything the
pipeline produces: runs, the live corpus, pending edits, held approvals,
archived versions, the audit trail, queues, and errors. Works against a
deployed S3 stage or a local tree. Ships with a built-in operator playbook
(`ui/playbook.md`, rendered in-app under **Playbook & Docs**).

## Install

```bash
uv sync --group ui        # fastapi + uvicorn + markdown (dev-only; never ships to Lambda)
```

## Run

```bash
# Live staging (read-only) — needs AWS creds for the account/region in config.yaml
uv run --group ui python ui/server.py --target staging

# Staging with mutations enabled (trigger runs, approve/reject, restore, edit, backfill)
uv run --group ui python ui/server.py --target staging --allow-writes

# Prod (read-only strongly recommended)
uv run --group ui python ui/server.py --target prod

# Standalone: a local tree, no AWS
uv run --group ui python ui/server.py --target local
```

Then open **http://127.0.0.1:8000**. Press `/` anywhere to jump to an article id.

## Targets

Defined in `ui/config.yaml`:

| target | source | AWS creds | mutations |
|---|---|---|---|
| `local` | a tree on disk (see layouts below) | none | with `--allow-writes` (edit/restore only) |
| `staging` | `f5kb-articles-{acct}-staging` + live SQS/CloudWatch/Lambda | yes (us-east-2) | with `--allow-writes` |
| `prod` | `f5kb-articles-{acct}-prod` + live services | yes | with `--allow-writes` |

The bucket is derived as `f5kb-articles-{account_id}-{stage}`; `account_id`
resolves via STS unless set in the config.

`local` auto-detects two layouts:

- **S3 mirror** — `root/` contains `live/`, `runs/`, `pending/`, … (e.g. from
  `aws s3 sync s3://bucket outputs/`). Full console feature set.
- **CLI outputs** — the classic `outputs/dump/<Type>/<id>.json` tree with
  `_pending/`, `_replaced/`, `_changelog.jsonl`. Corpus browsing, pending
  diffs, version history, and history views map onto the CLI's files; runs
  and queues don't exist there and say so.

## Pages

- **Overview** — corpus totals, latest-run state, pending/held counts, queue
  health, hash-index status.
- **Runs** — run history; per-type dump/enrich progress with live bars,
  phase stepper (scrape → track → approve → done), alerts ("why is it
  stuck"), track risk breakdown, P2 handoff counts, and the held-article
  queue with inline diff + approve/reject. With `--allow-writes`: run
  controls (pause/resume the pipeline, stop a runaway run, delete a run's
  tracking data with a dry-run preview).
- **Review** — every staged edit in `pending/`, grouped by type, plus the
  held queue of the open run. With `--allow-writes`: checkbox selection with
  per-type select-all and bulk approve/reject on the pending list (console-
  side full protocol; held articles still route through the Approve Lambda —
  note bulk pending approval publishes no P2 handoff, use backfill after).
- **Corpus** — browse each type; searchable, paginated article tables; per-
  article view with body / metadata / raw JSON tabs, pending diff, version
  history, restore, and a JSON editor.
- **History** — the audit trail: applied changes and decisions (including
  rejects), filterable by month and free text.
- **Operations** — health checks (Coveo token + live search, bucket, queues,
  lambdas — one button, failing rows carry a what-to-check hint), DLQ depths
  with message peek (long-polled, non-consuming) and one-click redrive
  (single message or all — back to the work queue, deleted from the DLQ),
  recent Lambda errors, a full log viewer (all lambdas, INFO/ERROR/platform
  lines, filterable by function/level/time/free text, paginated 50/page,
  live-tail toggle), a compute cost + duration panel (parsed from REPORT
  lines: invocations, GB-seconds, est. dollars per lambda), manual levers
  (trigger
  runs, pause/resume pipeline, purge work queues, backfill to P2, restore),
  and a raw key browser for any whitelisted prefix.
- **Integrations** — every f5kb SNS topic for the stage, its subscribers, and
  each subscriber queue's backlog (visible / in-flight / delayed) — a live
  view of downstream ingestion status, plus the last completed handoff run.
- **Playbook & Docs** — the operator playbook plus all repo docs
  (README/HOWTO/OUTLINE/FINDINGS/MEMORIES/P2 handoff) rendered in-app.

## Mutations (require `--allow-writes`)

Every mutation asks for confirmation, is attributed `actor=console` in the
audit trail, and follows the pipeline's own safety protocol:

- **Approve / Reject** (single or all) — invokes the Approve Lambda, same as
  the Slack buttons.
- **Trigger run** — invokes the orchestrator (incremental or full).
- **Restore** — via the Restore Lambda on AWS targets (refused while a run is
  open); applied directly with the same archive-first protocol on local trees.
- **Edit article** — archives the current live version, recomputes envelope
  hashes, rewrites live, refreshes the hash-index (so the next run doesn't
  re-stage or undo the edit), and appends an `edited` audit record.
- **Backfill** — republishes a past run's manifest to the P2 handoff topic.
- **Pause / Resume pipeline** — disables/enables the dump + enrich SQS
  triggers (nothing deleted; messages wait).
- **Stop run** — pause + purge the WORK queues (queued messages deleted;
  DLQs and S3 run data untouched) to kill a self-requeue chain.
- **Delete run** — removes `runs/{date}/` + `lambda/state/{date}/` (and
  optionally the pending/ articles the run staged, resolved from its own
  manifests) plus any DLQ messages referencing the run's date. Dry-run
  preview first; live/, archive/, audit/, hash-index are never touched; an
  audit record is written.
- **DLQ redrive** — re-sends a DLQ message body to its work queue and deletes
  it from the DLQ; the type resumes from its saved cursor.

## Target switching

A dropdown in the topbar switches between the targets in `ui/config.yaml`
(local / staging / prod) without restarting the server. Safety rule: only the
target the server was **started** against keeps write access — a switched-to
target is always read-only. An undeployed stage (e.g. prod) still loads; the
health checks and empty pages show exactly what's missing.

## Safety

- Read-only by default; without `--allow-writes` the action endpoints return
  HTTP 403 and the buttons are hidden.
- Binds to `127.0.0.1` only — never exposed to the network.
- The generic object/key browser only serves whitelisted pipeline prefixes.
- Nothing is ever hard-deleted: every overwrite archives the displaced
  version first, so any action can be reversed from the **Versions** tab.

## Architecture

```
ui/
  server.py     FastAPI routes (thin) + docs rendering
  readers.py    data layer: AwsReader / LocalReader (S3 mirror) / CliOutputsReader
  runview.py    composes run status from the per-stage S3 files
  playbook.md   the operator playbook (rendered in-app)
  config.yaml   target definitions
  static/       no-build frontend: app.js (router/shell), pages.js, ui.js
                (components), api.js, style.css, index.html
```

The reader layer shares the mutation protocol with the pipeline itself via
`f5kb.storage` (`StorageBackend`), so local and AWS behave identically.

### Performance (no database)

The console never sweeps `live/` with `list_objects_v2` to count the corpus —
that costs 100+ sequential LIST round-trips at ~106k articles. Instead:

- **Corpus counts and per-type key lists come from `hash-index/current.json.gz`**
  (one ~3MB GET). Every promotion to `live/` also writes its `db_key` into the
  index, so it mirrors `live/` exactly. `/api/corpus?refresh=true` (the Corpus
  page's refresh button) re-lists the store as ground truth.
- **Every expensive listing is cached stale-while-revalidate** (`_TTLCache.swr`):
  a stale hit is served instantly and refreshed in a background thread, so
  polling pages never block on S3/SQS after the first load. Server startup
  pre-warms the caches in the background.
- **Small state files are fetched in parallel batches** (`get_json_many` in the
  run detail view; run summaries and DLQ depths likewise), and large run
  manifests are line-counted through a short-TTL cache instead of being
  re-downloaded on every poll.
