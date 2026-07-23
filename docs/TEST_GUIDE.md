# Test Guide

## Setup (fresh clone)

```
git clone https://github.com/worldtechit/f5kb-python
cd f5kb-python
uv sync
```

---

## Part 1 — Full automated run (`run_tests.sh`)

`run_tests.sh` is the single command that exercises everything. Run from repo root:

```
bash run_tests.sh
```

Sections it covers:

| Section | What | Network |
|---------|------|---------|
| §0 | version + help flags | offline |
| §1 | full pytest suite — 488 offline tests across 37 files (5 live tests skipped unless `RUN_LIVE=1`) | offline |
| §2 | track — first run, idempotent, `--json`, `--types`, `--exclude-types` | offline |
| §3 | status — table + JSON output, missing DB | offline |
| §4 | approve — empty pending dir, `--list`, `--reject`, `--json` | offline |
| §5 | changelog — track writes JSONL, sample line is valid JSON | offline |
| §6 | dump — `--all + --limit`, `--days`, per-type | network |
| §7 | enrich — Bug_Tracker, `--refetch-errors`, `--exclude-types`, enrich report | network |
| §8 | sync — `--dry-run`, `--yes`, `--since-last-run` | network |
| §9 | reconcile — report-only, `--json` | network |
| §10 | fetch — `--product`, `--type`, `--csv`, `--output` | network |
| §11 | recent — `--days`, `--types`, `--limit`, `_index.json` | network |
| §12 | list-types — table + JSON | network |
| §13 | list-products — table + JSON | network |
| §14 | discover — yaml + json output (~3-4 min, ~250 API calls) | network |
| §15 | global flags — `--verbose`, `--quiet`, `--json-logs`, stdout/stderr separation | offline/network |
| §16 | approval gate end-to-end — stage fake edit, `approve --list`, approve, verify archive | offline |
| §17 | error handling — bad flags, bad subcommand, conflicting flags, missing paths | offline |
| §18 | SHA256 hash — hash a fixture article directly | offline |

Log saved automatically to: `/tmp/f5kb_test_run_<timestamp>.log`

**Env overrides:**

```
RUN_LIVE=1        include live/network pytest tests in §1
SKIP_DISCOVER=1   skip §14 discover (~3-4 min)
```

Full unattended run (live tests on, discover skipped):

```
RUN_LIVE=1 SKIP_DISCOVER=1 bash run_tests.sh
```

> **Note:** `pytest-regression` (§1) will log `✗ exit non-zero` — the regression dir is an empty placeholder. Known false failure, not a real bug.

---

## Part 2 — Offline pytest (no network)

```
uv run pytest                  # 302 tests, ~5s
uv run pytest tests/unit/      # 24 files, pure logic
uv run pytest tests/integration/  # CLI smoke tests (CliRunner)
```

Specific modules:

```
uv run pytest tests/unit/test_dump.py          # dump: new/skip/stage/--yes/limit
uv run pytest tests/unit/test_sync.py          # sync: --days/--all/--since-last-run/--dry-run
uv run pytest tests/unit/test_reconcile.py     # reconcile: detect/apply/threshold guard/purge
uv run pytest tests/unit/test_approve.py       # gate: promote/reject/risky/archive
uv run pytest tests/unit/test_staging.py       # _pending/_manifest read/write
uv run pytest tests/unit/test_track_db.py      # SQLite rows, hashes, run/changes tables
uv run pytest tests/unit/test_status.py        # status aggregation
uv run pytest tests/unit/test_changelog.py     # JSONL append, all op types
uv run pytest tests/unit/test_hashing.py       # sha256_obj, has_body
uv run pytest tests/unit/test_coveo_client.py  # CoveoClient + scripted transport
uv run pytest tests/unit/test_coveo_dates.py   # date-range chunking
uv run pytest tests/unit/test_aura.py          # Aura guest-token fetch
uv run pytest tests/unit/test_progress.py      # TTY vs plain progress output
uv run pytest tests/unit/test_enrichers.py     # body extractors (mocked HTTP)
uv run pytest tests/unit/test_enrich_driver.py # enrich orchestrator
uv run pytest tests/unit/test_bugtracker.py    # Bug Tracker HTML parser
uv run pytest tests/unit/test_docpage.py       # doc-page scraper (host→selector map)
uv run pytest tests/unit/test_nextdata.py      # Next.js __NEXT_DATA__ extractor
uv run pytest tests/unit/test_html_serialize.py  # HTML→text serializer
uv run pytest tests/unit/test_github.py        # GitHub REST client
uv run pytest tests/unit/test_fsutil.py        # path helpers, iso_now, now_stamp
uv run pytest tests/unit/test_config_loader.py # config.yaml parse + validation
uv run pytest tests/unit/test_coveo_fields.py  # field routing (metadata vs content)
uv run pytest tests/unit/test_logger.py        # structured logging, --json-logs
```

Verbose output on failure:

```
uv run pytest tests/unit/test_dump.py -v --tb=short
```

---

## Part 3 — Live / network tests

Requires internet access to my.f5.com and f5networksproduction5vkhn00h.org.coveo.com.
No login or token needed — guest token fetched automatically.

```
uv run pytest -m live -v
```

| Test | What it verifies |
|------|-----------------|
| `test_fetch_coveo_config_returns_org_id` | guest token + org ID returned |
| `test_list_facet_values_returns_types` | Coveo facet returns ≥1 document type |
| `test_fetch_type_since_returns_limit` | fetch 3 Knowledge articles from live API |
| `test_fetch_results_have_required_keys` | id/title/metadata/content present on live results |
| `test_dump_cmd_writes_json_files` | CLI `dump --all --types=Knowledge --limit=3` writes 3 JSON files |

Optional: set `GITHUB_TOKEN` to raise the GitHub API limit (60 → 5,000 req/hr) for F5_GitHub enrichment.

---

## Part 4 — Code quality

```
uv run ruff check .        # lint (E, F, I rules; config.yaml excluded)
uv run ruff format .       # format (or --check to verify without writing)
uv run mypy f5kb/          # type-check the package
```

---

## Part 5 — Manual spot-checks (post-network run)

After `run_tests.sh`, spot-check a few outputs:

```
# 1. Dump structure
ls /tmp/f5kb_testrun_dump/Knowledge/
python3 -m json.tool /tmp/f5kb_testrun_dump/_index.json

# 2. Enrich report
python3 -m json.tool /tmp/f5kb_testrun_dump/_enrich_report.json

# 3. Changelog
cat /tmp/f5kb_testrun.jsonl | python3 -m json.tool

# 4. Approval gate archive
ls /tmp/f5kb_testrun_gate/_replaced/Knowledge/
# should contain K14448.<timestamp>.json

# 5. Fetch CSV
head -5 /tmp/f5kb_testrun_nginx.csv

# 6. Recent index
python3 -m json.tool /tmp/f5kb_testrun_recent/_index.json

# 7. SHA256 determinism (run twice — hashes must match)
uv run python3 -c "
from f5kb.track.hashing import sha256_obj
import json
art = json.load(open('tests/fixtures/dump_mini/Knowledge/K14448.json'))
print(sha256_obj(art['metadata']))
print(sha256_obj(art.get('content', {})))
"
```

---

## Coverage summary

| What | Count | How to run |
|------|-------|-----------|
| Offline pytest tests | 302 | `uv run pytest` |
| Live/network pytest tests | 5 | `uv run pytest -m live` |
| End-to-end CLI sections | 18 | `bash run_tests.sh` |
| Code quality checks | 3 | ruff + mypy |

**Everything in one command:**

```
RUN_LIVE=1 bash run_tests.sh && uv run ruff check . && uv run mypy f5kb/
```

---

## Capturing output for review

`run_tests.sh` saves to `/tmp/f5kb_test_run_<timestamp>.log` automatically.
To also capture ruff and mypy:

```
RUN_LIVE=1 bash run_tests.sh \
  && uv run ruff check . 2>&1 | tee /tmp/f5kb_ruff.log \
  && uv run mypy f5kb/ 2>&1 | tee /tmp/f5kb_mypy.log
```

Or combine everything into one file:

```
{ RUN_LIVE=1 bash run_tests.sh; uv run ruff check .; uv run mypy f5kb/; } 2>&1 | tee /tmp/f5kb_full_run.log
```
