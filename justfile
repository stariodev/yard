# `just` lists the targets. Install: uv tool install rust-just

set shell := ["bash", "-uo", "pipefail", "-c"]

# load secrets/config from .env (gitignored)
set dotenv-load := true

# run the app with hot reload
dev:
    uv run watchfiles 'python -m app.server.main' app/

# run the app once, no reload
serve:
    uv run python -m app.server.main

# format, fix lint, typecheck, test - run before done; prints only problems
check:
    @uv run ruff format -q .
    @uv run ruff check -q --fix .
    @uv run pyrefly check 2>&1 | { grep -v ' INFO ' || true; }
    @uv run pytest -q --no-header -n auto

# the same checks without changes, for CI
ci:
    uv run ruff check .
    uv run ruff format --check .
    uv run pyrefly check
    uv run pytest -q -n auto

# run the tests
test *args:
    uv run pytest -q --no-header -n auto {{args}}

# refresh the pinned template base digests in detect/bases.json
bump-bases:
    uv run scripts/bump_bases.py

# refresh AGENTS.md, the skills, and the Cloud Agent files from the latest stario-create
sync:
    uvx stario-create@latest sync
