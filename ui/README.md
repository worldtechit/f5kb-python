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
  queue with inline diff + approve/reject.
- **Review** — every staged edit in `pending/`, grouped by type, plus the
  held queue of the open run.
- **Corpus** — browse each type; searchable, paginated article tables; per-
  article view with body / metadata / raw JSON tabs, pending diff, version
  history, restore, and a JSON editor.
- **History** — the audit trail: applied changes and decisions (including
  rejects), filterable by month and free text.
- **Operations** — DLQ depths, recent Lambda errors, manual levers (trigger
  runs, backfill to P2, restore), and a raw key browser for any whitelisted
  prefix.
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
