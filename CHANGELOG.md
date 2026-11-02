# Changelog

## Unreleased

- The deploy container `deploy-<name>` is the only deploy record. Before, Yard also wrote `apps/<name>/deployed` and the `deploy.end` event after the apply ended. A Yard self-deploy that replaced the process in the middle of another app's deploy lost both: the app ran the new revision, but `yard status` showed the old one, and the check loop deployed the same revision again (seen on 2026-11-02 for stario-site).
- Yard writes every label when it creates the deploy container: `yard.sha`, `yard.image`, `yard.runs`, `yard.kind`, `yard.reason`, `yard.template`, `yard.cause`, `yard.actor`, `yard.previous`, and the plan that ran before the deploy (`yard.before.revision`, `yard.before.runs`, `yard.before.kind`, `yard.before.reason`, `yard.before.template`). Nothing is written after the deploy.
- The running revision is the new plan on exit 0, else the plan before the deploy. A host with no labelled deploy container reads the image of `<name>-app-1`: `localhost/<name>:<sha12>-<tpl8>` for a repo app, `<ref>@sha256:...` for an image app. A URL app with no record has no running revision until its next deploy. A 0.11.0 record without the plan labels reads the template from its image.
- The check loop reads the last tried (revision, template) pair from the deploy container, so a failed pair is not tried again after a restart either.
- `apps/<name>/deployed` is no longer read or written. An upgraded host can delete it.
- The `deploy.start` and `deploy.end` journal events are gone. `GET /api/v1/events` makes the `deploy.start` and `deploy.end` rows from Podman's `create` and `died` events of `deploy-<name>`, with the same shape (`source` is `yard`). The status comes from the exit code: 0 is `ok`, 3 is `reverted`, any other code is `failed`. `yard events --event deploy.` works as before. Rows from older Yard versions still come from the journal.
- A running `deploy-<name>` container is the deploy lock, also after a Yard restart. Before, only the in-memory job stopped a second deploy, and each deploy ran `podman run --replace`. On 2026-11-02 from 21:25 to 21:28, each push replaced the running `deploy-yard`, so three yard self-deploys stopped in the middle and had no `deploy.end`.
- Yard removes the last deploy container with a plain `podman rm` (no `-f`), then runs the new one without `--replace`. Podman refuses to remove a running container, so a running deploy is never replaced.
- A check or a webhook that finds a running deploy for another revision follows it. When it ends, the check runs again, so the newest revision still deploys once. The new Yard process after a self-deploy picks up a newer tip the same way.
- `POST /api/v1/apps/{name}/deploy` joins a running deploy, also after a restart: the job streams the deploy container's log from its first line, and its status comes from the exit code. `yard app deploy` follows it as before.
- A URL app deploy prunes the old compose copies when it starts. It keeps the running copy and the new one.
- The deploy job reads the exposed port when the client asks for the job after an ok deploy. The `SHA` env of the Yard container is no longer read.
- Breaking: upgrade a 0.10 host through 0.11 first. The 0.11 move from the bind routes is gone: a boot with no site file no longer writes default sites from the routes Caddy resumed, and the `caddy.import` event is gone.
- Breaking: a forge `state.json` from before 0.10 with `installation_id` at the top level no longer reads as one installation, and an installation with no account no longer owns every repo. `yard forge setup` writes the `installations` list; a host that ran it on 0.10 or later has it.
- `GET /api/v1/status` and `yard status` ask Caddy about each site file once, and again only when the file changes. Before, each status call sent one Caddy `/adapt` request for each app.

## 0.11.0

- Breaking: the CLI prints JSON only with `--json`. Before, it printed JSON whenever stdout was not a terminal, so `yard env cat > .env` wrote JSON and `yard logs | grep` searched a JSON blob. `--text` is gone.
- The apply container runs only `up` and `purge`. The unused `build`, `start`, and `down` commands and the unread `WAIT_SECONDS` setting are gone; the wait stays 120 seconds.
- A deploy request with a `sha` field gets the same 400 `unknown_field` as any other field. The special message from 0.8.0 is gone.
- The deploy container `deploy-<name>` is the record of the last deploy. Yard labels it with `yard.sha`, `yard.image`, `yard.cause`, `yard.actor`, `yard.previous`, and `yard.error`, and reads its exit code: 0 is `ok`, 3 is `reverted`, any other code is `failed`. The record survives a Yard restart. Before, the status rows reported the sha of the apply image (`latest`) and showed a reverted deploy as `failed`.
- A deploy that fails before the apply (a git sync, a detect refusal, a registry error) now leaves a failed deploy container. Its log holds the deploy lines, so `yard app logs --deploy` shows the last deploy in every case.
- Breaking: the app page has `deploy` (one record or `null`) in place of `deploys` (a list of at most one) and `job`. `GET /api/v1/apps/{name}/deploy` answers the running deploy with its lines, else the record from the deploy container, else 404. `yard app logs --deploy` reads the deploy container's log; with `-f` on a running deploy it streams the deploy from its first line.
- `GET /api/v1/forges` only reads the saved state. Before, a read asked the forge for the installations and saved them when it had none. New route `POST /api/v1/forges/{name}/installations` asks the forge, saves the answer, and returns the summary, with `error` when the forge refused. `yard forge setup` polls it. The installation webhook and the route share one function.
- Each forge reads its own field names: Origin answers camelCase (`expiresAt`, `repoSelectionMode`, `target.slug`, `displayName`, `webhookUrl`), GitHub answers snake_case (`expires_at`, `repository_selection`, `account.login`, `name`). Yard no longer tries both spellings.
- `yard forge setup github` has two ways in. The default still creates the app with the one-click manifest form; `--public` makes an app that any account can install (a private app installs only on the account that owns it). `--existing` connects an app you already have, or one whose setup broke: it asks for the App ID, the private key path, and the webhook secret, or reads them from `--app-id`, `--key`, and `--webhook-secret`, and asks again when Yard refuses them. Breaking: `--manual` is gone. It walked through a hand-made app with a generated secret; `--existing` covers the same case with the values GitHub shows.
- Every setup ends in one install step: open the install page, then wait for the installation.
- `--json` sends `Accept: application/json`. `yard logs --json` prints the logs route's entries page (`{"entries", "cursor"}`, the page Medic reads) in place of `{"log": text}`.
- `yard events -f` prints one table line for each event; with `--json` it prints the NDJSON stream as before.
- `yard status --full --json` prints the status report with the doctor rows under `doctor`. Before, it wrapped the report as `{"status", "doctor"}`, a different shape from `yard status --json`.
- The logs and events routes take `lines` from 1 to 10000, 100 by default. Before, events took 1 to 5000, 200 by default.
- The app page has `dir`, the app's folder on the host. `yard app rm` prints the clone and data paths from it.
- Caddy reads Caddyfiles. Each app has one site file on Yard, `apps/yard/caddy/config/sites/<name>.caddy`. `yard caddy cat` prints it, and `yard caddy edit` opens it in `$EDITOR` or reads it from stdin. Yard loads Caddy after each save. Caddy checks the whole config first; a refused file keeps the old config, and the error names the file and the line. An empty file removes the site. A refused edit in the editor opens the editor again on the same text.
- `yard app new --caddy HOST` writes the default site after the first deploy: every host, comma-separated, to `<name>-app-1` on the port the container exposes, else 8000 for an app with a repo. `--caddy` repeats. An image or URL app without one exposed port gets the template printed and no file. `--caddy` with `--no-deploy` is refused.
- New routes: `GET /api/v1/apps/{name}/caddy` answers the site file as text (empty when there is none), and `PUT` takes the whole file as the body and answers `{"hosts"}`. A refused file is a 400 `bad_caddyfile` with Caddy's error.
- Breaking: the binds are gone. `yard bind ls/set/rm`, `/api/v1/binds`, `yard app new --public/--try/--port`, and `yard app rm --with-binds` are removed. `yard app rm` removes the site file first, and its answer is `{"name", "hosts"}`.
- Caddy starts from `ops/Caddyfile`; `ops/caddy.json` is gone. Caddy keeps `--resume`, and Yard loads the site files at every boot, so they are the state.
- The upgrade needs no manual step. Caddy resumes the 0.10 bind routes and keeps serving. When Yard boots and finds no site file, it writes one default site for each app from those routes (the hosts, the dial, the retry window), then loads the Caddyfile. A route whose dial names no app is logged and skipped. The journal event `caddy.import` names the apps.
- `yard env show` is `yard env cat`. New `yard env get KEY` prints one value.
- The status table's `bind` column is `host`, and the app page has a `caddy` line. Both list the hosts from the site file. `yard status --full` has a `caddy config` row in place of `caddy routes`: Caddy adapts every site file on disk and loads nothing.
- `yard repo new [NAME] [--owner] [--forge]` creates a private repo for the checkout, adds it as `origin`, and pushes the branch with your own git credentials. It needs a commit and no `origin` remote. It never runs `git add` or `git commit`.
- New route `POST /api/v1/repos` takes `{"name", "owner?", "forge?"}` and answers `{"repo"}`. The server picks the one connected forge and its one installation; with more than one it answers 400 `forge_required` or `owner_required`.
- Repo creation needs an installation on all repositories, on GitHub and on Origin: a "selected" installation cannot see the repo it makes. A separate account for generated apps keeps your other repos out of that installation.
- New route `POST /api/v1/repos/token` takes `{"repo"}` and answers `{"username", "token", "expires_at"}`: a GitHub installation token for contents write on that one repo, for one hour. Plain git uses it over HTTPS as the password for the user `x-access-token`; no `gh` is needed. The same token clones, fetches, and pushes: contents write includes read. An app that makes apps uses it and holds no GitHub key. The journal event `repo.token` names the repo, never the token. Origin answers 501.
- Commit statuses are gone. Yard no longer posts "pending" and "live" on commits, and `yard status --full` has no status row. The deploy record stays in `yard app` and the journal.
- New GitHub setups request contents write, metadata read, and repository creation write; commit statuses are no longer requested. New Origin setups request `namespace:repositories:create`. An existing host changes the permissions in the app's settings and accepts them on the installation.
- The install step prints "Install the app on your account and select all repositories." for both forges.
- An installation on selected repos gives a clearer warning in `yard forge setup` and `yard status --full`: deploys work, but `yard repo new` needs all repositories. The warning links the installation settings page on GitHub.
- The settings hint is gone. Yard no longer reads the code of an app to guess the env keys it needs, and the deploy job has no `missing_env` field. The scan missed a key read through a helper such as `require_env(name)`, and the app log already names a missing key when the app stops.
- `yard app new --edit-env` starts the editor from a template. With no value it reads the first of `.env.example`, `.env.sample`, `.env.template`, and `env.example` at the top of the checkout. `--edit-env PATH` reads that file instead. The `-e` and `--env-file` values come first, and the template adds the keys they do not set. A key left empty in the editor is not saved, and the CLI prints the `yard env set` command for it.

## 0.10.0

- The repo moved to `github.com/stariodev/yard`. `install.sh` clones from there by default, and the image is `ghcr.io/stariodev/yard`.
- The GitHub forge keeps one installation per account. Installing the Yard app on a second account no longer replaces the first: each repo gets a token from the installation that owns it, `yard status --full` shows one installation row per account, and `yard forge setup github` offers "Add another account". An older `state.json` with one installation still works and is rewritten on the next save.
- Detect refusals say more: the nothing-found message lists Nushell and links to stario.dev/yard/what-runs#when-yard-refuses, and a Python repo with no entrypoint says `found a Stario app` or `found a Python app` instead of naming the marker file.
- `yard status` no longer sends `GET /` to each app; the state comes from Podman and its healthcheck. The `answers` field is gone from the status API. An app without a healthcheck that does not listen now shows `up` — add a `HEALTHCHECK` ([stario.dev/yard/compose#healthchecks](https://stario.dev/yard/compose#healthchecks)).
- `yard logs` printed every entry on one line: the text path joined the formatted entries with no separator, so `--tail 3` came back as a single glued line. Each journal entry is now its own line; the app page's log tail fixes the same way.
- `yard logs` filters on the host with repeatable `--hide EXPR` and `--only EXPR`: `key=value` / `key!=value` globs and `key>n` / `key<n` on a JSON record (a top-level key first, then `attributes`), anything else a substring glob on the message. Lines that share a `trace_id` keep or drop as one trace; `lines`/`--tail` still count entries read before filters, and a JSON page's cursor stays the last entry read. With no flags the output is unchanged.

## 0.9.1

- `yard app new --runs https://…/uptime-kuma.compose.yml` names the app `uptime-kuma`. Before, the name kept `.compose` and the command stopped with "name must be a short lowercase word", so the documented example failed without `--app`.

## 0.9.0

- Deploys are jobs. `POST /api/v1/apps/{name}/deploy` answers 202 with the job, `GET` on the same route returns it (`?follow=1` streams the log as `x-ndjson`, ending with the deploy object), and the app page gains a `job` field. Breaking for API callers that expected the deploy body back in the POST.
- `yard app deploy` and `yard app new` stream the deploy log live; `yard app logs --deploy` reads the job's lines.
- `yard app new` takes `-e/--env KEY=VALUE`, `--env-file`, and `--edit-env`, so the first deploy can have its settings.
- The one-time 0.8.0 record migration is removed: a host older than 0.8.0 upgrades to 0.8.0 first.
- The `yard status` bind column elides the app's own container (`host → :PORT` for another port, `host` for 8000).
- The settings hint stays a warning: it never blocks a deploy.

## 0.8.0

Breaking: the app record's four fields are one `runs`.

- `runs` replaces `containerfile`, `compose`, `image`, and `recipe`: `auto` (the default), a path in the repo, `yard:<template>`, an image `name:tag`, or an https URL to a compose file. A repo app runs a file from it; image and URL apps have no repo. Boot rewrites old records once.
- Recipes are gone. A URL in `runs` fetches any compose file; it cannot `build:` (there is no checkout) and the old `recipe/` directory becomes `compose/`.
- `yard app detect` and `deploy --sha` are gone. Roll back by changing the source: git revert, an older tag, or an older URL.
- The `yard:` templates are `python-uv`, `python-pip`, `go`, and `node-npm`, pinned to index digests (`detect/bases.json`; `just bump-bases` refreshes). The rendered Containerfile's hash joins the deployed revision, so a template change redeploys once. One deploy path and one check loop cover every kind.
- `POST`/`PATCH /api/v1/apps` reject unknown fields with `unknown_field`. `app_json` carries `runs` and a `deployed` object; the files route reads the deployed answer.
- Removed old-host compatibility: `up-`/`-job` containers, routes without an id, the :8443 Caddy convergence, the account backfill, and install.sh's yardd/podman-static/tailscale/caddy.service cleanup. Off-host `yard status --full` omits the skipped rows.
- `app new --public` binds after the first deploy, on the port the container exposes (`--port` overrides; a repo app falls back to 8000).
- An image or URL app has an empty `ref`; setting one is a 400 `bad_ref`. The `yard app ls` column is `source`.
- New template `yard:http-nu` runs cross.stream (xs) apps: a repo with `serve.nu` gets a static http-nu binary (sha256-checked per arch), a persistent xs store in `/data/store`, and the Datastar bundle served. The env hint reads `$env.KEY`.

## 0.7.0

- `yard forge setup github` creates the GitHub App with the manifest flow: one click, and GitHub sends the key and the webhook secret back. `--manual` keeps the paste flow.
- `yard app new` deploys right away; `--no-deploy` only creates the app.
- `yard app rm` lists what it removes and asks first; `--with-binds` removes the app's binds too, and `--yes` skips the question.
- A daily prune removes old images that Yard built.
- `yard status --full` warns about an app with no healthcheck.
- `install.sh --domain` writes the bind for Yard itself.
- CLI errors name the next command to run.

## 0.6.2

- Yard mounts the host's Podman socket directory, not the socket file, so a restart of `podman.socket` (an install.sh repair run or a Podman upgrade) no longer leaves Yard unable to reach Podman until its container restarts.

## 0.6.1

- `install.sh` targets Ubuntu 26.04 LTS and uses its podman package (5.x) instead of podman-static 6.1.4. It refuses Podman older than 5 and warns on another OS. It removes the 0.6.0 `podman.service` drop-in if present.
- The podman-remote in the Yard and apply images moves to 5.7.0 to match the host.

## 0.6.0

- `install.sh` installs a pinned Podman (podman-static 6.1.4) under `/usr/local` and points yard's `podman.service` at it. Podman 4.9's compat API does not return container labels on the list endpoint, so Compose recreated every container on every `up` — including Caddy on each Yard self-deploy. On Podman 6 an unchanged service stays up. The first Podman 6 run migrates the BoltDB state to SQLite.
- The podman-remote in the Yard and apply images moves to 6.1.4 to match, and the Yard image now verifies its checksum.
- `yard podman` and `POST /api/v1/podman` are gone. Events and logs come from journald, `status` already carries restarts, health, oom, exit error, cpu, and mem, and `ssh` covers ad-hoc podman.
- The doctor `disk` row says where the space went: filesystem free, image size and reclaimable from `podman system df`, total app data, and the journal.
- A deploy on a GitHub repo posts a commit status on the sha (context `yard`): `pending` while it runs, `success` live (linked to the app's public host) or `failure` on a revert or error, and a boot marks the running yard commit `live`. The GitHub App needs `Commit statuses: Read and write` — `yard forge setup github` asks for it — and doctor shows a `<forge> status` row with the last report or its error.

## 0.5.0

Yard no longer hosts git. Cursor Origin is the git host. There is no migration path; rebuild the host and reconnect it by hand.

- `repos/` and `worktree/` are gone. `apps/<name>/repo` is a plain clone that a deploy resets to the remote tip of the ref.
- The app record is exactly `repo`, `ref`, `trigger`, and `created`. `dir` is gone. A trigger is `push` or `manual`; `every` is gone.
- Two forges: Cursor Origin and GitHub, both as Apps. An app's `repo` is the full `https://<host>/<owner>/<name>` URL; the host picks the forge.
- Everything observable goes through journald: container logs, Podman container events, and Yard's own events (`hook.received`, `check.moved`, `deploy.*`, `app.*`, `env.set`, `bind.*`, `token.*`, `forge.*`, `prune`). Yard writes its events to the native journald socket, `SYSLOG_IDENTIFIER=yard` with `YARD_*` fields; a missing socket falls back to one JSON line on stdout. No secret ever becomes a field.
- `GET /api/v1/logs` reads any container's journal log by name or id; `GET /api/v1/events` is the merged Podman+Yard timeline (JSON, NDJSON on `follow=1`). `GET /api/v1/apps/{name}/logs` is gone — `yard app logs` and `yard logs` use the new routes, and `yard events` shows the timeline.
- The deploy gate is `compose up -d --wait`: a healthcheck the app's compose file defines decides healthy (the settle window and `YARD_SETTLE_SECONDS` are gone). A failed `up` reverts to the previous image and the outcome is `reverted`.
- One deploy path: `POST /api/v1/apps/{name}/deploy` takes an optional `sha` (`yard app deploy --sha SHA`). A prefix resolves against the deploy journal; an image the host still holds skips the build. Rollback is gone — an older revision is a deploy of that sha.
- The apply container is `deploy-<name>` (was `up-<name>`); a leftover `up-` container still reads as the deploy record.
- The API contracts are trimmed: `GET /api/v1/apps/{name}` is the full app page (the `/status` route is gone), binds are `PUT|DELETE /api/v1/binds/{host}`, and the forge key/installation routes are gone — `GET /api/v1/forges` reports the Origin public key, discovers the installation, and carries `selection` and `error`.
- The CLI loses `app show`, `app history`, `app rollback`, and `bind new`. `app status` reads the app page, `bind set` takes the host as an argument, and `forge setup` polls `GET /api/v1/forges` for the installation.
- Status rows carry the container `id` plus `oom` and `error` for a stopped container.
- `yard forge setup origin` and `yard forge setup github` connect the host from the laptop. Each prints the app values, opens the create and install links, and waits for the install. `install.sh` no longer asks for a repo or runs setup; it prints the next steps.
- `/hooks/origin` takes Origin events; `/hooks/github` takes GitHub events and verifies `X-Hub-Signature-256` before anything else. A push schedules a repo check; a burst folds into one check now and one at the end of the window. A full check runs every `YARD_CHECK_SECONDS` (five minutes).
- A deploy syncs the clone through the repo's forge — the ref tip, or one commit with `--sha` — builds `localhost/<name>:<sha12>`, and runs compose up. The token never lands in `.git/config`.
- Containers log to journald. `GET /api/v1/logs`, `yard logs`, and `yard app logs` read them back — the log survives the container. `install.sh` writes the journald and Podman config.
- The yard app still deploys itself: push to its Origin repo, detached apply, `pending` outcome.
- Removed: smart-HTTP git hosting, `/api/v1/repos*`, `repo`/`git`/`init`/`clone`/`up`/`caddy` CLI commands, `app edit` and `app run`, deploy outcomes `superseded`, `waiting`, `current`, the merge/delete app cleanup, and the projects→apps layout migration.
- New env API: `GET|PATCH|PUT /api/v1/apps/{name}/env`. App responses never carry env.
- Doctor gains five rows per connected forge: app, installation, token, webhook, check.
- The code is three layers in about 20 modules: `app.shared`, `app.cli`, and `app.server` (api, services, adapters). The server is `python -m app.server.main`; the CLI script is unchanged.
- Push and installation events debounce with a leading plus trailing check, so a burst never waits five minutes.
- Every disk write is atomic. The git adapter raises `YardError` like the others. The CLI times calls out at 30 seconds (deploy at 30 minutes; streams never).
- Caddy's admin API is a unix socket at `apps/yard/caddy/run/admin.sock`. The curl-to-admin path and the autosave reads are gone.
- HTTP/3 is on. Caddy listens on 443 inside its container, the host publishes 8443 for TCP and UDP, and nft redirects public 443 for both protocols, so Alt-Svc's `h3=":443"` is a port clients can reach. An existing host must rerun `install.sh` — or add `udp dport 443 redirect to :8443` to the yard nft table and allow `8443/udp` in ufw — for UDP to reach Caddy.
- `yard forge setup github` recovers: a bad App ID or an unreadable key path re-prompts instead of exiting (quotes and `\ ` escapes on a pasted path come off), a 4xx on the forge PUT asks again, and a stopped run continues on the next `yard forge setup github` with the same webhook secret — `--new` starts over. A run against a host that already holds the app id skips to the install step; a connected forge exits early.

## 0.4.0

The store is files. One project is one directory. Move the host files once, then rerun `install.sh`.

- `projects/<name>/project` holds repo, ref, dir, trigger, and created. Env is `projects/<name>/env`. Data is `projects/<name>/data`. The checkout is `projects/<name>/worktree`.
- The command and the API say project. A container gets `PROJECT_NAME` and `PROJECT_DATA_DIR`.
- A project write saves the file. `yard up` builds it. `yard clone --ref` adds a feature project, and a merge removes it.
- Binds are `/api/v1/binds`. Caddy keeps its autosave under the yard project.
- `yard prune` removes unused localhost images.

## 0.3.1

Formatting only. `app/api.py` and `app/git.py` are reformatted under the locked ruff 0.17 so CI's `ruff format --check` agrees with the workspace's local run. No behavior change.

## 0.3.0

Stario 5.0 and a round of hardening after the 0.2 cutover. Same install.sh path.

- The table names the container (`engratia-app-1`); an idle row keeps the slot name. CPU and memory read the keys Podman actually prints.
- Image tags use twelve chars of sha. Yard's own containers follow the compose convention: `yard-app-1`, `yard-caddy-1`.
- Caddy no longer recreates on a Yard self-deploy. Its seed file mounts from a stable path under `run/` and the admin socket is fixed.
- The recurring container healthcheck is gone. The apply probe is the only readiness gate; the healthcheck alone was about half of Yard's idle CPU.
- The apply image builds with podman-remote, not the Compose API path.
- `YardError` renders on every route through `catch_errors`; anything else gets the framework's 500. A log follow drains its writer and stops on disconnect.
- install.sh fails early without root, installs `sudo`, and creates `checkouts/` itself. `saved_project` ensures the slot env file.
- `staryard` is a virtual package (`package = false`). The image ships `app/` as source; `uv build` still publishes the PyPI wheel.

## 0.2.0

A rewrite of the server and the command. Same host design: bare git repos, rootless Podman, Compose, Caddy. Backwards incompatible; rerun `install.sh` on the host and `uv tool upgrade staryard` on the laptop.

- One apply container does the whole deploy: build, start, probe, revert. `yard deploys <slot> <id>` is that one log, and a Yard self-upgrade reverts on its own.
- A repo's compose file runs as it is. No generated override. The contract: `image: ${IMAGE}`, `build.context: ${CHECKOUT}`, network `yard` with alias `${SLOT_NAME}`, `env_file: ${APP_ENV}`, `${DATA_DIR}:/data`. Yard checks it before the build.
- Slot env is a file, `/srv/yard/env/<slot>.env`, mode 600. Not in SQLite.
- `TRIGGER` replaces `--every`: empty is a service; `push`, `manual`, or `every 1h` make a job. `yard run <slot>` runs any slot now.
- New: `yard status <slot>`, `yard doctor`, `yard prune`. The table says `no answer` and counts restarts. `yard` alone prints the table.
- Gone: experiments, the token tree, `yard.*` labels, `yard logs --sha`, the `revert` role, the `STARIO_*` defaults Yard injected. `token revoke` is `token rm`.
- Host: `build/` is `checkouts/`; Caddy's first config is `ops/caddy.json`; the apply container is started with `podman run`, no apply compose file.

Migration: slots with `every` become `TRIGGER=every <interval>`; slot env values are not carried over, enter them again with `yard slot edit`; revoked tokens are deleted.

## 0.1.1

- An update keeps the running container until the new image answers `/health`.
- The swap no longer passes `--group-add keep-groups` to remote Podman.

## 0.1.0

- First release of the `yard` command.
- The PyPI package name is `staryard`.
- `yard --version` prints this version.
