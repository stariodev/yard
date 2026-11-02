"""`yard forge setup`: connect Cursor Origin or a GitHub App, then wait for the installation."""

import argparse
import getpass
import html
import http.server
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from app.cli import client
from app.shared import atomic_write


def cmd_forge_setup(args: argparse.Namespace) -> None:
    session = client.load(args)
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


def _ask_key(key_flag: str) -> str:
    """The PEM text. An unreadable path or a file without a BEGIN line asks again."""
    default = _normalize_path(key_flag) if key_flag else ""
    while True:
        if default:
            key_path = _normalize_path(client.ask(f"Private key path [{default}]: ")) or default
        else:
            key_path = _normalize_path(client.ask("Private key path: "))
        if not key_path:
            print("pass --key PATH or paste a path", file=sys.stderr)
            continue
        pem, problem = _pem_at(key_path)
        if pem:
            return pem
        print(problem, file=sys.stderr)


def _pem_at(path: str) -> tuple[str, str]:
    """The PEM text at a path, or "" and why not."""
    try:
        pem = Path(_normalize_path(path)).read_text()
    except OSError as exc:
        return "", f"cannot read the key: {exc}"
    if "-----BEGIN" not in pem:
        return "", f"{path} is not a PEM file (no -----BEGIN line)"
    return pem, ""


def _setup_origin(session: client.Client, args: argparse.Namespace) -> None:
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
        app_id = client.ask("App ID: ")
        if re.fullmatch(r"app_[A-Za-z0-9]+", app_id):
            break
        print("That is not an app ID. It starts with app_.", file=sys.stderr)
    wrap = client.object_of(client.api(session, "PUT", "/api/v1/forges/origin", {"app_id": app_id}))
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


def _setup_github(session: client.Client, args: argparse.Namespace) -> None:
    """Connect to GitHub: shared checks, then the manifest form or the values of an existing app."""
    parts = urlsplit(session.url)
    if parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "::1", "localhost"}:
        client.die("refusing to send the private key over http; log in with an https url")
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


def _install(session: client.Client, args: argparse.Namespace, forge: str, url: str) -> None:
    """The last step of every setup: open the install page, then wait for the installation."""
    print("Install the app on your account and select all repositories.")
    _open(args, url)
    _poll_installation(session, forge)
    print("Done.")


def _pending_manifest(path: Path, url: str) -> dict:
    """A saved manifest app for this host, or an empty dict."""
    try:
        raw = json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict) or raw.get("url") != url:
        return {}
    return raw if isinstance(raw.get("pem"), str) else {}


def _manifest_page(session: client.Client, args: argparse.Namespace, base: str, state: str) -> str:
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
        server = cast(_ManifestServer, self.server)
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._page(200, server.manifest_page)
            return
        if parsed.path == "/callback":
            params = dict(parse_qsl(parsed.query))
            code = str(params.get("code") or "")
            if params.get("state") != server.manifest_state or not code:
                self._page(400, "<p>This link does not belong to this setup run. Go back to the terminal.</p>")
                return
            server.manifest_code.put(code)
            self._page(
                200,
                "<p>GitHub created the app. Go back to the terminal; it saves the key and opens the install page.</p>",
            )
            return
        self._page(404, "<p>not found</p>")


def _manifest_exchange(session: client.Client, code: str, pending_path: Path) -> dict:
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
        client.die(f"GitHub answered {exc.code}: {body}\n{again}")
    except urllib.error.URLError as exc:
        client.die(f"GitHub did not answer: {exc.reason}\n{again}")
    if not isinstance(reply, dict) or not isinstance(reply.get("pem"), str):
        client.die(f"GitHub's answer had no key in it. {again}")
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


def _manifest_run(session: client.Client, args: argparse.Namespace, pending_path: Path) -> dict:
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
        if args.no_browser:
            print("Open it in a browser on this computer; GitHub sends you back to it.")
        try:
            code = got.get(timeout=SETUP_LIMIT)
        except queue.Empty:
            client.die("No answer from GitHub in ten minutes. Run the command again.")
        except EOFError, KeyboardInterrupt:
            client.die("stopped; run the command again to continue")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return _manifest_exchange(session, code, pending_path)


def _save_github_app(session: client.Client, app: dict, pending_path: Path) -> dict:
    """PUT the app credentials to Yard. The file keeps them until the PUT lands."""
    code, text = _put_github(
        session, str(app.get("app_id") or ""), str(app.get("pem") or ""), str(app.get("webhook_secret") or "")
    )
    if code >= 300:
        detail = client.error_text(text)
        client.die(f"{detail}\nThe key is kept in {pending_path}; run the command again to retry.")
    pending_path.unlink(missing_ok=True)
    wrap = client.object_of(text)
    saved = wrap.get("app") if isinstance(wrap, dict) else None
    return saved if isinstance(saved, dict) else {}


def _put_github(session: client.Client, app_id: str, pem: str, secret: str) -> tuple[int, str]:
    """Yard checks the values against GitHub before it saves them."""
    body = {"app_id": app_id, "private_key": pem, "webhook_secret": secret}
    return client.api_try(session, "PUT", "/api/v1/forges/github", body)


def _setup_github_manifest(session: client.Client, args: argparse.Namespace) -> None:
    """The manifest form: GitHub makes the key and the webhook secret itself."""
    pending_path = client.config_path().parent / "github-manifest.json"
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


def _setup_github_existing(session: client.Client, args: argparse.Namespace) -> None:
    """An app made before, or a manifest run that broke: the user gives its ID, key, and webhook secret."""
    print(f"The app needs the webhook url {session.url}/hooks/github, the push event,")
    print("and the permissions contents write and repository creation write.")
    app_id = str(args.app_id)
    secret = str(args.webhook_secret)
    pem = _pem_at(args.key)[0] if args.key else ""
    while True:
        while not app_id.isdigit():
            if app_id:
                print("That is not an app ID. It is digits.", file=sys.stderr)
            app_id = client.ask("App ID: ")
        pem = pem or _ask_key(str(args.key))
        while not secret:
            try:
                secret = getpass.getpass("Webhook secret: ").strip()
            except EOFError, KeyboardInterrupt:
                client.die("stopped; run the command again to continue")
        code, text = _put_github(session, app_id, pem, secret)
        if 400 <= code < 500:
            # Yard checks the key against GitHub before it saves. Ask for every value again.
            print(client.error_text(text), file=sys.stderr)
            app_id, pem, secret = "", "", ""
            continue
        if code >= 300:
            client.die(client.error_text(text))
        break
    wrap = client.object_of(text)
    app = wrap.get("app") if isinstance(wrap, dict) else None
    _install(session, args, "github", _github_install_url(str((app or {}).get("slug") or "")))


def _open(args: argparse.Namespace, url: str) -> None:
    """A link to open in the browser. --no-browser only prints it."""
    print(url)
    if not args.no_browser:
        webbrowser.open(url)


SETUP_POLL = 3.0


SETUP_LIMIT = 600.0


def _forge_row(session: client.Client, name: str) -> dict:
    """One forge summary by name, or an empty one when the row is missing."""
    rows = client.object_of(client.api(session, "GET", "/api/v1/forges", None))
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("name") == name:
                return row
    return {}


def _poll_installation(session: client.Client, name: str) -> None:
    """Ask the forge every three seconds until the installation lands. Ten minutes tops."""
    deadline = time.monotonic() + SETUP_LIMIT
    seen_error = ""
    while time.monotonic() < deadline:
        found = client.object_of(client.api(session, "POST", f"/api/v1/forges/{name}/installations", None))
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
    client.die("No installation showed up in ten minutes.")
