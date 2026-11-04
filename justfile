# `just` lists the targets. Install: uv tool install rust-just

# load secrets/config from .env (gitignored)
set dotenv-load := true

# run the app with hot reload
dev:
    uv run watchfiles 'python -m app.server.main' app/

# run the app once, no reload
serve:
    uv run python -m app.server.main

# lint, typecheck, test - run before done
check:
    uv run ruff check .
    uv run ruff format --check .
    uv run pyrefly check
    uv run pytest -q

# auto-fix lint and format
fix:
    uv run ruff check --fix .
    uv run ruff format .

# run the tests
test:
    uv run pytest -q

# refresh the pinned template base digests in detect/bases.json
bump-bases:
    uv run scripts/bump_bases.py

# refresh AGENTS.md, the skills, and the Cloud Agent files from the latest stario-create
sync:
    uvx stario-create@latest sync
