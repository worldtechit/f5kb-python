F5 KB Article Index — Technical Findings
========================================

Reference document for rebuilding or extending the article fetcher.
The goal was to programmatically retrieve F5 KB article metadata (title, URL,
summary, dates) for BIG-IP Support Solution articles without requiring login —
since extended to all 15 document types and to full article bodies (recovered
off-API for the types the search index leaves empty; see the body-recovery
subsection at the end).

ARCHITECTURE OF MY.F5.COM
-------------------------

The portal is built on Salesforce Experience Cloud (formerly Community Cloud)
with the Lightning/Aura framework. Search is powered by Coveo Headless
(the newer Coveo JS SDK, not the legacy Coveo JS UI Framework).

Key URLs:
- Portal base:      `https://my.f5.com/manage/s/`
- Search page:      `https://my.f5.com/manage/s/global-search/%40uri`
- Article format:   `https://my.f5.com/manage/s/article/K000160969`
- Aura API:         `https://my.f5.com/manage/s/sfsites/aura`
- Salesforce Org ID: `00D00000000hXqv` (from inline.js)

TOKEN / CREDENTIAL DISCOVERY
----------------------------

Dead ends
~~~~~~~~~

- The page is fully JavaScript-rendered and returns an empty loading shell to curl
- The Salesforce REST API (`/services/data/`) requires authentication
- The Chatter Connect API is disabled for this org
- Searching for `organizationId` in the minified JS yields only template variable
  references, not actual values
- Many Apex class names exist (`CoveoCustomSearch`, `CoveoFullSearch`,
  `CoveoResources`, `CoveoTechDocSearch`, etc.) but none expose `getSearchToken`

What works
~~~~~~~~~~

The Salesforce LWC component `c/quanticSearchInterface` imports:

    @salesforce/apex/HeadlessController.getHeadlessConfiguration

This is callable via the Aura ApexActionController as a guest/unauthenticated
user. The response contains the Coveo organization ID and a fresh bearer token.

Aura API call to get credentials:

    POST https://my.f5.com/manage/s/sfsites/aura?r=7
    Content-Type: application/x-www-form-urlencoded

    message={"actions":[{"id":"1","descriptor":"aura://ApexActionController/ACTION$execute",
      "callingDescriptor":"UNKNOWN","params":{
        "classname":"HeadlessController",
        "method":"getHeadlessConfiguration",
        "params":{},"cacheable":false,"isContinuation":false}}]}
    &aura.context={"mode":"PROD","fwuid":"ZkJhOVpLN2NZQkJrd2NWd3pMcnFOdzJEa1N5en...",
      "app":"siteforce:communityApp",
      "loaded":{"APPLICATION@markup://siteforce:communityApp":"1547_6p-2GBd9IQWZ4UXs1Im3BQ"},
      "dn":[],"globals":{},"uad":false}
    &aura.pageURI=/manage/s/global-search/%40uri
    &aura.token=null

Response shape:

    {
      "actions": [{
        "state": "SUCCESS",
        "returnValue": {
          "returnValue": "{\"platformUrl\":\"https://f5networksproduction5vkhn00h.org.coveo.com\",\"accessToken\":\"eyJ...\",\"organizationId\":\"f5networksproduction5vkhn00h\"}"
        }
      }]
    }

Note: the `returnValue.returnValue` is a JSON-encoded string, not an object.
Parse it with a second `JSON.parse()`.

The access token is a JWT issued to the guest/community user, valid for 24 hours.
The script fetches a fresh one on every run — no need to cache it.

Important: The Aura response is plain JSON (no `/.../` wrapper) when called
from Python/httpx. The `/.../ERROR*/` wrapper only appears in curl error responses.

COVEO SEARCH API
----------------

Endpoint:

    POST https://f5networksproduction5vkhn00h.org.coveo.com/rest/search/v2?organizationId=f5networksproduction5vkhn00h

Auth header:

    Authorization: Bearer <token from HeadlessController.getHeadlessConfiguration>

Coveo credentials (static — the org ID never changes):
- Organization ID:  `f5networksproduction5vkhn00h`
- Platform URL:     `https://f5networksproduction5vkhn00h.org.coveo.com`
- Search hub:       `myF5`

Minimal request body:

    {
      "q": "",
      "aq": "@f5_document_type==\"Support Solution\" @f5_version==\"BIG-IP\"",
      "numberOfResults": 100,
      "firstResult": 0,
      "searchHub": "myF5",
      "sortCriteria": "date descending"
    }

Pagination: increment `firstResult` by `numberOfResults` each page.
Max `numberOfResults` per page is 1000 (Coveo platform limit).

Total count: returned as `totalCountFiltered` (preferred) or `totalCount` in
the response root.

API LIMITS
----------

Two hard limits are enforced by the Coveo platform. Both are handled
automatically by the toolkit (lib/coveo/).

Limit 1 — 5,000-result offset cap
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Coveo API rejects any request where `firstResult + numberOfResults > 5000`.
This is a platform-wide limit, not configurable.

Symptom: Standard pagination silently stops at 5,000 articles. The total
count in the response drops to 0 on the page that would push past the boundary.

Fix — recursive date-range chunking (`--days`) + `@rowid` keyset (`--all`):
Split the query into date windows using `@date>=YYYY/MM/DD@HH:MM:SS` filters
in the `aq` field. Recursively halve any window whose count still exceeds 5,000.
Each leaf window is then safely paged with standard pagination.

`@date` chunking alone is NOT sufficient for a full-corpus dump (learned the
hard way on the 2026-06 full dump). Two failure modes:
1. `@date` filtering is only 1-second resolution. A bulk re-index can stamp
   >5,000 articles with the SAME `@date` second (Manual had 12,992 in one second),
   which is irreducible by date — the 5,000 cap then drops the rest.
2. Date-range queries silently EXCLUDE documents whose `@date` is null or outside
   the window (Release_Note had 31 such articles → bare count 757 vs windowed 726).

Real fix: keyset (cursor) pagination by `@rowid` — the one sortable, unique,
monotonic system field (`@permanentid`/`@urihash`/`@f5_kb_id` are NOT sortable —
`InvalidSortField`). Sort `@rowid ascending`, page with `@rowid>=cursor`, no
offset cap. `@rowid` (~1.8e18) exceeds Python float precision in JSON, so json.loads rounds
it (ULP ~256); back the cursor off a margin (4096) and use `>=` + dedup by
permanentid to avoid a boundary skip. `f5kb dump --all` (coveo/paging.py,
`fetch_keyset()`) now keyset-pages
the WHOLE type (no `@date` window at all), which also captures the null/out-of-
window-`@date` articles; `--days` keeps date-chunking (recency is the point) and
defers to keyset only for an irreducible sub-second window.

Date filter syntax for the `aq` field:

    @date>=2024/07/01@00:00:00 @date<2024/10/01@00:00:00

Coveo's `@date` field reflects the indexing date, not the original
publication date. Most F5 articles cluster around mid-September 2024 (a bulk
import when F5 migrated to this platform). The distribution for BIG-IP Support
Solutions by year:

| Period                         | Count  |
|--------------------------------|--------|
| 2000–2023 (combined)           | ~  100 |
| 2024 Q1–Q2 (Jan–Jun)           | ~    7 |
| 2024 Q3 (Jul–Sep)              | ~6,200 |
| — July                         | ~   32 |
| — August                       | ~  110 |
| — September                    | ~6,060 |
| 2024 Q4 (Oct–Dec)              | ~  405 |
| 2025                           | ~3,671 |
| 2026 (to date)                 | ~  957 |

September 2024 alone holds ~6,060 articles and requires further splitting.
The binary-split recursion handles this automatically.

Limit 2 — 20 MB response size cap
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Coveo API returns HTTP 400 with `ResponseExceededMaximumSizeException` when
a single response exceeds 20,971,520 bytes (~20 MB).

Symptom: Hits when using `--page-size` values above ~200 on queries that
return large result sets (e.g. all BIG-IP Support Solutions).

Fix — `fieldsToInclude`:
Add a `fieldsToInclude` array to the search request body listing only the raw
fields the script actually reads. This strips the large HTML body
(`sfdetails__c`) and dozens of other unused fields from each result:

    "fieldsToInclude": [
      "clickableuri",
      "f5_original_published_date",
      "f5_updated_published_date",
      "sffirstpublisheddate",
      "sflastmodifieddate",
      "date"
    ]

With this in place, `--page-size=500` is safe even for the largest result sets.
Without it, safe page sizes are roughly ≤100–150 for full-field responses.

`f5kb dump` takes the opposite approach. It deliberately requests
all fields (no `fieldsToInclude`) because its whole purpose is full-fidelity
capture + field cataloguing. To stay under the 20 MB cap it instead (a) uses a
smaller default page size and (b) auto-recovers: if a page trips
`ResponseExceededMaximumSizeException`, it halves that request's page size and
retries, repeating until the page fits. Measured response sizes for Support
Solution with all fields (~17 KB/article):

| numberOfResults | response size | result       |
|-----------------|---------------|--------------|
| 50              | 0.85 MB       | ok           |
| 200             | 3.36 MB       | ok           |
| 500             | 8.33 MB       | ok           |
| 1000            | > 20 MB       | 400 rejected |

So the dumper defaults to `--page-size=200` and caps it at 500. Larger content
types (e.g. Manual) are heavier per article, which is exactly why the
auto-halving retry exists — it lets one default work across all types.

Note this also clarifies a common misconception: the search API has always
returned results in batches (`numberOfResults` per call); it was never
one-article-per-request. The per-article files that `f5kb dump` writes
are an output-format choice, independent of how many articles each API call
returns.

KEY FIELDS IN SEARCH RESULTS
----------------------------

Field availability varies by backend. F5 uses three source systems behind Coveo:
Salesforce Knowledge (Support Solution, Known Issue, Knowledge, Security Advisory,
Operations Guide, Policy, Video), non-SF connectors (Manual, Release Note,
Supplemental Document, Bug Tracker), and Zendesk (Education). The `sf*` fields
are only present on Salesforce Knowledge types.

| Field path          | Description                              | Availability |
|---------------------|------------------------------------------|--------------|
| `title`             | Full article title including K-number    | All types    |
| `clickUri`          | Direct article URL                       | All types    |
| `excerpt`           | Short extract/summary (~150 chars)       | All types    |
| `raw.f5_original_published_date` | Publication timestamp (ms) | SF Knowledge + Bug Tracker |
| `raw.f5_updated_published_date`  | Modification timestamp (ms) | SF Knowledge + Bug Tracker |
| `raw.sffirstpublisheddate`       | Fallback publication timestamp | SF Knowledge only |
| `raw.sflastmodifieddate`         | Fallback modification timestamp | SF Knowledge only |
| `raw.f5_document_type`  | Array: e.g. `["Support Solution"]`   | All types    |
| `raw.f5_version`        | Array with full version hierarchy    | Most types   |
| `raw.f5_kb_id`          | KB article number, e.g. `K000160969` | SF Knowledge only |
| `raw.f5_product`        | Array: e.g. `["BIG-IP","BIG-IP APM"]`| Most types (not always on Support Solution) |
| `raw.sfdetails__c`      | Full article HTML body (large)       | SF Knowledge only |
| `raw.concepts`          | Coveo-extracted key concepts         | All types    |
| `raw.wordcount`         | Word count of indexed content        | All types    |
| `raw.f5_bug_id`         | Bug ID number                        | Bug Tracker only |
| `raw.f5_bug_severity`   | e.g. "3-Major"                       | Bug Tracker only |
| `raw.f5_bug_state`      | e.g. "Closed", "Open"               | Bug Tracker only |
| `raw.zendeskid`         | Zendesk article ID                   | Education only |

All timestamps are Unix milliseconds. Convert with `new Date(tsMs)`.

See Appendix A (Field Inventory) at the end of this file; the machine-readable
field->description map is the `field_descriptions:` section of config.yaml.

SITEMAP (ALTERNATIVE ARTICLE DISCOVERY)
---------------------------------------

The portal exposes sitemaps at:

    https://my.f5.com/manage/s/sitemap.xml         (index of 4 sitemaps)
    https://my.f5.com/manage/s/sitemap-topicarticle-1.xml  (~20,000 URLs)
    https://my.f5.com/manage/s/sitemap-topicarticle-2.xml  (~15,000 URLs)
    https://my.f5.com/manage/s/sitemap-topicarticle-weekly.xml (~93 URLs)
    https://my.f5.com/manage/s/sitemap-view-1.xml           (5 URLs)

Each entry contains `<loc>` (article URL) and `<lastmod>` only — no title,
summary, or content type. No auth needed; plain `curl` works.

How to check it / follow the references:

    # 1. index -> list child sitemaps
    curl -s https://my.f5.com/manage/s/sitemap.xml | grep -oE '<loc>[^<]+</loc>'
    # 2. fetch each child, extract all article URLs
    for s in sitemap-topicarticle-1 sitemap-topicarticle-2 \
             sitemap-topicarticle-weekly sitemap-view-1; do
      curl -s "https://my.f5.com/manage/s/$s.xml"
    done | grep -oE '<loc>[^<]+</loc>' | sed 's/<[^>]*>//g'
    # 3. reduce to K-IDs
    ... | grep -oE '/article/(K?[0-9]+)' | sed 's#/article/##' | sort -u
    # lastmod pairs (loc immediately followed by lastmod):
    #   <loc>…/article/<id></loc><lastmod>YYYY-MM-DDThh:mm:ssZ</lastmod>

Scope (verified 2026-06-03): ~35,424 URLs, all of the form `/article/K…`
(Salesforce-Knowledge articles only) plus 5 nav stubs. The sitemap does NOT list
Bug Tracker (cdn.f5.com), Manual / Release Note / Supplemental Document (TechComm
doc sites), F5 GitHub, Community, or Education (Zendesk) — those live on other
hosts. So it overlaps only the 8 SF-Knowledge types and omits ~71k of our corpus;
it is not a broader or richer source than the Coveo dump.

Sitemap-vs-dump gap analysis (2026-06-03)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Diffed the sitemap's distinct K-IDs against our dump's K-articles (filenames):

| | count |
|---|-------|
| Distinct K-IDs in sitemap                | 35,337 |
| K-articles in our dump                   | 35,576 |
| In sitemap not in dump (the gap)     | 47 |
| In dump not in sitemap (we're ahead) | 286    |

The 47 gap articles are all absent from the Coveo index itself (confirmed by
`@f5_kb_id=="K…"` and free-text queries returning nothing), so they are not
retrievable by the Coveo-based pipeline — not a dump bug. By `lastmod`: only 2
are recent (≥2026-05, plausible Coveo indexing lag); the other 45 are old
(2023–early 2026, incl. the 2024-09 migration) — migrated into Salesforce but never
(re)indexed by Coveo (likely superseded/unpublished/merged). Their pages return
HTTP 200 but only as the my.f5.com Salesforce SPA shell, so the only way to get them
is scraping that JS SPA per-article (deliberately avoided; low value). The 47 IDs
are saved in TODO.txt (the sitemap-gap follow-up section).

The 286 "extras" are articles we have that the sitemap omits — 252 are Security
Advisories (+14 Known Issue, 13 Support Solution, 7 Knowledge); the sitemap
under-lists Security Advisories, so our Coveo dump is the more complete source there.

Bottom line: the only true gap the sitemap reveals is ~45 old Coveo-unindexed
K-articles (practically unreachable, low value) plus a couple of indexing-lag items.
The sitemap's real utility is exactly that: surfacing the handful of Salesforce
articles missing from Coveo, and a cheap `lastmod` feed for change-polling (poll
`sitemap-topicarticle-weekly.xml` — ~93 entries, the most recently updated — to
detect new/changed K-articles without a full crawl). Use the Coveo API for
everything else.

QUERY FILTER SYNTAX
-------------------

The `aq` field uses Coveo query language.

Filter by document type (use exact string, case-sensitive):

    @f5_document_type=="Support Solution"
    @f5_document_type=="Release Note"
    @f5_document_type=="Security Advisory"
    @f5_document_type=="Known Issue"
    @f5_document_type=="Manual"

Filter by product (top-level value, no version suffix):

    @f5_version=="BIG-IP"
    @f5_version=="NGINX Plus"
    @f5_version=="F5OS"

Combine with a space (implicit AND):

    @f5_document_type=="Support Solution" @f5_version=="BIG-IP"

The `@f5_version` field uses a pipe-delimited hierarchy
(`"BIG-IP LTM|16|16.1|16.1.2"`). Matching on the top-level name
(e.g. `"BIG-IP"`) returns articles tagged with any version of that product.

AVAILABLE DOCUMENT TYPES (WITH ARTICLE COUNTS)
----------------------------------------------

Counts verified live on 2026-06-02 via the `f5_document_type` facet
(`moreValuesAvailable: false`; the 15 types sum to 410,388 of the 410,406
unfiltered total — a 0.004% gap, so this list is complete).

| Document Type          | Count   |
|------------------------|---------|
| Bug Tracker            | 22,246  |
| Community              | 303,122 |
| Compliance             | 1       |
| Education              | 171     |
| F5 GitHub              | 1,234   |
| Knowledge              | 6,479   |
| Known Issue            | 8,326   |
| Manual                 | 46,811  |
| Operations Guide       | 148     |
| Policy                 | 223     |
| Release Note           | 757     |
| Security Advisory      | 5,042   |
| Supplemental Document  | 135     |
| Support Solution   | 15,164 |
| Video                  | 529     |

AVAILABLE PRODUCTS (TOP-LEVEL, WITH ARTICLE COUNTS)
---------------------------------------------------

Complete list from `f5kb discover` plus manual investigation (321 total:
73 from global facet, 247 type-filtered, 1 facet-excluded). Counts cover all
document types combined. The machine-readable version with metadata is the
`products:` section of config.yaml (refreshed via `f5kb discover`).

Source key: `global facet` = returned by unfiltered global facet; `hidden*` = excluded
from global facet, found via type-filtered query; `facet-excluded†` = excluded from
ALL facets (global and type-filtered), queryable only directly; `(dup)` = confirmed
duplicate — different tag name, identical article set (see Duplicate Product Tags section).

| Product | Count | Source |
|---------|-------|--------|
| BIG-IP | 48,453 | global facet |
| BIG-IP LTM | 29,260 | global facet |
| BIG-IP APM | 24,451 | global facet |
| BIG-IP ASM | 21,436 | global facet |
| BIG-IP AFM | 15,610 | global facet |
| BIG-IP Link Controller | 14,069 | global facet |
| BIG-IP PEM | 13,950 | global facet |
| BIG-IP DNS | 13,608 | global facet |
| BIG-IP AAM | 12,911 | global facet |
| BIG-IP Analytics | 12,329 | global facet |
| BIG-IP GTM | 9,741 | global facet |
| Legacy Products | 8,756 | global facet |
| BIG-IQ Centralized Management | 7,764 | global facet |
| BIG-IP FPS | 7,715 | global facet |
| BIG-IQ | 6,391 | global facet |
| F5OS | 4,875 | global facet |
| BIG-IP WebAccelerator | 4,724 | global facet |
| Traffix SDC | 4,538 | global facet |
| BIG-IP Edge Gateway | 4,292 | global facet |
| BIG-IP PSM | 3,608 | global facet |
| BIG-IP TMOS | 3,570 | hidden* |
| F5OS-C | 2,780 | global facet |
| F5OS-A | 2,688 | global facet |
| BIG-IP WOM | 2,678 | global facet |
| Enterprise Manager | 2,530 | global facet |
| FirePass | 2,445 | global facet |
| F5 App Protect | 2,316 | hidden* |
| NGINX Plus | 2,178 | global facet |
| F5 SSL Orchestrator | 2,169 | hidden* |
| ARX | 2,097 | global facet |
| NGINX Products | 2,093 | global facet |
| F5 Distributed Cloud Services | 1,950 | global facet |
| NGINX Open Source | 1,919 | global facet |
| F5 Distributed Cloud | 1,786 | global facet |
| NGINX Instance Manager | 1,765 | global facet |
| F5 DDoS Hybrid Defender | 1,721 | hidden* |
| NGINX Ingress Controller | 1,681 | global facet |
| BIG-IP Documentation | 1,654 | facet-excluded† |
| NGINX App Protect WAF | 1,647 | global facet |
| NGINX Unit | 1,643 | global facet |
| NGINX Controller | 1,629 | global facet |
| BIG-IP Next | 1,625 | global facet |
| BIG-IP Platforms | 1,613 | hidden* |
| BIG-IP Next SPK | 1,591 | global facet |
| F5 iWorkflow | 1,539 | hidden* |
| BIG-IQ Cloud | 1,537 | global facet |
| NGINX App Protect DoS | 1,532 | global facet |
| BIG-IQ Security | 1,531 | global facet |
| BIG-IQ Device | 1,489 | global facet |
| BIG-IP All | 1,447 | hidden* |
| NGINX Agent | 1,371 | global facet |
| F5 Silverline | 1,336 | global facet |
| LineRate | 1,327 | hidden* |
| BIG-IP Next CNF | 1,288 | global facet (dup) |
| BIG_IP_NEXT(CNF) | 1,288 | hidden* (dup) |
| BIG-IQ Cloud and Orchestration | 1,266 | global facet |
| NGINX Service Mesh | 1,254 | global facet |
| BIG-IP Next CGNAT CNF | 1,238 | global facet |
| BIG-IP Next DNS CNF | 1,237 | global facet |
| NGINX API Connectivity Manager | 1,232 | global facet |
| BIG-IP Next Edge Firewall CNF | 1,231 | global facet |
| BIG-IQ ADC | 1,193 | global facet |
| NGINX Management Suite Security Monitoring | 1,160 | global facet |
| Virtual Editions | 1,156 | hidden* |
| VIPRION Platforms | 1,052 | hidden* |
| F5 WebSafe | 961 | hidden* |
| NGINX One Console | 956 | global facet |
| NGINX Gateway Fabric | 921 | global facet |
| BIG-IP Next Central Manager | 902 | global facet |
| BIG-IP Next LTM | 878 | global facet |
| BIG-IP 4 | 855 | global facet |
| BIG-IP Install/Upgrade | 834 | hidden* |
| BIG-IP Next WAF | 828 | global facet |
| APM Clients | 811 | global facet (dup) |
| APM-Clients | 811 | hidden* (dup) |
| BIG-IP SSL Orchestrator | 809 | global facet |
| BIG-IP DDoS Hybrid Defender | 769 | global facet |
| F5 BIG-IQ API | 767 | hidden* |
| BIG-IP Next for Kubernetes | 713 | global facet |
| F5 Distributed Cloud Bot Defense | 666 | hidden* |
| F5 Distributed Cloud Data Intelligence | 651 | hidden* |
| F5 Silverline DDoS Protection | 643 | hidden* |
| F5 Silverline Web Application Firewall | 642 | hidden* |
| Service Proxy for Kubernetes | 523 | hidden* |
| F5 AI Gateway | 521 | global facet |
| 3-DNS | 519 | hidden* |
| BIG-IP Distributed Cloud Services | 512 | hidden* |
| F5 MobileSafe | 512 | hidden* |
| Public Cloud | 449 | hidden* |
| F5 WAF for NGINX | 376 | global facet |
| BIG-IP AVR | 374 | hidden* |
| 5G Products | 327 | global facet |
| F5 Distributed Cloud Account Protection | 326 | hidden* |
| F5 Distributed Cloud Aggregator Management | 326 | hidden* |
| F5 Distributed Cloud Application Traffic Insight | 326 | hidden* |
| F5 Distributed Cloud Authentication Intelligence | 326 | hidden* |
| F5 DoS for NGINX | 306 | global facet |
| F5 Modules for Ansible | 298 | hidden* |
| 3-DNS Controller versions 1.x - 4.x | 289 | hidden* |
| BIG-IP Cloud Edition | 267 | global facet |
| F5 Application Services 3 Extension | 253 | hidden* |
| rSeries Platforms | 246 | hidden* |
| VELOS Platforms | 230 | hidden* |
| BIG-IQ Platforms | 226 | hidden* |
| F5 Access | 201 | global facet |
| Private Cloud | 188 | hidden* |
| APM-Clients APM | 187 | hidden* |
| F5 Automation and Orchestration | 172 | hidden* |
| Aspen Mesh | 169 | hidden* |
| BIG-IP CGN | 156 | hidden* |
| BIG-IQ Platform | 142 | hidden* |
| BIG-IP SWG | 140 | hidden* |
| BIG-IP versions 1.x - 4.x | 131 | hidden* |
| BIG-IP i2000 Series | 122 | hidden* |
| Amazon Web Services | 121 | hidden* |
| Microsoft Azure | 121 | hidden* |
| BIG-IP i5000 Series | 119 | hidden* |
| BIG-IP i7000 Series | 119 | hidden* |
| BIG-IP i4000 Series | 117 | hidden* |
| BIG-IP i10000 Series | 115 | hidden* |
| BIG-IP SSLO | 102 | hidden* |
| BIG-IP i11000 Series | 95 | hidden* |
| BIG-IP i15000 Series | 88 | hidden* |
| Cloud-Native Network Functions for OpenShift | 85 | hidden* |
| BIG-IP 10000 Series | 85 | hidden* |
| WANJet | 83 | hidden* |
| BIG-IP 5000 Series | 76 | hidden* |
| BIG-IP 7000 Series | 75 | hidden* |
| Support Portal | 72 | hidden* |
| VIPRION B2250 Blade | 72 | hidden* |
| APM-Clients TMOS | 70 | hidden* |
| BIG-IP 2000 Series | 66 | hidden* |
| BIG-IP 4000 Series | 65 | hidden* |
| VIPRION B2100 Blade | 65 | hidden* |
| BIG-IQ Web App Security (ASM) | 64 | hidden* |
| F5 BIG-IP Container Ingress Services | 62 | hidden* |
| VIPRION B4300 Blade | 62 | hidden* |
| F5 Distributed Apps | 61 | hidden* |
| VIPRION B2150 Blade | 58 | hidden* |
| rSeries 10000 | 56 | hidden* |
| GLOBAL-SITE Controller | 56 | hidden* |
| BIG-IQ Applications | 55 | hidden* |
| BIG-IP i850 | 55 | hidden* |
| rSeries 5000 | 55 | hidden* |
| Vmware ESXI and vCloud Director | 54 | hidden* |
| VIPRION B4450 Blade | 53 | hidden* |
| BIG-IP BIG-IQ | 52 | hidden* |
| F5 Distributed Cloud Multi-Cloud App Connect | 51 | hidden* |
| VIPRION 2400 | 51 | hidden* |
| F5 Distributed Cloud WAF | 50 | hidden* |
| Herculon Platforms | 50 | hidden* |
| NGINX Amplify | 49 | hidden* |
| F5 Distributed Cloud Web App & API Protection | 48 | hidden* |
| BIG-IP 12250v | 47 | hidden* |
| F5 SDK | 45 | hidden* |
| VIPRION B4340N Blade NEBS | 45 | hidden* |
| rSeries 4000 | 43 | hidden* |
| rSeries 2000 | 42 | hidden* |
| BIG-IP 6900 Series | 41 | hidden* |
| VELOS CX410 | 40 | hidden* |
| BIG_IP_NEXT(CNF) CNF | 39 | hidden* |
| VELOS BX110 Blade | 39 | hidden* |
| F5 Distributed Cloud DNS Management | 38 | hidden* |
| VIPRION B4200 Blade | 38 | hidden* |
| BIG-IP 11000 Series | 37 | hidden* |
| BIG-IP 8900 Series | 37 | hidden* |
| VIPRION 4800 | 37 | hidden* |
| F5 BIG-IP Telemetry Streaming | 36 | hidden* |
| VIPRION B4200 Blade NEBS | 35 | hidden* |
| BIG-IP 3600 | 34 | hidden* |
| BIG-IP 3900 | 34 | hidden* |
| VIPRION B4100 Blade | 33 | hidden* |
| VIPRION C2200 | 32 | hidden* |
| BIG-IP Velos | 31 | hidden* |
| BIG-IP 1600 Series | 31 | hidden* |
| VIPRION B4100 Blade NEBS | 31 | hidden* |
| Google Cloud | 30 | hidden* |
| VIPRION 4480 | 29 | hidden* |
| F5 Cloud Services | 28 | hidden* |
| rSeries 12000 | 28 | hidden* |
| KVM | 27 | hidden* |
| Microsoft Hyper-V | 27 | hidden* |
| Link Controller | 27 | hidden* |
| F5 Distributed Cloud DNS Load Balancer | 26 | hidden* |
| LineRate End-of-Life | 26 | hidden* |
| VIPRION 4400 | 25 | hidden* |
| APM-Clients Install/Upgrade | 24 | hidden* |
| BIG-IP 6400 Series | 24 | hidden* |
| F5 BIG-IP Cloud Failover | 23 | hidden* |
| BIG-IP Next Access | 23 | hidden* |
| VELOS CX1610 | 22 | hidden* |
| BIG-IP 6800 Series | 21 | hidden* |
| BIG-IP Next SSL Orchestrator | 21 | hidden* |
| F5 Insight | 20 | global facet |
| NGINX ModSecurity WAF | 20 | global facet |
| BIG-IP 3400 Series | 20 | hidden* |
| BIG-IP 8400 Series | 20 | hidden* |
| VELOS BX520 Blade | 20 | hidden* |
| Herculon i2000 Series | 20 | hidden* |
| Network Function Virtualization | 19 | hidden* |
| BIG-IP 10200v FIPS | 18 | hidden* |
| BIG-IP 1500 | 18 | hidden* |
| BIG-IP 800 | 18 | hidden* |
| BIG-IP 8800 | 18 | hidden* |
| BIG-IP 10200v | 17 | hidden* |
| BIG-IP 10200v SSL | 17 | hidden* |
| BIG-IP 10350v | 17 | hidden* |
| VIPRION 4400 DC Power NEBS | 17 | hidden* |
| ARX Cloud Extender | 16 | global facet |
| BIG-IP 10250v | 16 | hidden* |
| F5 Distributed Multi-Cloud Network Connect | 16 | hidden* |
| Herculon i10000 Series | 16 | hidden* |
| Herculon i5000 Series | 16 | hidden* |
| BIG-IQ 7000 | 16 | hidden* |
| F5 Monitoring Pack | 16 | hidden* |
| Citrix Xenserver | 16 | hidden* |
| F5 Driver for OpenStack LBaaSv2 | 15 | hidden* |
| BIG-IP 10350v NEBS | 15 | hidden* |
| BIG-IP SAM | 15 | hidden* |
| TrafficShield | 15 | hidden* |
| F5 Container Ingress Services | 14 | hidden* |
| F5 Distributed Cloud Routed DDoS | 14 | hidden* |
| AGC | 13 | hidden* |
| BIG-IP 10000s | 13 | hidden* |
| BIG-IP 5250v FIPS | 13 | hidden* |
| Xen Project | 13 | hidden* |
| BIG-IP 10050s | 12 | hidden* |
| BIG-IP 10255v | 12 | hidden* |
| F5 Distributed Cloud Web App Scanning | 12 | hidden* |
| F5 VNF Manager | 12 | hidden* |
| BIG-IP 7200v FIPS | 12 | hidden* |
| SDK | 12 | hidden* |
| Openstack | 11 | hidden* |
| BIG-IP 10055s | 10 | hidden* |
| BIG-IP 10150s | 10 | hidden* |
| Web Accelerator 5.x | 10 | hidden* |
| Enterprise Manager Platforms | 10 | hidden* |
| NGINX as a Service for Azure | 9 | hidden* |
| WebAccelerator 4500 | 9 | hidden* |
| Legacy Platforms | 9 | hidden* |
| SSLO Troubleshooting Guide | 8 | hidden* |
| BIG-IP SAM 4340 | 8 | hidden* |
| AGC APM | 7 | hidden* |
| rSeries 10900 | 7 | hidden* |
| rSeries 5800 | 7 | hidden* |
| rSeries 5900 | 7 | hidden* |
| FirePass Platforms | 7 | hidden* |
| Herculon i2800 | 7 | hidden* |
| TrafficShield / BIG-IP ASM Platforms | 7 | hidden* |
| F5 BIG-IP Controller for Kubernetes | 6 | hidden* |
| BIG IP i5800 | 6 | hidden* |
| Cisco APIC | 6 | hidden* |
| F5 Distributed Cloud Client-Side Defense | 6 | hidden* |
| F5 Application Connector | 6 | hidden* |
| ARX Platforms | 6 | hidden* |
| Herculon i10800 | 6 | hidden* |
| Herculon i5800 | 6 | hidden* |
| BIG IP i10800 | 5 | hidden* |
| BIG IP i11800 | 5 | hidden* |
| BIG IP i15800 | 5 | hidden* |
| F5 Distributed Cloud Administration | 5 | hidden* |
| rSeries 5920-DF | 5 | hidden* |
| F5 Application Services Proxy | 5 | hidden* |
| rSeries 5600 | 5 | hidden* |
| Enterprise Manager 4000 | 5 | hidden* |
| BIG IP i2800 | 4 | hidden* |
| BIG IP i4800 | 4 | hidden* |
| BIG IP i7800 | 4 | hidden* |
| F5 Distributed Cloud Audit Logs & Alerts | 4 | hidden* |
| F5 Distributed Cloud Billing | 4 | hidden* |
| F5 Distributed Cloud Support | 4 | hidden* |
| F5 DNS Cloud Service | 4 | hidden* |
| rSeries 2800 | 4 | hidden* |
| rSeries 4800 | 4 | hidden* |
| BIG-IP 7200v-SSL | 4 | hidden* |
| BIG-IP 7255s | 4 | hidden* |
| WANJet Platforms | 4 | hidden* |
| F5 Distributed Cloud Observability | 3 | hidden* |
| F5 Distributed Cloud Shared Configuration | 3 | hidden* |
| F5 DNS Load Balancer Cloud Service | 3 | hidden* |
| F5 Essential App Protect Service | 3 | hidden* |
| rSeries 10920-DF | 3 | hidden* |
| rSeries 12800-DS | 3 | hidden* |
| rSeries 12900-DS | 3 | hidden* |
| ARX 4000/4000+ | 3 | hidden* |
| BIG-IP 7055s | 3 | hidden* |
| FirePass 1200 | 3 | hidden* |
| FirePass 1205 | 3 | hidden* |
| FirePass 1210 | 3 | hidden* |
| FirePass 1220 | 3 | hidden* |
| FirePass 1230 | 3 | hidden* |
| FirePass 4100 | 3 | hidden* |
| FirePass 4300 | 3 | hidden* |
| NGINX JavaScript | 2 | global facet |
| BIG-IP MA-All | 2 | hidden* |
| F5 Distributed Cloud Content Delivery Network | 2 | hidden* |
| F5 Distributed Cloud Delegated Access | 2 | hidden* |
| F5 Distributed Cloud NGINX One | 2 | hidden* |
| F5 Distributed Cloud Platform | 2 | hidden* |
| AI Security | 2 | hidden* |
| F5 AI Guardrails | 2 | hidden* |
| ARX 1500 | 2 | hidden* |
| ASM 4100 | 2 | hidden* |
| FirePass 4110 | 2 | hidden* |
| FirePass 4120 | 2 | hidden* |
| FirePass 4130 | 2 | hidden* |
| FirePass 4305 | 2 | hidden* |
| FirePass 4310 | 2 | hidden* |
| FirePass 4320 | 2 | hidden* |
| FirePass 4330 | 2 | hidden* |
| FirePass 4340 | 2 | hidden* |
| TrafficShield 4100 | 2 | hidden* |
| F5 Distributed Cloud BIG-IP APM | 1 | hidden* |
| F5 Distributed Cloud Managed Services | 1 | hidden* |
| rSeries 12600-DS | 1 | hidden* |
| ARX 2000 | 1 | hidden* |
| BIG-IP eBPF Observability | 1 | hidden* |
| Enterprise Manager 3000 | 1 | hidden* |
| F5 AI Red Team | 1 | hidden* |
| WANJet 300 | 1 | hidden* |
| WANJet 500 | 1 | hidden* |

COUNT DISCREPANCY (UI VS API)
-----------------------------

The browser search UI reports ~10,616 articles for BIG-IP + Support Solution.
The Coveo API returns ~11,324 for the same query. The difference is likely
because the UI applies additional filters (e.g. archived exclusion via
`@f5_archived`) that the browser hash encodes as `aq=%40f5_archived`.

To match the UI result set more closely, add to `aq`:

    @f5_archived=="Archived documents excluded"

MULTI-PRODUCT/TYPE SUPPORT
--------------------------

Implemented in `f5kb fetch` (coveo/flat.py, `build_aq()`). The `aq` field is built
from `--product` and `--type` CLI flags:

    --product="NGINX Plus" --type="Security Advisory"
    → @f5_document_type=="Security Advisory" @f5_version=="NGINX Plus"

Use `f5kb list-types` to enumerate valid document types at runtime. For products,
prefer `f5kb discover` over `f5kb list-products` — the global facet used by
`list-products` silently omits ~247 valid product names (see Facet API Gaps
section below). `f5kb discover` writes `discovered_products.yaml` with
all ~321 known products (copy its `products:` block into config.yaml); that block
is the authoritative source for `--product` values.

Important: The `@f5_version` filter matches on the exact top-level product
name, not sub-products. For example, `@f5_version=="BIG-IP"` does NOT match
articles tagged only with `BIG-IP APM` or `BIG-IP LTM`. Each is a separate
product tag. Release Notes, for instance, are tagged under `BIG-IP APM`, not
the generic `BIG-IP`. Always verify with `f5kb list-products` when counts come
back zero for an expected combination.

FACET API GAPS — list-products IS INCOMPLETE
--------------------------------------------

The `f5kb list-products` subcommand queries the Coveo facet API for all values of the
`f5_version` field. This API has two separate limitations that both cause valid
product names to be missing from the output.

Gap 1 — numberOfValues slot starvation (fixed)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The `f5_version` field stores a pipe-delimited version hierarchy, so a single
article tagged with BIG-IP LTM 16.1.0 contributes multiple values:
`"BIG-IP LTM"`, `"BIG-IP LTM|16"`, `"BIG-IP LTM|16|16.1"`,
`"BIG-IP LTM|16|16.1|16.1.0"`.

The original script requested `numberOfValues: 500`. Coveo fills those 500 slots
with the most-common values regardless of whether they have a pipe or not:

| numberOfValues requested | Total returned | Top-level (no pipe) | moreAvailable |
|--------------------------|----------------|---------------------|---------------|
| 500                      | 500            | 37                  | true          |
| 1000                     | 1000           | 43                  | true          |
| 2000                     | 2000           | 62                  | true          |
| 5000                     | 2769           | 73                  | false     |

Fix: request `numberOfValues: 5000`. The API is exhausted at 2,769 values,
yielding 73 distinct top-level product names. The script was updated accordingly.

Gap 2 — facet exclusion (unfixable from our side)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Even with `numberOfValues: 5000` and `moreValuesAvailable: false`, certain
valid `@f5_version` values do not appear in the facet results at all.

Confirmed example: "BIG-IP Documentation"
- `@f5_version=="BIG-IP Documentation"` returns 1,654 articles
- The raw field on those articles contains `f5_version: ["BIG-IP Documentation"]`
- It does not appear anywhere in the 2,769 facet values

This is a Coveo platform-level configuration: the F5 admin has excluded this
value (and possibly others) from facet indexing, likely for UI cleanliness.
There is no way to enumerate excluded values via the API.

How "BIG-IP Documentation" was discovered: when `--product="BIG-IP"
--type="Manual"` returned only 2 results, a diagnostic facet query was run
with the Manual filter active (`aq: "@f5_document_type==\"Manual\""`). A
filtered facet is computed in the context of matching documents only, so
"BIG-IP Documentation" became prominent enough to appear despite being excluded
from the global unfiltered facet. This is the technique for discovering hidden
products for a specific document type:

    POST /rest/search/v2
    {
      "q": "",
      "aq": "@f5_document_type==\"Manual\"",
      "numberOfResults": 0,
      "facets": [{ "field": "f5_version", "numberOfValues": 5000, "type": "specific" }]
    }

Run the same query with each document type to build a complete product map.
This technique is now automated: `f5kb discover` (cmd/discover.py)
iterates every document type, collects all hidden product names, then runs a
count query for each to produce `discovered_products.yaml` (321 products total:
73 global, 247 type-filtered, 1 facet-excluded); copy its `products:` block into
config.yaml.

Practical rule: Use `f5kb discover` to refresh the `products:` block of config.yaml
and use that as the authoritative product list. `f5kb list-products` is fine for a
quick check but will silently omit ~248 products. If a product+type combination
returns zero results, the product name is likely hidden — run `f5kb discover` or use
the type-filtered facet query above to find the correct tag.

DUPLICATE PRODUCT TAGS
----------------------

Two confirmed pairs of product tags in config.yaml's `products:` section refer to the
exact same set of articles. Both tags co-exist on the same documents simultaneously.

| Clean name (global facet) | Legacy name (hidden) | Article count | Discovered via |
|---------------------------|----------------------|---------------|----------------|
| BIG-IP Next CNF           | BIG_IP_NEXT(CNF)     | 1,288         | Bug Tracker    |
| APM Clients               | APM-Clients          | 1,288         | Bug Tracker    |

Verification: querying both tags simultaneously
(`@f5_version=="BIG-IP Next CNF" @f5_version=="BIG_IP_NEXT(CNF)"`) returns 1,288
results — the full count — confirming every article carries both tags. The legacy
underscore/hyphen variants are Bug Tracker-specific labels applied to the same
documents that use the clean names elsewhere.

Practical rule: When filtering with `--product`, use the clean name
(`BIG-IP Next CNF`, `APM Clients`). Both forms work, but the clean name is the one
returned by the global facet and is less likely to change.

"BIG-IP DOCUMENTATION" — TECHCOMM/SITEMAP SOURCE
------------------------------------------------

`@f5_version=="BIG-IP Documentation"` returns 1,654 articles. All of them have:
- `f5_document_type: ["Manual"]`
- `source: "TechComm"`, `sourcetype: "Sitemap"`

These are F5's official product documentation pages, crawled via a separate Sitemap
connector — a completely different pipeline from KB articles (which use the Salesforce
connector). The "BIG-IP Documentation" tag is a synthetic marker applied to all
TechComm-sourced docs and is excluded from both the global facet and all
type-filtered facets by Coveo admin configuration.

Despite being excluded from facets, it DOES appear in a Manual type-filtered facet
with `numberOfValues: 5000` — the `f5kb discover` scan ran this query but the
entry was not captured, making it a known gap in discovery. It has been
manually added to config.yaml's `products:` section with `source: "manual_query"`
and `hiddenFromTypeFilteredFacets: true`.

To fetch these articles directly:

    POST /rest/search/v2
    { "q": "", "aq": "@f5_version==\"BIG-IP Documentation\"",
      "numberOfResults": 100, "searchHub": "myF5" }

These articles also carry many standard product/version hierarchy tags (BIG-IP AAM,
APM, ASM, etc.) so they will appear in `--product="BIG-IP APM"` queries alongside
regular KB articles.

SFAPPLIES_TO_PRODUCTS__C AND SFAPPLIES_TO_VERSIONS__C — FILL RATES
------------------------------------------------------------------

These fields are present on Salesforce Knowledge types only. Fill rates measured
against each type's total article count:

| Doc Type         | Total  | products__c | fill % | versions__c | fill % |
|------------------|--------|-------------|--------|-------------|--------|
| Support Solution | 15,142 | 14,361      | 94.8%  | 11,791      | 77.9%  |
| Known Issue      | 8,326  | 8,326       | 100%   | 8,260       | 99.2%  |
| Knowledge        | 6,479  | 6,443       | 99.4%  | 6,234       | 96.2%  |
| Security Advisory| 5,039  | 5,038       | 100%   | 5,036       | 99.9%  |
| Operations Guide | 148    | 148         | 100%   | 145         | 98.0%  |
| Policy           | 223    | 218         | 97.8%  | 193         | 86.5%  |
| Video            | 528    | 180         | 34.1%  | 167         | 31.6%  |

`sfapplies_to_versions__c` is consistently sparser than `sfapplies_to_products__c`
across all types. Video has only 34% fill — most videos are not product-version tagged.
The 781 Support Solutions without `sfapplies_to_versions__c` are typically older or
cross-version articles.

Field value formats:

`sfapplies_to_products__c` — JSON array string (requires a second JSON.parse):
    [{"Product":"BIG-IP"},
     {"Versions":["13.X.X","14.X.X","15.X.X","16.X.X","17.X.X","17.1.1","17.1.0",...],"Product":"BIG-IP APM"}]
Each element is an object with a `Product` key and an optional `Versions` array.

`sfapplies_to_versions__c` — plain comma-separated string:
    "13.0.0, 13.0.1, 13.0.X, 13.1.0, 13.1.1, 13.1.3, 13.1.4, 13.1.5, ..."

Note: `sfapplies_to_products__c` is not returned by default — add it to
`fieldsToInclude` explicitly if needed.

ARTICLE DEPRECATION / LIFECYCLE
-------------------------------

Question: which field marks a document as deprecated / no-longer-current?
Investigated via corpus-wide facet queries (2026-06-02).

`@f5_lifecycle` is the only reliable deprecation signal. Values: `release`
and `archived`. It is present ONLY on the documentation content types:

| Document type         | release | archived |
|-----------------------|---------|----------|
| Manual                | 6,180   | 399      |
| Release Note          | 391     | 220      |
| Supplemental Document | 63      | 4        |

Filter live vs deprecated docs directly:
`@f5_lifecycle=="release"` / `@f5_lifecycle=="archived"`.

Support Solution (KB) articles have NO deprecation field. Confirmed:

- `@f5_status` is always `"Online"` (35,517 across the whole corpus — single
  facet value). No "Retired"/"Offline"/"Obsolete" state is exposed publicly.
- `@f5_archived` is a red herring. Its only value is the UI-filter label
  `"Archived documents excluded"` (14,311 of 15,164 Support Solutions); the
  other 853 simply lack the field. `@f5_archived<>"Archived documents excluded"`
  returns 0. So it does not discriminate archived-vs-live articles — it is a
  search-facet artifact, not a per-article archived flag. (The earlier note to
  add `@f5_archived=="Archived documents excluded"` to "match the browser UI
  count" is about replicating the UI's default filter, not about archival
  status.)
- `@f5_lifecycle` is absent on Support Solution entirely (0 of 15,164).

Conclusion: a superseded/retired KB Support Solution is only ever indicated in
its body text (e.g. "this article is no longer maintained, see Kxxxxx"), if at
all — there is no structured field. For the documentation types, capture
`f5_lifecycle` to preserve deprecation state.

FULL-FIDELITY PER-ARTICLE DUMP (f5kb dump)
------------------------------------------

The `f5kb dump` subcommand (alongside the flat `f5kb fetch` / `f5kb recent`),
built for archiving complete article records and for discovering exactly which
fields each document type carries.

What it does:
1. Fetches a guest Coveo token (same Aura flow as every other subcommand).
2. For each document type listed in config.yaml's `types:` section, pulls all
   articles modified in the last `--days` days (server-side `@date` window +
   recursive date chunking to beat the 5,000-offset cap, then an exact client-side
   modification-date filter — same technique as `f5kb recent`), or the whole type
   with `--all` (`@rowid` keyset).
3. Requests all fields (no `fieldsToInclude`); see the Limit 2 section for
   how it stays under the 20 MB response cap (page-size 200, auto-halving retry).
4. Writes one JSON file per article to `<out>/<TypeKey>/<id>.json`, splitting
   fields into `metadata` vs `content` objects per the config.
5. Builds a per-type field catalogue (`_catalogue.json` + `_catalogue.md`):
   every field seen, its source (top-level vs `raw`), observed JS type(s),
   occurrence/coverage, a sample value, and a description pulled from config.yaml's
   `field_descriptions:` section when documented.

Config (config.yaml's `types:` section) — one entry per document type with
`documentType`, `metadata`, and `content`. `metadata`/`content` each take `"*"`
(all fields) or an explicit name list; `content` wins on overlap. Intended
workflow: run with `metadata: "*"`, read the catalogue, then narrow to an
explicit keep-list.

Field-selection mechanics: results are flattened into a single name→value
map combining top-level keys (except `raw`) and every `raw.*` key, tagged by
source; top-level wins on a bare-name clash. This is why the catalogue surfaces
both the canonical fields and the noise — e.g. for Support Solution the 134
fields include 20 `sys*` mirrors, 6 duplicate-case variants
(`Title`/`Excerpt`/`ClickUri`/`Uri`/`UniqueId`/`PrintableUri`), ~20 per-query
Coveo plumbing fields (`score`, `rankingInfo`, `Highlights`, `folding`, …),
and redundant `f5_big_ip__version_hierarchy` / `sfdatacategory` encodings.

All 15 types are configured. Each was catalogued (300-article sample over a
10-year window) and curated down to its substantive fields. The catalogue is
generated regardless of the `metadata`/`content` setting (it always surveys
every field seen), so the same run both produces the catalogue and writes the
curated output. Field-count per type after curation ranges from 11 (F5 GitHub)
to 52 (Community).

Body field varies by source backend — discovered while curating:

| Backend / types | Body field |
|-----------------|------------|
| Salesforce Knowledge (Support Solution, Known Issue, Knowledge, Security Advisory, Operations Guide, Policy, Video, Compliance) | `sfdetails__c` |
| Community (Khoros/Lithium forum) | `limessagebody` |
| Education (Zendesk) | `zendeskdescription` |
| Manual, Release Note, Supplemental Document, F5 GitHub, Bug Tracker | none → `content: []` |

The last group is the key finding: the Coveo search index does not return a
full body field for the TechComm/sitemap-sourced docs (Manual, Release Note,
Supplemental Document, F5 GitHub) or for Bug Tracker — the longest free text
available is `excerpt` (~150 chars) and `concepts`. So a full-text dump of those
types is not possible via this API; only metadata + excerpt are captured. (The
actual Manual/Release-Note body lives on the rendered TechComm HTML pages, not
in the search index.)

Body recovery — IMPLEMENTED off-API in `f5kb enrich` (lib/enrich/, 2026-06-03). A
separate resumable post-processor walks a dump directory, fetches each empty-body
article's public page, extracts ONLY the body (no site chrome, nothing that just
repeats the metadata), and writes `content.body_text` (+ `content.sections` where
the source is labelled), `content.bodySource`, `content.fetchedAt`, or
`content.bodyError` on failure. A `TYPE_ENRICHERS` registry maps each type to an
enricher; resumability skips already-bodied/errored articles (`--refetch` /
`--refetch-errors` override). HTTP retry/backoff mirrors `coveoPost`.

- Bug Tracker — deterministic static URL
  `https://cdn.f5.com/product/bugtracker/ID<f5_bug_id>.html`. Extract ONLY
  `<div class="bug-content">` (Symptoms/Impact/Conditions/Workaround/Fix
  Information/Behavior Change/Guides) into `sections` + `body_text`; the header
  block (Affected Products/Versions/Severity/Opened) duplicates metadata and is
  excluded.
- F5 GitHub — GitHub REST API (not HTML). URL dispatcher: `/issues/N`,
  `/pull/N` → `body` markdown; repo root → README (base64-decoded); `/blob/ref/path`
  → raw file. Reads `GITHUB_TOKEN` for the 60→5000 req/hr limit. PRs with no
  description are recorded as a benign `bodyError`.
- Manual / Release Note / Supplemental Document — doc-page scrape via a
  `HOST_RULES` host→selector map + generic fallback (unmapped hosts logged).
  Selectors verified: clouddocs.f5.com `[role=main]` (Sphinx; strip `.headerlink`
  ¶ anchors), techdocs.f5.com `div.pageContent`, docs.nginx.com
  `[data-testid=content]`. Strips in-page nav/sidebar/breadcrumb/prev-next/footer,
  then serializes to markdown (headings/lists/code/links resolved to absolute);
  follows redirects and records the final URL.
- docs.cloud.f5.com (702 of the Manual sample) — Next.js app. Its rendered
  DOM on API pages contains ONLY the nav menu, so it first looked like it needed
  a headless browser — but the body is embedded in the page's
  `<script id="__NEXT_DATA__">` JSON, recoverable by plain fetch + parse (NO
  headless, not even for discovery). Two shapes:
  `props.pageProps.docData.compiledSource` (prose) keeps the authored MDX as
  interleaved `/ ... /` comment blocks → recovered and joined; `docData.swaggerFile`
  (API specs) → rendered to markdown. Host rule flag `nextData: true`.

Full-corpus enrichment (2026-06-04), 13 types (all except Community + F5 GitHub),
106,042 articles. The full Manual corpus surfaced more hosts/edge cases than the
7-day sample; all handled:
- More doc hosts: added `nginx.org` and `unit.nginx.org` (both body in `#content`;
  nginx.org changelog/dir-listing pages have no container → last-resort `<pre>`/`<body>`
  fallback). Generic fallback logs unmapped hosts via console.warn.
- Bug Tracker second template: security/CVE bugs have no `div.bug-content`; a
  fallback parses labelled fields and keeps CVE / Related Article / Vulnerability
  Severity (skips metadata-duplicates). ~6.5% of bugs.
- Soft 404s: techdocs dead links return HTTP 200 with a "404 - Page Not Found"
  body — detected by signature, recorded as bodyError (not captured as body).
- Moved-to-landing redirects: clouddocs `service-proxy/latest/*` URLs redirect to
  a product landing page — detected (specific file or changed top-level section →
  directory root) and recorded as bodyError, not captured.
- F5-KB redirects: some docs.nginx.com URLs 302 into my.f5.com/article/K… (body
  already captured under the Salesforce type) → recorded as a cross-reference.

Final body coverage: Bug_Tracker 22,247/22,247 (100%), Release_Note 757 (100%),
Supplemental_Document 135 (100%), Manual 46,572/46,811 (99%). The ~239 Manual
no-body cases are all legitimate: 117 soft-404 dead links, 90 moved-to-landing
redirects, ~31 image-only/empty stub pages, 1 KB cross-reference. A subagent audit
of all 69,864 enriched bodies confirmed no junk/wrong-container content remains.
All 13 dumped types' on-disk counts equal their live Coveo counts. See README.md
for usage and `f5kb track` for the SQLite change-tracking overview.

Type-specific fields worth noting: Bug Tracker carries `f5_bug_*`
(severity/state/affected+fix version, CVE id); Security Advisory keeps CVE in
`f5_title`/`sfdetails__c` (no dedicated CVE/CVSS facet fields exist); Video adds
the `yt` set (video id, duration, view/like counts); Education uses `zendesk`;
Community uses the `li*` (Lithium) board/topic/message hierarchy.

Resilience: `coveoPost` retries transient failures (network timeout/reset,
HTTP 429/5xx) up to 5 times with exponential backoff (750 ms × 2^n). This was
added after a single transient timeout aborted a full 15-type catalogue run
mid-way. The 400 response-size error is deliberately NOT retried here — it is
handled by `fetchPaged`'s page-halving instead.


APPENDIX A — FIELD INVENTORY
---------------------------

Full field reference across document types (formerly available_fields.txt).
The machine-readable field->description map used by `f5kb dump` to annotate
the catalogue is config.yaml's `field_descriptions:` section; this appendix is the
human view.

Fields returned by the Coveo search API when no fieldsToInclude restriction
is applied. Based on samples from 12 document types across multiple date
ranges (Support Solution, Release Note, Security Advisory, Known Issue,
Manual, Bug Tracker, Knowledge, Policy, Operations Guide, Supplemental
Document, Video, Education).

Both fetch scripts request only a small subset (marked ***) to keep response
sizes below Coveo's 20 MB per-response limit. To use additional fields, add
the raw field name to the FIELDS_TO_INCLUDE array in the script.

To request all fields, remove fieldsToInclude from the search body entirely —
but keep page sizes small (~50 or less) to avoid the 20 MB limit.


DOC TYPE SOURCE MAPPING
-----------------------
F5 uses three distinct backends behind Coveo. The backend determines which
fields are present — especially the sf* group.

  Salesforce Knowledge (SF Knowledge):
    Support Solution, Known Issue, Knowledge, Security Advisory,
    Operations Guide, Policy, Video
    Fields: all sf* fields, f5_kb_id, f5_article_creator, f5_status,
            f5_kb_public, sfdetails__c, sfapplies_to_*, sfdatacategory*, etc.

  Non-Salesforce (file / custom connector):
    Manual, Release Note, Supplemental Document, Bug Tracker
    Fields: no sf* fields. May have filename, sitemapparenturl, f5_bug_*,
            f5_isparentpage, f5_source_category, f5_language, etc.

  Zendesk:
    Education
    Fields: zendesk* fields entirely replace the sf* group.
            subject/syssubject replace title equivalents.


TOP-LEVEL FIELDS
----------------
These sit directly on each result object (result.fieldName).
All doc types return the same top-level shape.

  Field                    Type     Notes
  -----------------------  -------  ------------------------------------------------
  title                    string   Full title including K-number prefix
  clickUri                 string   *** Direct article URL (used as "link")
  printableUri             string   Same as clickUri for these articles
  uri                      string   Internal Salesforce URI (not the public URL)
  excerpt                  string   *** Short extract (~150 chars) used as "summary"
  firstSentences           null     Always null for these articles
  summary                  null     Always null for these articles
  uniqueId                 string   Coveo internal document identifier
  primaryid                string   Coveo content hash identifier
  score                    int      Relevance score (varies by query)
  percentScore             float    Score as a percentage
  rankingInfo              null     Populated only when debug ranking is requested
  rating                   float    Always 0.0 (ratings not used)
  flags                    string   e.g. "HasHtmlVersion;HasAllMetaDataStream"
  hasHtmlVersion           bool     Always true for KB articles
  hasMobileHtmlVersion     bool     Always false
  isTopResult              bool     Promoted result flag
  isRecommendation         bool     Recommended result flag
  isUserActionView         bool     Analytics flag
  titleHighlights          list     Highlight ranges for query terms (empty if no q)
  excerptHighlights        list     Highlight ranges (empty if no q)
  firstSentencesHighlights list     Always empty
  printableUriHighlights   list     Always empty
  summaryHighlights        list     Always empty
  absentTerms              list     Query terms not found in this result
  parentResult             null     Used for folded/grouped results
  childResults             list     Child results when folding is active
  totalNumberOfChildResults int     Count of child results

Note: Coveo also returns capitalised duplicates of many fields (Title, ClickUri,
Excerpt, etc.) — these are identical to the lowercase versions and can be ignored.


RAW FIELDS  (result.raw.fieldName)
-----------------------------------
Fields marked *** are currently included via fieldsToInclude.
Fields marked [sf]  are from Salesforce Knowledge records (SF Knowledge types only).
Fields marked [sys] are Coveo system mirrors of another raw field (same value).
Fields marked [bt]  are Bug Tracker specific.
Fields marked [man] are Manual specific.
Fields marked [rel] appear on Release Note and/or Supplemental Document.
Fields marked [edu] are Education (Zendesk) specific.

  Field                           Type    Notes
  ------------------------------  ------  -----------------------------------------------

  -- Article identity --
  f5_kb_id                        string  KB article number, e.g. "K000161453"
                                          SF Knowledge types only
  sfarticlenumber                 string  [sf]  Same without the "K" prefix: "000161453"
  sfkbid                          string  [sf]  Salesforce KnowledgeArticle ID (kA0Po...)
  sfkavid                         string  [sf]  Salesforce KnowledgeArticleVersion ID (ka0Po...)
  sfknowledgearticleid            string  [sf]  Same as sfkbid
  sfid                            string  [sf]  Same as sfkavid
  syssfid                         string  [sys] Mirror of sfid
  sfurlname                       string  [sf]  URL slug, e.g. "K000161453"
  permanentid                     string  Coveo content hash (stable across re-indexes)
  primaryid                       string  Coveo primary content identifier
  filename                        string  [bt][man][edu] Source filename for non-SF docs
  sysfilename                     string  [sys] Mirror of filename

  -- Title --
  f5_title                        string  Title without the K-number prefix
                                          SF Knowledge types only
  sftitle                         string  [sf]  Same as f5_title
  title                           string  [sys] Full title with K-number prefix
  systitle                        string  [sys] Same as title
  subject                         string  [edu] Article subject (Zendesk title field)
  syssubject                      string  [sys] Mirror of subject

  -- URLs --
  clickableuri                    string  *** Public article URL (used as "link")
  printableuri                    string  Same as clickableuri
                                          Not present on Bug Tracker, Manual, Release Note,
                                          Supplemental Document, Education
  uri                             string  Same as clickableuri
  sfpublic_facing_url__c          string  [sf]  Same as clickableuri
  sysclickableuri                 string  [sys] Mirror of clickableuri
  sysprintableuri                 string  [sys] Mirror of printableuri
  sysuri                          string  [sys] Mirror of uri
  urihash                         string  Hash of the URI
  sysurihash                      string  [sys] Mirror of urihash
  sitemapparenturl                string  [bt][man][rel] Parent URL from sitemap metadata
  sitemaplastmodified             string  [rel] Last-modified date from sitemap entry

  -- Dates (all Unix milliseconds unless noted) --
  f5_original_published_date      int     *** Original publication date
                                          SF Knowledge + Bug Tracker only; absent on
                                          Manual, Release Note, Supplemental Document
  f5_updated_published_date       int     *** Most recent publication/update date
                                          SF Knowledge + Bug Tracker only; absent on
                                          Manual, Release Note, Supplemental Document
  f5_created_date                 int     Article creation date in Salesforce
                                          SF Knowledge + Bug Tracker only
  sffirstpublisheddate            int     *** [sf]  First published date (fallback)
  sfdevc_firstpublisheddate__c    int     [sf]  Same as sffirstpublisheddate
  sflastmodifieddate              int     *** [sf]  Last modified date (fallback)
  sflastpublisheddate             int     [sf]  Last published date
  date                            int     *** [sys] Coveo index date (= sysdate)
  sysdate                         int     [sys] Same as date
  indexeddate                     int     When Coveo last indexed this document (ms)
  sysindexeddate                  int     [sys] Same as indexeddate

  -- Content type & product --
  f5_document_type                list    e.g. ["Support Solution"]
  sfcontent_type__c               string  [sf]  Same value, as a plain string
  f5_version                      list    Full version hierarchy, pipe-delimited.
                                          e.g. ["BIG-IP", "BIG-IP APM|17|17.1", ...]
                                          Not returned for Support Solution in all cases
  f5_product                      list    Top-level product names only,
                                          e.g. ["BIG-IP", "BIG-IP APM"]
                                          Not returned for Support Solution in all cases
  f5_product_grouping             list    Broad grouping, e.g. ["Uncategorized"]
                                          SF Knowledge types only (excl. Support Solution)
  f5_product_module               string  [bt][rel] Sub-module tag, e.g. "LTM", "APM"
  f5_source_name                  string  Always "Service Knowledge" for KB articles
  f5_source_category              string  [rel] Source category label for non-SF content
  f5_language                     string  [rel] Language code for Release Notes /
                                          Supplemental Documents (e.g. "en")
                                          Distinct from sflanguage / language
  sfrecord_type_name__c           string  [sf]  e.g. "Support Service"
  sfrecordtypename                string  [sf]  Same as sfrecord_type_name__c
  sfrecordtypedevelopername       string  [sf]  API name, e.g. "Support_Service"
  sfrecordtypeid                  string  [sf]  Salesforce RecordType ID

  -- Version hierarchy helpers --
  Per-product version hierarchies are populated on articles tagged with that
  product. Each comes in two forms:
    f5_{slug}_version_hierarchy        list  Full numeric hierarchy
    f5_{slug}_short_version_hierarchy  list  Abbreviated numeric hierarchy
  Plus for some older products:
    f5_{slug}_version_pad              list  Zero-padded version for sorting

  Observed slugs and the doc types they appear on:

    Slug                              Types seen on
    --------------------------------  --------------------------------------------------
    big_ip                            Bug Tracker, Knowledge, Known Issue, Operations
                                      Guide, Policy, Security Advisory, Video
    big_ip_aam                        Knowledge, Operations Guide, Policy, Video
    big_ip_afm                        Knowledge, Operations Guide, Policy, Security
                                      Advisory, Video
    big_ip_analytics                  Knowledge, Operations Guide, Policy, Security
                                      Advisory, Video
    big_ip_apm                        Knowledge, Operations Guide, Policy, Security
                                      Advisory, Video
    big_ip_asm                        Knowledge, Operations Guide, Policy, Security
                                      Advisory, Video
    big_ip_dns                        Bug Tracker, Knowledge, Operations Guide, Policy,
                                      Security Advisory, Video
    big_ip_edge_gateway               Knowledge
    big_ip_fps                        Operations Guide, Policy, Security Advisory, Video
    big_ip_gtm                        Knowledge, Manual
    big_ip_link_controller            Knowledge, Operations Guide, Policy, Security
                                      Advisory, Video
    big_ip_ltm                        Knowledge, Known Issue, Operations Guide, Policy,
                                      Security Advisory
    big_ip_next_spk                   Knowledge, Policy, Security Advisory
    big_ip_pem                        Knowledge, Operations Guide, Policy, Security
                                      Advisory, Video
    big_ip_psm                        Knowledge
    big_ip_webaccelerator             Knowledge
    big_ip_wom                        Knowledge
    big_iq_centralized_management     Knowledge, Policy, Security Advisory
    arx                               Knowledge
    enterprise_manager                Knowledge
    f5os_a                            Knowledge, Policy, Security Advisory
    f5os_c                            Knowledge, Policy, Security Advisory
    firepass                          Knowledge
    nginx_controller                  Knowledge            (short form not observed)
    nginx_plus                        Knowledge, Policy, Security Advisory
    nginx_service_mesh                Knowledge            (short form not observed)
    nginx_unit                        Knowledge            (short form not observed)
    traffix_sdc                       Policy, Security Advisory

  Note: Bug Tracker also has f5_big_ip_version_pad and f5_big_ip_dns_version_pad
  (zero-padded versions for sort ordering). Manual has f5_big_ip_gtm_version_pad.

  -- Article body & summary --
  sfdetails__c                    string  [sf]  Full article HTML body (can be large).
                                          SF Knowledge types only.
  sfapplies_to_products__c        string  [sf]  JSON-encoded array of affected products
                                          with embedded version lists. SF Knowledge
                                          types only. Fill rates by type:
                                            Support Solution  94.8%
                                            Known Issue      100.0%
                                            Knowledge         99.4%
                                            Security Advisory 100.0%
                                            Operations Guide  100.0%
                                            Policy            97.8%
                                            Video             34.1%
                                          Value is a JSON string — requires a second
                                          JSON.parse(). Format:
                                          [{"Product":"BIG-IP"},
                                           {"Versions":["16.X.X",...],"Product":"BIG-IP APM"}]
                                          Not returned unless in fieldsToInclude.
  sfapplies_to_versions__c        string  [sf]  Comma-separated version list, e.g.:
                                          "13.0.0, 13.0.1, 13.0.X, 13.1.0, ..."
                                          SF Knowledge types only. Fill rates are
                                          consistently lower than sfapplies_to_products__c
                                          (Support Solution 77.9%, Video 31.6%).
  concepts                        string  Coveo-extracted key concepts (semicolon-sep.)
  sysconcepts                     string  [sys] Mirror of concepts
  wordcount                       int     Word count of the indexed content
  size                            int     Indexed document size in bytes
  syssize                         int     [sys] Mirror of size

  -- Status & visibility --
  f5_status                       string  Publication status, e.g. "Online"
                                          SF Knowledge types only
  sfpublishstatus                 string  [sf]  Same as f5_status
  f5_kb_public                    string  Visibility, e.g. "Public"
                                          SF Knowledge types only
  f5_archived                     string  e.g. "Archived documents excluded"
  f5_is_new_kb_article            string  "true"/"false" string
                                          SF Knowledge types only (excl. Education)
  sfislatestversion               string  [sf]  "true"/"false" string
  sfisvisibleinapp                string  [sf]  Salesforce channel visibility flags
  sfisvisibleincsp                string  [sf]  (Customer Self-service Portal)
  sfisvisibleinpkb                string  [sf]  (Public Knowledge Base)
  sfisvisibleinprm                string  [sf]  (Partner Portal)
  sfvalidationstatus              string  [sf]  e.g. "Validated"

  -- Author / org --
  f5_article_creator              string  Author display name
                                          SF Knowledge types only
  sfarticlecreatedbyname          string  [sf]  Same as f5_article_creator
  f5_employee                     string  "true"/"false" — whether author is F5 employee
                                          SF Knowledge types only
  sff5_employee__c                string  [sf]  Same as f5_employee
  sforganizationid                string  [sf]  Salesforce Org ID: 00D00000000hXqv
  sflanguage                      string  [sf]  e.g. "en_US"
  language                        list    Coveo language, e.g. ["English"]
  syslanguage                     list    [sys] Mirror of language

  -- Salesforce Data Categories (SF Knowledge types only) --
  These are Salesforce Knowledge data category group values used for
  article taxonomy. Observed groups and the types they appear on:

  sfdatacategorybig_ip_group1         string  [sf]  BIG-IP primary category value
                                              Knowledge, Policy, Support Solution
  sfdatacategorybig_ip_group1expanded string  [sf]  Expanded form of above
  sfdatacategorybig_ip_group2         string  [sf]  BIG-IP secondary category value
                                              Knowledge, Policy
  sfdatacategorybig_ip_group2expanded string  [sf]  Expanded form of above
  sfdatacategorynginx                 string  [sf]  NGINX category value. Policy only
  sfdatacategorynginxexpanded         string  [sf]  Expanded form of above
  sfdatacategoryother_f5              string  [sf]  Other F5 products category
                                              Knowledge, Policy
  sfdatacategoryother_f5expanded      string  [sf]  Expanded form of above

  -- Bug Tracker specific fields --
  These fields only appear on Bug Tracker articles.

  f5_bug_id                           string  [bt]  Bug ID number
  f5_bug_severity                     string  [bt]  e.g. "3-Major"
  f5_bug_state                        string  [bt]  e.g. "Closed", "Open"
  f5_bug_product_known_affected_version string [bt] Affected version string
  f5_fix_category                     string  [bt]  Fix classification
  f5_related_kb_id                    string  [bt]  Related KB article number(s)
  f5_alternate_bug_id                 string  [bt]  Alternate/legacy bug ID
  f5_alternate_related_kb_id          string  [bt]  Alternate related KB ID
  f5_agc                              string  [bt]  AGC (product area code) tag
  f5_product_family                   string  [bt]  Product family grouping
  f5_version_display_name             string  [bt]  Human-readable version label
  f5_big_ip                           string  [bt]  BIG-IP version tag for bug
  f5_big_ip_dns                       string  [bt]  BIG-IP DNS version tag for bug
  f5_big_ip_version_pad               list    [bt]  Zero-padded BIG-IP version (sort)
  f5_big_ip_dns_version_pad           list    [bt]  Zero-padded DNS version (sort)

  -- Manual specific fields --
  These fields only appear on Manual (documentation) articles.

  f5_isparentpage                     string  [man] "true" if this is a parent/TOC page
  f5_keywords                         string  [man] SEO/search keywords
  f5_lifecycle                        string  [man] Documentation lifecycle status
  f5_big_ip_gtm                       string  [man] BIG-IP GTM version tag
  f5_big_ip_gtm_version_pad           list    [man] Zero-padded GTM version (sort)

  -- Education / Zendesk fields --
  Education articles are sourced from Zendesk (not Salesforce). The sf* fields
  are entirely absent; these Zendesk-specific fields appear instead.

  zendeskid                           int     [edu] Zendesk article ID
  zendeskitemtype                     string  [edu] e.g. "article"
  zendeskcategoryname                 string  [edu] Zendesk category name
  zendesksectionname                  string  [edu] Zendesk section name
  zendesksubject                      string  [edu] Article subject/title
  zendeskdescription                  string  [edu] Article description/excerpt
  zendesksubmittername                string  [edu] Author name
  zendeskcreationdate                 int     [edu] Creation timestamp (ms)
  zendeskupdatedate                   int     [edu] Last update timestamp (ms)
  zendesklocale                       string  [edu] Locale, e.g. "en-us"
  zendeskdraft                        string  [edu] "true"/"false" draft status
  zendeskfoldingchild                 list    [edu] Zendesk folding child hashes
  zendeskfoldingcollection            string  [edu] Zendesk folding collection hash

  -- Coveo infrastructure --
  source                          string  Coveo source connector name
  syssource                       string  [sys] Mirror of source
  sourcetype                      string  e.g. "Salesforce", "Zendesk", "SitemapCrawler"
  syssourcetype                   string  [sys] Mirror of sourcetype
  collection                      string  Always "default"
  syscollection                   string  [sys] Mirror of collection
  connectortype                   string  e.g. "Salesforce2", "Zendesk2"
  sysconnectortype                string  [sys] Mirror of connectortype
  documenttype                    string  e.g. "Knowledge", "SitemapItem"
  sysdocumenttype                 string  [sys] Mirror of documenttype
  objecttype                      string  "Knowledge" for SF Knowledge types
  objecttypelabel                 string  "Knowledge" for SF Knowledge types
  objecttypelabelplural           string  "Knowledge" for SF Knowledge types
  filetype                        string  e.g. "SalesforceItem", "YouTubeVideo"
  sysfiletype                     string  [sys] Mirror of filetype
  orderingid                      int     Coveo result ordering identifier
  rowid                           int     Coveo internal row ID
  sysrowid                        int     [sys] Mirror of rowid
  transactionid                   int     Coveo transaction ID for this index operation
  systransactionid                int     [sys] Mirror of transactionid

  -- Folding (result grouping) --
  foldingparent                   string  Hash of the parent document in a fold group
                                          Not present on Bug Tracker, Manual, Release Note,
                                          Supplemental Document
  foldingchild                    list    Hashes of child documents in this fold group
                                          Not present on Bug Tracker, Manual, Release Note,
                                          Supplemental Document
  foldingcollection               string  Hash identifying the fold group
                                          Not present on Bug Tracker, Manual, Release Note,
                                          Supplemental Document

  -- Hierarchy (parent chain) --
  parents                         string  XML snippet describing parent records
                                          SF Knowledge types + Education only
  sysparents                      string  [sys] Mirror of parents


FIELDS CURRENTLY USED BY THE FLAT FETCHERS
------------------------------------------
These are the six raw fields requested via fieldsToInclude by `f5kb fetch` /
`f5kb recent`, plus the three top-level fields always returned regardless:

  Top-level (always returned):
    title       → article "name"
    clickUri    → article "link"
    excerpt     → article "summary"

  Raw (via fieldsToInclude):
    clickableuri              fallback for "link" if clickUri is absent
    f5_original_published_date  → "publicationDate" (primary)
    sffirstpublisheddate        → "publicationDate" (fallback)
    f5_updated_published_date   → "modificationDate" (primary)
    sflastmodifieddate          → "modificationDate" (fallback)
    date                        → "modificationDate" (last-resort fallback)

Note: f5_original_published_date and f5_updated_published_date are absent on
Manual, Release Note, and Supplemental Document articles — for those types the
fallback chain (sffirstpublisheddate → sflastmodifieddate → date) is always used.


FACET API GAPS — f5_version / f5_document_type ENUMERATION
------------------------------------------------------------
The f5_version and f5_document_type fields are used for `f5kb list-products` and
`f5kb list-types` respectively. The facet API has two separate limitations that
cause valid filter values to be missing from these lists.

GAP 1 — numberOfValues slot starvation (fixed)

  f5_version stores a pipe-delimited version hierarchy, so a single article
  tagged with "BIG-IP LTM 16.1.0" contributes four facet values:
    "BIG-IP LTM"
    "BIG-IP LTM|16"
    "BIG-IP LTM|16|16.1"
    "BIG-IP LTM|16|16.1|16.1.0"

  Requesting numberOfValues: 500 fills the 500 slots with the most-frequent
  values; versioned hierarchy entries crowd out less-common top-level names:

    numberOfValues  Total returned  Top-level names  moreAvailable
    500             500             37               true
    1000            1000            43               true
    2000            2000            62               true
    5000            2769            73               false

  Fix: the facet helper (coveo/client.py, `list_facet_values()`) requests
  numberOfValues: 5000. The API is exhausted at 2,769 values, returning all 73
  top-level product names.

GAP 2 — Coveo admin exclusion (unfixable from the client side)

  Even with numberOfValues: 5000 and moreValuesAvailable: false, certain
  valid @f5_version values do not appear in the facet results at all.

  Confirmed example: "BIG-IP Documentation"
    - @f5_version=="BIG-IP Documentation" returns 1,654 articles
    - Those articles carry f5_version: ["BIG-IP Documentation"] in their raw fields
    - The value does not appear anywhere in the 2,769 facet results

  This is a Coveo platform-level admin decision; the F5 Coveo administrator
  has excluded specific values from facet indexing (likely for UI cleanliness).
  There is no API call that can reveal which values are excluded.

WORKAROUND — type-filtered facet for hidden products

  Running a facet query with an @f5_document_type filter active scopes the
  facet computation to only the matching documents. Products that own a large
  share of that document type become prominent enough to surface even if they
  are excluded from the global unfiltered facet.

  Example — find all products that have Manual articles:

    POST /rest/search/v2
    {
      "q": "",
      "aq": "@f5_document_type==\"Manual\"",
      "numberOfResults": 0,
      "facets": [{ "field": "f5_version", "numberOfValues": 5000, "type": "specific" }]
    }

  "BIG-IP Documentation" appeared via this technique after
  --product="BIG-IP" --type="Manual" returned only 2 results.

  Use this diagnostic any time a product+type combination returns unexpectedly
  few results: re-run with numberOfResults: 0 and a facets block (filtered by
  that document type) to see what product tags those articles actually carry.

AUTOMATED DISCOVERY — f5kb discover

  The type-filtered facet technique is automated in `f5kb discover`
  (cmd/discover.py).

  How it works:
    1. Fetches all document types from the global f5_document_type facet.
    2. Fetches the global f5_version facet (baseline 73 top-level products).
    3. For each document type, re-runs the f5_version facet with an
       @f5_document_type filter active, collecting any new top-level names.
    4. For each newly discovered name, runs a count query to get total articles.
    5. Writes discovered_products.yaml with all discovered products (its
       `products:` block is copied into config.yaml by hand).

  Final counts (as of last run):

    Source                  Products
    ----------------------  --------
    Global facet            73
    Hidden (type-filtered)  247
    Manually added           1
    Total                   321

  Note — known gap: "BIG-IP Documentation" is not caught by the automated
  scan. It uses a TechComm/Sitemap source connector (not Salesforce), and
  while it does appear in the Manual type-filtered facet results, the
  discover script's top-level name extraction misses it due to how F5's
  Coveo admin has configured that facet. It was added manually to config.yaml's
  `products:` section (source: "manual_query", count: 1,654).
  Query directly with @f5_version=="BIG-IP Documentation".

  Note — confirmed duplicate tags: Two product pairs have confirmed
  duplicate tag names; every article matching one also carries the other:
    "BIG-IP Next CNF"  ↔  "BIG_IP_NEXT(CNF)"   (1,288 articles each)
    "APM Clients"      ↔  "APM-Clients"         (811 articles each)
  Both names appear in config.yaml's `products:` section (both are valid filter
  values). Use the clean name (no underscores/hyphens) when filtering.

  Hidden products by document type that first revealed them:

    Document Type       New hidden products found
    ----------------    ------------------------
    Support Solution    134
    Policy               44
    Bug Tracker          21
    Knowledge            14
    Known Issue          12
    Release Note         10
    Manual                7
    Operations Guide      4
    Security Advisory     1
    Community, F5 GitHub, Video, Education,
    Supplemental Document, Compliance         0

  Output format (one entry per product):
    {
      "product": "BIG-IP TMOS",
      "count": 3570,
      "source": "type_filtered_facet",
      "hiddenFromGlobalFacet": true,
      "discoveredViaTypes": ["Bug Tracker"]
    }

  Run:
    uv run f5kb discover

  Takes ~3-4 minutes (~250 API calls). Use config.yaml's `products:` section as the
  authoritative list of all valid --product values instead of `f5kb list-products`.
