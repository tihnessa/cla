# clp

A Python 3.9+ project managed with [uv](https://docs.astral.sh/uv/) and linted
and formatted with [Ruff](https://docs.astral.sh/ruff/).

## Development

Install the project and its development dependencies:

```bash
uv sync --dev
```

Run the checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Apply automatic lint and formatting fixes with:

```bash
uv run ruff check --fix .
uv run ruff format .
```
