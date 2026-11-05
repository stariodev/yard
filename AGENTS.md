# Stario API guide

How to build a Stario JSON API. This is the default; when the developer asks for something else, do that. App notes live in `## This app` at the end.

Stario is a Python HTTP framework. This app has no HTML and no Datastar. A handler reads the request and writes JSON or text. Docs: https://stario.dev/docs.

Before you call work done, run `just check` (ruff, ruff format, pyrefly, pytest) and fix every failure.

## Stario rules

- Not ASGI: no Uvicorn, FastAPI, or response objects. The main module runs `asyncio.run(serve(bootstrap))`.
- `bootstrap(app: App, span: Span)` is an async generator with one `yield`: build shared objects, register routes, `yield`, clean up. Routes are fixed after `bootstrap`.
- A handler is `async def handler(c: Context, w: Writer) -> None`. It answers once: `w.json`, `w.text`, `w.empty`, or `w.redirect`.
- `Route("GET /apps/{name}")` is one method on one path; a GET route answers HEAD too. `app.add(ROUTE, handler)` registers it; `ROUTE.href(name)` builds the path. No `url_for`, no `app.get`.
- A literal segment beats a parameter, in any order: `/rooms/live` and `/rooms/{room_id}` both work.
- `c.match.params`, `c.req.query`, `c.req.cookies`; `c.state` is filled by middleware. Middleware is `(inner: Handler) -> Handler`, added with `app.use("/prefix", mw)`.
- A long loop iterates `c.alive(source)`. Background work is `c.app.create_task(coro)`, never bare `asyncio.create_task`. A timer loop reads `shutdown = app.shutdown` once, then ends each pass with `await asyncio.wait({shutdown}, timeout=interval)`, never `asyncio.sleep`, so it stops at once on shutdown.
- JSON goes through `stario.json` (`w.json`), not `json.dumps`.
- One process, one event loop. Shared state is an object built in `bootstrap` and passed to the code that registers routes. No module-level state. No `os.environ` outside the config module.
- Stario does not read `.env`. Settings come from the environment; `set dotenv-load := true` in the `justfile` loads a local `.env`.

## Requests

- `await c.req.body()` and `c.req.stream()` accept 10 MiB (`STARIO_REQUESTS_MAX_BODY_BYTES`). A route that takes more passes `max_size=`; past it, the read raises 413.
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
- `app/cli/` — the `yard` command. Stdlib only; it talks to the API over HTTP. `main.py` holds the argument tree and the commands. `client.py` holds the login, the API calls, the `--json` and `--no-color` flags, and `die`. `checkout.py` reads the git checkout and derives the app name. `forge.py` holds `yard forge setup`. `render.py` holds the tables. A test patches `client.api_try` to fake the API.
- `app/server/` — the API. `api.py` owns HTTP parsing and JSON. It holds the Route values and the page classes (`HooksPage`, `HostPage`, `AppsPage`, `AccessPage`), and `register(app, yard)` wires them. The services (`apps`, `sites`, `repos`, `status`, `doctor`) return dataclasses. The adapters (`git`, `podman`, `caddy`, `compose`, `forges`, `journal`, `sources`, `store`) wrap one tool each. `detect/` finds what a repo runs, one module for each language. `logfilter.py` holds the `--hide` and `--only` rules of `yard logs`. `forges.py` is Origin and GitHub behind one `Forge` class. `yard.py` holds the dependencies and runtime state only. `main.py` is the composition root. Forge setup is not a service: it lives in the CLI (`yard forge setup`).

Imports go one way: api → services → yard/store/adapters → shared. Only `app.server.main` imports `app.server.api`. `app.cli` never imports `app.server`, `stario`, or `cryptography`. `tests/test_layers.py` enforces it. HTTP tests use `TestClient(bootstrap)` from `app.server.main`; service tests build a `Yard` with fake adapters.

Run the server with `python -m app.server.main`.

`Config.from_env` reads `YARD_ROOT` (default `yard-data`), `YARD_PODMAN_SOCK`, `YARD_PODMAN_BIN`, `YARD_SKIP_DEPLOY`, `YARD_BOOT`, `YARD_CHECK_SECONDS`, `YARD_PRUNE_DAYS`, `YARD_JOURNAL_DIR`, `YARD_JOURNALCTL_BIN`, `YARD_JOURNAL_SOCKET`, and `CONTAINER_HOST`. App state lives under `YARD_ROOT/apps/<name>/`.

Caddy reads `ops/Caddyfile`, which imports `/config/sites/*.caddy`. That directory is `apps/yard/caddy/config/sites/` on the host, inside the `/config` mount Caddy already has; Caddy sees no env file and no data. Each app has one site file, `<name>.caddy`. Yard stores the text and never parses it. A change writes the file, then posts `import /etc/caddy/Caddyfile` to the admin `/load`; when Caddy answers 400, Yard writes the old file back and returns Caddy's error. A deploy does not touch Caddy. Caddy runs with `--resume`, so a restart serves the last loaded config; Yard loads the Caddyfile again at boot (`sites.boot`).

A deploy runs one apply container, `deploy-<name>`. `apps._ready_repo` (a repo app) and `apps._ready_source` (an image or a compose URL in `--runs`) each return a `Ready`; `apps._apply` runs it the same way for both. The container is the only deploy record. Yard writes all its `yard.*` labels when it creates the container: the new plan (`sha`, `runs`, `kind`, `reason`, `template`) and the plan before it (`before.revision`, `before.runs`, ...). Nothing is written after the deploy, so a Yard restart in the middle of a deploy loses nothing. `status.running_plan` gives what an app runs: the new plan on exit 0, else the `before.` plan; with no labelled record, the image of `<name>-app-1`. `status.deploy_record` gives the last deploy for the status page, and `status.last_try` gives the pair the check loop does not try again. A running deploy container is the deploy lock: `compose` removes the last one with a plain `podman rm` and never uses `--replace`, and `apps._join` follows a running one to its end. A check that joins checks again after it. A deploy that fails before the apply leaves a failed container through `compose.record_failure`. `GET /api/v1/events` makes the `deploy.start` and `deploy.end` rows from Podman's `create` and `died` events of `deploy-*`. The in-memory `Job` only streams a running deploy to `yard app deploy`.

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
