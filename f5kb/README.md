# f5kb/ — Python package

This directory is the importable Python package. Its name must match the CLI
command name (`f5kb`) because `pyproject.toml` wires them together:

```toml
[project.scripts]
f5kb = "f5kb.cli:cli"
```

After `uv sync`, `uv run f5kb <sub>` calls into this package directly — no `.py`
extension, no manual activation. Also runnable as `python -m f5kb`.

See the repo root README.md for usage.
