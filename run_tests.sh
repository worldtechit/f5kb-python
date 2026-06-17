#!/usr/bin/env bash
# run_tests.sh — exercise every f5kb feature and log all output for review.
# Run from repo root: bash run_tests.sh
# Output saved to: /tmp/f5kb_test_run_<timestamp>.log

set -u
LOG="/tmp/f5kb_test_run_$(date -u +%Y%m%dT%H%M%SZ).log"
PASS=0
FAIL=0
SKIPPED=0

# ── helpers ─────────────────────────────────────────────────────────────────

section() {
    echo "" | tee -a "$LOG"
    echo "══════════════════════════════════════════════════════════" | tee -a "$LOG"
    echo "  $*" | tee -a "$LOG"
    echo "══════════════════════════════════════════════════════════" | tee -a "$LOG"
}

run() {
    local label="$1"; shift
    echo "" | tee -a "$LOG"
    echo "▶ [$label]" | tee -a "$LOG"
    echo "  cmd: $*" | tee -a "$LOG"
    echo "  ---" | tee -a "$LOG"
    ("$@" 2>&1) | tee -a "$LOG"
    if [[ ${PIPESTATUS[0]} -eq 0 ]]; then
        echo "  ✓ exit 0" | tee -a "$LOG"
        PASS=$((PASS + 1))
    else
        echo "  ✗ exit non-zero" | tee -a "$LOG"
        FAIL=$((FAIL + 1))
    fi
}

expect_fail() {
    local label="$1"; shift
    echo "" | tee -a "$LOG"
    echo "▶ [$label] (expect non-zero exit)" | tee -a "$LOG"
    echo "  cmd: $*" | tee -a "$LOG"
    echo "  ---" | tee -a "$LOG"
    ("$@" 2>&1) | tee -a "$LOG"
    if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
        echo "  ✓ correctly failed" | tee -a "$LOG"
        PASS=$((PASS + 1))
    else
        echo "  ✗ expected non-zero but got exit 0" | tee -a "$LOG"
        FAIL=$((FAIL + 1))
    fi
}

# ── setup ────────────────────────────────────────────────────────────────────

: > "$LOG"
echo "f5kb Python — full feature test run" | tee -a "$LOG"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "$LOG"
echo "PWD: $(pwd)" | tee -a "$LOG"

DUMP_MINI=tests/fixtures/dump_mini
DUMP=/tmp/f5kb_testrun_dump
SYNC_DUMP=/tmp/f5kb_testrun_sync
DB=/tmp/f5kb_testrun.db
DB2=/tmp/f5kb_testrun2.db
DB_CL=/tmp/f5kb_testrun_cl.db
DB_SYNC=/tmp/f5kb_testrun_sync.db
CL=/tmp/f5kb_testrun.jsonl
GATE_DUMP=/tmp/f5kb_testrun_gate

# clean slate
rm -rf "$DUMP" "$SYNC_DUMP" "$GATE_DUMP" "$DB" "$DB2" "$DB_CL" "$DB_SYNC" "$CL" \
       /tmp/f5kb_testrun_* /tmp/gate_testrun_*
mkdir -p "$DUMP" "$SYNC_DUMP"

# ── §0  version / help ───────────────────────────────────────────────────────

section "§0  version / help [offline]"
run "version"       uv run f5kb --version
run "help"          uv run f5kb --help

# ── §1  test suite ───────────────────────────────────────────────────────────

section "§1  full test suite [offline]"
run "pytest-all"        uv run pytest tests/ -q
run "pytest-unit"       uv run pytest tests/unit/ -v
run "pytest-integ"      uv run pytest tests/integration/ -v
run "pytest-regression" uv run pytest tests/regression/ -v
if [[ "${RUN_LIVE:-}" == "1" ]]; then
    run "pytest-live"   uv run pytest -m live -v
else
    echo "  SKIPPED pytest-live (set RUN_LIVE=1 to include)" | tee -a "$LOG"
    SKIPPED=$((SKIPPED + 1))
fi

# ── §2  track ────────────────────────────────────────────────────────────────

section "§2  track [offline]"
run "track-first"       uv run f5kb track --dump "$DUMP_MINI" --db "$DB"
run "track-idempotent"  uv run f5kb track --dump "$DUMP_MINI" --db "$DB"
run "track-json"        uv run f5kb track --dump "$DUMP_MINI" --db "$DB" --json
run "track-types"       uv run f5kb track --dump "$DUMP_MINI" --db "$DB" --types=Knowledge
run "track-exclude"     uv run f5kb track --dump "$DUMP_MINI" --db "$DB" --exclude-types=Knowledge

# ── §3  status ───────────────────────────────────────────────────────────────

section "§3  status [offline]"
run "status-init"       uv run f5kb track --dump "$DUMP_MINI" --db "$DB2"
run "status-table"      uv run f5kb status --dump "$DUMP_MINI" --db "$DB2"
run "status-json"       uv run f5kb status --dump "$DUMP_MINI" --db "$DB2" --json
run "status-no-db"      uv run f5kb status --dump "$DUMP_MINI"

# ── §4  approve (empty) ──────────────────────────────────────────────────────

section "§4  approve — empty dump [offline]"
TMP_DUMP=$(mktemp -d)
run "approve-list"      uv run f5kb approve --dump "$TMP_DUMP" --list
run "approve-reject"    uv run f5kb approve --dump "$TMP_DUMP" --reject
run "approve-list-json" uv run f5kb approve --dump "$TMP_DUMP" --list --json
rm -rf "$TMP_DUMP"

# ── §5  changelog ────────────────────────────────────────────────────────────

section "§5  changelog [offline]"
run "track-changelog"   uv run f5kb track --dump "$DUMP_MINI" --db "$DB_CL" --changelog "$CL"
run "changelog-sample"  bash -c "head -1 '$CL' | python3 -m json.tool"

# ── §6  dump [network] ───────────────────────────────────────────────────────

section "§6  dump [network]"
run "dump-knowledge"    uv run f5kb dump --all --out "$DUMP" --types=Knowledge --limit=3
run "dump-ls"           ls "$DUMP/Knowledge/"
run "dump-index"        bash -c "python3 -m json.tool '$DUMP/_index.json'"
run "dump-days"         uv run f5kb dump --days=7 --out "$DUMP" --types=Support_Solution --limit=5

# ── §7  enrich [network] ─────────────────────────────────────────────────────

section "§7  enrich [network]"
run "dump-bug"          uv run f5kb dump --all --out "$DUMP" --types=Bug_Tracker --limit=3 --yes
run "enrich-bug"        uv run f5kb enrich --dump "$DUMP" --types=Bug_Tracker --limit=2 --concurrency=2
run "enrich-refetch"    uv run f5kb enrich --dump "$DUMP" --refetch-errors
run "enrich-excl"       uv run f5kb enrich --dump "$DUMP" --exclude-types=F5_GitHub
run "enrich-report"     bash -c "python3 -m json.tool '$DUMP/_enrich_report.json'"

# ── §8  sync [network] ───────────────────────────────────────────────────────

section "§8  sync [network]"
run "sync-dry"          uv run f5kb sync --days=3 --out "$SYNC_DUMP" --db "$DB_SYNC" --dry-run --types=Knowledge --limit=5
run "sync-real"         uv run f5kb sync --days=3 --out "$SYNC_DUMP" --db "$DB_SYNC" --yes --types=Knowledge --limit=5 --no-enrich
run "sync-since-last"   uv run f5kb sync --since-last-run --out "$SYNC_DUMP" --db "$DB_SYNC" --types=Knowledge --no-enrich --limit=5

# ── §9  reconcile [network] ──────────────────────────────────────────────────

section "§9  reconcile [network]"
run "reconcile-report"  uv run f5kb reconcile --dump "$SYNC_DUMP" --db "$DB_SYNC" --types=Knowledge
run "reconcile-json"    uv run f5kb reconcile --dump "$SYNC_DUMP" --db "$DB_SYNC" --types=Knowledge --json

# ── §10  fetch [network] ─────────────────────────────────────────────────────

section "§10  fetch [network]"
run "fetch-nginx"   uv run f5kb fetch --product="NGINX Plus" --type="Security Advisory" --limit=5 --output /tmp/f5kb_testrun_nginx.json --csv /tmp/f5kb_testrun_nginx.csv
run "fetch-json"    bash -c "python3 -m json.tool /tmp/f5kb_testrun_nginx.json | head -30"
run "fetch-csv"     bash -c "head -3 /tmp/f5kb_testrun_nginx.csv"
run "fetch-bigip"   uv run f5kb fetch --product="BIG-IP" --limit=5 --output /tmp/f5kb_testrun_bigip.json

# ── §11  recent [network] ────────────────────────────────────────────────────

section "§11  recent [network]"
run "recent-multi"  uv run f5kb recent --days=7 --out /tmp/f5kb_testrun_recent --types=Knowledge,Support_Solution --limit=5
run "recent-ls"     ls /tmp/f5kb_testrun_recent/
run "recent-index"  bash -c "python3 -m json.tool /tmp/f5kb_testrun_recent/_index.json"
run "recent-1day"   uv run f5kb recent --days=1 --out /tmp/f5kb_testrun_recent2

# ── §12  list-types [network] ────────────────────────────────────────────────

section "§12  list-types [network]"
run "list-types"        uv run f5kb list-types
run "list-types-json"   bash -c "uv run f5kb list-types --json | python3 -m json.tool | head -30"

# ── §13  list-products [network] ─────────────────────────────────────────────

section "§13  list-products [network]"
run "list-products"       uv run f5kb list-products
run "list-products-json"  bash -c "uv run f5kb list-products --json | python3 -m json.tool | head -30"

# ── §14  discover [network, slow ~3-4 min] ───────────────────────────────────

section "§14  discover [network, ~3-4 min]"
if [[ "${SKIP_DISCOVER:-}" == "1" ]]; then
    echo "  SKIPPED (set SKIP_DISCOVER=1)" | tee -a "$LOG"
    SKIPPED=$((SKIPPED + 1))
else
    run "discover-yaml"  uv run f5kb discover --out /tmp/f5kb_testrun_products.yaml
    run "discover-json"  uv run f5kb discover --format=json --out /tmp/f5kb_testrun_products.json
    run "discover-peek"  bash -c "python3 -m json.tool /tmp/f5kb_testrun_products.json | head -30"
fi

# ── §15  global flags ────────────────────────────────────────────────────────

section "§15  global flags [offline/network]"
run "flag-verbose"    uv run f5kb --verbose track --dump "$DUMP_MINI" --db /tmp/f5kb_testrun_v.db
run "flag-quiet"      uv run f5kb --quiet   track --dump "$DUMP_MINI" --db /tmp/f5kb_testrun_q.db
run "flag-json-logs"  bash -c "uv run f5kb --json-logs track --dump '$DUMP_MINI' --db /tmp/f5kb_testrun_j.db 2>/tmp/f5kb_testrun_logs.jsonl; head -1 /tmp/f5kb_testrun_logs.jsonl | python3 -m json.tool"
run "flag-sep-out"    bash -c "uv run f5kb track --dump '$DUMP_MINI' --db /tmp/f5kb_testrun_sep.db --json >/tmp/f5kb_testrun_result.json 2>/tmp/f5kb_testrun_logs.txt; python3 -m json.tool /tmp/f5kb_testrun_result.json"

# ── §16  approval gate end-to-end ────────────────────────────────────────────

section "§16  approval gate end-to-end [offline]"
cp -r "$DUMP_MINI" "$GATE_DUMP"
GATE_DB=/tmp/f5kb_testrun_gate.db
run "gate-track"   uv run f5kb track --dump "$GATE_DUMP" --db "$GATE_DB"
# stage a fake edit
PDIR="$GATE_DUMP/_pending/Knowledge"
mkdir -p "$PDIR"
cp "$GATE_DUMP/Knowledge/K14448.json" "$PDIR/K14448.json"
python3 -c "
import json, pathlib
m = {
    'generatedAt': '2026-06-15T00:00:00Z',
    'entries': [{
        'typeKey': 'Knowledge',
        'id': 'K14448',
        'op': 'edited',
        'source': 'dump',
        'changed': ['metadata'],
        'stagedAt': '2026-06-15T00:00:00Z'
    }]
}
pathlib.Path('$GATE_DUMP/_pending/_manifest.json').write_text(json.dumps(m, indent=2))
"
run "gate-list"    uv run f5kb approve --dump "$GATE_DUMP" --db "$GATE_DB" --list
run "gate-apply"   uv run f5kb approve --dump "$GATE_DUMP" --db "$GATE_DB"
run "gate-verify"  bash -c "ls '$GATE_DUMP/_replaced/Knowledge/' && echo 'archive present'"

# ── §17  error handling ──────────────────────────────────────────────────────

section "§17  error handling [offline]"
expect_fail "err-dump-no-mode"    uv run f5kb dump --out /tmp/x
expect_fail "err-bad-subcommand"  uv run f5kb not-a-command
expect_fail "err-sync-conflict"   uv run f5kb sync --all --days=3 --out /tmp/x
run         "err-status-missing"  uv run f5kb status --dump /tmp/does_not_exist

# ── §18  hash compatibility ──────────────────────────────────────────────────

section "§18  SHA256 hash check [offline]"
run "hash-check" uv run python3 -c "
from f5kb.track.hashing import sha256_obj
import json
art = json.load(open('tests/fixtures/dump_mini/Knowledge/K14448.json'))
print('metadata hash:', sha256_obj(art['metadata']))
print('content hash: ', sha256_obj(art.get('content', {})))
"

# ── summary ──────────────────────────────────────────────────────────────────

section "SUMMARY"
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "$LOG"
echo "  passed:  $PASS" | tee -a "$LOG"
echo "  failed:  $FAIL" | tee -a "$LOG"
echo "  skipped: $SKIPPED" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "Full log: $LOG" | tee -a "$LOG"

[[ $FAIL -eq 0 ]]
