# f5kb Console Playbook

The operator's guide to this console: what you're looking at, the daily loop,
and exactly what to do when something needs a human. For the CLI toolkit see
docs/HOWTO.md; for the SNS/S3 integration contract see docs/CONSUMER_GUIDE.md.

---

## 1. What you're looking at

The console reads (and, with `--allow-writes`, drives) the f5kb pipeline — the
system that mirrors every F5 Knowledge Base article into an S3 bucket and hands
approved changes to the P2 ingest team.

| Target | Source | Typical use |
|---|---|---|
| `staging` | `f5kb-articles-…-staging` + live AWS services | rehearsal, testing |
| `prod` | `f5kb-articles-…-prod` | the real corpus |
| `local` | a tree on disk (`outputs/` or an S3 mirror) | offline inspection |

The badge in the top bar shows the target and whether this session can mutate
anything. **Read-only is the default.** Every mutating button also asks for
confirmation, and every mutation lands in the audit trail with
`actor=console`.

### The pipeline in one diagram

```
02:00 UTC daily (or ▶ manual)
orchestrator ─► dump (13 types, SQS fan-out) ─► enrich (4 body-less types)
                                   │
                                   ▼  scrape/_done
                                 track  ── computes new/changed/unchanged + risk
                                   │
                                   ▼  track/_done
                                approve ── clean articles → live/ + SNS to P2
                                   │
                                   └── risky articles → HELD for a human
```

An article is only ever visible to P2 after it reaches `live/`. Everything
risky waits in `pending/` until someone decides — that's Gate 1, and this
console is one of the two places to decide (the other is Slack).

---

## 2. The daily loop (5 minutes)

1. **Overview** — the four tiles answer "is everything fine?":
   - *Latest run* should be `done` by the morning (runs start 02:00 UTC).
   - *Held for review* should be 0. If not → step 3.
   - *Pending staged* should be 0 outside a running pipeline.
   - Queue rows in *Health* should all be 0 — a DLQ above zero is an incident (§6).
2. **Runs → today** — glance at the per-type table. Every row should reach
   `done`; `resuming after timeout` on big types is normal mid-run.
3. **Review** — if anything is held, decide it (§3). The run stays open (and
   P2 doesn't get the held articles) until every hold is decided.
4. Done. History has the receipts if anyone asks what changed.

The watchdog auto-escalates holds older than 24 h (approves them) and pages
the ops alerts topic, so an unattended day fails safe — but reviewing beats
auto-escalation.

---

## 3. Reviewing held articles

An article is held when the new version looks like it would *damage* good
data we already have:

| Risk flag | Meaning | Usual verdict |
|---|---|---|
| `body-dropped` | live has a body; new version has none | **Reject** unless F5 really emptied the page |
| `body-error` | the body fetch errored (the message is shown) | **Reject**; enrich will retry next run |
| `body-shrank-NN%` | body much smaller than live — **informational only, auto-approves** (never held) | No action needed; visible in track's summary |

How to decide, per article:

1. Click **View diff** — metadata changes and a body diff against live.
2. Open **the article on my.f5.com** if the diff is ambiguous — is the page
   really smaller/empty now?
3. **Approve** (promote pending → live, archive the old live, hand to P2) or
   **Reject** (drop the pending version, live untouched).

**Approve all / Reject all** exist for bulk verdicts — e.g. an upstream site
reformat that legitimately shrank hundreds of bodies (approve), or a
transient fetch outage that emptied them (reject).

Nothing you approve is lost even if you're wrong: the replaced live version
went to `archive/` first, and §4 brings it back.

---

## 4. Restoring a previous version

Any overwrite (approve, edit, restore) archives the displaced live version to
`archive/{type}/{id}/{timestamp}.json` first — so every mistake is reversible.

**From the article page:** Corpus → open the article → **Versions** tab →
click a version to see its diff vs current live → **Restore this version**.

**From Operations:** *Restore an article…* if you'd rather type the id.

A restore updates, atomically from your point of view:

1. `live/…` — gets the restored version (current live archived first)
2. `hash-index` — recomputed, so the next incremental run doesn't
   silently undo the restore
3. the audit trail (`changed_ids` + `decisions`, op=`restored`)
4. on AWS, an SNS note so P2 re-ingests the restored article

On AWS targets restores run through the Restore Lambda and are **refused
while a run is open** — finish or close the run first.

---

## 5. Editing an article directly

Corpus → article → **✎ Edit JSON**. This is the scalpel — for fixing a
mangled body, a bad field, or testing what P2 receives. On save the console:

- archives the current live version (reversible, §4),
- recomputes the envelope hashes so the edit is internally consistent,
- refreshes the hash-index (the next run treats your edit as the known state
  and won't re-stage the article unless F5 changes it again),
- appends an `edited` record to the audit trail.

In local CLI mode the same protocol applies to the `outputs/` tree
(`_replaced/` + `_changelog.jsonl`), and the response reminds you to re-run
`f5kb track` so `articles.db` catches up.

---

## 6. Incident runbook

### A DLQ shows > 0

A type failed all 3 SQS retries — its articles were not scraped this run.

1. **Operations → recent lambda errors** — find the exception for that type.
2. Common causes: Coveo token endpoint down (transient — re-run tonight will
   fix), a schema change in the Coveo response (needs a code fix).
3. After the cause is fixed, trigger an **incremental run** — the hash-index
   makes re-runs cheap; already-current articles are skipped.
4. The DLQ drains by redrive or by purging once the messages are understood.

### A run looks stuck

- Per-type rows stuck at `dumping (resumed)` with growing counts are **not**
  stuck — big types resume across multiple 15-minute Lambda slots.
- A row stuck at `queued` for hours + an empty queue → check the DLQ, then
  the dump Lambda's errors.
- `approve` phase that never finishes = holds nobody decided (Review page),
  or check the orchestrator's nightly sweep errors.

### P2 says they missed a run

Operations → **Publish backfill to P2…** with the run date; the manifest key
autofills to that run's `changed_ids.jsonl`. Consumers upsert idempotently,
so re-delivery is always safe. Manifests are kept 90 days.

### Something got approved that shouldn't have been

Restore the previous version (§4). The bad approval stays in the audit trail;
the restore is its own audited event, and P2 gets the corrected version.

---

## 7. Where the data lives (reference)

```
live/{Type}/{id}.json                 the approved corpus — what P2 reads
pending/{Type}/{id}.json              staged, awaiting a decision
archive/{Type}/{id}/{ts}.json         every displaced live version (restore points)
hash-index/current.json.gz            metadata hashes → skip-unchanged
runs/{date}/…                         everything a single run produced
  manifest/{Type}.jsonl               what dump staged
  track/summary.json                  new/changed/unchanged + risk counts
  approve/changed_ids.jsonl           the auto-approved manifest handed to P2
  approve/changed_ids-holds.jsonl     the human-approved follow-up manifest
lambda/state/{date}/approve_held.json the held queue this console reads
audit/{YYYY-MM}/changed_ids.jsonl     every applied change (History → Changes)
audit/{YYYY-MM}/decisions.jsonl       every decision incl. rejects (History → Decisions)
```

Glossary: **staged** = written to `pending/`, invisible to P2 · **held** =
staged + risky, needs a human · **promoted** = copied to `live/`, archived
the old version, handed to P2 · **run** = one dated end-to-end pipeline
execution.

---

## 8. Full reset — wipe everything and start clean

Use this when you want a completely blank slate: no corpus, no history, no
hash-index. Every article in the next run will be `op: new`, the audit log
starts fresh, and the hash-index is rebuilt from scratch by Approve.

```bash
export B=s3://{BucketName}   # change suffix for prod

# 1. Stop anything running
for q in f5kb-dump-queue-staging f5kb-enrich-queue-staging; do
  url=$(aws sqs get-queue-url --queue-name $q --region us-east-2 --query QueueUrl --output text)
  aws sqs purge-queue --queue-url "$url" --region us-east-2 && echo "purged $q"
done

# 2. Wipe all S3 data
aws s3 rm --recursive $B/runs/
aws s3 rm --recursive $B/lambda/
aws s3 rm --recursive $B/pending/
aws s3 rm --recursive $B/live/
aws s3 rm --recursive $B/archive/
aws s3 rm --recursive $B/hash-index/
aws s3 rm --recursive $B/audit/
aws s3 rm --recursive $B/changelogs/

# 3. Rebuild + deploy
sam build && sam deploy --config-env staging

# 4. Sync config.yaml to S3 (required after every deploy + every config change)
make sync-config BUCKET={BucketName}

# 5. Run
aws lambda invoke --function-name f5kb-orchestrator-staging \
  --region us-east-2 --payload '{"mode":"full"}' \
  --cli-binary-format raw-in-base64-out r.json && cat r.json
```

**What each prefix holds (so you know what you're deleting):**

| Prefix | Contents |
|---|---|
| `runs/` | run state, markers, manifests |
| `lambda/` | cursors, orchestrator state, held articles |
| `pending/` | staged (not-yet-approved) articles |
| `live/` | the approved corpus |
| `archive/` | all prior versions (restore points) |
| `hash-index/` | skip-unchanged index (rebuilt by Approve) |
| `audit/` | approval history and decisions log |
| `changelogs/` | monthly change records |

---

## 8b. Deploying (staging release checklist)

Staging runs on the prod cadence since 2026-07-13: **daily run at 02:00 UTC**
(incremental Mon–Sat, full on Sunday) plus the **hourly watchdog** (stale-hold
escalation + stall auto-redrive). `ScheduleState=ENABLED` is baked into
`samconfig.toml [staging]`, so schedules survive every deploy — no manual
toggling.

```bash
# 0. gate — everything green before shipping
uv run pytest

# 1. build + deploy (--config-env on BOTH; build has its own [staging] section)
sam build --config-env staging
sam deploy --config-env staging

# 2. ONLY if config.yaml changed since the last deploy:
make sync-config BUCKET={BucketName}

# 3. verify schedules
aws scheduler list-schedules --region us-east-2 \
  --query 'Schedules[].{name:Name,state:State}' --output table
# expect: f5kb-daily-staging ENABLED, f5kb-watchdog-staging ENABLED

# 4. verify the stack: console → Operations → health checks → "Run checks"
#    (Coveo token, live search, bucket, queues, lambdas — all green)
```

**Schedule semantics to remember:**

- The `ScheduleState` CloudFormation parameter owns BOTH schedules. A manual
  `aws scheduler update-schedule` toggle is reset by the next deploy — change
  cadence in `samconfig.toml`, not by hand.
- Pausing the pipeline from the console (Pause button) stops the WORKERS, not
  the schedule — the 02:00 cron still fires and its orchestrator sweep will
  first try to close any prior open run. Pausing across a retry window can
  strand a resume message in a DLQ; the hourly watchdog auto-redrives it
  (capped at 3 attempts, then it emails and waits for a human).
- Deploying mid-run is safe for S3 state (cursors, markers, manifests survive),
  but an in-flight Lambda finishes on the OLD code and the next resume runs on
  the NEW code — prefer deploying while no run is active.

**Watchdog self-healing (what the emails mean):**

| Email subject contains | Meaning | Your action |
|---|---|---|
| `stalled type(s) auto-redriven` | a dead resume message was re-queued; run continues | none — informational |
| `redrive cap exceeded — NEEDS HUMAN` | same message died 3+ times; something is actually broken | check the type's logs (Operations → log viewer), fix cause, redrive from the DLQ modal |
| `hold(s) escalated` | holds older than 24h were auto-approved | review History → decisions |
| `outstanding held approvals` (daily 06:00) | holds waiting on a decision | Review page → approve/reject |

---

## 9. Console operation

```bash
uv sync --group ui                                    # once
uv run --group ui python ui/server.py --target staging               # read-only
uv run --group ui python ui/server.py --target staging --allow-writes
uv run --group ui python ui/server.py --target local                  # outputs/ on disk
```

- Binds to `127.0.0.1` only. AWS targets need credentials for the account in
  `ui/config.yaml`.
- `/` focuses the global article-id search from anywhere.
- Read-only mode hides every mutating control and the API refuses writes
  (HTTP 403) regardless of the UI.
