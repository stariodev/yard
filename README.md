# Yard

Push a repo to GitHub or Cursor Origin. Yard builds the container and serves the app over HTTPS.

![yard app new, then git push: Yard builds the container with Podman, Caddy serves it over HTTPS, and the app is live](https://raw.githubusercontent.com/stariodev/yard/main/docs/demo.gif)

Yard runs on one Ubuntu VM. It watches the forge, builds each push with Podman, and points Caddy at the new container. A `git push` is the whole deploy. There is no pipeline file and no dashboard.

**Docs: [stario.dev/yard](https://stario.dev/yard)**

## Quick start

On a fresh Ubuntu 26.04 LTS VM, as root, from a clone of this repo:

```sh
./install.sh --domain yard.example.com
```

The script prints an admin token. Then, on your laptop:

```sh
uv tool install staryard
yard login --url https://yard.example.com --token <token>
yard forge setup github          # or: yard forge setup origin

cd my-app
yard app new --caddy app.example.com
git push
```

`--caddy` writes the Caddy site for the app after the first deploy. `yard caddy edit` changes it later: add a host, `basic_auth`, or `forward_auth` to a login service. Caddy checks each change before it serves it.

The command is `yard`. The PyPI name is `staryard`, because `yard` is taken. Python 3.14 or newer is required.

## What an app needs

- Something Yard can build. That is a `compose.yml` with a service `app` on the network `yard`, or a `Containerfile` or `Dockerfile` at the repo root or in `ops/`. Or it is code that Yard knows, and Yard writes the Containerfile itself: Stario, Python (`uv.lock` or `requirements.txt`), Go, Node (npm), or http-nu.
- A web process on port 8000.
- Settings from the environment. `yard env set KEY=VALUE` writes them.
- Persistent files in `/data`. That directory survives every deploy.

Yard runs any app in a container. A [Stario](https://stario.dev) app needs the least setup: no Containerfile. Yard sees `stario` in `pyproject.toml`, installs from `uv.lock`, starts `python -m app.main`, and sets `STARIO_HOST=0.0.0.0`. The deploy log names what Yard chose and why. When Yard is not sure, it stops and says what to add. [How Yard reads a repo](https://stario.dev/yard/what-runs#how-yard-reads-a-repo) lists each file it reads. Any other app in a container works too, and so does software that someone else publishes: [Self-host a service](https://stario.dev/yard/self-host) runs Vaultwarden from an image and Umami from a compose file, with no repo and no forge.

## Docs

The docs at [stario.dev/yard](https://stario.dev/yard) read as a short book:

- [The whole idea](https://stario.dev/yard/the-idea) — Yard in one page: what each tool does and what Yard leaves out.
- [One push, start to finish](https://stario.dev/yard/one-push) — follow a single commit from your laptop to a live container.
- [Set up a host](https://stario.dev/yard/install) — a fresh VM to a working Yard in one script and a few commands.
- [Your first app](https://stario.dev/yard/first-app) — the four rules an app follows, then one push.
- [Self-host a service](https://stario.dev/yard/self-host) — run someone else's software from an image or a compose file, and what Yard does not do.
- [What an app runs](https://stario.dev/yard/what-runs) — how Yard reads a repo, the Containerfile it writes for a Stario app, what it refuses, and how an app runs an image or a compose URL instead.
- [Day to day](https://stario.dev/yard/day-to-day) — logs, settings, domains, rollbacks, and cleanup.
- [Your own compose file](https://stario.dev/yard/compose) — workers, sidecars, healthchecks, and apps in any language.
- [How it works](https://stario.dev/yard/how-it-works) — the internals: files, the check loop, the deploy container, Caddy, journald.
- [CLI](https://stario.dev/yard/cli-reference), [API](https://stario.dev/yard/api-reference), [Glossary](https://stario.dev/yard/glossary) — the lookup pages.

## Develop

```sh
uv run pytest
uv run python -m app.server.main
just check                       # ruff, format, pyrefly, pytest
```

The code layout is in [`AGENTS.md`](AGENTS.md). Changes are in [`CHANGELOG.md`](CHANGELOG.md).
