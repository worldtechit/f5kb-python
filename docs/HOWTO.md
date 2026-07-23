# F5 KB Toolkit — How-To Guide

A task-oriented guide to f5kb: get started fast, then look up the exact commands for
the things you'll actually want to do. For the full flag reference see README.md; for
how the code works see OUTLINE.md; for what we learned about F5's backend see
FINDINGS.md.


## QUICK START

You need **Python 3.11+** and **[uv](https://docs.astral.sh/uv/)**. No login required
— F5's public Coveo search backend is reached with a token fetched at runtime.

1. Build the full local index (one JSON per article + a SQLite overview). This is
   three steps: dump (metadata + indexed bodies) → enrich (the bodies the search
   index leaves empty) → track (the SQLite overview). We normally skip the two
   big/low-value types, Community and F5_GitHub:

   ```
   uv run f5kb dump --all --out=outputs/dump --exclude-types=Community,F5_GitHub
   uv run f5kb enrich --dump=outputs/dump --exclude-types=F5_GitHub
   uv run f5kb track --dump=outputs/dump --exclude-types=Community,F5_GitHub
   ```

   Drop `--exclude-types=...` to index everything.

2. See what you've got:

   ```
   uv run f5kb status
   ```

That's it. Everything lands under `outputs/` (gitignored). From here on you don't
rebuild — you **refresh** incrementally (see below).


## THE MENTAL MODEL

- One CLI, `f5kb`, with subcommands. `f5kb --help` lists them; `f5kb <sub> --help`
  shows a subcommand's flags.
- `outputs/dump/<Type>/<id>.json` is one article (metadata + content). The SQLite
  DB `outputs/articles.db` is a per-article overview used to tell what changed
  between runs.
- `dump`/`enrich`/`track` are the first-build pipeline. `sync` is the everyday
  refresher: it runs all three but only touches what changed.
- **SAFETY:** `sync`/`dump`/`enrich` never silently overwrite an article that already
  has good data. A changed article is "staged" for your review instead; you apply
  staged changes with `approve`. This protects you when an upstream page changes in a
  way that would make our copy worse (e.g. a reformat that yields an empty body). Pass
  `--yes` to skip review when you trust the source.
- `reconcile` is the only command that deletes anything on your side, and only when you
  ask it to with `--apply`.
- **SELECTING TYPES:** every type-aware command (dump/enrich/track/sync/reconcile/
  approve/recent) takes `--types=A,B` (include only these) and `--exclude-types=A,B`
  (drop these). We routinely exclude Community and F5_GitHub.


## TASK: refresh the index (the everyday command)

Pull upstream changes and update your local copy. `sync` skips unchanged articles,
re-fetches only what changed, updates the DB, and (under `--all`) reports anything
deleted upstream. The usual production run excludes Community and F5_GitHub:

```
uv run f5kb sync --all --exclude-types=Community,F5_GitHub
```

Everything (no exclusions):

```
uv run f5kb sync --all
```

Faster catch-up since your last run (no deletion scan — a date window can't prove an
article is gone):

```
uv run f5kb sync --since-last-run --exclude-types=Community,F5_GitHub
```

Just one or a few types:

```
uv run f5kb sync --all --types=Manual,Release_Note
```

Preview what a sync WOULD do without writing anything:

```
uv run f5kb sync --all --exclude-types=Community,F5_GitHub --dry-run
```

If `sync` reports "N edited article(s) STAGED for review", go to the next task.


## TASK: include or exclude document types

`--types` keeps only the listed types; `--exclude-types` drops them; both can be
combined (include is applied first, then exclude removes from the result, so exclude
wins on a conflict). With neither flag you get all configured types. This works the
same on dump, enrich, track, sync, reconcile, approve, and recent.

```
# build everything except Community + F5_GitHub (what we normally index)
uv run f5kb dump --all --out=outputs/dump --exclude-types=Community,F5_GitHub
uv run f5kb enrich --dump=outputs/dump --exclude-types=F5_GitHub
uv run f5kb track --dump=outputs/dump --exclude-types=Community,F5_GitHub

# only specific types
uv run f5kb sync --all --types=Manual,Release_Note,Bug_Tracker
```

Unknown type names are warned about and ignored, so a typo won't silently select
nothing — check the warning if a run touches fewer types than expected.


## TASK: review and apply staged changes

When `sync` (or `dump`/`enrich`) finds that an existing article CHANGED, it writes
the new version to `outputs/dump/_pending/<type>/<id>.json` and leaves your current
file untouched. Nothing is overwritten until you approve it.

1. See what's waiting:

   ```
   uv run f5kb approve --list
   ```

   Each line shows the article, what the edit touches in parentheses —
   `(metadata-only)`, `(content-only)`, or `(metadata+content)` — and any RISK flags
   (e.g. `body-dropped` means the new version would lose a body your current file has).

2. Compare a staged change against what you have now (any diff tool works):

   ```
   diff outputs/dump/Manual/K00012345.json \
        outputs/dump/_pending/Manual/K00012345.json
   ```

3. Apply the safe ones (risky edits are HELD BACK automatically):

   ```
   uv run f5kb approve
   ```

   Each replaced file is archived under `outputs/dump/_replaced/` first, then the DB
   is updated. Anything risky is left pending and listed. Every promotion is recorded
   to the changelog by default (op="edited", source="approve") — add `--no-changelog`
   to skip, or `--changelog=FILE` to redirect.

4. For a risky change you've checked and want anyway:

   ```
   uv run f5kb approve --include-risky --ids=K00012345
   ```

   Or apply ALL pending including risky:

   ```
   uv run f5kb approve --include-risky
   ```

5. To throw away staged changes and keep your current files:

   ```
   uv run f5kb approve --reject
   ```


## TASK: refresh without the review step (unattended / trusted)

For a cron job or when you trust the source, bypass staging — edits overwrite in
place (the replaced file is still archived to `_replaced/` so you can recover):

```
uv run f5kb sync --all --yes
```

The same `--yes` works on `dump` and `enrich`.


## TASK: recover an article you approved by mistake

Every overwrite (via approve or --yes) archives the previous version under
`outputs/dump/_replaced/<type>/<id>.<timestamp>.json`. Copy it back:

```
cp outputs/dump/_replaced/Manual/K00012345.2026-06-04T12-00-00Z.json \
   outputs/dump/Manual/K00012345.json
uv run f5kb track --dump=outputs/dump     # re-index so the DB matches
```


## TASK: handle articles deleted upstream

`sync --all` REPORTS deletions but never removes them. To act on them, use
`reconcile` — report-only by default:

```
uv run f5kb reconcile --all
```

When the report looks right, apply it. Files are archived to
`outputs/dump/_deleted/` (recoverable) and the DB rows are removed:

```
uv run f5kb reconcile --all --apply
```

Guards: reconcile aborts if deletions exceed 10% of a type's articles (raise with
`--max-delete-pct=N`) and backs up the DB first. Use `--purge` to hard-delete
instead of archiving.


## TASK: re-fetch a body that looks wrong

Enrich normally skips articles that already have a body. To force a re-fetch (e.g.
after a page was fixed upstream, or you mapped a new host):

```
uv run f5kb enrich --dump=outputs/dump --types=Manual --refetch
```

Because re-fetching overwrites good data, the results are STAGED for approval just
like a sync — review with `uv run f5kb approve --list`, then `uv run f5kb approve`.
Add `--yes` to overwrite in place instead.

To retry only the articles that previously errored (leaving good bodies alone):

```
uv run f5kb enrich --dump=outputs/dump --refetch-errors
```


## TASK: check health and see what changed

A read-only health report (per-type counts, body coverage, last run, staleness, the
last run's changelog tally, and any pending-approval count):

```
uv run f5kb status
```

Every change a command makes can be recorded to a JSONL changelog (on by default for
`sync`, opt-in elsewhere with `--changelog`). Read the most recent entries:

```
tail outputs/dump/_changelog.jsonl
```

Each line is one JSON object: `{runId, ts, op, documentType, id, ...}`. ops are
`added` / `edited` / `deleted` / `body-added` / `body-changed` / `body-error`.
See README.md "Changelog format" for the full schema.


## TASK: build or fix just one type

Dump a single type (e.g. after a partial run flagged a shortfall):

```
uv run f5kb dump --all --out=outputs/dump --types=Bug_Tracker
```

Enrichment failures are written to `outputs/dump/_enrich_report.json`. After fixing
a host rule or parser, re-process only the failures:

```
uv run f5kb enrich --dump=outputs/dump --refetch-errors
```

List the available type keys and product names:

```
uv run f5kb list-types
uv run f5kb list-products
```


## TASK: find an article in the dump

Files are grouped by type and named by id, so plain tools work:

```
ls outputs/dump/Manual | head
grep -rl "BIG-IP DNS" outputs/dump/Manual        # files mentioning a term
```

Or query the DB via status:

```
uv run f5kb status --json | python3 -m json.tool
```


## TASK: explore without the full pipeline

A lighter, flat fetch by product/type (predates the pipeline; good for one-off
pulls). Writes a flat JSON array (+ optional CSV):

```
uv run f5kb fetch --product="NGINX Plus" --type="Security Advisory" --csv=nginx.csv
```


## TIPS & GOTCHAS

- **First build vs refresh.** A first `dump` into an empty directory is all-new, so
  it writes everything with nothing to approve. Re-dumps/syncs over an existing copy
  only stage genuine edits.
- **"Changed" = the article's `metadata_hash` differs** (metadata includes the
  published/updated dates). A body-only upstream change that bumps no date won't be
  detected automatically — force it with `enrich --refetch` if you suspect one.
- **The live corpus drifts between runs; that's normal.** A dump validates
  written-vs-server per type and marks `partial`; re-run just those with `--types=`.
- **`outputs/` is gitignored** (large, regenerable). Commit code, config.yaml, and
  the docs — not the dump or the DB.
- **Logs/progress go to stderr; `--json` output goes to stdout**, so you can pipe a
  `--json` payload cleanly.
- Stuck or unsure what a flag does? `uv run f5kb <subcommand> --help`.
