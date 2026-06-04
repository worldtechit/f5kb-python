F5 KB Article Index Fetcher
===========================

Scripts (typical full-dump pipeline: dump_articles -> enrich_bodies -> track_articles):

  fetch_f5_articles.ts       Hardcoded to BIG-IP + Support Solution.
                             Simple, no configuration needed.

  fetch_f5_articles_flex.ts  Supports any product and content type via
                             --product and --type flags. Also adds
                             --list-products, --list-types, and
                             --discover-products discovery modes.

  discover_products.ts       Standalone version of --discover-products.
                             Same output as the flag; useful if you want
                             to run the discovery independently.

  fetch_recent_by_type.ts    Fetches articles modified within the last N days
                             and writes one JSON file per document type into a
                             chosen output directory. See its own section below.

  dump_articles.ts           Full-fidelity dumper: writes ONE JSON file per
                             ARTICLE, grouped by document type, splitting each
                             article's fields into "metadata" vs "content"
                             objects driven by dump_config.yaml. Also emits a
                             per-type field catalogue. See its own section below.

  enrich_bodies.ts           Post-processes a dump directory to fill in article
                             BODIES for the types whose body is absent from the
                             Coveo search index (content was left empty). Fetches
                             each article's public page and extracts ONLY the body
                             (no site chrome, nothing that just repeats metadata),
                             writing content.sections + content.body_text back into
                             the per-article JSON. Covers all 5 empty-body types
                             (Bug Tracker, F5 GitHub, Manual, Release Note,
                             Supplemental Document). See its own section below.

  track_articles.ts          Maintains a master overview of every dumped article
                             in an embedded SQLite DB (outputs/articles.db): one
                             row per article with its several dates + a hash of
                             the metadata and a hash of the content/body. On each
                             run it classifies articles new/changed/unchanged/
                             removed vs the prior run and logs every change. See
                             its own section below.

Both fetch scripts handle all known Coveo API limits automatically (see NOTES below).

DOCUMENTATION MAP
-----------------
  readme.txt     This file — usage: every script, its flags, examples, output.
  outline.md     Our code: script flows, strategies, decisions, obstacles overcome.
  findings.md    Discoveries about the scraped system (Coveo token flow, API limits,
                 field meanings, counts, deprecation/lifecycle).
  CLAUDE.md      Orientation for Claude Code working in this repo.
  TODO.txt       Open work.
  available_fields.txt  Field-name -> description reference (feeds the catalogue).
  sitemap_note.txt      Notes on the my.f5.com sitemap (alternative discovery path).
  sitemap_gap_ids.txt   The 47 K-article IDs in the sitemap but absent from Coveo
                        (see findings.md "Sitemap" for method + analysis).

OUTPUT LOCATIONS
----------------
Generated data lives under outputs/ (e.g. outputs/dump and the SQLite
outputs/articles.db) and is git-ignored. Pass --out/--dump to override.

ENRICH_BODIES.TS (article bodies for empty-content types)
---------------------------------------------------------
    deno run --allow-net --allow-read --allow-write --allow-env enrich_bodies.ts \
        --dump=outputs/dump --types=Bug_Tracker,F5_GitHub

  Walks outputs/dump/<Type>/*.json, fetches each article's page, and writes the
  extracted body into content.sections (title -> markdown) and content.body_text,
  plus content.bodySource and content.fetchedAt. Resumable: an article is skipped
  if it already has body_text or a recorded bodyError (use --refetch to force).
  Flags: --concurrency=N (default 4), --delay-ms=N (default 200), --limit=N.

  Implemented types: Bug_Tracker, F5_GitHub, Manual, Release_Note,
  Supplemental_Document.
    - F5_GitHub: set GITHUB_TOKEN (and pass --allow-env) to raise the GitHub API
      limit from 60 to 5,000 req/hr; handles /issues, /pull, repo-root README,
      and /blob/ raw-file links.
    - Manual/Release_Note/Supplemental_Document: doc-page scrape driven by the
      HOST_RULES host->selector map (clouddocs.f5.com, techdocs.f5.com,
      docs.nginx.com, nginx.org, unit.nginx.org) with a generic fallback for
      unmapped hosts (logged) and a last-resort <pre>/<body> fallback for plain
      pages (e.g. nginx.org changelogs). Extracts ONLY the content container as
      markdown (headings/lists/code/links).
      docs.cloud.f5.com (Next.js) renders its body client-side, so it is read
      from the page's embedded <script id="__NEXT_DATA__"> JSON instead of the
      DOM (prose from docData.compiledSource, API specs from docData.swaggerFile)
      — no headless browser needed.
      Edge cases recorded as content.bodyError (not captured as body): soft 404s
      (HTTP 200 "Page Not Found"), moved-to-landing redirects, and docs.nginx.com
      URLs that 302 into the F5 KB (my.f5.com/article/K…, body captured under the
      Salesforce type). Bug Tracker has two page templates (standard narrative +
      security/CVE) — both handled. Full-corpus Manual body coverage: 99% (the
      remainder are legitimately bodiless: dead links, moved content, image-only
      or empty stub pages).

  Re-running after mapping a new host: use --refetch-errors to re-process only
  the articles that previously recorded a content.bodyError (already-bodied
  articles stay skipped). Each run writes outputs/dump/_enrich_report.json
  (per-type enriched/failed/skipped + the list of errored articles).

TRACK_ARTICLES.TS (master overview / change tracking)
-----------------------------------------------------
    deno run --allow-read --allow-write track_articles.ts --dump=outputs/dump

  Walks the dump and upserts one row per article into an embedded SQLite DB
  (default outputs/articles.db) with: id, document_type, title, link; the dates
  created_ms / original_published_ms / updated_published_ms / modified_ms /
  captured_at; metadata_hash and content_hash (SHA-256 over canonicalized JSON,
  with volatile keys bodySource/fetchedAt excluded from the content hash);
  has_body; body_error; and first_seen_run / last_seen_run / last_changed_run.
  Compares against the stored row and classifies each article new / changed /
  unchanged, logging changes to a `changes` table and a per-run summary to a
  `runs` table. Removed articles (rows in the scanned types absent from this
  dump) are logged, not deleted.
  Flags: --db=FILE, --types="A,B" (subset), --run-id=ID, --json.
  Run it AFTER enrich_bodies.ts so bodies are included in the content hash.

REQUIREMENTS
------------
- Deno (https://deno.com) must be installed
- Internet access to my.f5.com and f5networksproduction5vkhn00h.org.coveo.com
- No login or API key required — the script fetches a guest token automatically

QUICK START — fetch_f5_articles.ts (BIG-IP Support Solutions only)
------------------------------------------------------------------
Fetch the first 500 articles (useful for testing):

    deno run --allow-net --allow-write fetch_f5_articles.ts \
        --limit=500 \
        --output=f5_articles_500.json \
        --csv=f5_articles_500.csv

Fetch ALL BIG-IP Support Solution articles (~11,000+):

    deno run --allow-net --allow-write fetch_f5_articles.ts \
        --page-size=500 \
        --output=all_articles.json \
        --csv=all_articles.csv

OPTIONS (fetch_f5_articles.ts)
------------------------------
  --limit=N        Stop after N articles. Omit to fetch all.
  --output=FILE    JSON output file (default: f5_articles.json)
  --csv=FILE       Also write a CSV output file (optional)
  --page-size=N    Results per API call (default: 100, max: 1000)

QUICK START — fetch_f5_articles_flex.ts (any product / content type)
--------------------------------------------------------------------
List all available document types with counts:

    deno run --allow-net fetch_f5_articles_flex.ts --list-types

List products known to the global facet (~73, fast):

    deno run --allow-net fetch_f5_articles_flex.ts --list-products

Discover ALL products including hidden ones (~321, writes supplemental_products.json):

    deno run --allow-net --allow-write fetch_f5_articles_flex.ts --discover-products

Fetch NGINX Plus Security Advisories:

    deno run --allow-net --allow-write fetch_f5_articles_flex.ts \
        --product="NGINX Plus" --type="Security Advisory" \
        --csv=nginx_security.csv

Fetch all BIG-IP APM Release Notes:

    deno run --allow-net --allow-write fetch_f5_articles_flex.ts \
        --product="BIG-IP APM" --type="Release Note" \
        --csv=apm_relnotes.csv

Fetch everything for a product (all content types):

    deno run --allow-net --allow-write fetch_f5_articles_flex.ts \
        --product="F5OS" \
        --output=f5os_all.json

OPTIONS (fetch_f5_articles_flex.ts)
------------------------------------
  --product=NAME       Filter by product (e.g. "BIG-IP", "NGINX Plus", "F5OS")
  --type=NAME          Filter by document type (e.g. "Support Solution", "Release Note")
  --limit=N            Stop after N articles. Omit to fetch all.
  --output=FILE        JSON output file (default: auto-named from filters,
                       e.g. f5_NGINX_Plus_Security_Advisory.json).
                       Also controls output path for --discover-products
                       (default: supplemental_products.json).
  --csv=FILE           Also write a CSV output file (optional)
  --page-size=N        Results per API call (default: 100, max: 1000)
  --list-types         Print all document types with counts and exit
  --list-products      Print products from the global facet and exit (fast,
                       ~73 products; prints a reminder about --discover-products)
  --discover-products  Deep scan: queries every document type to surface products
                       hidden from the global facet. Writes supplemental_products.json
                       (or --output path). Takes ~3-4 min (~250 API calls).
                       See PRODUCT DISCOVERY below.

PRODUCT DISCOVERY
-----------------
The Coveo global facet only returns ~73 top-level product names. A further
~247 valid product names are hidden from the global facet by F5's Coveo admin
configuration. These hidden products are still queryable with --product= but
will not appear in --list-products output.

--discover-products works around this by running a type-filtered facet query
for each document type. Products excluded from the global facet become visible
when the facet is computed over the narrower set of documents matching a
specific type. After collecting all names, it runs a count query for each
hidden product to determine its total article count.

Known gaps in automated discovery:
- "BIG-IP Documentation" (1,654 articles) uses a TechComm/Sitemap source
  connector and is excluded from all facets by Coveo admin config. It was
  added manually to supplemental_products.json (source: "manual_query").
  Query directly with @f5_version=="BIG-IP Documentation".
- Two confirmed duplicate tag pairs exist — every article carries both names
  simultaneously: "BIG-IP Next CNF" / "BIG_IP_NEXT(CNF)" (1,288 articles)
  and "APM Clients" / "APM-Clients" (811 articles). Both names remain in
  supplemental_products.json as both are valid filter values; prefer the
  clean name (no underscores/hyphens) when filtering.

Output: supplemental_products.json — one entry per product:

  {
    "product": "BIG-IP TMOS",
    "count": 3570,
    "source": "type_filtered_facet",
    "hiddenFromGlobalFacet": true,
    "discoveredViaTypes": ["Bug Tracker"]
  }

  {
    "product": "BIG-IP",
    "count": 48453,
    "source": "global_facet",
    "hiddenFromGlobalFacet": false
  }

Fields: product (use as --product value), count (total across all doc types),
source (how it was found), hiddenFromGlobalFacet, discoveredViaTypes (for
hidden products: which document types revealed it).

This file can be used as a reference for all valid --product values instead
of relying on --list-products. Run it periodically to pick up new products
as F5 adds content to the portal.

See findings.md for a full technical explanation of why the global facet is
incomplete, how the type-filtered technique works, the BIG-IP Documentation
TechComm source, and the confirmed duplicate product tag pairs.

QUICK START — fetch_recent_by_type.ts (recently-modified, split per type)
------------------------------------------------------------------------
Fetch everything modified in the last 7 days, one file per document type:

    deno run --allow-net --allow-write fetch_recent_by_type.ts \
        --days=7 --out=last_week

Limit to specific document types:

    deno run --allow-net --allow-write fetch_recent_by_type.ts \
        --days=30 --out=last_month \
        --types="Support Solution,Release Note,Security Advisory"

OPTIONS (fetch_recent_by_type.ts)
---------------------------------
  --days=N         REQUIRED. Window size: articles modified in the last N days.
  --out=DIR        REQUIRED. Output directory (created if missing). One JSON
                   file is written per document type (e.g. Support_Solution.json,
                   Release_Note.json), plus an _index.json manifest summarising
                   counts across all types.
  --types="A,B"    Comma-separated subset of document types. Default: all types.
                   Use fetch_f5_articles_flex.ts --list-types for valid names.
  --page-size=N    Results per API call (default: 500, max: 1000).
  --limit=N        Cap articles per type (default: no cap). Mainly for testing.

Each per-type file looks like:

    {
      "documentType": "Support Solution",
      "days": 7,
      "cutoff": "2026-05-26T...Z",
      "generatedAt": "2026-06-02T...Z",
      "count": 57,
      "articles": [ { name, link, summary, publicationDate, modificationDate }, ... ]
    }

HOW "MODIFIED WITHIN N DAYS" IS DETERMINED
------------------------------------------
The query filters server-side on Coveo's @date (index) field, which the F5
index bumps whenever an article is re-indexed, so it tracks modifications and
is the only date field present on all document types. Because an article can be
re-indexed without its content-modified date changing, @date is treated as a
superset and an exact client-side filter is then applied on the per-record
modification timestamp (f5_updated_published_date -> sflastmodifieddate -> date)
so the output strictly honours the requested window. Coveo's 5,000-result
offset cap is handled automatically via recursive date-range chunking (a type
like Manual exceeds 5,000 in a 30-day window).

QUICK START — dump_articles.ts (full per-article dump, config-driven)
--------------------------------------------------------------------
Dump every Support Solution modified in the last 7 days, one file per article:

    deno run --allow-net --allow-read --allow-write dump_articles.ts \
        --days=7 --out=outputs/dump --types=Support_Solution

Dump the ENTIRE corpus for a type (no date window) with --all:

    deno run --allow-net --allow-read --allow-write dump_articles.ts \
        --all --out=outputs/dump --types=Support_Solution

Output layout:

    dump/
      _index.json                     manifest (window, counts, per-type dirs)
      Support_Solution/
        _catalogue.json               every field seen + source/type/coverage/sample
        _catalogue.md                 same, as a readable table
        K000161535.json               one file per article (named by KB id)
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

OPTIONS (dump_articles.ts)
--------------------------
  --days=N         Only dump articles modified in the last N days.
  --all            Dump the entire corpus (no lower date bound). Provide one of
                   --days or --all. With --all the script validates the written
                   count against the server count per type and flags shortfalls.
  --out=DIR        REQUIRED. Output directory (created if missing).
  --config=FILE    Config YAML (default: dump_config.yaml).
  --fields-doc=F   Field-description reference used to annotate the catalogue
                   (default: available_fields.txt). Optional.
  --types="A,B"    Subset of config type keys to dump (default: all in config).
  --page-size=N    Results per API call (default: 200, max: 500). Coveo caps
                   each response at 20 MB; if a page exceeds that, the script
                   automatically halves the page size for that request and
                   retries, so large content types degrade gracefully.
  --limit=N        Cap articles per type (default: no cap). For testing.

  Resilience: the guest Coveo token is auto-refreshed if it expires mid-run
  (401/419); each type is isolated so one type's failure does not abort the
  others; _index.json records per-type status (ok/partial/failed) with the
  written-vs-server counts, and the script exits non-zero if any type failed
  (re-run just those with --types=...).

CONFIGURING WHICH FIELDS ARE KEPT (dump_config.yaml)
----------------------------------------------------
dump_config.yaml has one entry per document type. For each type:

  documentType   Exact Coveo f5_document_type value (what the API filters on).
  metadata       Fields routed to the entry's "metadata" object.
  content        Fields routed to the entry's "content" object.

metadata / content each accept either:
  "*"            include every field returned by the API (the other section's
                 fields are removed from this one).
  [ a, b, c ]    include only the named fields (matched by bare name against
                 both the top-level result object and the raw field bag;
                 top-level wins on a name clash). content takes precedence: a
                 field named in content never also appears in metadata.

Recommended workflow:
  1. Set metadata: "*" for a new type and run once.
  2. Open that type's _catalogue.md to see every field, its coverage, a
     description, and a sample value.
  3. Replace "*" with an explicit keep-list of the fields you actually want.

The shipped config covers ALL 15 document types, each with a curated metadata
keep-list and its correct body field. Body field varies by source backend:

  Salesforce Knowledge types (Support Solution, Known Issue, Knowledge,
    Security Advisory, Operations Guide, Policy, Video, Compliance) -> sfdetails__c
  Community (Khoros/Lithium forum)                                  -> limessagebody
  Education (Zendesk)                                               -> zendeskdescription
  Manual, Release Note, Supplemental Document, F5 GitHub, Bug Tracker -> content: []

The last group's full body text is NOT returned by the Coveo search index
(these are TechComm/sitemap- or tracker-sourced), so only metadata + the short
excerpt are captured for them. f5_lifecycle (release/archived) is kept for the
three documentation types that carry it (Manual, Release Note, Supplemental
Document). Per-article metadata counts may be lower than the config list because
a configured field is only written when present on that article.

PLANNED: populating bodies for the content: [] types (Manual, Bug Tracker,
F5 GitHub, Release Note, Supplemental Document) by fetching each article's public
rendered page / API is scoped in TODO.txt. Their bodies are reachable (the
article `link` resolves to a public HTTP-200 page; Bug Tracker has a
deterministic cdn.f5.com URL; F5 GitHub uses the GitHub REST API). See TODO.txt
for the per-type plan, known doc-host content selectors, and risks.

ARTICLE DEPRECATION / LIFECYCLE
-------------------------------
The only reliable "is this document deprecated?" signal in the index is the
@f5_lifecycle field, with values "release" vs "archived". It is present ONLY on
the documentation content types:

  Manual                (~6,180 release / ~399 archived)
  Release Note          (~391  release / ~220 archived)
  Supplemental Document (~63   release /   ~4 archived)

Support Solution (KB) articles have NO deprecation field: f5_status is always
"Online", and f5_archived is a non-discriminating UI-filter label ("Archived
documents excluded" or absent), NOT a per-article archived flag. A KB article
being superseded is only ever noted in its body text, if at all.

To capture deprecation when you add the documentation types to dump_config.yaml,
include f5_lifecycle in their metadata list. To fetch only live (or only
deprecated) docs, filter the query:  @f5_lifecycle=="release"  /  =="archived".

OUTPUT FORMAT (both BIG-IP/flex fetch scripts)
-----------------------------
Each article has:
  name              Full article title, e.g.:
                      K000160969: Configsync stops working after implementing jumbo frames
  link              Direct article URL, e.g.:
                      https://my.f5.com/manage/s/article/K000160969
  summary           Excerpt/summary from the search index
  publicationDate   e.g.: Apr 24, 2026
  modificationDate  e.g.: Apr 24, 2026

NOTES
-----
- The guest access token fetched at startup is valid for ~24 hours.
  Re-running the script always fetches a fresh token automatically.
- Results are sorted newest first (by Coveo index date).
- A 200ms delay is inserted between pages to avoid hammering the API.
- Total article count in the API (~11,324) may differ slightly from the
  number shown in the browser UI (~10,616) due to filter differences.
  See findings.md for details.
- Field availability varies by document type. F5 uses three source backends:
  Salesforce Knowledge (Support Solution, Known Issue, Knowledge, Security
  Advisory, Operations Guide, Policy, Video), non-SF connectors (Manual,
  Release Note, Supplemental Document, Bug Tracker), and Zendesk (Education).
  The sf* fields and sfdetails__c are only present on Salesforce Knowledge
  types. Bug Tracker has its own f5_bug_* fields. Education articles have
  zendesk* fields instead. See available_fields.txt for the full breakdown.

API LIMITS (handled automatically)
-----------------------------------
Coveo enforces two hard limits:

1. The sum of firstResult + numberOfResults cannot exceed 5,000. Both scripts
   detect when the total result count exceeds this and automatically switch to
   recursive date-range chunking to retrieve all results.

2. A single response cannot exceed 20 MB. Both scripts send a fieldsToInclude
   list so only the fields actually used are returned, keeping responses small.
   --page-size=500 is safe with this in place.

See findings.md for a full technical explanation of both limits and how they
are worked around.
