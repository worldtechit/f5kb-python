# F5 KB Article Index — Technical Findings

Reference document for rebuilding or extending the article fetcher.
The goal was to programmatically retrieve F5 KB article metadata (title, URL,
summary, dates) for BIG-IP Support Solution articles without requiring login —
since extended to all 15 document types and to full article bodies (recovered
off-API for the types the search index leaves empty; see the body-recovery
subsection at the end).

---

## Architecture of my.f5.com

The portal is built on **Salesforce Experience Cloud** (formerly Community Cloud)
with the **Lightning/Aura framework**. Search is powered by **Coveo Headless**
(the newer Coveo JS SDK, not the legacy Coveo JS UI Framework).

Key URLs:
- Portal base:      `https://my.f5.com/manage/s/`
- Search page:      `https://my.f5.com/manage/s/global-search/%40uri`
- Article format:   `https://my.f5.com/manage/s/article/K000160969`
- Aura API:         `https://my.f5.com/manage/s/sfsites/aura`
- Salesforce Org ID: `00D00000000hXqv` (from inline.js)

---

## Token / Credential Discovery

### Dead ends
- The page is fully JavaScript-rendered and returns an empty loading shell to curl
- The Salesforce REST API (`/services/data/`) requires authentication
- The Chatter Connect API is disabled for this org
- Searching for `organizationId` in the minified JS yields only template variable
  references, not actual values
- Many Apex class names exist (`CoveoCustomSearch`, `CoveoFullSearch`,
  `CoveoResources`, `CoveoTechDocSearch`, etc.) but none expose `getSearchToken`

### What works
The Salesforce **LWC component** `c/quanticSearchInterface` imports:

    @salesforce/apex/HeadlessController.getHeadlessConfiguration

This is callable via the **Aura ApexActionController** as a guest/unauthenticated
user. The response contains the Coveo organization ID and a fresh bearer token.

**Aura API call to get credentials:**

```
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
```

**Response shape:**

```json
{
  "actions": [{
    "state": "SUCCESS",
    "returnValue": {
      "returnValue": "{\"platformUrl\":\"https://f5networksproduction5vkhn00h.org.coveo.com\",\"accessToken\":\"eyJ...\",\"organizationId\":\"f5networksproduction5vkhn00h\"}"
    }
  }]
}
```

Note: the `returnValue.returnValue` is a **JSON-encoded string**, not an object.
Parse it with a second `JSON.parse()`.

The access token is a JWT issued to the guest/community user, valid for **24 hours**.
The script fetches a fresh one on every run — no need to cache it.

**Important:** The Aura response is plain JSON (no `*/.../*` wrapper) when called
from Deno/fetch. The `*/.../*ERROR*/` wrapper only appears in curl error responses.

---

## Coveo Search API

**Endpoint:**

    POST https://f5networksproduction5vkhn00h.org.coveo.com/rest/search/v2?organizationId=f5networksproduction5vkhn00h

**Auth header:**

    Authorization: Bearer <token from HeadlessController.getHeadlessConfiguration>

**Coveo credentials (static — the org ID never changes):**
- Organization ID:  `f5networksproduction5vkhn00h`
- Platform URL:     `https://f5networksproduction5vkhn00h.org.coveo.com`
- Search hub:       `myF5`

**Minimal request body:**

```json
{
  "q": "",
  "aq": "@f5_document_type==\"Support Solution\" @f5_version==\"BIG-IP\"",
  "numberOfResults": 100,
  "firstResult": 0,
  "searchHub": "myF5",
  "sortCriteria": "date descending"
}
```

**Pagination:** increment `firstResult` by `numberOfResults` each page.
Max `numberOfResults` per page is 1000 (Coveo platform limit).

**Total count:** returned as `totalCountFiltered` (preferred) or `totalCount` in
the response root.

---

## API Limits

Two hard limits are enforced by the Coveo platform. Both are handled
automatically in both scripts.

### Limit 1 — 5,000-result offset cap

The Coveo API rejects any request where `firstResult + numberOfResults > 5000`.
This is a platform-wide limit, not configurable.

**Symptom:** Standard pagination silently stops at 5,000 articles. The total
count in the response drops to 0 on the page that would push past the boundary.

**Fix — recursive date-range chunking:**
Split the query into date windows using `@date>=YYYY/MM/DD@HH:MM:SS` filters
in the `aq` field. Recursively halve any window whose count still exceeds 5,000.
Each leaf window is then safely paged with standard pagination.

Date filter syntax for the `aq` field:

    @date>=2024/07/01@00:00:00 @date<2024/10/01@00:00:00

Coveo's `@date` field reflects the **indexing date**, not the original
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

### Limit 2 — 20 MB response size cap

The Coveo API returns HTTP 400 with `ResponseExceededMaximumSizeException` when
a single response exceeds 20,971,520 bytes (~20 MB).

**Symptom:** Hits when using `--page-size` values above ~200 on queries that
return large result sets (e.g. all BIG-IP Support Solutions).

**Fix — `fieldsToInclude`:**
Add a `fieldsToInclude` array to the search request body listing only the raw
fields the script actually reads. This strips the large HTML body
(`sfdetails__c`) and dozens of other unused fields from each result:

```json
"fieldsToInclude": [
  "clickableuri",
  "f5_original_published_date",
  "f5_updated_published_date",
  "sffirstpublisheddate",
  "sflastmodifieddate",
  "date"
]
```

With this in place, `--page-size=500` is safe even for the largest result sets.
Without it, safe page sizes are roughly ≤100–150 for full-field responses.

**`dump_articles.ts` takes the opposite approach.** It deliberately requests
*all* fields (no `fieldsToInclude`) because its whole purpose is full-fidelity
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
returned results in **batches** (`numberOfResults` per call); it was never
one-article-per-request. The per-article *files* that `dump_articles.ts` writes
are an output-format choice, independent of how many articles each API call
returns.

---

## Key Fields in Search Results

Field availability varies by backend. F5 uses three source systems behind Coveo:
**Salesforce Knowledge** (Support Solution, Known Issue, Knowledge, Security Advisory,
Operations Guide, Policy, Video), **non-SF connectors** (Manual, Release Note,
Supplemental Document, Bug Tracker), and **Zendesk** (Education). The `sf*` fields
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

All timestamps are **Unix milliseconds**. Convert with `new Date(tsMs)`.

See `available_fields.txt` for the complete field inventory across all doc types.

---

## Sitemap (alternative article discovery)

The portal exposes sitemaps at:

    https://my.f5.com/manage/s/sitemap.xml         (index of 4 sitemaps)
    https://my.f5.com/manage/s/sitemap-topicarticle-1.xml  (~20,000 URLs)
    https://my.f5.com/manage/s/sitemap-topicarticle-2.xml  (~15,000 URLs)
    https://my.f5.com/manage/s/sitemap-topicarticle-weekly.xml (~93 URLs)
    https://my.f5.com/manage/s/sitemap-view-1.xml           (5 URLs)

Each entry contains `<loc>` (article URL) and `<lastmod>` only — no title,
summary, or content type. The sitemap covers **all** article types/products
with no filtering capability. Fetching metadata would require a separate HTTP
request per article. Use the Coveo API instead.

---

## Query Filter Syntax

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

---

## Available Document Types (with article counts)

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
| **Support Solution**   | **15,164** |
| Video                  | 529     |

---

## Available Products (top-level, with article counts)

Complete list from `--discover-products` plus manual investigation (321 total:
73 from global facet, 247 type-filtered, 1 facet-excluded). Counts cover all
document types combined. The machine-readable version with metadata is in
`supplemental_products.json`.

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

---

## Count Discrepancy (UI vs API)

The browser search UI reports ~10,616 articles for BIG-IP + Support Solution.
The Coveo API returns ~11,324 for the same query. The difference is likely
because the UI applies additional filters (e.g. archived exclusion via
`@f5_archived`) that the browser hash encodes as `aq=%40f5_archived`.

To match the UI result set more closely, add to `aq`:

    @f5_archived=="Archived documents excluded"

---

## Multi-Product/Type Support

Implemented in `fetch_f5_articles_flex.ts`. The `aq` field is built from
`--product` and `--type` CLI flags:

    --product="NGINX Plus" --type="Security Advisory"
    → @f5_document_type=="Security Advisory" @f5_version=="NGINX Plus"

Use `--list-types` to enumerate valid document types at runtime. For products,
prefer `--discover-products` over `--list-products` — the global facet used by
`--list-products` silently omits ~247 valid product names (see Facet API Gaps
section below). `--discover-products` writes `supplemental_products.json` with
all ~321 known products and is the authoritative source for `--product` values.

**Important:** The `@f5_version` filter matches on the exact top-level product
name, not sub-products. For example, `@f5_version=="BIG-IP"` does NOT match
articles tagged only with `BIG-IP APM` or `BIG-IP LTM`. Each is a separate
product tag. Release Notes, for instance, are tagged under `BIG-IP APM`, not
the generic `BIG-IP`. Always verify with `--list-products` when counts come
back zero for an expected combination.

---

## Facet API Gaps — `--list-products` Is Incomplete

The `--list-products` flag queries the Coveo facet API for all values of the
`f5_version` field. This API has two separate limitations that both cause valid
product names to be missing from the output.

### Gap 1 — numberOfValues slot starvation (fixed)

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
| 5000                     | 2769           | 73                  | **false**     |

**Fix:** request `numberOfValues: 5000`. The API is exhausted at 2,769 values,
yielding 73 distinct top-level product names. The script was updated accordingly.

### Gap 2 — facet exclusion (unfixable from our side)

Even with `numberOfValues: 5000` and `moreValuesAvailable: false`, certain
valid `@f5_version` values do not appear in the facet results at all.

Confirmed example: **"BIG-IP Documentation"**
- `@f5_version=="BIG-IP Documentation"` returns 1,654 articles
- The raw field on those articles contains `f5_version: ["BIG-IP Documentation"]`
- It does not appear anywhere in the 2,769 facet values

This is a Coveo platform-level configuration: the F5 admin has excluded this
value (and possibly others) from facet indexing, likely for UI cleanliness.
There is no way to enumerate excluded values via the API.

**How "BIG-IP Documentation" was discovered:** when `--product="BIG-IP"
--type="Manual"` returned only 2 results, a diagnostic facet query was run
*with the Manual filter active* (`aq: "@f5_document_type==\"Manual\""`). A
filtered facet is computed in the context of matching documents only, so
"BIG-IP Documentation" became prominent enough to appear despite being excluded
from the global unfiltered facet. This is the technique for discovering hidden
products for a specific document type:

```
POST /rest/search/v2
{
  "q": "",
  "aq": "@f5_document_type==\"Manual\"",
  "numberOfResults": 0,
  "facets": [{ "field": "f5_version", "numberOfValues": 5000, "type": "specific" }]
}
```

Run the same query with each document type to build a complete product map.
This technique is now automated: `fetch_f5_articles_flex.ts --discover-products`
iterates every document type, collects all hidden product names, then runs a
count query for each to produce `supplemental_products.json` (321 products total:
73 global, 247 type-filtered, 1 facet-excluded). The standalone `discover_products.ts`
does the same thing independently.

**Practical rule:** Use `--discover-products` once to build `supplemental_products.json`
and use that as the authoritative product list. `--list-products` is fine for a quick
check but will silently omit ~248 products. If a product+type combination returns zero
results, the product name is likely hidden — run `--discover-products` or use the
type-filtered facet query above to find the correct tag.

---

## Duplicate Product Tags

Two confirmed pairs of product tags in `supplemental_products.json` refer to the
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

**Practical rule:** When filtering with `--product`, use the clean name
(`BIG-IP Next CNF`, `APM Clients`). Both forms work, but the clean name is the one
returned by the global facet and is less likely to change.

---

## "BIG-IP Documentation" — TechComm/Sitemap Source

`@f5_version=="BIG-IP Documentation"` returns 1,654 articles. All of them have:
- `f5_document_type: ["Manual"]`
- `source: "TechComm"`, `sourcetype: "Sitemap"`

These are F5's official product documentation pages, crawled via a separate Sitemap
connector — a completely different pipeline from KB articles (which use the Salesforce
connector). The "BIG-IP Documentation" tag is a synthetic marker applied to all
TechComm-sourced docs and is excluded from both the global facet **and** all
type-filtered facets by Coveo admin configuration.

Despite being excluded from facets, it DOES appear in a Manual type-filtered facet
with `numberOfValues: 5000` — the `--discover-products` scan ran this query but the
entry was not captured, making it a known gap in the discovery script. It has been
manually added to `supplemental_products.json` with `source: "manual_query"` and
`hiddenFromTypeFilteredFacets: true`.

**To fetch these articles directly:**

```
POST /rest/search/v2
{ "q": "", "aq": "@f5_version==\"BIG-IP Documentation\"",
  "numberOfResults": 100, "searchHub": "myF5" }
```

These articles also carry many standard product/version hierarchy tags (BIG-IP AAM,
APM, ASM, etc.) so they will appear in `--product="BIG-IP APM"` queries alongside
regular KB articles.

---

## `sfapplies_to_products__c` and `sfapplies_to_versions__c` — Fill Rates

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

**Field value formats:**

`sfapplies_to_products__c` — JSON array string (requires a second JSON.parse):
```json
[{"Product":"BIG-IP"},
 {"Versions":["13.X.X","14.X.X","15.X.X","16.X.X","17.X.X","17.1.1","17.1.0",...],"Product":"BIG-IP APM"}]
```
Each element is an object with a `Product` key and an optional `Versions` array.

`sfapplies_to_versions__c` — plain comma-separated string:
```
"13.0.0, 13.0.1, 13.0.X, 13.1.0, 13.1.1, 13.1.3, 13.1.4, 13.1.5, ..."
```

Note: `sfapplies_to_products__c` is not returned by default — add it to
`fieldsToInclude` explicitly if needed.

---

## Article Deprecation / Lifecycle

Question: which field marks a document as deprecated / no-longer-current?
Investigated via corpus-wide facet queries (2026-06-02).

**`@f5_lifecycle` is the only reliable deprecation signal.** Values: `release`
and `archived`. It is present ONLY on the documentation content types:

| Document type         | release | archived |
|-----------------------|---------|----------|
| Manual                | 6,180   | 399      |
| Release Note          | 391     | 220      |
| Supplemental Document | 63      | 4        |

Filter live vs deprecated docs directly:
`@f5_lifecycle=="release"` / `@f5_lifecycle=="archived"`.

**Support Solution (KB) articles have NO deprecation field.** Confirmed:

- `@f5_status` is always `"Online"` (35,517 across the whole corpus — single
  facet value). No "Retired"/"Offline"/"Obsolete" state is exposed publicly.
- `@f5_archived` is a red herring. Its only value is the UI-filter *label*
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

---

## `dump_articles.ts` — Full-Fidelity Per-Article Dump

A fourth output mode (alongside the flat fetchers and `fetch_recent_by_type.ts`),
built for archiving complete article records and for discovering exactly which
fields each document type carries.

**What it does:**
1. Fetches a guest Coveo token (same Aura flow as every other script).
2. For each document type listed in `dump_config.yaml`, pulls all articles
   modified in the last `--days` days (server-side `@date` window + recursive
   date chunking to beat the 5,000-offset cap, then an exact client-side
   modification-date filter — same technique as `fetch_recent_by_type.ts`).
3. Requests **all** fields (no `fieldsToInclude`); see the Limit 2 section for
   how it stays under the 20 MB response cap (page-size 200, auto-halving retry).
4. Writes one JSON file per article to `<out>/<TypeKey>/<id>.json`, splitting
   fields into `metadata` vs `content` objects per the config.
5. Builds a per-type field catalogue (`_catalogue.json` + `_catalogue.md`):
   every field seen, its source (top-level vs `raw`), observed JS type(s),
   occurrence/coverage, a sample value, and a description pulled from
   `available_fields.txt` when documented.

**Config (`dump_config.yaml`)** — one entry per document type with
`documentType`, `metadata`, and `content`. `metadata`/`content` each take `"*"`
(all fields) or an explicit name list; `content` wins on overlap. Intended
workflow: run with `metadata: "*"`, read the catalogue, then narrow to an
explicit keep-list.

**Field-selection mechanics:** results are flattened into a single name→value
map combining top-level keys (except `raw`) and every `raw.*` key, tagged by
source; top-level wins on a bare-name clash. This is why the catalogue surfaces
both the canonical fields and the noise — e.g. for Support Solution the 134
fields include 20 `sys*` mirrors, 6 duplicate-case variants
(`Title`/`Excerpt`/`ClickUri`/`Uri`/`UniqueId`/`PrintableUri`), ~20 per-query
Coveo plumbing fields (`score`, `rankingInfo`, `*Highlights`, `folding*`, …),
and redundant `f5_big_ip_*_version_hierarchy` / `sfdatacategory*` encodings.

**All 15 types are configured.** Each was catalogued (300-article sample over a
10-year window) and curated down to its substantive fields. The catalogue is
generated regardless of the `metadata`/`content` setting (it always surveys
every field seen), so the same run both produces the catalogue and writes the
curated output. Field-count per type after curation ranges from 11 (F5 GitHub)
to 52 (Community).

**Body field varies by source backend** — discovered while curating:

| Backend / types | Body field |
|-----------------|------------|
| Salesforce Knowledge (Support Solution, Known Issue, Knowledge, Security Advisory, Operations Guide, Policy, Video, Compliance) | `sfdetails__c` |
| Community (Khoros/Lithium forum) | `limessagebody` |
| Education (Zendesk) | `zendeskdescription` |
| Manual, Release Note, Supplemental Document, F5 GitHub, Bug Tracker | none → `content: []` |

The last group is the key finding: the Coveo search index does **not** return a
full body field for the TechComm/sitemap-sourced docs (Manual, Release Note,
Supplemental Document, F5 GitHub) or for Bug Tracker — the longest free text
available is `excerpt` (~150 chars) and `concepts`. So a full-text dump of those
types is not possible via this API; only metadata + excerpt are captured. (The
actual Manual/Release-Note body lives on the rendered TechComm HTML pages, not
in the search index.)

**Body recovery — IMPLEMENTED off-API in `enrich_bodies.ts` (2026-06-03).** A
separate resumable post-processor walks a dump directory, fetches each empty-body
article's public page, extracts ONLY the body (no site chrome, nothing that just
repeats the metadata), and writes `content.body_text` (+ `content.sections` where
the source is labelled), `content.bodySource`, `content.fetchedAt`, or
`content.bodyError` on failure. A `TYPE_ENRICHERS` registry maps each type to an
enricher; resumability skips already-bodied/errored articles (`--refetch` /
`--refetch-errors` override). HTTP retry/backoff mirrors `coveoPost`.

- **Bug Tracker** — deterministic static URL
  `https://cdn.f5.com/product/bugtracker/ID<f5_bug_id>.html`. Extract ONLY
  `<div class="bug-content">` (Symptoms/Impact/Conditions/Workaround/Fix
  Information/Behavior Change/Guides) into `sections` + `body_text`; the header
  block (Affected Products/Versions/Severity/Opened) duplicates metadata and is
  excluded.
- **F5 GitHub** — GitHub REST API (not HTML). URL dispatcher: `/issues/N`,
  `/pull/N` → `body` markdown; repo root → README (base64-decoded); `/blob/ref/path`
  → raw file. Reads `GITHUB_TOKEN` for the 60→5000 req/hr limit. PRs with no
  description are recorded as a benign `bodyError`.
- **Manual / Release Note / Supplemental Document** — doc-page scrape via a
  `HOST_RULES` host→selector map + generic fallback (unmapped hosts logged).
  Selectors verified: clouddocs.f5.com `[role=main]` (Sphinx; strip `.headerlink`
  ¶ anchors), techdocs.f5.com `div.pageContent`, docs.nginx.com
  `[data-testid=content]`. Strips in-page nav/sidebar/breadcrumb/prev-next/footer,
  then serializes to markdown (headings/lists/code/links resolved to absolute);
  follows redirects and records the final URL.
- **docs.cloud.f5.com** (702 of the Manual sample) — Next.js app. Its rendered
  DOM on API pages contains ONLY the nav menu, so it first *looked* like it needed
  a headless browser — but the body is embedded in the page's
  `<script id="__NEXT_DATA__">` JSON, recoverable by plain fetch + parse (NO
  headless, not even for discovery). Two shapes:
  `props.pageProps.docData.compiledSource` (prose) keeps the authored MDX as
  interleaved `/* ... */` comment blocks → recovered and joined; `docData.swaggerFile`
  (API specs) → rendered to markdown. Host rule flag `nextData: true`.

**Coverage (full Manual run over the --days=7 dump, 2026-06-03): 3336/3338 (99.9%)**
— clouddocs 1792/1794, techdocs 510/510, nginx 332/332, docs.cloud 702/702; the 2
misses are genuinely-empty stub pages. All 15 document types now have bodies (9 from
the index, 5 enriched off-API). See `readme.txt` for the `enrich_bodies.ts` usage.

**Type-specific fields worth noting:** Bug Tracker carries `f5_bug_*`
(severity/state/affected+fix version, CVE id); Security Advisory keeps CVE in
`f5_title`/`sfdetails__c` (no dedicated CVE/CVSS facet fields exist); Video adds
the `yt*` set (video id, duration, view/like counts); Education uses `zendesk*`;
Community uses the `li*` (Lithium) board/topic/message hierarchy.

**Resilience:** `coveoPost` retries transient failures (network timeout/reset,
HTTP 429/5xx) up to 5 times with exponential backoff (750 ms × 2^n). This was
added after a single transient timeout aborted a full 15-type catalogue run
mid-way. The 400 response-size error is deliberately NOT retried here — it is
handled by `fetchPaged`'s page-halving instead.
