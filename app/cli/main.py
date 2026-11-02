"""The `yard` argument tree and the entry point. One function per command; `main` dispatches."""

import argparse
import getpass
import html
import http.server
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from app import package_version
from app.cli.render import (
    _deploy_attention,
    ansi,
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
    FORGE_HOSTS,
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

_no_color = False
_json = False

TIMEOUT = 30

FORGE_HINT = "No forge is connected. Run yard forge setup origin or yard forge setup github."

# `app new --edit-env` with no PATH starts from the first of these at the top of the checkout.
ENV_TEMPLATES = (".env.example", ".env.sample", ".env.template", "env.example")


@dataclass(slots=True)
class Client:
    url: str
    token: str


def config_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.environ.get("XDG_CONFIG_HOME"):
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        base = Path.home() / ".config"
    return base / "yard" / "config.json"


def _load(args: argparse.Namespace) -> Client:
    raw: dict[str, Any] = {}
    path = config_path()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            raw = loaded

    # A flag replaces the environment, and the environment replaces the file.
    url = str(raw.get("url") or "")
    token = str(raw.get("token") or "")
    if os.environ.get("YARD_URL"):
        url = os.environ["YARD_URL"]
    if os.environ.get("YARD_TOKEN"):
        token = os.environ["YARD_TOKEN"]
    if args.url:
        url = args.url
    if args.token:
        token = args.token
    url = _http_url(url)
    if not url or not token:
        _die(
            "not logged in.\n"
            "Run: yard login --url https://<your yard host> --token <token>\n"
            "The admin token is in /srv/yard/tokens/admin.token on the host."
        )
    return Client(url, token)


def _http_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        return "https://" + url
    return url


def _api(client: Client, method: str, path: str, body: object, timeout: int = TIMEOUT) -> str:
    status, text = _api_try(client, method, path, body, timeout)
    if status >= 300:
        _die(text if _json else explain(text))
    return text


def _api_try(client: Client, method: str, path: str, body: object, timeout: int = TIMEOUT) -> tuple[int, str]:
    request = _request(client, method, path, body)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode().strip()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode().strip()
    except TimeoutError:
        _die(_unreachable(client, f"no answer in {timeout} s", method, path))
    except urllib.error.URLError as exc:
        reason = f"no answer in {timeout} s" if isinstance(exc.reason, TimeoutError) else str(exc.reason)
        _die(_unreachable(client, reason, method, path))


def _unreachable(client: Client, reason: str, method: str, path: str) -> str:
    """A connection failure says where it dialed and what to check. A deploy may
    still be running on the host, so a timed-out POST points at its log."""
    message = (
        f"the Yard API at {client.url} did not answer: {reason}\n"
        "Check the address with yard login, or on the host: sudo -u yard podman ps"
    )
    deploy = re.fullmatch(r"/api/v1/apps/([^/]+)/deploy", path)
    if method == "POST" and deploy:
        message += (
            "\nThe deploy may still be running."
            f" Follow it with: yard app logs --deploy --app {unquote(deploy.group(1))}"
        )
    return message


def _stream_get(client: Client, path: str) -> None:
    _stream(client, "GET", path, None)


def _stream(client: Client, method: str, path: str, body: object) -> None:
    """A streaming call: log follow has no timeout."""
    request = _request(client, method, path, body)
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode().strip()
        _die(raw if _json else explain(raw))
    except TimeoutError:
        _die(_unreachable(client, "no answer", method, path))
    except urllib.error.URLError as exc:
        _die(_unreachable(client, str(exc.reason), method, path))
    with response:
        if response.status >= 300:
            raw = response.read().decode().strip()
            _die(raw if _json else explain(raw))
        shutil.copyfileobj(response, sys.stdout.buffer)


def _request(client: Client, method: str, path: str, body: object) -> urllib.request.Request:
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    headers = {"authorization": f"Bearer {client.token}"}
    if _json:
        # The logs route answers its entries page for JSON; every other route answers the same either way.
        headers["accept"] = "application/json"
    if data is not None:
        headers["content-type"] = "text/caddyfile" if isinstance(body, bytes) else "application/json"
    return urllib.request.Request(client.url + path, data=data, headers=headers, method=method)


def _fail(exc: YardError) -> NoReturn:
    if _json:
        _die(json.dumps({"error": exc.payload()}))
    _die(str(exc))


def _die(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _object(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _die(raw)


def _git_out(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if proc.returncode:
        return ""
    return proc.stdout.strip()


def _checkout_pair() -> tuple[str, str]:
    """(https repo URL, branch) when this directory is a forge checkout, else two empties."""
    if not _git_out("rev-parse", "--show-toplevel"):
        return "", ""
    repo = _remote_repo(_git_out("remote", "get-url", "origin"))
    if not repo:
        return "", ""
    return repo, _git_out("branch", "--show-current")


def _remote_repo(remote: str) -> str:
    """The https repo URL when the remote is on Origin or GitHub. Three spellings count."""
    remote = remote.strip()
    short = re.fullmatch(r"git@([^:/]+):(.+)", remote)
    if short:
        host, path = short.group(1), short.group(2)
    else:
        parts = urlsplit(remote)
        host, path = parts.hostname or "", parts.path.lstrip("/")
    if host not in FORGE_HOSTS:
        return ""
    if path.endswith(".git"):
        path = path[:-4]
    found = path.split("/")
    if len(found) != 2 or not all(found):
        return ""
    return f"https://{host}/{found[0]}/{found[1]}"


def _source_name(kind: str, runs: str) -> str:
    """The app name for a repo-less app: the image's last repo segment, or the URL file's stem."""
    if kind == "image":
        return runs.split("@", 1)[0].rsplit(":", 1)[0].rsplit("/", 1)[-1]
    tail = runs.rsplit("/", 1)[-1]
    return tail.removesuffix(".yml").removesuffix(".yaml").removesuffix(".compose")


def _slug(text: str) -> str:
    """An app-name slug from loose text: lowercase, digits, and hyphens."""
    name = re.sub(r"[^a-z0-9-]+", "-", text.lower())
    return re.sub(r"-+", "-", name).strip("-")[:63].strip("-")


def _default_name(repo: str, ref: str) -> str:
    """repo name for main and master; otherwise <repo>-<branch> as an app name."""
    name = repo.split("/")[-1]
    if ref not in {"main", "master"}:
        name = f"{name}-{ref}"
    return _slug(name)


def _app_name(client_: Client, args: argparse.Namespace) -> str:
    """The app for this checkout. --app overrides. Every error mentions --app."""
    if str(getattr(args, "app", "") or "").strip():
        return str(args.app).strip()
    if not _git_out("rev-parse", "--show-toplevel"):
        _die("not a git checkout here; pass --app NAME, or cd into the app's checkout. yard app ls lists the apps.")
    repo = _remote_repo(_git_out("remote", "get-url", "origin"))
    if not repo:
        _die(
            "the origin remote is not on Origin or GitHub; pass --app NAME, or cd into the app's checkout."
            " yard app ls lists the apps."
        )
    branch = _git_out("branch", "--show-current")
    if not branch:
        _die("detached HEAD; pass --app NAME. yard app ls lists the apps.")
    rows = _object(_api(client_, "GET", "/api/v1/apps", None))
    found = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("repo") or "").lower() == repo.lower()
        and str(row.get("ref") or "") == branch
    ]
    if not found:
        _die(
            f"no app follows {repo} {branch}.\n"
            "Create one here: yard app new   (or pass --app NAME; yard app ls lists the apps)"
        )
    if len(found) > 1:
        _die(f"more than one app follows {repo} {branch}; pass --app NAME. yard app ls lists the apps.")
    return str(found[0]["name"])


def _cmd_status(args: argparse.Namespace) -> None:
    session = _load(args)
    path = "/api/v1/status" + ("?running=1" if getattr(args, "running", False) else "")
    raw = _api(session, "GET", path, None)
    report = _object(raw)
    checks: Any = None
    if getattr(args, "full", False):
        checks = _object(_api(session, "GET", "/api/v1/doctor", None))
    if _json:
        # One shape: the status report, with the doctor rows under "doctor" for --full.
        print(json.dumps({**report, "doctor": checks}) if checks is not None and isinstance(report, dict) else raw)
        _doctor_exit(checks)
        return
    if not isinstance(report, dict):
        print(raw)
        return
    print(format_status(report, datetime.now(UTC), color=_color_on()), end="")
    hint = status_hint(report)
    if hint and sys.stderr.isatty():
        if _color_on(sys.stderr):
            hint = ansi(hint, "2")
        print(hint, file=sys.stderr)
    _forge_warn(report)
    if isinstance(checks, list):
        print(format_doctor(checks, color=_color_on()), end="")
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
    session = _load(args)
    raw = _api(session, "POST", "/api/v1/prune", {"days": args.days, "dry": args.dry_run})
    found = _object(raw)
    if _json or not isinstance(found, dict):
        print(raw)
        return
    print(format_prune(found), end="")


def _cmd_login(args: argparse.Namespace) -> None:
    url = _http_url(args.url)
    token = args.token.strip()
    if not url or not token:
        _die("yard login needs --url and --token")

    # The token file stays private to this user.
    path = config_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    atomic_write(path, json.dumps({"url": url, "token": token}, indent=2) + "\n")
    print("logged in")
    print(f"{'host'.ljust(6)}  {url}")
    print(f"{'saved'.ljust(6)}  {path}")

    # The login stands even when this call fails.
    code, text = _api_try(Client(url, token), "GET", "/api/v1/status", None)
    if code < 300:
        _forge_warn(_object(text))


def _cmd_app_ls(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "GET", "/api/v1/apps", None)
    rows = _object(raw)
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_apps(rows, color=_color_on()), end="")


def _cmd_repo_new(args: argparse.Namespace) -> None:
    """Make a private repo on the forge, wire origin, push.

    The checkout stays untouched: no `git add`, no `git commit` — one wrong add
    could commit an .env file.
    """
    top = _git_out("rev-parse", "--show-toplevel")
    if not top:
        _die("not a git checkout here. Run git init, commit the files the app needs, then run this again.")
    if not _git_out("rev-parse", "--verify", "HEAD"):
        _die("no commit yet. Commit the files the app needs, then run this again.")
    branch = _git_out("branch", "--show-current")
    if not branch:
        _die("detached HEAD; check out a branch, then run this again.")
    remote = _git_out("remote", "get-url", "origin")
    if remote:
        existing = _remote_repo(remote)
        if existing:
            _die(f"origin is already {existing}. yard app new uses it.")
        _die(f"origin is already {remote}. Remove origin to create a repo.")
    body = {"name": args.name.strip() or _slug(Path(top).name), "owner": args.owner.strip(), "forge": args.forge}
    result = _object(_api(_load(args), "POST", "/api/v1/repos", {key: value for key, value in body.items() if value}))
    repo = str(result.get("repo") or "") if isinstance(result, dict) else ""
    proc = subprocess.run(["git", "remote", "add", "origin", f"{repo}.git"], capture_output=True, text=True)
    if proc.returncode:
        _die(f"{repo} was created, but git remote add failed: {proc.stderr.strip()}")
    if _git_out("status", "--porcelain"):
        print("warning: uncommitted changes are not pushed", file=sys.stderr)
    proc = subprocess.run(["git", "push", "-u", "origin", branch])
    if proc.returncode:
        _die(f"{repo} was created, but the push failed. Push it: git push -u origin {branch}")
    if _json:
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
            _die(f"{runs} needs no repo; an app with a repo runs a file from it")
        if not repo_less:
            if not repo or not ref:
                found_repo, found_ref = _checkout_pair()
                repo = repo or found_repo
                if not ref:
                    # The checkout branch counts only when the repo is the checkout's.
                    ref = found_ref if found_repo and repo.lower() == found_repo.lower() else ""
            if kind == "path":
                top = _git_out("rev-parse", "--show-toplevel")
                if top and not (Path(top) / runs).is_file():
                    _die(f"{runs} is not a file in this checkout. An image needs a tag: {runs}:TAG")
    except YardError as exc:
        _fail(exc)
    ref = ref or "main"
    name = args.app.strip()
    try:
        if repo_less:
            name = name or _source_name(kind, runs)
        else:
            if not repo:
                _die(
                    "no repo; pass --repo https://<host>/<owner>/<name>, run inside a forge checkout,"
                    " or run yard repo new first"
                )
            name = name or _default_name(repo, ref)
            repo = check_repo(repo)
        check_app_name(name)
        hosts = [host.strip() for host in args.caddy]
        for host in hosts:
            check_domain(host)
    except YardError as exc:
        _fail(exc)
    if hosts and args.no_deploy:
        _die("--caddy needs the first deploy: Yard reads the port from the running container.")

    env = _new_app_env(args, name)
    session = _load(args)
    body: dict[str, object] = {"name": name, "repo": repo}
    if not repo_less:
        body["ref"] = ref
    if args.trigger:
        body["trigger"] = args.trigger
    if runs:
        body["runs"] = runs
    raw = _api(session, "POST", "/api/v1/apps", body)
    if env:
        code, text = _api_try(session, "PUT", f"/api/v1/apps/{quote(name)}/env", env)
        if code >= 300:
            _die(f"{name} was created, but its env was not saved: {explain(text)}\nSet it: yard env edit --app {name}")
    wrap = _object(raw)
    app = wrap.get("app") if isinstance(wrap, dict) else None
    if args.no_deploy:
        if _json:
            print(raw)
            return
        if isinstance(app, dict):
            print(format_record(json.dumps(app)), end="")
        else:
            print(raw)
        print(f"Not deployed yet. Deploy it: yard app deploy --app {name}")
        return
    if not _json:
        if isinstance(app, dict):
            print(format_record(json.dumps(app)), end="")
        else:
            print(raw)
        print(f"Deploying {name} now…")
    code, text = _api_try(session, "POST", f"/api/v1/apps/{quote(name)}/deploy", {})
    if code >= 300:
        _die(f"{name} was created, but the first deploy failed.\n" + (text if _json else explain(text)))
    note = _follow_deploy(session, name) or {}
    if note and not _json:
        _print_deploy_note(note, name)
    if note.get("status") == "failed":
        if hosts:
            print(f"Caddy has no site for {name} yet. After a good deploy, run: yard caddy edit --app {name}")
        _die(f"{name} was created, but the first deploy failed.")
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
                _die("--edit-env needs a terminal. Use -e KEY=VALUE or --env-file PATH instead.")
            env = parse_env(_edit_text(_env_draft(env, args.edit_env)))
            empty = [key for key, value in env.items() if not value]
            if empty:
                env = {key: value for key, value in env.items() if value}
                print(f"These keys are empty, so they are not saved: {', '.join(empty)}", file=sys.stderr)
                print(f"Set them: yard env set --app {name} {' '.join(f'{key}=…' for key in empty)}", file=sys.stderr)
    except OSError as exc:
        _die(f"cannot read {exc.filename}: {exc.strerror or exc}")
    except YardError as exc:
        _fail(exc)
    return env


def _env_draft(env: dict[str, str], path: str) -> str:
    """The editor text: the env, then the template lines for the keys it does not set.

    The template is PATH, else the first of ENV_TEMPLATES at the top of the checkout, else nothing.
    """
    if not path:
        top = Path(_git_out("rev-parse", "--show-toplevel") or ".")
        path = next((str(top / name) for name in ENV_TEMPLATES if (top / name).is_file()), "")
    template = Path(path).read_text() if path else ""
    rest = "\n".join(
        line
        for line in template.splitlines()
        if line.lstrip().startswith("#") or line.strip().removeprefix("export ").partition("=")[0].strip() not in env
    ).strip()
    parts = [text for text in (format_env(env).strip(), rest) if text]
    return "\n\n".join(parts) + "\n" if parts else ""


def _write_default_site(session: Client, name: str, hosts: list[str], port: int) -> None:
    """The default site, with the container and the port that the deploy found. No port, no file."""
    if not hosts:
        return
    container = app_container(name)
    if not port:
        print(f"Caddy has no site for {name}: {container} does not expose exactly one port.", file=sys.stderr)
        print(f"Write the site with the port the app listens on: yard caddy edit --app {name}", file=sys.stderr)
        print(default_site(hosts, f"{container}:PORT"), end="", file=sys.stderr)
        raise SystemExit(1)
    _print_site_saved(name, _api(session, "PUT", _site_path(name), default_site(hosts, f"{container}:{port}").encode()))


def _cmd_app_set(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    patch: dict[str, str] = {}
    for key in ("repo", "ref", "trigger", "runs"):
        value = str(getattr(args, key, "") or "").strip()
        if value:
            patch[key] = value
    if not patch:
        _die("yard app set needs --repo, --ref, --trigger, or --runs")
    page = _object(_api(session, "GET", "/api/v1/apps/" + quote(name), None))
    current = page.get("app") if isinstance(page, dict) else None
    current = current if isinstance(current, dict) else {}
    raw = _api(session, "PATCH", "/api/v1/apps/" + quote(name), patch)
    if _json:
        print(raw)
        return
    wrap = _object(raw)
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
    session = _load(args)
    name = _app_name(session, args)
    code, text = _api_try(session, "GET", f"/api/v1/apps/{quote(name)}/files/{args.which}", None)
    if code >= 300:
        _die(text if _json else explain(text))
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")


def _cmd_app_status(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    raw = _api(session, "GET", f"/api/v1/apps/{quote(name)}", None)
    if _json:
        print(raw)
        return
    report = _object(raw)
    if not isinstance(report, dict):
        print(raw)
        return
    print(format_app_status(report, datetime.now(UTC), color=_color_on()), end="")


def _cmd_logs(args: argparse.Namespace) -> None:
    session = _load(args)
    _logs(session, args.container, args)


def _cmd_app_logs(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    if args.deploy and args.follow and not args.container:
        code, text = _api_try(session, "GET", f"/api/v1/apps/{quote(name)}/deploy", None)
        wrap = _object(text) if code == 200 else None
        record = wrap.get("deploy") if isinstance(wrap, dict) else None
        if isinstance(record, dict) and record.get("status") == "running":
            # A running deploy streams its lines from the start, the sync included.
            _follow_deploy(session, name)
            return
    # The deploy container's log is the last deploy, in the journal.
    container = args.container or (deploy_container(name) if args.deploy else app_container(name))
    _logs(session, container, args)


def _logs(session: Client, container: str, args: argparse.Namespace) -> None:
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
        _stream_get(session, path)
        return
    raw = _api(session, "GET", path, None)
    print(raw, end="" if raw.endswith("\n") or not raw else "\n")


def _cmd_events(args: argparse.Namespace) -> None:
    session = _load(args)
    query: dict[str, str] = {"lines": str(args.tail)}
    for key in ("since", "until", "app", "container", "source", "event"):
        value = str(getattr(args, key, "") or "")
        if value:
            query[key] = value
    if args.follow:
        query["follow"] = "1"
    path = "/api/v1/events?" + urlencode(query)
    if args.follow and _json:
        _stream_get(session, path)
        return
    if args.follow:
        _follow_events(session, path)
        return
    raw = _api(session, "GET", path, None)
    if _json:
        print(raw)
        return
    rows = _object(raw)
    if not isinstance(rows, list):
        print(raw)
        return
    print(format_events(rows, color=_color_on()), end="")


def _follow_events(session: Client, path: str) -> None:
    """The NDJSON event stream, one table line for each event as it arrives."""
    request = _request(session, "GET", path, None)
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        _die(explain(exc.read().decode().strip()))
    except urllib.error.URLError as exc:
        _die(_unreachable(session, str(exc.reason), "GET", path))
    with response:
        try:
            for raw in response:
                row = _object(raw.decode())
                if isinstance(row, dict):
                    print(format_events([row], color=_color_on()), end="", flush=True)
        except KeyboardInterrupt:
            sys.exit(130)


def _follow_deploy(session: Client, name: str) -> dict:
    """Print the deploy log live; return the final deploy object."""
    path = f"/api/v1/apps/{quote(name)}/deploy?follow=1"
    request = _request(session, "GET", path, None)
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode().strip()
        _die(raw if _json else explain(raw))
    except TimeoutError:
        _die(_unreachable(session, "no answer", "GET", path))
    except urllib.error.URLError as exc:
        _die(_unreachable(session, str(exc.reason), "GET", path))
    final: dict = {}
    with response:
        try:
            for raw in response:
                if _json:
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
                row = _object(raw.decode())
                if not isinstance(row, dict):
                    continue
                if "line" in row:
                    if not _json:
                        print(row["line"])
                elif isinstance(row.get("deploy"), dict):
                    final = row["deploy"]
        except KeyboardInterrupt:
            print(f"The deploy continues on the host. Follow it: yard app logs --deploy -f --app {name}")
            sys.exit(130)
    return final


def _cmd_app_deploy(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    _api(session, "POST", f"/api/v1/apps/{quote(name)}/deploy", {})
    if not _json:
        print(f"Deploying {name} now…")
    note = _follow_deploy(session, name)
    if _json:
        return
    if note.get("status") == "pending":
        print("Yard restarts itself. Check: yard app status --app yard")
        return
    _print_deploy_note(note, name)
    if note.get("status") == "failed":
        sys.exit(1)


def _cmd_app_rm(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    if not args.yes:
        if not sys.stdin.isatty():
            _die("yard app rm deletes the app's data and needs a confirmation.\nRun it in a terminal, or pass --yes.")
        _rm_preview(session, name)
        answer = _ask("Type the app name to confirm: ", stopped="Not removed.")
        if answer != name:
            _die(f"Not removed: you typed '{answer}', the app is '{name}'.")
    raw = _api(session, "DELETE", f"/api/v1/apps/{quote(name)}", None)
    if _json:
        print(raw)
        return
    data = _object(raw)
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


def _rm_preview(session: Client, name: str) -> None:
    """What `app rm` takes away: the containers, the files, the Caddy site. One page read."""
    page = _object(_api(session, "GET", f"/api/v1/apps/{quote(name)}", None))
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
    if _color_on():
        line = ansi(line, _deploy_attention(status)) or line
    if line:
        print(line)
    if status == "failed":
        print(f"Read the full log: yard app logs --deploy --app {name}")
    elif status == "reverted":
        print("The new container never got healthy, so the previous image still runs.")
        print(f"Read why: yard app logs --deploy --app {name}, then yard app logs --app {name}")


def _cmd_env_cat(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    raw = _api(session, "GET", f"/api/v1/apps/{quote(name)}/env", None)
    if _json:
        print(raw)
        return
    env = _object(raw)
    if not isinstance(env, dict):
        print(raw)
        return
    print(format_env(env), end="")


def _cmd_env_get(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    env = _object(_api(session, "GET", f"/api/v1/apps/{quote(name)}/env", None))
    if not isinstance(env, dict) or args.key not in env:
        _die(f"{args.key} is not set for {name}")
    value = str(env[args.key])
    print(json.dumps({args.key: value}) if _json else value)


def _env_saved(name: str) -> None:
    print(f"Saved. {name} keeps its old settings until the next deploy.")
    print(f"Apply now: yard app deploy --app {name}")


def _cmd_env_set(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    patch: dict[str, str] = {}
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            _die(f"need KEY=VALUE, got {pair}\nExample: yard env set DATABASE_PATH=/data/app.sqlite3")
        patch[key] = value
    raw = _api(session, "PATCH", f"/api/v1/apps/{quote(name)}/env", patch)
    if _json:
        print(raw)
        return
    _env_saved(name)


def _cmd_env_unset(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    patch = {key: None for key in args.keys}
    raw = _api(session, "PATCH", f"/api/v1/apps/{quote(name)}/env", patch)
    if _json:
        print(raw)
        return
    _env_saved(name)


def _cmd_env_edit(args: argparse.Namespace) -> None:
    session = _load(args)
    name = _app_name(session, args)
    if sys.stdin.isatty():
        env = _object(_api(session, "GET", f"/api/v1/apps/{quote(name)}/env", None))
        text = _edit_text(format_env(env if isinstance(env, dict) else {}))
    else:
        text = _read_stdin()
    try:
        env = parse_env(text)
    except YardError as exc:
        _fail(exc)
    raw = _api(session, "PUT", f"/api/v1/apps/{quote(name)}/env", env)
    if _json:
        print(raw)
        return
    _env_saved(name)


def _site_path(name: str) -> str:
    return f"/api/v1/apps/{quote(name)}/caddy"


def _print_site_saved(name: str, raw: str) -> None:
    if _json:
        print(raw)
        return
    data = _object(raw)
    hosts = [str(host) for host in data.get("hosts") or []] if isinstance(data, dict) else []
    if hosts:
        print(f"Saved. Caddy serves {', '.join(hosts)} for {name}.")
    else:
        print(f"Saved. Caddy serves no host for {name}.")


def _cmd_caddy_cat(args: argparse.Namespace) -> None:
    session = _load(args)
    text = _api(session, "GET", _site_path(_app_name(session, args)), None)
    if text:
        sys.stdout.write(text + "\n")


def _cmd_caddy_edit(args: argparse.Namespace) -> None:
    """$EDITOR on the site file, or the file from stdin. A refused file opens the editor again."""
    session = _load(args)
    name = _app_name(session, args)
    if not sys.stdin.isatty():
        raw = _api(session, "PUT", _site_path(name), _read_stdin().encode())
        _print_site_saved(name, raw)
        return
    current = _api(session, "GET", _site_path(name), None)
    start = current + "\n" if current else _site_example(name)
    text = _edit_text(start, suffix=".caddy")
    while True:
        if text == start:
            print("No change.")
            return
        code, raw = _api_try(session, "PUT", _site_path(name), text.encode())
        if code < 300:
            _print_site_saved(name, raw)
            return
        print(explain(raw), file=sys.stderr)
        if _ask("Edit again? [Y/n] ", stopped="Not saved.").lower().startswith("n"):
            _die("Not saved. Caddy keeps the site it had.")
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
    raw = _api(_load(args), "GET", "/api/v1/tokens", None)
    rows = _object(raw)
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_tokens(rows, color=_color_on()), end="")


def _cmd_token_new(args: argparse.Namespace) -> None:
    _show_record(_api(_load(args), "POST", "/api/v1/tokens", {"name": args.name}))


def _cmd_token_rm(args: argparse.Namespace) -> None:
    _show_record(_api(_load(args), "DELETE", "/api/v1/tokens/" + quote(args.name), None))


def _cmd_forge_ls(args: argparse.Namespace) -> None:
    raw = _api(_load(args), "GET", "/api/v1/forges", None)
    rows = _object(raw)
    if _json or not isinstance(rows, list):
        print(raw)
        return
    print(format_forges(rows, color=_color_on()), end="")


def _cmd_forge_setup(args: argparse.Namespace) -> None:
    session = _load(args)
    if args.forge == "origin":
        _setup_origin(session, args)
    else:
        _setup_github(session, args)


def _normalize_path(text: str) -> str:
    """A pasted path: whitespace, one pair of quotes, and `\\ ` escapes come off; ~ expands."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return os.path.expanduser(text.replace("\\ ", " "))


def _ask(prompt: str, stopped: str = "stopped; run the command again to continue") -> str:
    """One wizard answer. EOF or Ctrl-C exits without a traceback."""
    try:
        return input(prompt).strip()
    except EOFError, KeyboardInterrupt:
        _die(stopped)


def _ask_key(key_flag: str) -> str:
    """The PEM text. An unreadable path or a file without a BEGIN line asks again."""
    default = _normalize_path(key_flag) if key_flag else ""
    while True:
        if default:
            key_path = _normalize_path(_ask(f"Private key path [{default}]: ")) or default
        else:
            key_path = _normalize_path(_ask("Private key path: "))
        if not key_path:
            print("pass --key PATH or paste a path", file=sys.stderr)
            continue
        try:
            pem = Path(key_path).read_text()
        except OSError as exc:
            print(f"cannot read the key: {exc}", file=sys.stderr)
            continue
        if "-----BEGIN" not in pem:
            print(f"{key_path} is not a PEM file (no -----BEGIN line)", file=sys.stderr)
            continue
        return pem


def _setup_origin(session: Client, args: argparse.Namespace) -> None:
    """Connect to Cursor Origin: the host makes the key, the user makes the app."""
    key = _forge_row(session, "origin").get("public_key")
    print("Open https://cursor.com/codebase/settings/apps and create an app with these values.")
    print()
    print("public key:")
    print(str(key or ""), end="")
    print(f"webhook url: {session.url}/hooks/origin")
    print("event: repository.pushed")
    print("scope: repository:contents:read namespace:repositories:create")
    print()
    _open(args, "https://cursor.com/codebase/settings/apps")

    while True:
        app_id = _ask("App ID: ")
        if re.fullmatch(r"app_[A-Za-z0-9]+", app_id):
            break
        print("That is not an app ID. It starts with app_.", file=sys.stderr)
    wrap = _object(_api(session, "PUT", "/api/v1/forges/origin", {"app_id": app_id}))
    app = wrap.get("app") if isinstance(wrap, dict) else None
    if not isinstance(app, dict):
        app = {}
    want = f"{session.url}/hooks/origin"
    have = str(app.get("webhook_url") or "")
    if have and have != want:
        print(f"warning: the webhook url is {have}; this host wants {want}", file=sys.stderr)
    events = app.get("events")
    if isinstance(events, list) and "repository.pushed" not in events:
        print("warning: the app does not subscribe to repository.pushed", file=sys.stderr)
    _install(
        session,
        args,
        "origin",
        "https://cursor.com/codebase/apps/install?"
        + urlencode({"client_id": app_id, "scope": "repository:contents:read namespace:repositories:create"}),
    )


def _setup_github(session: Client, args: argparse.Namespace) -> None:
    """Connect to GitHub: shared checks, then the manifest form or the values of an existing app."""
    parts = urlsplit(session.url)
    if parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "::1", "localhost"}:
        _die("refusing to send the private key over http; log in with an https url")
    forge = {} if args.new else _forge_row(session, "github")
    if forge.get("connected"):
        listed = ", ".join(
            f"{entry.get('account')} ({entry.get('id')})"
            for entry in forge.get("installations") or []
            if isinstance(entry, dict)
        )
        print(
            f"GitHub is already connected: app {forge.get('app_id')},"
            f" installation{'s' if ',' in listed else ''} {listed}. Run with --new to replace it."
        )
        slug = str(forge.get("slug") or "")
        if slug:
            print(f"Add another account: https://github.com/apps/{slug}/installations/new")
        return
    if forge.get("app_id") and not forge.get("installations") and not _existing(args):
        print(f"App {forge['app_id']} is already set up.")
        _install(session, args, "github", _github_install_url(str(forge.get("slug") or "")))
        return
    if _existing(args):
        _setup_github_existing(session, args)
    else:
        _setup_github_manifest(session, args)


def _existing(args: argparse.Namespace) -> bool:
    """The values of an app made before: --existing, or any of its flags."""
    return bool(args.existing or args.app_id or args.webhook_secret)


def _github_install_url(slug: str) -> str:
    return f"https://github.com/apps/{slug}/installations/new" if slug else "https://github.com/settings/installations"


def _install(session: Client, args: argparse.Namespace, forge: str, url: str) -> None:
    """The last step of every setup: open the install page, then wait for the installation."""
    print("Install the app on your account and select all repositories.")
    _open(args, url)
    _poll_installation(session, forge)
    print("Done.")


def _read_pem(path: str) -> str:
    """The PEM at a --key path, or "" when the path is empty or holds no key; the prompt then asks."""
    if not path:
        return ""
    try:
        pem = Path(_normalize_path(path)).read_text()
    except OSError:
        return ""
    return pem if "-----BEGIN" in pem else ""


def _pending_manifest(path: Path, url: str) -> dict:
    """A saved manifest app for this host, or an empty dict."""
    try:
        raw = json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict) or raw.get("url") != url:
        return {}
    return raw if isinstance(raw.get("pem"), str) else {}


def _manifest_page(session: Client, args: argparse.Namespace, base: str, state: str) -> str:
    """The local page that posts the app manifest to GitHub."""
    host = urlsplit(session.url).hostname or "host"
    manifest = {
        "name": f"yard-{host}"[:34],
        "url": session.url,
        "hook_attributes": {"url": f"{session.url}/hooks/github", "active": True},
        "redirect_url": f"{base}/callback",
        # A private app installs only on the account that owns it; a public one on any account.
        "public": bool(args.public),
        "default_permissions": {
            "contents": "write",
            "metadata": "read",
            "repository_creation": "write",
        },
        "default_events": ["push"],
    }
    action = (
        f"https://github.com/organizations/{quote(str(args.org))}/settings/apps/new"
        if args.org
        else "https://github.com/settings/apps/new"
    )
    action = f"{action}?state={quote(state)}"
    return (
        "<!doctype html><title>yard forge setup</title>"
        "<p>GitHub creates the Yard app and sends its ID, private key, "
        "and webhook secret back to this computer.</p>"
        f'<form method="post" action="{html.escape(action, quote=True)}">'
        f'<input type="hidden" name="manifest" '
        f'value="{html.escape(json.dumps(manifest), quote=True)}">'
        '<button type="submit">Create the GitHub App</button></form>'
        "<script>document.forms[0].submit()</script>"
    )


class _ManifestServer(http.server.ThreadingHTTPServer):
    """Carries the run state the handler reads: the page, the state token, the code slot."""

    manifest_page = ""
    manifest_state = ""
    manifest_code: queue.Queue[str]


class _ManifestHandler(http.server.BaseHTTPRequestHandler):
    """Serves the manifest page at / and catches GitHub's code at /callback."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """No access log."""

    def _page(self, status: int, body: str) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._page(200, str(getattr(self.server, "manifest_page", "")))
            return
        if parsed.path == "/callback":
            params = dict(parse_qsl(parsed.query))
            code = str(params.get("code") or "")
            if params.get("state") != getattr(self.server, "manifest_state", "") or not code:
                self._page(400, "<p>This link does not belong to this setup run. Go back to the terminal.</p>")
                return
            got = getattr(self.server, "manifest_code", None)
            if isinstance(got, queue.Queue):
                got.put(code)
            self._page(
                200,
                "<p>GitHub created the app. Go back to the terminal; it saves the key and opens the install page.</p>",
            )
            return
        self._page(404, "<p>not found</p>")


def _manifest_exchange(session: Client, code: str, pending_path: Path) -> dict:
    """Trade GitHub's one-time code for the app ID, key, and secret; keep a copy on disk."""
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{quote(code)}/conversions",
        data=b"",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "yard",
        },
        method="POST",
    )
    again = "The code works once and for one hour. Run the command again."
    try:
        with urllib.request.urlopen(request) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode("utf-8", errors="replace")
        _die(f"GitHub answered {exc.code}: {body}\n{again}")
    except urllib.error.URLError as exc:
        _die(f"GitHub did not answer: {exc.reason}\n{again}")
    if not isinstance(reply, dict) or not isinstance(reply.get("pem"), str):
        _die(f"GitHub's answer had no key in it. {again}")
    app = {
        "url": session.url,
        "app_id": str(reply.get("id") or ""),
        "slug": str(reply.get("slug") or ""),
        "pem": str(reply.get("pem") or ""),
        "webhook_secret": str(reply.get("webhook_secret") or ""),
        "html_url": str(reply.get("html_url") or ""),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    atomic_write(pending_path, json.dumps(app, indent=2) + "\n")
    return app


def _manifest_run(session: Client, args: argparse.Namespace, pending_path: Path) -> dict:
    """Serve the manifest page locally until GitHub sends the code back, then exchange it."""
    state = secrets.token_urlsafe(24)
    got: queue.Queue[str] = queue.Queue()
    server = _ManifestServer(("127.0.0.1", 0), _ManifestHandler)
    server.manifest_state = state
    server.manifest_code = got
    base = f"http://127.0.0.1:{server.server_address[1]}"
    server.manifest_page = _manifest_page(session, args, base, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        print("Opening the GitHub App form. Keep the name or change it, then click Create GitHub App.")
        _open(args, base + "/")
        if getattr(args, "no_browser", False):
            print("Open it in a browser on this computer; GitHub sends you back to it.")
        try:
            code = got.get(timeout=SETUP_LIMIT)
        except queue.Empty:
            _die("No answer from GitHub in ten minutes. Run the command again.")
        except EOFError, KeyboardInterrupt:
            _die("stopped; run the command again to continue")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return _manifest_exchange(session, code, pending_path)


def _save_github_app(session: Client, app: dict, pending_path: Path) -> dict:
    """PUT the app credentials to Yard. The file keeps them until the PUT lands."""
    code, text = _api_try(
        session,
        "PUT",
        "/api/v1/forges/github",
        {
            "app_id": str(app.get("app_id") or ""),
            "private_key": str(app.get("pem") or ""),
            "webhook_secret": str(app.get("webhook_secret") or ""),
        },
    )
    if code >= 300:
        detail = text if _json else explain(text)
        _die(f"{detail}\nThe key is kept in {pending_path}; run the command again to retry.")
    pending_path.unlink(missing_ok=True)
    wrap = _object(text)
    saved = wrap.get("app") if isinstance(wrap, dict) else None
    return saved if isinstance(saved, dict) else {}


def _setup_github_manifest(session: Client, args: argparse.Namespace) -> None:
    """The manifest form: GitHub makes the key and the webhook secret itself."""
    pending_path = config_path().parent / "github-manifest.json"
    if args.new:
        pending_path.unlink(missing_ok=True)
        app = {}
    else:
        app = _pending_manifest(pending_path, session.url)
    if app:
        print(f"Finishing the app created at {app.get('created')}: saving its key in Yard.")
    else:
        app = _manifest_run(session, args, pending_path)
    saved = _save_github_app(session, app, pending_path)
    _install(session, args, "github", _github_install_url(str(app.get("slug") or saved.get("slug") or "")))


def _setup_github_existing(session: Client, args: argparse.Namespace) -> None:
    """An app made before, or a manifest run that broke: the user gives its ID, key, and webhook secret."""
    print(f"The app needs the webhook url {session.url}/hooks/github, the push event,")
    print("and the permissions contents write and repository creation write.")
    app_id = str(args.app_id)
    secret = str(args.webhook_secret)
    pem = _read_pem(str(args.key))
    while True:
        while not app_id.isdigit():
            if app_id:
                print("That is not an app ID. It is digits.", file=sys.stderr)
            app_id = _ask("App ID: ")
        pem = pem or _ask_key(str(args.key))
        while not secret:
            try:
                secret = getpass.getpass("Webhook secret: ").strip()
            except EOFError, KeyboardInterrupt:
                _die("stopped; run the command again to continue")
        code, text = _api_try(
            session,
            "PUT",
            "/api/v1/forges/github",
            {"app_id": app_id, "private_key": pem, "webhook_secret": secret},
        )
        if 400 <= code < 500:
            # Yard checks the key against GitHub before it saves. Ask for every value again.
            print(text if _json else explain(text), file=sys.stderr)
            app_id, pem, secret = "", "", ""
            continue
        if code >= 300:
            _die(text if _json else explain(text))
        break
    wrap = _object(text)
    app = wrap.get("app") if isinstance(wrap, dict) else None
    _install(session, args, "github", _github_install_url(str((app or {}).get("slug") or "")))


def _open(args: argparse.Namespace, url: str) -> None:
    """A link to open in the browser. --no-browser only prints it."""
    print(url)
    if not getattr(args, "no_browser", False):
        webbrowser.open(url)


SETUP_POLL = 3.0
SETUP_LIMIT = 600.0


def _forge_row(session: Client, name: str) -> dict:
    """One forge summary by name, or an empty one when the row is missing."""
    rows = _object(_api(session, "GET", "/api/v1/forges", None))
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("name") == name:
                return row
    return {}


def _poll_installation(session: Client, name: str) -> None:
    """Ask the forge every three seconds until the installation lands. Ten minutes tops."""
    deadline = time.monotonic() + SETUP_LIMIT
    seen_error = ""
    while time.monotonic() < deadline:
        found = _object(_api(session, "POST", f"/api/v1/forges/{name}/installations", None))
        forge = found if isinstance(found, dict) else {}
        error = str(forge.get("error") or "")
        if error and error != seen_error:
            print(f"warning: {error}", file=sys.stderr)
            seen_error = error
        installations = forge.get("installations")
        if installations:
            for entry in installations:
                if isinstance(entry, dict):
                    selection = str(entry.get("selection") or "")
                    if selection and selection != "all":
                        account = str(entry.get("account") or "")
                        where = str(entry.get("settings_url") or "") or "the installation page"
                        print(
                            f"warning: the {account + ' ' if account else ''}installation covers selected repos only;"
                            f" yard repo new needs all repositories. Change it on {where}",
                            file=sys.stderr,
                        )
            return
        time.sleep(SETUP_POLL)
    _die("No installation showed up in ten minutes.")


def _show_record(raw: str) -> None:
    if _json:
        print(raw)
        return
    print(format_record(raw), end="")


def _color_on(stream: object | None = None) -> bool:
    if _no_color or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _read_stdin() -> str:
    raw = sys.stdin.buffer.read((1 << 20) + 1)
    if len(raw) > 1 << 20:
        _die("the file is too large")
    return raw.decode()


def _edit_text(initial: str, suffix: str = ".env") -> str:
    handle = tempfile.NamedTemporaryFile(prefix="yard-", suffix=suffix, delete=False)
    path = Path(handle.name)
    try:
        path.write_text(initial)
        editor = os.environ.get("EDITOR") or "vi"
        proc = subprocess.run([*editor.split(), str(path)])
        if proc.returncode:
            _die(f"the editor exited with {proc.returncode}; nothing was saved")
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
    events.add_argument("--since", default="", help="-15m, -1h, -2d, or 2025-01-31 14:00")
    events.add_argument("--until", default="", help="same forms as --since")
    events.add_argument("--tail", type=int, default=200, help="how many rows")
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
    setup.set_defaults(func=_cmd_forge_setup)


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
    global _no_color, _json
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    _no_color = args.no_color
    _json = args.json
    if args.version:
        print(package_version())
        return
    if not hasattr(args, "func"):
        _parser().print_help()
        return
    args.func(args)
