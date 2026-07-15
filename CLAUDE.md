# CLAUDE.md

Project guide for Claude Code working in this repo. Read before making changes.

## What this is

A toolkit that builds and maintains a local, full-fidelity index of **F5 Knowledge
Base articles** (metadata + full body text) for every document type, with **no
login**. F5's support portal (my.f5.com) has no public REST API; the only public
path is the **Coveo guest-token search backend**, accessed via a token fetched at
runtime from a Salesforce Aura endpoint.

## Runtime & dependencies

- **Python 3.11+** with **uv** package manager (required; no pip/venv).
- `uv sync` installs everything from `pyproject.toml` + `uv.lock`.
- Key dependencies: `httpx` (HTTP), `click` (CLI), `beautifulsoup4` + `lxml` (HTML
  parsing), `pyyaml` (config), `pytest` (testing).
- SQLite3 via Python's built-in `sqlite3` module (no external install).
- Run any subcommand: `uv run f5kb <sub>` — no venv activation needed.

## Portability (keep the project zip-and-move-able)

Hard project goal: the toolkit must stay self-contained — runnable on any OS after
a plain `git clone` + `uv sync`, with zero manual setup. Preserve these invariants:

- **No absolute or machine-specific paths in code.** Every path default is relative
  to CWD (`outputs/dump`, `outputs/articles.db`) and overridable via flags (`--out`,
  `--dump`, `--db`). Build paths with `pathlib.Path` and `/` joins, not string concat.
- **No secrets in the repo or in `outputs/`.** The Coveo guest token is fetched at
  runtime; `GITHUB_TOKEN` is read from env only.
- **`uv.lock` is committed** so a fresh machine resolves identical versions with no
  extra steps.
- **`outputs/` is git-ignored but travels in the zip.** Code + `config.yaml` + docs
  come from git; the dump + DB come from the zip. Either can be regenerated from
  the other.
- **Offline-by-default tests.** The suite uses DI'd httpx transports; `uv run pytest`
  passes immediately on a new machine. Live (network) tests are opt-in: `pytest -m live`.

## The CLI

Everything is one entry point with subcommands. `f5kb --help` lists them;
`f5kb <sub> --help` shows flags. Subcommands: `dump`, `enrich`, `track`, `sync`,
`reconcile`, `approve`, `status`, `fetch`, `recent`, `list-types`, `list-products`,
`discover`. Global flags: `--verbose` / `--debug` / `--quiet` / `--json-logs` /
`--help` / `--version`. Logs/progress go to STDERR; `--json` payload goes to STDOUT.

Common `uv run` shortcuts:
```
uv run f5kb dump ...
uv run f5kb sync ...
uv run pytest             # run offline tests
uv run pytest -m live     # run live/network tests
uv run ruff check .       # lint
uv run ruff format .      # format (NOT config.yaml — see below)
uv run mypy f5kb/         # type-check
```

## The pipeline (run in order)

```
f5kb dump  →  f5kb enrich  →  f5kb track     (then f5kb status for a health report)
```
1. **`f5kb dump`** (reads `config.yaml`) — one JSON per article under
   `outputs/dump/<Type>/<id>.json`, fields split into `metadata`/`content` per config.
   Use `--all` (full corpus) or `--days=N`; `--out`, `--types`.
2. **`f5kb enrich`** — fills `content` for the 5 types the Coveo index leaves empty
   (Bug_Tracker, Manual, Release_Note, Supplemental_Document, F5_GitHub).
   `--dump`, `--types`, `--refetch-errors`, `--concurrency`.
3. **`f5kb track`** — SQLite master overview (`outputs/articles.db`): per-article
   dates + metadata/content hashes; new/changed/unchanged/removed across runs.

**Incremental refresh: `f5kb sync`** runs all three steps but only rewrites/
re-enriches articles whose `metadata_hash` changed, and under `--all` DETECTS +
reports upstream deletions (never removes). **`f5kb reconcile`** is the only command
that deletes: report-only unless `--apply` (threshold guard + DB backup + soft-delete
to `_deleted/`, or `--purge`). Any mutating op takes `--changelog[=FILE]` to append
a JSONL change record; `sync` writes one by default.

**Overwrite protection (the approval gate).** `sync`/`dump`/`enrich` never silently
overwrite a live article that already holds good data: an EDIT is staged to
`<dump>/_pending/<type>/<id>.json` (live untouched) and recorded in
`_pending/_manifest.json`; new articles write directly, unchanged are skipped.
**`f5kb approve`** promotes staged edits (archiving each replaced file to
`<dump>/_replaced/`) and holds back edits flagged risky (body dropped/errored) unless
`--include-risky`. Pass `--yes` to sync/dump/enrich to bypass the gate.

## Where the docs live (don't duplicate; update the right one)

- **README.md** — usage: every subcommand, flags, examples, output layout.
- **FINDINGS.md** — discoveries about the scraped system (Coveo token flow, API
  limits, field meanings, counts). Appendix A is the full field inventory; the
  my.f5.com sitemap notes + gap analysis are in its "Sitemap" section.
- **OUTLINE.md** — our code: module tree, the dump→enrich→track flow, the
  network-injection design, strategies, decisions.
- **HOWTO.md** — task-oriented user guide: quick start + common workflows with
  copy-paste examples.
- **MEMORIES.md** — durable project memory & handoff (current state, credentials,
  gotchas, data layout, open work).
- **TODO.md** — open work + log of shipped work.
- **config.yaml** — the machine config the CLI reads (`types:` + `field_descriptions:`
  + `products:`). Hand-edit only; excluded from `ruff format`.
- **ui/README.md** — the web console (`ui/`): pages, targets, mutation safety.
  **ui/playbook.md** — the operator playbook rendered inside the console.
- **P2_HANDOFF_PLAYBOOK.md** — the SNS/S3 integration contract for the P2 team.

## Conventions & gotchas

- **`outputs/` is gitignored** (large regenerable data: dumps + `articles.db`).
  Commit code, curated config (`config.yaml`), and docs.
- **No headless browser.** Every body is reachable via plain httpx — JS-rendered sites
  embed it in `__NEXT_DATA__` JSON or render server-side. Don't add Playwright.
- **Beating Coveo's 5,000-offset cap:** `--all` uses **keyset pagination by `@rowid`**
  (the only sortable/unique field; `@date` is 1-second-resolution and misses
  null/out-of-window docs). See OUTLINE.md §4.
- **`dbKey` must equal `load_hash_index`'s key byte-for-byte.** `db_key(document_type, id)`
  in `f5kb/lib/dump.py` builds `"<document_type> <id>"`. A separator mismatch makes
  every lookup miss and silently disables skip-unchanged (every article looks new).
- **`listTypeDirs` skips `_`-prefixed dirs** so `track`/`status` never index
  `_pending/_replaced/_deleted` as article types. A real type dir never starts with `_`.
- **`config.yaml` is excluded from formatters.** `ruff format` is configured to
  exclude `config.yaml` (`tool.ruff` exclude in `pyproject.toml`). Hand-edit only.
- **The gate stages; `approve` applies.** A staged edit is NOT in the DB and NOT logged
  until `approve` promotes it. `approve` recomputes risk AND the metadata/content split
  fresh from the actual files.
- **Network is dependency-injected.** `CoveoClient` (`f5kb/coveo/client.py`) and
  `HttpClient` (`f5kb/http/fetcher.py`) take an httpx client or transport; tests use
  `_ScriptedTransport(httpx.BaseTransport)` to script responses offline.
- **`has_body()` has two implementations with different semantics:**
  - `f5kb/track/hashing.py` — canonical version used for DB tracking (exported)
  - `f5kb/enrich/enrichers.py` — local version with different threshold for enrichment
  Do NOT consolidate these.
- **`_now_stamp()` in `staging.py` and `reconcile.py` uses dashes (`%H-%M-%S`)**,
  intentionally, for filesystem-safe filenames. Do NOT change to colons.
- **`limit=0` means "no cap"** in all paging helpers. Never pass `limit=0` as a raw
  `max_results` integer — `paging.py` normalizes it to `float("inf")`.
- **Incremental skip hinges on one string.** `db_key(document_type, id)` must produce
  the EXACT same `"<document_type> <id>"` key that `load_hash_index` builds from the DB.

## Testing

```
uv run pytest                  # all 301 offline tests (default: -m 'not live')
uv run pytest -m live          # live/network tests (requires my.f5.com access)
uv run pytest tests/unit/      # unit only
uv run pytest tests/integration/  # CLI smoke tests
uv run pytest tests/regression/   # schema/contract regression tests
```

Tests use `_ScriptedTransport(httpx.BaseTransport)` for offline mocking — responses
are scripted per-call. `@pytest.mark.live` tests require network; they're skipped by
default (`addopts = "-m 'not live'"` in `pyproject.toml`).

The `noop_sleep` fixture is in `tests/conftest.py`; fixtures (Aura JSON, Coveo search
responses, a 25-article mini dump) are under `tests/fixtures/`.

## Git

- `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>` on commits.
- Commit only when work is in a coherent state.
- Use a HEREDOC for multi-line commit messages so backticks aren't mangled.

## Credentials (no secrets stored)

Coveo org `f5networksproduction5vkhn00h`; guest token fetched at runtime via
`HeadlessController.getHeadlessConfiguration` (no auth needed). Optional
`GITHUB_TOKEN` env raises the GitHub API limit for F5_GitHub enrichment. Full
token/credential details in FINDINGS.md.
