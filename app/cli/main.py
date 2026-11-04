"""The `yard` argument tree and the entry point. One function per command; `main` dispatches."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from app import package_version
from app.cli import checkout, client, forge
from app.cli.render import (
    ansi,
    deploy_attention,
    format_app_status,
    format_apps,
    format_doctor,
    format_events,
    format_forges,
    format_prune,
    format_record,
    format_status,
    format_tokens,
    status_hint,
)
from app.shared import (
    YardError,
    app_container,
    atomic_write,
    check_app_name,
    check_domain,
    check_repo,
    default_site,
    deploy_container,
    explain,
    format_bytes,
    format_env,
    parse_env,
    parse_runs,
)

FORGE_HINT = "No forge is connected. Run yard forge setup origin or yard forge setup github."


# `app new --edit-env` with no PATH starts from the first of these at the top of the checkout.
ENV_TEMPLATES = (".env.example", ".env.sample", ".env.template", "env.example")


def _cmd_status(args: argparse.Namespace) -> None:
    session = client.load(args)
    path = "/api/v1/status" + ("?running=1" if getattr(args, "running", False) else "")
    raw = client.api(session, "GET", path, None)
    report = client.object_of(raw)
    checks: Any = None
    if getattr(args, "full", False):
        checks = client.object_of(client.api(session, "GET", "/api/v1/doctor", None))
    if client.out.json:
        # One shape: the status report, with the doctor rows under "doctor" for --full.
        print(json.dumps({**report, "doctor": checks}) if checks is not None and isinstance(report, dict) else raw)
        _doctor_exit(checks)
        return
    if not isinstance(report, dict):
        print(raw)
        return
    print(format_status(report, datetime.now(UTC), color=client.color_on()), end="")
    hint = status_hint(report)
    if hint and sys.stderr.isatty():
        if client.color_on(sys.stderr):
            hint = ansi(hint, "2")
        print(hint, file=sys.stderr)
    _forge_warn(report)
    if isinstance(checks, list):
        print(format_doctor(checks, color=client.color_on()), end="")
    _doctor_exit(checks)


def _doctor_exit(checks: Any) -> None:
    if isinstance(checks, list) and any(not check.get("ok") for check in checks if isinstance(check, dict)):
        raise SystemExit(1)


def _forge_warn(report: Any) -> None:
    """The forge hint on stderr. One connected forge stays quiet."""
    if not isinstance(report, dict):
        return
    host = report.get("host")
    forges = host.get("forges") if isinstance(host, dict) else {}
    if isinstance(forges, dict) and forges and not any(forges.values()):
        print(FORGE_HINT, file=sys.stderr)


def _cmd_prune(args: argparse.Namespace) -> None:
    session = client.load(args)
    # A real prune removes hundreds of images on a busy host: it can take minutes.
    raw = client.api(session, "POST", "/api/v1/prune", {"days": args.days, "dry": args.dry_run}, timeout=600)
    client.show(raw, format_prune, dict)


def _cmd_login(args: argparse.Namespace) -> None:
    url = client.http_url(args.url)
    token = args.token.strip()
    if not url or not token:
        client.die("yard login needs --url and --token")

    # The token file stays private to this user.
    path = client.config_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    atomic_write(path, json.dumps({"url": url, "token": token}, indent=2) + "\n")
    print("logged in")
    print(f"{'host'.ljust(6)}  {url}")
    print(f"{'saved'.ljust(6)}  {path}")

    # The login stands even when this call fails.
    code, text = client.api_try(client.Client(url, token), "GET", "/api/v1/status", None)
    if code < 300:
        _forge_warn(client.object_of(text))


def _cmd_app_ls(args: argparse.Namespace) -> None:
    raw = client.api(client.load(args), "GET", "/api/v1/apps", None)
    client.show(raw, partial(format_apps, color=client.color_on()))


def _cmd_repo_new(args: argparse.Namespace) -> None:
    """Make a private repo on the forge, wire origin, push.

    The checkout stays untouched: no `git add`, no `git commit` — one wrong add
    could commit an .env file.
    """
    top = checkout.git_out("rev-parse", "--show-toplevel")
    if not top:
        client.die("not a git checkout here. Run git init, commit the files the app needs, then run this again.")
    if not checkout.git_out("rev-parse", "--verify", "HEAD"):
        client.die("no commit yet. Commit the files the app needs, then run this again.")
    branch = checkout.git_out("branch", "--show-current")
    if not branch:
        client.die("detached HEAD; check out a branch, then run this again.")
    remote = checkout.git_out("remote", "get-url", "origin")
    if remote:
        existing = checkout.remote_repo(remote)
        if existing:
            client.die(f"origin is already {existing}. yard app new uses it.")
        client.die(f"origin is already {remote}. Remove origin to create a repo.")
    body = {
        "name": args.name.strip() or checkout.slug(Path(top).name),
        "owner": args.owner.strip(),
        "forge": args.forge,
    }
    result = client.object_of(
        client.api(client.load(args), "POST", "/api/v1/repos", {key: value for key, value in body.items() if value})
    )
    repo = str(result.get("repo") or "") if isinstance(result, dict) else ""
    proc = subprocess.run(["git", "remote", "add", "origin", f"{repo}.git"], capture_output=True, text=True)
    if proc.returncode:
        client.die(f"{repo} was created, but git remote add failed: {proc.stderr.strip()}")
    if checkout.git_out("status", "--porcelain"):
        print("warning: uncommitted changes are not pushed", file=sys.stderr)
    proc = subprocess.run(["git", "push", "-u", "origin", branch])
    if proc.returncode:
        client.die(f"{repo} was created, but the push failed. Push it: git push -u origin {branch}")
    if client.out.json:
        print(json.dumps({"repo": repo, "branch": branch}))
    else:
        print(f"created {repo}")
        print(f"pushed {branch}")


def _cmd_app_new(args: argparse.Namespace) -> None:
    repo, ref = args.repo.strip(), args.ref.strip()
    try:
        kind, runs = parse_runs(args.runs)
        repo_less = kind in ("image", "url")
        if repo_less and repo:
            client.die(f"{runs} needs no repo; an app with a repo runs a file from it")
        if not repo_less:
            if not repo or not ref:
                _top, found_repo, found_ref = checkout.checkout()
                repo = repo or found_repo
                if not ref:
                    # The checkout branch counts only when the repo is the checkout's.
                    ref = found_ref if found_repo and repo.lower() == found_repo.lower() else ""
            if kind == "path":
                top = checkout.git_out("rev-parse", "--show-toplevel")
                if top and not (Path(top) / runs).is_file():
                    client.die(f"{runs} is not a file in this checkout. An image needs a tag: {runs}:TAG")
    except YardError as exc:
        client.fail(exc)
    ref = ref or "main"
    name = args.app.strip()
    try:
        if repo_less:
            name = name or checkout.source_name(kind, runs)
        else:
            if not repo:
                client.die(
                    "no repo; pass --repo https://<host>/<owner>/<name>, run inside a forge checkout,"
                    " or run yard repo new first"
                )
            name = name or checkout.default_name(repo, ref)
            repo = check_repo(repo)
        check_app_name(name)
        hosts = [host.strip() for host in args.caddy]
        for host in hosts:
            check_domain(host)
    except YardError as exc:
        client.fail(exc)
    if hosts and args.no_deploy:
        client.die("--caddy needs the first deploy: Yard reads the port from the running container.")

    env = _new_app_env(args, name)
    session = client.load(args)
    body: dict[str, object] = {"name": name, "repo": repo}
    if not repo_less:
        body["ref"] = ref
    if args.trigger:
        body["trigger"] = args.trigger
    if runs:
        body["runs"] = runs
    raw = client.api(session, "POST", "/api/v1/apps", body)
    if env:
        code, text = client.api_try(session, "PUT", f"/api/v1/apps/{quote(name)}/env", env)
        if code >= 300:
            client.die(
                f"{name} was created, but its env was not saved: {explain(text)}\nSet it: yard env edit --app {name}"
            )
    wrap = client.object_of(raw)
    app = wrap.get("app") if isinstance(wrap, dict) else None
    if args.no_deploy:
        if client.out.json:
            print(raw)
            return
        if isinstance(app, dict):
            print(format_record(json.dumps(app)), end="")
        else:
            print(raw)
        print(f"Not deployed yet. Deploy it: yard app deploy --app {name}")
        return
    if not client.out.json:
        if isinstance(app, dict):
            print(format_record(json.dumps(app)), end="")
        else:
            print(raw)
        print(f"Deploying {name} now…")
    code, text = client.api_try(session, "POST", f"/api/v1/apps/{quote(name)}/deploy", {})
    if code >= 300:
        client.die(f"{name} was created, but the first deploy failed.\n" + (client.error_text(text)))
    note = _follow_deploy(session, name) or {}
    if note and not client.out.json:
        _print_deploy_note(note, name)
    if note.get("status") == "failed":
        if hosts:
            print(f"Caddy has no site for {name} yet. After a good deploy, run: yard caddy edit --app {name}")
        client.die(f"{name} was created, but the first deploy failed.")
    # A repo app listens on 8000 (the templates set it). An image or a URL app must expose one port.
    _write_default_site(session, name, hosts, int(note.get("port") or 0) or (0 if repo_less else 8000))


def _new_app_env(args: argparse.Namespace, name: str) -> dict[str, str]:
    """The env for `app new`: --env-file, then -e pairs, then --edit-env on top."""
    env: dict[str, str] = {}
    try:
        if args.env_file:
            env.update(parse_env(Path(args.env_file).read_text()))
        for pair in args.env or []:
            env.update(parse_env(pair))
        if args.edit_env is not None:
            if not sys.stdin.isatty():
                client.die("--edit-env needs a terminal. Use -e KEY=VALUE or --env-file PATH instead.")
            env = parse_env(_edit_text(_env_draft(env, args.edit_env)))
            empty = [key for key, value in env.items() if not value]
            if empty:
                env = {key: value for key, value in env.items() if value}
                print(f"These keys are empty, so they are not saved: {', '.join(empty)}", file=sys.stderr)
                print(f"Set them: yard env set --app {name} {' '.join(f'{key}=…' for key in empty)}", file=sys.stderr)
    except OSError as exc:
        client.die(f"cannot read {exc.filename}: {exc.strerror or exc}")
    except YardError as exc:
        client.fail(exc)
    return env


def _env_draft(env: dict[str, str], path: str) -> str:
    """The editor text: the env, then the template lines for the keys it does not set.

    The template is PATH, else the first of ENV_TEMPLATES at the top of the checkout, else nothing.
    """
    if not path:
        top = Path(checkout.git_out("rev-parse", "--show-toplevel") or ".")
        path = next((str(top / name) for name in ENV_TEMPLATES if (top / name).is_file()), "")
    template = Path(path).read_text() if path else ""
    rest = "\n".join(
        line
        for line in template.splitlines()
        if line.lstrip().startswith("#") or line.strip().removeprefix("export ").partition("=")[0].strip() not in env
    ).strip()
    parts = [text for text in (format_env(env).strip(), rest) if text]
    return "\n\n".join(parts) + "\n" if parts else ""


def _write_default_site(session: client.Client, name: str, hosts: list[str], port: int) -> None:
    """The default site, with the container and the port that the deploy found. No port, no file."""
    if not hosts:
        return
    container = app_container(name)
    if not port:
        print(f"Caddy has no site for {name}: {container} does not expose exactly one port.", file=sys.stderr)
        print(f"Write the site with the port the app listens on: yard caddy edit --app {name}", file=sys.stderr)
        print(default_site(hosts, f"{container}:PORT"), end="", file=sys.stderr)
        raise SystemExit(1)
    _print_site_saved(
        name, client.api(session, "PUT", _site_path(name), default_site(hosts, f"{container}:{port}").encode())
    )


def _cmd_app_set(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    patch: dict[str, str] = {}
    for key in ("repo", "ref", "trigger", "runs"):
        value = str(getattr(args, key, "") or "").strip()
        if value:
            patch[key] = value
    if not patch:
        client.die("yard app set needs --repo, --ref, --trigger, or --runs")
    page = client.object_of(client.api(session, "GET", "/api/v1/apps/" + quote(name), None))
    current = page.get("app") if isinstance(page, dict) else None
    current = current if isinstance(current, dict) else {}
    raw = client.api(session, "PATCH", "/api/v1/apps/" + quote(name), patch)
    if client.out.json:
        print(raw)
        return
    wrap = client.object_of(raw)
    found = wrap.get("app") if isinstance(wrap, dict) else None
    app = found if isinstance(found, dict) else {}
    moved = any(key in patch and patch[key] != str(current.get(key) or "") for key in ("repo", "ref"))
    changed = "runs" in patch and patch["runs"] != str(current.get("runs") or "")
    if (moved or changed) and str(app.get("trigger") or "") == "push":
        print(f"Saved. The next check deploys it. Deploy now: yard app deploy --app {name}")
    elif moved:
        print(f"Saved. {name} still runs the old repo and ref until the next deploy.")
        print(f"Deploy now: yard app deploy --app {name}")
    elif changed:
        print(f"Saved. {name} still runs the old way until the next deploy.")
        print(f"Deploy now: yard app deploy --app {name}")
    else:
        print("Saved.")


def _cmd_app_cat(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    code, text = client.api_try(session, "GET", f"/api/v1/apps/{quote(name)}/files/{args.which}", None)
    if code >= 300:
        client.die(client.error_text(text))
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")


def _cmd_app_status(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    raw = client.api(session, "GET", f"/api/v1/apps/{quote(name)}", None)
    client.show(raw, lambda report: format_app_status(report, datetime.now(UTC), color=client.color_on()), dict)


def _cmd_logs(args: argparse.Namespace) -> None:
    session = client.load(args)
    _logs(session, args.container, args)


def _cmd_app_logs(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    if args.deploy and args.follow and not args.container:
        code, text = client.api_try(session, "GET", f"/api/v1/apps/{quote(name)}/deploy", None)
        wrap = client.object_of(text) if code == 200 else None
        record = wrap.get("deploy") if isinstance(wrap, dict) else None
        if isinstance(record, dict) and record.get("status") == "running":
            # A running deploy streams its lines from the start, the sync included.
            _follow_deploy(session, name)
            return
    # The deploy container's log is the last deploy, in the journal.
    container = args.container or (deploy_container(name) if args.deploy else app_container(name))
    _logs(session, container, args)


def _logs(session: client.Client, container: str, args: argparse.Namespace) -> None:
    pairs = [("lines", str(args.tail)), ("container", container)]
    if args.since:
        pairs.append(("since", args.since))
    if args.until:
        pairs.append(("until", args.until))
    if args.follow:
        pairs.append(("follow", "1"))
    pairs += [("hide", expr) for expr in args.hide]
    pairs += [("only", expr) for expr in args.only]
    path = "/api/v1/logs?" + urlencode(pairs)
    if args.follow:
        client.stream(session, path)
        return
    raw = client.api(session, "GET", path, None)
    print(raw, end="" if raw.endswith("\n") or not raw else "\n")


def _cmd_events(args: argparse.Namespace) -> None:
    session = client.load(args)
    query: dict[str, str] = {"lines": str(args.lines)}
    for key in ("since", "until", "after", "app", "container", "source", "event"):
        value = str(getattr(args, key, "") or "")
        if value:
            query[key] = value
    if args.follow:
        query["follow"] = "1"
    path = "/api/v1/events?" + urlencode(query)
    if args.follow and client.out.json:
        client.stream(session, path)
        return
    if args.follow:
        _follow_events(session, path)
        return
    raw = client.api(session, "GET", path, None)
    client.show(raw, partial(format_events, color=client.color_on()))
    rows = client.object_of(raw)
    if not client.out.json and isinstance(rows, list) and len(rows) == args.lines and isinstance(rows[-1], dict):
        print(f"more: yard events --after {rows[-1].get('cursor')}", file=sys.stderr)


def _follow_events(session: client.Client, path: str) -> None:
    """The NDJSON event stream, one table line for each event as it arrives."""
    with client.open_stream(session, path) as response:
        try:
            for raw in response:
                row = client.object_of(raw.decode())
                if isinstance(row, dict):
                    print(format_events([row], color=client.color_on()), end="", flush=True)
        except KeyboardInterrupt:
            sys.exit(130)


def _follow_deploy(session: client.Client, name: str) -> dict:
    """Print the deploy log live; return the final deploy object."""
    final: dict = {}
    with client.open_stream(session, f"/api/v1/apps/{quote(name)}/deploy?follow=1") as response:
        try:
            for raw in response:
                if client.out.json:
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
                row = client.object_of(raw.decode())
                if not isinstance(row, dict):
                    continue
                if "line" in row:
                    if not client.out.json:
                        print(row["line"])
                elif isinstance(row.get("deploy"), dict):
                    final = row["deploy"]
        except KeyboardInterrupt:
            print(f"The deploy continues on the host. Follow it: yard app logs --deploy -f --app {name}")
            sys.exit(130)
    return final


def _cmd_app_deploy(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    client.api(session, "POST", f"/api/v1/apps/{quote(name)}/deploy", {})
    if not client.out.json:
        print(f"Deploying {name} now…")
    note = _follow_deploy(session, name)
    if client.out.json:
        return
    if note.get("status") == "pending":
        print("Yard restarts itself. Check: yard app status --app yard")
        return
    _print_deploy_note(note, name)
    if note.get("status") == "failed":
        sys.exit(1)


def _cmd_app_rm(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    if not args.yes:
        if not sys.stdin.isatty():
            client.die(
                "yard app rm deletes the app's data and needs a confirmation.\nRun it in a terminal, or pass --yes."
            )
        _rm_preview(session, name)
        answer = client.ask("Type the app name to confirm: ", stopped="Not removed.")
        if answer != name:
            client.die(f"Not removed: you typed '{answer}', the app is '{name}'.")
    raw = client.api(session, "DELETE", f"/api/v1/apps/{quote(name)}", None)
    if client.out.json:
        print(raw)
        return
    data = client.object_of(raw)
    if not isinstance(data, dict):
        print(raw)
        return
    print(f"removed {name}")
    hosts = [str(host) for host in data.get("hosts") or []]
    if hosts:
        print(f"removed the Caddy site: {', '.join(hosts)}")
    error = str(data.get("caddy_error") or "")
    if error:
        print(f"Caddy did not load the config without the site: {error}\nCheck with: yard status --full")


def _rm_preview(session: client.Client, name: str) -> None:
    """What `app rm` takes away: the containers, the files, the Caddy site. One page read."""
    page = client.object_of(client.api(session, "GET", f"/api/v1/apps/{quote(name)}", None))
    page = page if isinstance(page, dict) else {}
    env_file = str(page.get("env_file") or "")
    app_dir = str(page.get("dir") or "")
    rows = [row for row in page.get("rows") or [] if isinstance(row, dict)]
    containers = ", ".join(str(row["container"]) for row in rows if row.get("container"))
    data = sum(int(row.get("data") or 0) for row in rows if isinstance(row.get("data"), int))
    hosts = [str(host) for host in page.get("hosts") or []]
    print(f"This deletes the app {name}:")
    print(f"  {'containers'.ljust(10)}  {containers or '(none)'}")
    print(f"  {'clone'.ljust(10)}  {app_dir}/repo")
    print(f"  {'env file'.ljust(10)}  {env_file}")
    print(f"  {'data'.ljust(10)}  {app_dir}/data ({format_bytes(data)})")
    if hosts:
        print(f"  {'caddy site'.ljust(10)}  {', '.join(hosts)}")
    print("This cannot be undone. Copy the data directory first if you need it.")


def _print_deploy_note(note: dict, name: str) -> None:
    status = str(note.get("status") or "")
    image = str(note.get("image") or "")
    line = "  ".join(part for part in (status, image) if part)
    if client.color_on():
        line = ansi(line, deploy_attention(status)) or line
    if line:
        print(line)
    if status == "failed":
        print(f"Read the full log: yard app logs --deploy --app {name}")
    elif status == "reverted":
        print("The new container never got healthy, so the previous image still runs.")
        print(f"Read why: yard app logs --deploy --app {name}, then yard app logs --app {name}")


def _cmd_env_cat(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    client.show(client.api(session, "GET", f"/api/v1/apps/{quote(name)}/env", None), format_env, dict)


def _cmd_env_get(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    env = client.object_of(client.api(session, "GET", f"/api/v1/apps/{quote(name)}/env", None))
    if not isinstance(env, dict) or args.key not in env:
        client.die(f"{args.key} is not set for {name}")
    value = str(env[args.key])
    print(json.dumps({args.key: value}) if client.out.json else value)


def _env_saved(name: str) -> None:
    print(f"Saved. {name} keeps its old settings until the next deploy.")
    print(f"Apply now: yard app deploy --app {name}")


def _cmd_env_set(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    patch: dict[str, str] = {}
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            client.die(f"need KEY=VALUE, got {pair}\nExample: yard env set DATABASE_PATH=/data/app.sqlite3")
        patch[key] = value
    raw = client.api(session, "PATCH", f"/api/v1/apps/{quote(name)}/env", patch)
    if client.out.json:
        print(raw)
        return
    _env_saved(name)


def _cmd_env_unset(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    patch = {key: None for key in args.keys}
    raw = client.api(session, "PATCH", f"/api/v1/apps/{quote(name)}/env", patch)
    if client.out.json:
        print(raw)
        return
    _env_saved(name)


def _cmd_env_edit(args: argparse.Namespace) -> None:
    session = client.load(args)
    name = checkout.app_name(session, args)
    if sys.stdin.isatty():
        env = client.object_of(client.api(session, "GET", f"/api/v1/apps/{quote(name)}/env", None))
        text = _edit_text(format_env(env if isinstance(env, dict) else {}))
    else:
        text = _read_stdin()
    try:
        env = parse_env(text)
    except YardError as exc:
        return client.fail(exc)
    raw = client.api(session, "PUT", f"/api/v1/apps/{quote(name)}/env", env)
    if client.out.json:
        print(raw)
        return
    _env_saved(name)


def _site_path(name: str) -> str:
    return f"/api/v1/apps/{quote(name)}/caddy"


def _print_site_saved(name: str, raw: str) -> None:
    if client.out.json:
        print(raw)
        return
    data = client.object_of(raw)
    hosts = [str(host) for host in data.get("hosts") or []] if isinstance(data, dict) else []
    if hosts:
        print(f"Saved. Caddy serves {', '.join(hosts)} for {name}.")
    else:
        print(f"Saved. Caddy serves no host for {name}.")


def _cmd_caddy_cat(args: argparse.Namespace) -> None:
    session = client.load(args)
    text = client.api(session, "GET", _site_path(checkout.app_name(session, args)), None)
    if text:
        sys.stdout.write(text + "\n")


def _cmd_caddy_edit(args: argparse.Namespace) -> None:
    """$EDITOR on the site file, or the file from stdin. A refused file opens the editor again."""
    session = client.load(args)
    name = checkout.app_name(session, args)
    if not sys.stdin.isatty():
        raw = client.api(session, "PUT", _site_path(name), _read_stdin().encode())
        _print_site_saved(name, raw)
        return
    current = client.api(session, "GET", _site_path(name), None)
    start = current + "\n" if current else _site_example(name)
    text = _edit_text(start, suffix=".caddy")
    while True:
        if text == start:
            print("No change.")
            return
        code, raw = client.api_try(session, "PUT", _site_path(name), text.encode())
        if code < 300:
            _print_site_saved(name, raw)
            return
        print(explain(raw), file=sys.stderr)
        if client.ask("Edit again? [Y/n] ", stopped="Not saved.").lower().startswith("n"):
            client.die("Not saved. Caddy keeps the site it had.")
        start, text = "", _edit_text(text, suffix=".caddy")


def _site_example(name: str) -> str:
    return (
        f"# The Caddy site for {name}. Yard loads it on save; Caddy checks it first.\n"
        "# Docs: https://caddyserver.com/docs/caddyfile\n"
        "#\n"
        f"# {name}.example.com {{\n"
        f"# \treverse_proxy {name}-app-1:8000\n"
        "# }\n"
    )


def _cmd_token_list(args: argparse.Namespace) -> None:
    raw = client.api(client.load(args), "GET", "/api/v1/tokens", None)
    client.show(raw, partial(format_tokens, color=client.color_on()))


def _cmd_token_new(args: argparse.Namespace) -> None:
    _show_record(client.api(client.load(args), "POST", "/api/v1/tokens", {"name": args.name}))


def _cmd_token_rm(args: argparse.Namespace) -> None:
    _show_record(client.api(client.load(args), "DELETE", "/api/v1/tokens/" + quote(args.name), None))


def _cmd_forge_ls(args: argparse.Namespace) -> None:
    raw = client.api(client.load(args), "GET", "/api/v1/forges", None)
    client.show(raw, partial(format_forges, color=client.color_on()))


def _show_record(raw: str) -> None:
    if client.out.json:
        print(raw)
        return
    print(format_record(raw), end="")


def _read_stdin() -> str:
    raw = sys.stdin.buffer.read((1 << 20) + 1)
    if len(raw) > 1 << 20:
        client.die("the file is too large")
    return raw.decode()


def _edit_text(initial: str, suffix: str = ".env") -> str:
    handle = tempfile.NamedTemporaryFile(prefix="yard-", suffix=suffix, delete=False)
    path = Path(handle.name)
    try:
        path.write_text(initial)
        editor = os.environ.get("EDITOR") or "vi"
        proc = subprocess.run([*editor.split(), str(path)])
        if proc.returncode:
            client.die(f"the editor exited with {proc.returncode}; nothing was saved")
        return path.read_text()
    finally:
        path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON")
    common.add_argument("--no-color", action="store_true", help="plain output")
    common.add_argument("--url", default="", help="the host address")
    common.add_argument("--token", default="", help="a token")

    parser = argparse.ArgumentParser(
        prog="yard",
        description="Deploy a repo. Yard builds the image and serves the app.",
        parents=[common],
    )
    parser.add_argument("--version", action="store_true", help="print the version")
    subs = parser.add_subparsers(dest="command", metavar="command")

    status = subs.add_parser("status", help="running containers and app config", parents=[common])
    status.add_argument("--running", action="store_true", help="only running containers")
    status.add_argument("--full", action="store_true", help="include the host checks")
    status.set_defaults(func=_cmd_status)

    prune = subs.add_parser("prune", help="remove what no app needs", parents=[common])
    prune.add_argument("--days", type=int, default=7, help="keep what is newer than this")
    prune.add_argument("--dry-run", action="store_true", help="report only, remove nothing")
    prune.set_defaults(func=_cmd_prune)

    login = subs.add_parser("login", help="save a host and token", parents=[common])
    login.set_defaults(func=_cmd_login)

    logs = subs.add_parser("logs", help="container logs from the journal", parents=[common])
    logs.add_argument("container", help="a container name or id")
    _log_flags(logs)
    logs.set_defaults(func=_cmd_logs)

    events = subs.add_parser("events", help="the podman and yard event timeline", parents=[common])
    events.add_argument("--app", default="", help="only this app")
    events.add_argument("--container", default="", help="a container name or id")
    events.add_argument("--source", default="", choices=["", "podman", "yard"], help="podman or yard")
    events.add_argument("--event", default="", help="an event name prefix, like deploy.")
    events.add_argument(
        "--since", default="", help="where the rows start: -15m, -1h (the default), or 2025-01-31 14:00"
    )
    events.add_argument("--until", default="", help="same forms as --since")
    events.add_argument("--after", default="", help="start after this cursor; a row's cursor in --json")
    events.add_argument("--lines", type=int, default=200, help="how many rows from the start, oldest first")
    events.add_argument("-f", "--follow", action="store_true", help="stream new events; NDJSON with --json")
    events.set_defaults(func=_cmd_events)

    for name, help_text, build in (
        ("app", "apps", _app_parser),
        ("env", "app env", _env_parser),
        ("caddy", "the Caddy site of an app", _caddy_parser),
        ("token", "tokens", _token_parser),
        ("forge", "git forges", _forge_parser),
        ("repo", "repos on the forge", _repo_parser),
    ):
        group = subs.add_parser(name, help=help_text, parents=[common])
        verbs = group.add_subparsers(dest="verb", metavar="command", required=True)
        build(verbs, common)

    return parser


def _add_app_flag(parser: argparse.ArgumentParser, help_text: str = "the app name") -> None:
    parser.add_argument("--app", default="", help=help_text)


def _log_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-f", "--follow", action="store_true", help="stream the log")
    parser.add_argument("--tail", type=int, default=100, help="lines read before filters")
    parser.add_argument("--since", default="", help="-15m, -1h, -2d, or 2025-01-31 14:00")
    parser.add_argument("--until", default="", help="same forms as --since")
    parser.add_argument(
        "--hide",
        action="append",
        default=[],
        metavar="EXPR",
        help="hide lines or traces that match: key=value, key!=value, key>n, key<n, or text",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="EXPR",
        help="keep only lines or traces that match; same forms as --hide",
    )


def _app_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("ls", help="the apps", parents=[common])
    listed.set_defaults(func=_cmd_app_ls)

    new = subs.add_parser("new", help="create an app", parents=[common])
    new.add_argument("--repo", default="", help="the repo https URL")
    new.add_argument("--ref", default="", help="the branch to follow")
    new.add_argument("--trigger", default="", help="push or manual")
    new.add_argument(
        "--caddy",
        action="append",
        default=[],
        metavar="HOST",
        help="a public host; Caddy sends every host to the app container",
    )
    new.add_argument("--no-deploy", action="store_true", help="create the app without a first deploy")
    new.add_argument("-e", "--env", action="append", default=[], metavar="KEY=VALUE", help="a setting the app reads")
    new.add_argument("--env-file", metavar="PATH", default="", help="a dotenv file of settings")
    new.add_argument(
        "--edit-env",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=f"edit the settings in $EDITOR first, starting from PATH or {ENV_TEMPLATES[0]} in the checkout",
    )
    new.add_argument(
        "--runs", default="", help="auto, a path in the repo, yard:<template>, an image name:tag, or an https URL"
    )
    _add_app_flag(new, "the app name; the repo and branch give a default")
    new.set_defaults(func=_cmd_app_new)

    set_ = subs.add_parser("set", help="change the repo, ref, trigger, or runs", parents=[common])
    set_.add_argument("--repo", default="", help="the repo https URL")
    set_.add_argument("--ref", default="", help="the branch to follow")
    set_.add_argument("--trigger", default="", help="push or manual")
    set_.add_argument(
        "--runs", default="", help="auto, a path in the repo, yard:<template>, an image name:tag, or an https URL"
    )
    _add_app_flag(set_)
    set_.set_defaults(func=_cmd_app_set)

    stat = subs.add_parser("status", help="one app's page", parents=[common])
    _add_app_flag(stat)
    stat.set_defaults(func=_cmd_app_status)

    cat = subs.add_parser("cat", help="print the compose or Containerfile the app runs", parents=[common])
    cat.add_argument("which", choices=["compose", "containerfile"], help="which file to print")
    _add_app_flag(cat)
    cat.set_defaults(func=_cmd_app_cat)

    logs = subs.add_parser("logs", help="container logs", parents=[common])
    _log_flags(logs)
    logs.add_argument("--deploy", action="store_true", help="the deploy log instead")
    logs.add_argument("--container", default="", help="a container of the app")
    _add_app_flag(logs)
    logs.set_defaults(func=_cmd_app_logs)

    deploy = subs.add_parser("deploy", help="deploy what the source says now", parents=[common])
    _add_app_flag(deploy)
    deploy.set_defaults(func=_cmd_app_deploy)

    rm = subs.add_parser("rm", help="remove an app", parents=[common])
    _add_app_flag(rm)
    rm.add_argument("--yes", action="store_true", help="remove without confirming")
    rm.set_defaults(func=_cmd_app_rm)


def _forge_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("ls", help="the forges", parents=[common])
    listed.set_defaults(func=_cmd_forge_ls)

    setup = subs.add_parser("setup", help="connect this host to a forge", parents=[common])
    setup.add_argument("forge", choices=["origin", "github"], help="the forge")
    setup.add_argument("--org", default="", help="create the GitHub app in this organization")
    setup.add_argument("--public", action="store_true", help="create a GitHub app that any account can install")
    setup.add_argument(
        "--existing", action="store_true", help="connect a GitHub app you already have: its ID, key, and secret"
    )
    setup.add_argument("--app-id", default="", help="the ID of an existing GitHub app")
    setup.add_argument("--key", default="", help="the private key PEM of an existing GitHub app")
    setup.add_argument("--webhook-secret", default="", help="the webhook secret of an existing GitHub app")
    setup.add_argument(
        "--no-browser", action="store_true", help="print the links; open nothing (no browser here: use --existing)"
    )
    setup.add_argument("--new", action="store_true", help="replace a connected GitHub app; drop a pending setup")
    setup.set_defaults(func=forge.cmd_forge_setup)


def _repo_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    new = subs.add_parser(
        "new", help="create a private repo on the forge for this checkout and push it", parents=[common]
    )
    new.add_argument("name", nargs="?", default="", help="the repo name; the directory name is the default")
    new.add_argument("--owner", default="", help="the account on the forge, when the app is installed on several")
    new.add_argument("--forge", choices=["github", "origin"], default="", help="the forge, when both are connected")
    new.set_defaults(func=_cmd_repo_new)


def _env_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    cat = subs.add_parser("cat", help="print the env file", parents=[common])
    _add_app_flag(cat)
    cat.set_defaults(func=_cmd_env_cat)

    get = subs.add_parser("get", help="print one value", parents=[common])
    get.add_argument("key", metavar="KEY")
    _add_app_flag(get)
    get.set_defaults(func=_cmd_env_get)

    set_ = subs.add_parser("set", help="set env values", parents=[common])
    set_.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    _add_app_flag(set_)
    set_.set_defaults(func=_cmd_env_set)

    unset = subs.add_parser("unset", help="remove env values", parents=[common])
    unset.add_argument("keys", nargs="+", metavar="KEY")
    _add_app_flag(unset)
    unset.set_defaults(func=_cmd_env_unset)

    edit = subs.add_parser("edit", help="edit the env file", parents=[common])
    _add_app_flag(edit)
    edit.set_defaults(func=_cmd_env_edit)


def _caddy_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    cat = subs.add_parser("cat", help="print the site file", parents=[common])
    _add_app_flag(cat)
    cat.set_defaults(func=_cmd_caddy_cat)

    edit = subs.add_parser("edit", help="edit the site file; Caddy loads it on save", parents=[common])
    _add_app_flag(edit)
    edit.set_defaults(func=_cmd_caddy_edit)


def _token_parser(subs: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    listed = subs.add_parser("ls", help="the tokens", parents=[common])
    listed.set_defaults(func=_cmd_token_list)

    new = subs.add_parser("new", help="create a token", parents=[common])
    new.add_argument("name")
    new.set_defaults(func=_cmd_token_new)

    rm = subs.add_parser("rm", help="remove a token", parents=[common])
    rm.add_argument("name")
    rm.set_defaults(func=_cmd_token_rm)


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    client.out.no_color = args.no_color
    client.out.json = args.json
    if args.version:
        print(package_version())
        return
    if not hasattr(args, "func"):
        _parser().print_help()
        return
    args.func(args)
