F5 KB Article Index Toolkit (f5kb)
==================================

Usage guide for `f5kb`, a single-command CLI that builds and maintains a local,
full-fidelity index of F5 Knowledge Base articles (metadata + full body text) for
every document type, with no login.

The whole toolkit is one entry point, `f5kb.ts`, with subcommands. There is no
public REST API for my.f5.com; everything runs over the Coveo guest-token search
backend (a token is fetched at runtime — no key, no login). See FINDINGS.txt for
the technical detail behind every limit and workaround mentioned here.

REQUIREMENTS
------------
  - Deno 2.x (https://deno.com). This is not Node; scripts are TypeScript and run
    with `deno run`/`deno task`. Type-check with `deno task check`.
  - Internet access to my.f5.com and f5networksproduction5vkhn00h.org.coveo.com.
  - No login or API key required — a guest token is fetched automatically and
    refreshed if it expires mid-run.
  - Optional GITHUB_TOKEN env (raises the GitHub API limit for `f5kb enrich` on
    F5 GitHub articles); pass `--allow-env` when set.
  - External deps are fetched by URL on first run: `jsr:@std/yaml`,
    `jsr:@std/assert`, `jsr:@std/testing`, `jsr:@b-fuze/deno-dom`. `node:sqlite` is
    built into Deno (no install).

QUICK START — THE PIPELINE
--------------------------
The production flow is three subcommands in order: dump (metadata + indexed body)
-> enrich (fill bodies the index omits) -> track (master overview + change
tracking). Each reads the dump directory, so enrich and track can re-run anytime
without re-hitting the search API.

Using `deno task` (preferred — permissions are baked into deno.json):

    deno task dump --all --out=outputs/dump
    deno task enrich --dump=outputs/dump
    deno task track --dump=outputs/dump
    deno task status

The equivalent raw `deno run` forms (explicit permissions):

    deno run --allow-net --allow-read --allow-write f5kb.ts dump --all --out=outputs/dump
    deno run --allow-net --allow-read --allow-write --allow-env f5kb.ts enrich --dump=outputs/dump
    deno run --allow-read --allow-write f5kb.ts track --dump=outputs/dump
    deno run --allow-read f5kb.ts status

Generated data lives under `outputs/` (e.g. `outputs/dump/` and the SQLite
`outputs/articles.db`) and is git-ignored. Pass `--out`/`--dump`/`--db` to override.

Run `f5kb --help` for the subcommand list and global flags; `f5kb <sub> --help`
prints that subcommand's flag synopsis.

DENO TASKS
----------
deno.json defines these shortcuts (each is `deno run <perms> f5kb.ts <sub>`):

  Task            Wraps
  --------------  ------------------------------------------------------------
  deno task f5kb  the bare CLI (all perms; pass any subcommand)
  deno task dump  f5kb dump
  deno task enrich  f5kb enrich (includes --allow-env for GITHUB_TOKEN)
  deno task track  f5kb track
  deno task status  f5kb status
  deno task discover  f5kb discover
  deno task check  deno check over f5kb.ts + cmd/*.ts + lib/**/*.ts
  deno task test   offline test suite (deno test --allow-read)
  deno task test:live  opt-in live tests (F5_LIVE=1, hits the network)
  deno task fmt    deno fmt
  deno task lint   deno lint

GLOBAL FLAGS
------------
These apply to every subcommand (parsed before dispatch):

  --verbose      debug-level logging
  --debug        trace-level logging
  --quiet        warn-level logging only
  --json-logs    emit logs as NDJSON (one JSON object per line)
  --help, -h     show usage (bare) or the subcommand's flag synopsis
  --version      print the version and exit

Output discipline: human-readable progress and logs go to STDERR; machine output
(any `--json` payload) goes to STDOUT, so `f5kb track --json > out.json` captures
only the JSON.

SUBCOMMAND — dump
-----------------
Full-fidelity dumper. Writes ONE JSON file per article, grouped by document type,
splitting each article's fields into "metadata" vs "content" objects per
config.yaml. Also emits a per-type field catalogue.

Flags:

  --days=N         Only dump articles modified in the last N days.
  --all            Dump the entire corpus (no lower date bound). Provide exactly
                   one of --days or --all. With --all, the written count is
                   validated against the server count per type and shortfalls are
                   flagged.
  --out=DIR        REQUIRED. Output directory (created if missing).
  --config=FILE    Config YAML (default: config.yaml).
  --types="A,B"    Subset of config type keys (default: all in config).
  --page-size=N    Results per API call (default: 200, max: 500). Coveo caps each
                   response at 20 MB; if a page exceeds that, the page size is
                   halved for that request and retried, so large content types
                   degrade gracefully.
  --limit=N        Cap articles per type (default: none). For testing.
  --fields-doc=F   Deprecated no-op (catalogue annotations now come from
                   config.yaml's field_descriptions: section).

Example — dump the entire corpus:

    deno task dump --all --out=outputs/dump

Example — last 7 days, one type only:

    deno task dump --days=7 --out=outputs/dump --types=Support_Solution

Output layout:

    dump/
      _index.json                  manifest (window, counts, per-type status)
      Support_Solution/
        _catalogue.json            every field seen + source/type/coverage/sample
        _catalogue.md              same, as a readable table
        K000161535.json            one file per article (named by KB id)
        ...

Each per-article file:

    {
      "id": "K000161535",
      "documentType": "Support Solution",
      "title": "K000161535: ...",
      "link": "https://my.f5.com/manage/s/article/K000161535",
      "modifiedMs": 1780430428000,
      "modified": "2026-06-02T20:00:28.000Z",
      "capturedAt": "2026-06-02T...Z",
      "metadata": { ...selected fields... },
      "content":  { "sfdetails__c": "<full HTML body>" }
    }

Resilience: the guest token is auto-refreshed on expiry (401/419); each type is
isolated so one type's failure does not abort the others; `_index.json` records
per-type status (ok/partial/failed) with written-vs-server counts, and the run
exits non-zero if any type failed (re-run just those with --types=...).

SUBCOMMAND — enrich
-------------------
Post-processes a dump directory to fill in article BODIES for the five types whose
body is absent from the Coveo search index (their `content` is left empty):
Bug_Tracker, F5_GitHub, Manual, Release_Note, Supplemental_Document. Fetches each
article's public page and extracts ONLY the body (no site chrome, nothing that just
repeats metadata), writing `content.sections` + `content.body_text` (plus
`content.bodySource` and `content.fetchedAt`) back into the per-article JSON.

Flags:

  --dump=DIR        Dump directory to enrich (default: outputs/dump).
  --types="A,B"     Subset of the enrichable types (default: all five).
  --concurrency=N   Parallel fetches (default: 4).
  --delay-ms=N      Per-worker delay between fetches (default: 200).
  --limit=N         Cap articles per type (testing).
  --refetch         Re-fetch even articles that already have a body or bodyError.
  --refetch-errors  Re-process only articles that recorded a content.bodyError
                    (already-bodied articles stay skipped). Use after mapping a
                    new host or fixing a parser.

Env: GITHUB_TOKEN (pass --allow-env) raises the GitHub API limit from 60 to 5,000
req/hr for F5_GitHub enrichment.

Example:

    deno task enrich --dump=outputs/dump --types=Bug_Tracker,Manual

How bodies are recovered, by type:

  - Bug_Tracker: deterministic cdn.f5.com page; two templates handled (standard
    narrative + security/CVE).
  - F5_GitHub: GitHub REST API (issues, pulls, repo-root README, /blob/ raw
    files) — not HTML.
  - Manual / Release_Note / Supplemental_Document: doc-page scrape driven by a
    host->selector map (clouddocs.f5.com, techdocs.f5.com, docs.nginx.com,
    nginx.org, unit.nginx.org) with a generic fallback and a last-resort
    <pre>/<body> fallback for plain pages. docs.cloud.f5.com (Next.js) renders
    its body client-side, so it is read from the embedded <script
    id="__NEXT_DATA__"> JSON instead of the DOM — no headless browser needed.

Edge cases are recorded as `content.bodyError` (never captured as a body): soft
404s (HTTP 200 "Page Not Found"), moved-to-landing redirects, and docs.nginx.com
URLs that 302 into the F5 KB (captured under the Salesforce type instead).
Resumable: an article is skipped if it already has body_text or a bodyError. Each
run writes `outputs/dump/_enrich_report.json` (per-type enriched/failed/skipped +
the errored articles).

SUBCOMMAND — track
------------------
Maintains a master overview of every dumped article in an embedded SQLite DB
(default outputs/articles.db): one row per article with its several dates and a
hash of the metadata and of the content/body. On each run it classifies articles
new/changed/unchanged/removed vs the prior run and logs every change. Run it AFTER
enrich so bodies are included in the content hash.

Flags:

  --dump=DIR       Dump directory to index (default: outputs/dump).
  --db=FILE        SQLite file (default: <dump>/../articles.db).
  --types="A,B"    Subset of document types (scopes "removed" detection too).
  --run-id=ID      Label for this run (default: a timestamp).
  --json           Emit the run summary as JSON on STDOUT.

Example:

    deno task track --dump=outputs/dump

Per-article row: id, document_type, title, link; the dates created_ms /
original_published_ms / updated_published_ms / modified_ms / captured_at;
metadata_hash and content_hash (SHA-256 over canonicalized JSON, with volatile
keys bodySource/fetchedAt excluded so a re-fetch isn't a "change"); has_body;
body_error; first_seen_run / last_seen_run / last_changed_run. Changes are logged
to a `changes` table and a per-run summary to a `runs` table. Removed articles
(rows in the scanned types absent from this dump) are logged, not deleted.

SUBCOMMAND — status
-------------------
Read-only health report for a dump and its tracking DB. Aggregates four sources,
each handled gracefully when absent: `<dump>/_index.json` (per-type
expected/written/status), `<dump>/_enrich_report.json` (per-type
enriched/failed/skipped), on-disk per-type file counts, and the tracking DB
(articles/runs tables). Never writes.

Flags:

  --dump=DIR       Dump directory (default: outputs/dump).
  --db=FILE        SQLite file (default: <dump>/../articles.db).
  --json           Emit the report as JSON on STDOUT instead of a table.

Example:

    deno task status

SUBCOMMAND — fetch
------------------
Lighter-weight exploratory fetch (predates the pipeline). Fetches articles by
product and/or type into a flat JSON array, optionally also a CSV. Each article is
the five stable fields: name, link, summary, publicationDate, modificationDate.

Flags:

  --product=NAME   Filter by product (e.g. "BIG-IP", "NGINX Plus", "F5OS").
  --type=NAME      Filter by document type (e.g. "Support Solution").
  --limit=N        Stop after N articles (default: all).
  --output=FILE    JSON output (default: auto-named, e.g. f5_NGINX_Plus_...json).
  --csv=FILE       Also write a CSV file (optional).
  --page-size=N    Results per call (default: 100, max: 1000).

Example — NGINX Plus Security Advisories, JSON + CSV:

    deno run --allow-net --allow-write f5kb.ts fetch \
        --product="NGINX Plus" --type="Security Advisory" \
        --csv=nginx_security.csv

For valid product values, see config.yaml's `products:` section (or run `f5kb
list-products` for the global facet; `f5kb discover` for the full set). The 5,000-
offset cap is handled automatically via date-range chunking; results are deduped.

SUBCOMMAND — recent
-------------------
Fetches articles modified within the last N days and writes one JSON file per
document type into a chosen directory, plus an _index.json manifest. Exploratory;
prefer `dump --days=N` for the full-fidelity pipeline.

Flags:

  --days=N         REQUIRED. Window size: articles modified in the last N days.
  --out=DIR        REQUIRED. Output directory (created if missing).
  --types="A,B"    Subset of document types (default: all).
  --page-size=N    Results per call (default: 500, max: 1000).
  --limit=N        Cap articles per type (testing).

Example:

    deno run --allow-net --allow-write f5kb.ts recent --days=7 --out=last_week

Each per-type file: {documentType, days, cutoff, generatedAt, count, articles[]}
where each article is {name, link, summary, publicationDate, modificationDate}.
The window is enforced server-side on Coveo's @date (a re-index superset) and then
refined client-side on the per-record modification timestamp, so the output
strictly honours N days. The 5,000-offset cap is handled via date-range chunking.

SUBCOMMAND — list-types
-----------------------
Prints all document types with their article counts (from the global facet). No
flags.

    deno run --allow-net f5kb.ts list-types

SUBCOMMAND — list-products
--------------------------
Prints the products known to the global Coveo facet (~73, fast) with counts. No
flags. The global facet is incomplete — many valid products are hidden from it;
use `f5kb discover` for the full set.

    deno run --allow-net f5kb.ts list-products

SUBCOMMAND — discover
---------------------
Deep product discovery. The Coveo global facet returns only ~73 top-level
products; ~247 more valid product names are hidden from it by F5's admin config
but remain queryable with --product=. This subcommand surfaces them by running a
type-filtered facet query per document type, then a count query for each hidden
product. Takes ~3-4 minutes (~250 API calls).

Flags:

  --out=FILE       Output file (default: discovered_products.yaml).
  --format=yaml|json  Output format (default: yaml).

    deno run --allow-net --allow-write f5kb.ts discover

It writes a side-file (NOT config.yaml). The file uses the same `products:` schema
as config.yaml (generatedAt + entries:). To refresh the curated snapshot, copy the
`products:` block from discovered_products.yaml into config.yaml by hand — discover
never edits config.yaml. See the config section below and FINDINGS.txt "Product
discovery" for why the global facet is incomplete, the BIG-IP Documentation
TechComm source, and the confirmed duplicate product tag pairs.

Each entry:

    {"product": "BIG-IP TMOS", "count": 3570, "source": "type_filtered_facet",
     "hiddenFromGlobalFacet": true, "discoveredViaTypes": ["Bug Tracker"]}

    {"product": "BIG-IP", "count": 48453, "source": "global_facet",
     "hiddenFromGlobalFacet": false}

Fields: product (use as --product value), count (total across all doc types),
source (how it was found), hiddenFromGlobalFacet, discoveredViaTypes (for hidden
products: which document types revealed it).

CONFIG.YAML
-----------
config.yaml is the single source of truth, with three sections:

  - types:  one entry per document type, read by `f5kb dump`. Each entry:
      documentType  Exact Coveo f5_document_type value (what the API filters on).
      metadata      Fields routed to the entry's "metadata" object.
      content       Fields routed to the entry's "content" object.
    metadata / content each accept either "*" (include every field returned by the
    API) or a [a, b, c] keep-list (matched by bare name against both the top-level
    result object and the raw field bag; top-level wins on a name clash; content
    wins over metadata on overlap). The shipped config covers all 15 types with a
    curated metadata keep-list and the correct body field per backend (Salesforce
    -> sfdetails__c, Community -> limessagebody, Education -> zendeskdescription;
    Manual/Release Note/Supplemental Document/F5 GitHub/Bug Tracker -> content: []
    because the index returns no body — those are filled by `f5kb enrich`).

  - field_descriptions:  field-name -> short description, used only to annotate the
    dump catalogue. This is the machine copy of FINDINGS.txt Appendix A.

  - products:  a READ-ONLY snapshot of discovered products (was a separate file).
    The pipeline does NOT read this section; it is a reference for valid --product
    values. Refresh it via `f5kb discover` (writes discovered_products.yaml; copy
    its products: block in).

Workflow to curate a new type: set metadata: "*", run `f5kb dump` once, open that
type's _catalogue.md to see every field with coverage/description/sample, then
replace "*" with an explicit keep-list of the fields you want.

COVEO API LIMITS (handled automatically)
----------------------------------------
The Coveo backend enforces two hard limits; the toolkit works around both. See
FINDINGS.txt for the full technical explanation.

  1. firstResult + numberOfResults cannot exceed 5,000. When a result set is
     larger, `dump --all` uses keyset pagination by @rowid (no offset cap, and it
     captures docs a date window would miss); `dump --days`, `fetch`, and `recent`
     use recursive date-range chunking.

  2. A single response cannot exceed 20 MB. The dump halves the page size for any
     request that exceeds it and retries; the flat fetchers request only the
     fields they use, keeping responses small.

Other notes:
  - The guest token is valid ~24h and is auto-refreshed mid-run on 401/419.
  - Results are sorted newest first (by Coveo index @date) except `dump --all`,
    which sorts by @rowid for keyset paging.
  - The live corpus drifts between runs; counts changing is expected. `dump`
    validates written-vs-server and `track` records the delta.
  - Field availability varies by document type (three source backends:
    Salesforce Knowledge, non-SF connectors, Zendesk). See FINDINGS.txt Appendix A.

DOCUMENTATION MAP
-----------------
  README.txt     This file — the CLI usage guide (subcommands, flags, examples,
                 outputs, config, API limits).
  OUTLINE.txt    Our code: module tree, the dump->enrich->track flow, the
                 dependency-injection design, pagination strategy, testing,
                 decisions, and obstacles overcome.
  FINDINGS.txt   Discoveries about the scraped system (Coveo token flow, API
                 limits, field meanings, counts, deprecation/lifecycle). Appendix A
                 is the full field inventory; the sitemap gap analysis is in its
                 "Sitemap" section.
  TODO.txt       Open work (the 47-article sitemap gap follow-up; the deferred
                 skip-unchanged-bodies idea).
  CLAUDE.md      Orientation for Claude Code working in this repo (+ the .txt-file
                 formatting rules).
  config.yaml    Machine config the CLI reads (types: + field_descriptions: +
                 products:).
