# tests/

Test suite for f5kb. All tests run offline by default — no network required.

## Run

```
uv run pytest                  # all 301 offline tests (default)
uv run pytest -m live          # live/network tests (requires my.f5.com access)
uv run pytest tests/unit/      # unit tests only
uv run pytest tests/integration/  # integration + CLI smoke tests
uv run pytest tests/regression/   # schema/contract regression tests
```

## Structure

```
tests/
  conftest.py          shared fixtures (noop_sleep, fixture helpers)
  fixtures/            static test data (Aura JSON, Coveo responses, 25-article mini dump)
  unit/                one module's logic in isolation
  integration/         full subcommand end-to-end; test_live.py requires network
  regression/          lock on-disk contracts (article schema, SQLite schema, changelog schema)
```

## Mocking pattern

Offline tests use `_ScriptedTransport(httpx.BaseTransport)` — a list of
pre-built `httpx.Response` objects consumed one per request. Pass it to
`httpx.Client(transport=...)`, then into `CoveoClient` or `HttpClient`. No
monkey-patching, no `requests-mock` library needed.

## Live tests

`tests/integration/test_live.py` is marked `@pytest.mark.live`. Skipped by
default (`addopts = "-m 'not live'"` in `pyproject.toml`). Requires internet
access to my.f5.com and f5networksproduction5vkhn00h.org.coveo.com.
