"""The Caddy admin API on its unix socket. Caddy reads, checks, and loads every Caddyfile itself."""

import asyncio
import http.client
import socket as socketlib
from pathlib import Path

from stario import json

from app.shared import YardError

_TIMEOUT = 10
_ADAPTER = "adapting config using caddyfile adapter: "


class Caddy:
    """The admin client. The socket path comes from the config."""

    def __init__(self, socket: Path) -> None:
        self.socket = socket

    async def request(
        self, method: str, path: str, body: bytes | None = None, content_type: str = "application/json"
    ) -> tuple[int, bytes]:
        """One admin call. Caddy's 400 is the caller's 400; any other failure is a 502."""
        try:
            code, raw = await asyncio.to_thread(_round_trip, self.socket, method, path, body, content_type)
        except (OSError, http.client.HTTPException) as exc:
            raise YardError(502, "caddy", f"caddy {method} {path}: {exc}") from exc
        if code == 400:
            raise YardError(
                400,
                "bad_caddyfile",
                _error_text(raw),
                help_text="Caddy keeps the config it had. Fix the file and save it again.",
            )
        if code >= 300:
            detail = raw.decode(errors="replace").strip()[:400]
            raise YardError(502, "caddy", f"caddy {method} {path}: {code} {detail}".rstrip())
        return code, raw

    async def load(self, caddyfile: Path) -> None:
        """Caddy reads the main Caddyfile from its own disk, with every site file it imports."""
        await self.request("POST", "/load", f"import {caddyfile}\n".encode(), "text/caddyfile")

    async def adapt(self, text: str) -> dict:
        """The JSON config for a Caddyfile text. Caddy loads nothing."""
        _code, raw = await self.request("POST", "/adapt", text.encode(), "text/caddyfile")
        body = json.loads(raw or b"{}")
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else {}


def hosts(config: dict) -> list[str]:
    """The host matchers of the top routes on every server, in file order."""
    found: list[str] = []
    servers = _dict(_dict(_dict(config.get("apps")).get("http")).get("servers"))
    for server in servers.values():
        for route in _list(_dict(server).get("routes")):
            for match in _list(_dict(route).get("match")):
                for host in _list(_dict(match).get("host")):
                    if isinstance(host, str) and host not in found:
                        found.append(host)
    return found


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _error_text(raw: bytes) -> str:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    text = body.get("error") if isinstance(body, dict) else None
    text = text if isinstance(text, str) and text else raw.decode(errors="replace").strip()
    return text.removeprefix(_ADAPTER)[:1000] or "caddy refused the config"


class _Connection(http.client.HTTPConnection):
    """HTTP over one AF_UNIX socket."""

    def __init__(self, socket: Path) -> None:
        super().__init__("localhost", timeout=_TIMEOUT)
        self.socket_path = str(socket)

    def connect(self) -> None:
        self.sock = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        self.sock.settimeout(_TIMEOUT)
        self.sock.connect(self.socket_path)


def _round_trip(socket: Path, method: str, path: str, body: bytes | None, content_type: str) -> tuple[int, bytes]:
    conn = _Connection(socket)
    try:
        conn.request(method, path, body=body, headers={"content-type": content_type} if body else {})
        with conn.getresponse() as response:
            return response.status, response.read()
    finally:
        conn.close()
