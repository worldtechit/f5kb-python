# F5KB Consumer Guide — reading the live corpus & catching new articles

A quick-start for any team consuming the F5 Knowledge Base pipeline. Everything
here is read-only from your side. For the deep integration contract (retries,
DLQs, incident handling), ask the P1 team for the full **P2 Handoff Playbook**.

> Replace `<P1_ACCOUNT_ID>` with the pipeline account id and `<STAGE>` with
> `staging` or `prod` throughout. All examples use region `us-east-2`.

**The whole integration in one sentence:** approved articles live as JSON files
in an S3 bucket; every time a batch of new/changed articles is approved, an SNS
message lands in your queue pointing at a manifest that lists exactly which
files to fetch.

---

## 1. One-time setup

### a) Get read access to the bucket

Send the P1 team the IAM role ARN your consumer runs as. They register it on
the bucket policy, which grants `s3:GetObject` on the three prefixes you need
(`live/*`, `runs/*`, `changelogs/*`). Nothing else is visible to you.

Bucket name: `f5kb-articles-<P1_ACCOUNT_ID>-<STAGE>`

### b) Subscribe your queue to the handoff topic (for new-article notifications)

You own your queue; P1 owns the topic. Create a standard SQS queue (plus a DLQ,
recommended), allow the topic to send to it, and subscribe with **raw message
delivery** so the JSON arrives without an SNS envelope:

```bash
TOPIC=arn:aws:sns:us-east-2:<P1_ACCOUNT_ID>:f5kb-handoff-<STAGE>
QUEUE_ARN=arn:aws:sqs:us-east-2:<YOUR_ACCOUNT_ID>:<your-queue-name>

aws sns subscribe \
  --topic-arn "$TOPIC" \
  --protocol sqs \
  --notification-endpoint "$QUEUE_ARN" \
  --attributes RawMessageDelivery=true \
  --region us-east-2
```

Your queue's access policy must allow `sqs:SendMessage` from
`sns.amazonaws.com` with `aws:SourceArn` = the topic ARN.

Skipping notifications? See §4 for a poll-only alternative.

---

## 2. Reading the live corpus

Every approved article is one JSON file:

```
live/<Type>/<article-id>.json
```

The 13 types:

```
Support_Solution   Known_Issue   Knowledge   Security_Advisory   Video
Policy   Operations_Guide   Compliance   Education
Manual   Release_Note   Supplemental_Document   Bug_Tracker
```

Browse and fetch:

```bash
B=s3://f5kb-articles-<P1_ACCOUNT_ID>-<STAGE>

aws s3 ls $B/live/                          # list types
aws s3 ls $B/live/Policy/ | head            # list articles of a type
aws s3 cp $B/live/Policy/K000130410.json -  # print one article
aws s3 sync $B/live/Policy/ ./policy/       # mirror a whole type locally
```

### The article envelope

```json
{
  "run_date":     "2026-07-08",
  "captured_at":  "2026-07-08T02:34:56Z",
  "type_key":     "Policy",
  "id":           "K000130410",
  "documentType": "Policy",
  "title":        "K000130410: Example article title",
  "link":         "https://my.f5.com/manage/s/article/K000130410",
  "metadata_hash": "44a1…",
  "content_hash":  "9c2f…",
  "metadata":     { "f5_kb_id": "K000130410", "f5_title": "…", "f5_product": ["BIG-IP"], "…": "…" },
  "content":      { "body_text": "…", "…": "…" }
}
```

| Field | Use it for |
|---|---|
| `id` + `type_key` | Your primary key. IDs are unique **within** a type. |
| `title`, `link` | Display title and the canonical my.f5.com / docs URL. |
| `metadata` | The curated per-type field set (dates, products, versions, status…). |
| `content` | The article body — see below. |
| `metadata_hash`, `content_hash` | Change detection — if unchanged since your last ingest, skip. |

### Where the body text is (varies by type)

| Types | Body field |
|---|---|
| Support_Solution, Known_Issue, Knowledge, Security_Advisory, Video, Policy, Operations_Guide, Compliance | `content.sfdetails__c` (HTML) |
| Education | `content.zendeskdescription` (HTML) |
| Manual, Release_Note, Supplemental_Document, Bug_Tracker | `content.body_text` (plain text/markdown, fetched by the pipeline); `content.sections` may hold a per-heading breakdown |

Robust rule: **use `content.body_text` if present, else the HTML field for the
type.** If `content.bodyError` is set, the body fetch failed for that article —
index the metadata and skip the body.

---

## 3. Catching new articles (the queue)

### The message you receive

Each time a batch is approved, this JSON lands in your queue (raw, no envelope):

```json
{
  "schema":        "f5kb.handoff.v2",
  "run_date":      "2026-07-08",
  "mode":          "incremental",
  "batch":         "auto",
  "article_count": 141,
  "manifest_key":  "runs/2026-07-08/approve/changed_ids.jsonl",
  "bucket":        "f5kb-articles-<P1_ACCOUNT_ID>-<STAGE>",
  "published_at":  "2026-07-08T02:41:00Z"
}
```

### What to do with it — three steps

1. **If `article_count` is 0** → delete the message, done.
2. **Fetch the manifest** at `s3://{bucket}/{manifest_key}`. It's JSONL — one
   article per line:
   ```jsonl
   {"op":"new","id":"K12345","type_key":"Policy","s3_key":"live/Policy/K12345.json","run_date":"2026-07-08","approved_by":"auto"}
   ```
3. **For each line, GET `s3_key` and upsert** into your store, keyed on
   `type_key` + `id`. `op` is `new`, `changed`, or `restored` — all three mean
   the same thing to you: ingest this file.

Then delete the queue message. That's the entire consumer.

### Batch types (treat them all identically)

| `batch` | When |
|---|---|
| `auto` | Every run — the automatic approvals. Expect this daily. |
| `holds` | Only when a human approved held (risk-flagged) articles — arrives later than `auto`. |
| `restore` | A manual single-article rollback. |
| `backfill` | A manual re-publish of a past run (e.g. after an outage on your side). |

Most days you'll see one or two messages. Runs happen daily at 02:00 UTC
(incremental Mon–Sat, full corpus Sunday).

### Being idempotent (important)

Messages can occasionally be delivered twice, and a `backfill` intentionally
re-sends past articles. Upserting by `type_key` + `id` makes duplicates
harmless; comparing the envelope's `content_hash` with what you already have
lets you skip no-op work.

---

## 4. No queue? Poll instead

Everything the queue tells you is also on disk in S3, so a cron poll works:

- **Per-run manifests:** `runs/<YYYY-MM-DD>/approve/changed_ids.jsonl` (and
  `changed_ids-holds.jsonl` if humans approved holds). Present once the run's
  approve phase finishes; a run is fully closed when
  `runs/<date>/approve/_done` exists.
- **Monthly rollup:** `changelogs/<YYYY-MM>/changes.jsonl` — every *changed*
  article across all runs of the month, same line format.

Poll daily after ~03:00 UTC (staging schedules may differ), read any manifest
you haven't processed, fetch the listed `s3_key`s.

---

## 5. FAQ

**Are deletions ever pushed?** No. The pipeline never deletes from `live/`
automatically, and no delete events are published. If F5 retires an article
upstream, it simply stops changing.

**Can an article appear in two types?** IDs are unique within a type;
cross-type reuse is possible in principle, so always key on `type_key` + `id`.

**What if the body is missing?** Check `content.bodyError`. Fetch failures are
rare and retried on later runs; the metadata is still valid and searchable.

**How fresh is `live/`?** Updated once per daily run, immediately after
approval (typically minutes after 02:00 UTC for the auto batch). Held articles
can land hours later, after a human decision — that's the `holds` batch.

**Who do I contact?** The P1 pipeline team owns the bucket, topic, and this
contract. Your queue, its DLQ, and your consumer's health are yours.
