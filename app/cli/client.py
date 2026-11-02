"""The Yard API over HTTP, and the terminal: the login, the calls, the output flags, die and ask."""

import argparse
import http.client
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import unquote

from app.shared import YardError, explain


@dataclass(slots=True)
class Output:
    """The global flags. `main` sets them once from the arguments."""

    json: bool = False
    no_color: bool = False


out = Output()

TIMEOUT = 30


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


def load(args: argparse.Namespace) -> Client:
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
    url = http_url(url)
    if not url or not token:
        die(
            "not logged in.\n"
            "Run: yard login --url https://<your yard host> --token <token>\n"
            "The admin token is in /srv/yard/tokens/admin.token on the host."
        )
    return Client(url, token)


def http_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        return "https://" + url
    return url


def api(client: Client, method: str, path: str, body: object, timeout: int = TIMEOUT) -> str:
    status, text = api_try(client, method, path, body, timeout)
    if status >= 300:
        die(error_text(text))
    return text


def api_try(client: Client, method: str, path: str, body: object, timeout: int = TIMEOUT) -> tuple[int, str]:
    """The status and the body. An HTTP error is an answer; a connection failure dies."""
    with _urlopen(client, method, path, body, timeout) as response:
        return response.status or 0, response.read().decode().strip()


def open_stream(client: Client, path: str) -> http.client.HTTPResponse:
    """A GET with no timeout, for a follow. An error answer dies with its text."""
    response = _urlopen(client, "GET", path, None, None)
    if (response.status or 0) >= 300:
        with response:
            die(error_text(response.read().decode().strip()))
    return cast(http.client.HTTPResponse, response)


def stream(client: Client, path: str) -> None:
    """Copy a follow to stdout as it arrives."""
    with open_stream(client, path) as response:
        shutil.copyfileobj(response, sys.stdout.buffer)


def _urlopen(
    client: Client, method: str, path: str, body: object, timeout: int | None
) -> http.client.HTTPResponse | urllib.error.HTTPError:
    """The response, or the HTTPError that holds the error answer. Every call goes through here."""
    try:
        return urllib.request.urlopen(request(client, method, path, body), timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc
    except TimeoutError:
        die(_unreachable(client, f"no answer in {timeout} s" if timeout else "no answer", method, path))
    except urllib.error.URLError as exc:
        reason = f"no answer in {timeout} s" if isinstance(exc.reason, TimeoutError) else str(exc.reason)
        die(_unreachable(client, reason, method, path))


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


def request(client: Client, method: str, path: str, body: object) -> urllib.request.Request:
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    headers = {"authorization": f"Bearer {client.token}"}
    if out.json:
        # The logs route answers its entries page for JSON; every other route answers the same either way.
        headers["accept"] = "application/json"
    if data is not None:
        headers["content-type"] = "text/caddyfile" if isinstance(body, bytes) else "application/json"
    return urllib.request.Request(client.url + path, data=data, headers=headers, method=method)


def error_text(raw: str) -> str:
    """An error answer as the user reads it: the raw JSON with --json, else the explained text."""
    return raw if out.json else explain(raw)


def fail(exc: YardError) -> NoReturn:
    if out.json:
        die(json.dumps({"error": exc.payload()}))
    die(str(exc))


def die(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def object_of(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        die(raw)


def ask(prompt: str, stopped: str = "stopped; run the command again to continue") -> str:
    """One wizard answer. EOF or Ctrl-C exits without a traceback."""
    try:
        return input(prompt).strip()
    except EOFError, KeyboardInterrupt:
        die(stopped)


def color_on(stream: object | None = None) -> bool:
    if out.no_color or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def show(raw: str, render: Callable[[Any], str], shape: type = list) -> None:
    """Print an answer: the raw JSON with --json or when its shape is not `shape`, else `render` of it."""
    found = object_of(raw)
    if out.json or not isinstance(found, shape):
        print(raw)
        return
    print(render(found), end="")
