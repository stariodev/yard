# Stario API guide

How to build a Stario JSON API. This is the default; when the developer asks for something else, do that. App notes live in `## This app` at the end.

Stario is a Python HTTP framework. This app has no HTML and no Datastar. A handler reads the request and writes JSON or text. Docs: https://stario.dev/docs.

Before you call work done, run `just check` (ruff, ruff format, pyright, pytest) and fix every failure.

## Stario rules

- Not ASGI: no Uvicorn, FastAPI, or response objects. The main module runs `asyncio.run(serve(bootstrap))`.
- `bootstrap(app: App, span: Span)` is an async generator with one `yield`: build shared objects, register routes, `yield`, clean up. Routes are fixed after `bootstrap`.
- A handler is `async def handler(c: Context, w: Writer) -> None`. It answers once: `w.json`, `w.text`, `w.empty`, or `w.redirect`.
- `Route("GET /apps/{name}")` is one method on one path. `app.add(ROUTE, handler)` registers it; `ROUTE.href(name)` builds the path. No `url_for`, no `app.get`.
- A literal segment beats a parameter, in any order: `/rooms/live` and `/rooms/{room_id}` both work.
- `c.match.params`, `c.req.query`, `c.req.cookies`; `c.state` is filled by middleware. Middleware is `(inner: Handler) -> Handler`, added with `app.use("/prefix", mw)`.
- A long loop iterates `c.alive(source)`. Background work is `c.app.create_task(coro)`, never bare `asyncio.create_task`.
- JSON goes through `stario.json` (`w.json`), not `json.dumps`.
- One process, one event loop. Shared state is an object built in `bootstrap` and passed to the code that registers routes. No module-level state. No `os.environ` outside the config module.
- Stario does not read `.env`. Settings come from the environment; `set dotenv-load := true` in the `justfile` loads a local `.env`.

## Requests

- Validate every input. Malformed or forged input gets `w.text("why", status=400)` (or 404, 403, 409) and `return`.
- In a handler: read input, validate (4xx and `return`), do the work, reply.
- Slow work (a build, a download, a model call) runs in `c.app.create_task(self.run(job, c.span.step("job.run")))`. The handler records the job and replies. The task works inside `with span:`, updates the job status, and marks the job failed on `CancelledError` (Stario cancels it at shutdown). A client reads that status from another route.
- A delay, a timeout, or an external client is a config field, so tests run fast and use fakes.

## Storage

Choose with the developer. Do not add a database the app does not need.

- No persistence: an object built in `bootstrap`.
- SQLite: litewriter is the recommended default. Plain `sqlite3` is fine for a small app.
- Every `SCHEMA` statement is `IF NOT EXISTS`.
- Files live in `config.data_dir`: `$PROJECT_DATA_DIR`, or `./data` on a laptop. Yard sets `/data`, which survives every deploy. The database is `config.db_path` there. Tests set `PROJECT_DATA_DIR` to `tmp_path`.

## Code shape

- Routes that share dependencies live in one class: a frozen dataclass whose fields are the dependencies, `register(app)` adds the routes, and each route is one method (`app.add(APP, self.show)`). No handler factories.
- Code that wraps one external tool (a CLI, a socket, an HTTP API) is an adapter. Tests replace it with a fake.
- Queries and service methods are named by verb: `list_*`, `get_*` (one or `None`), `add_*`, `set_*`, `remove_*`.
- Fewer, fuller functions. Inline a function with one call site. No compatibility aliases, no comments that repeat the code.

## Tests

Tests run the production `bootstrap` through `TestClient`. Each route: a good request returns the expected status and body; malformed input returns 400. A service test builds the service with fake adapters. That is the proof. Do not open a browser.

## Cursor Cloud specific instructions

Install is `.cursor/cloud-install.sh`. Prove a change with `TestClient`. Do not use computer use.

## This app

Yard is a Stario JSON API plus the `yard` command line (the PyPI name is `staryard`). It builds app Containerfiles, runs the containers through Podman, and stores the Caddy site file of each app. The Yard app name is `yard`.

There is no `app/features/`; three layers carry the code:

- `app/shared.py` — errors, values, time helpers, and `atomic_write`. Stdlib only.
- `app/cli/` — the `yard` command. `main.py` holds the argument tree, the HTTP client, the checkout deduction, and the commands; `render.py` holds the tables. Stdlib only; it talks to the API over HTTP.
- `app/server/` — the API. `api.py` owns HTTP parsing and JSON. It holds the Route values and the page classes (`HooksPage`, `HostPage`, `AppsPage`, `AccessPage`), and `register(app, yard)` wires them. The services (`apps`, `sites`, `repos`, `status`, `doctor`) return dataclasses. The adapters (`git`, `podman`, `caddy`, `compose`, `forges`, `journal`, `sources`, `store`) wrap one tool each. `detect/` finds what a repo runs, one module for each language. `logfilter.py` holds the `--hide` and `--only` rules of `yard logs`. `forges.py` is Origin and GitHub behind one `Forge` class. `yard.py` holds the dependencies and runtime state only. `main.py` is the composition root. Forge setup is not a service: it lives in the CLI (`yard forge setup`).

Imports go one way: api → services → yard/store/adapters → shared. Only `app.server.main` imports `app.server.api`. `app.cli` never imports `app.server`, `stario`, or `cryptography`. `tests/test_layers.py` enforces it. HTTP tests use `TestClient(bootstrap)` from `app.server.main`; service tests build a `Yard` with fake adapters.

Run the server with `python -m app.server.main`.

`Config.from_env` reads `YARD_ROOT` (default `yard-data`), `YARD_PODMAN_SOCK`, `YARD_PODMAN_BIN`, `YARD_SKIP_DEPLOY`, `YARD_BOOT`, `YARD_CHECK_SECONDS`, `YARD_PRUNE_DAYS`, `YARD_JOURNAL_DIR`, `YARD_JOURNALCTL_BIN`, `YARD_JOURNAL_SOCKET`, and `CONTAINER_HOST`. App state lives under `YARD_ROOT/apps/<name>/`.

Caddy reads `ops/Caddyfile`, which imports `/config/sites/*.caddy`. That directory is `apps/yard/caddy/config/sites/` on the host, inside the `/config` mount Caddy already has; Caddy sees no env file and no data. Each app has one site file, `<name>.caddy`. Yard stores the text and never parses it. A change writes the file, then posts `import /etc/caddy/Caddyfile` to the admin `/load`; when Caddy answers 400, Yard writes the old file back and returns Caddy's error. A deploy does not touch Caddy. Caddy runs with `--resume`, so a restart serves the last loaded config; Yard loads the Caddyfile again at boot (`sites.boot`). With no site file at all, that boot first writes one default site for each app from the 0.10 bind routes that Caddy resumed (`_import_binds`).

The `journal` adapter works both ways: `send` writes a Yard event to the journald native socket (stdout JSON when the socket is missing), and `read`/`follow` run journalctl. No event field may carry a secret — an env change logs key names only.

The package uses `uv_build` with `module-name = ["app"]`. `package = false` in `[tool.uv]` keeps the top-level `app` importable from the checkout.

### Workflow

- Do each piece of work that changes this repo in a pull request. One pull request holds one intent.
- Open the pull request with your first push.
- Before each push, run `just check` and fix every failure.
- Merge only when the task tells you to merge. Then run `gh pr merge --squash --delete-branch`. Yard deploys each merge to `main`.
- When the task does not tell you to merge, keep the pull request open.
- When you drop the work, write the reason in the body and close the pull request.
- Do not push to `main` directly.

The pull request body is the record of the work. The squash merge puts it into the commit message. Write it for a reader who did not see the work. Keep it current while you work. Use these headings:

- **Intent**: the task in one or two sentences, and its source: a schedule, a Medic finding, or the user.
- **Found**: the state before the change. For a bug, write the cause and how you reproduced it.
- **Changed**: what you changed, and why you did it this way. Name the other options and why you did not use them.
- **Not done**: what you did not do, and why. Write "Nothing" when that is true.
- **Proof**: the output that shows the change works.
- **Questions**: the decisions that you made without the user. Write "None" when that is true.

Proof is output that a reader can check: the command that you ran and the lines that show the result. One proof for the change is enough:

- For a bug: the new test that reproduces it, now passing.
- For a route: the test or the `TestClient` response that shows the HTML, the JSON, or the header.
- For a security fix: the request that worked before the fix, and the status code that it gets now.
- After a merge, when you can reach production: a `curl` to the production URL that shows the fix.
