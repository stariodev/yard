"""Service-level helpers: a Yard on a temp root, and a fake Caddy admin."""

import json
from pathlib import Path

from app.server.caddy import Caddy
from app.server.store import Config, Store
from app.server.yard import Yard
from app.shared import YardError


def make_yard(
    tmp_path: Path, *, skip: bool = True, boot: bool = False, podman_bin: str = "yard-podman-missing"
) -> Yard:
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin=podman_bin,
        skip_deploy=skip,
        boot=boot,
        remote=False,
        journal_dir=tmp_path / "no-journal",
        journalctl_bin="journalctl-missing",
        journal_socket=tmp_path / "no-journal.sock",
    )
    return Yard(config, Store.open(tmp_path))


class FakeCaddy(Caddy):
    """The admin socket, faked. A set `refuse` makes every load and adapt answer Caddy's 400.

    An adapt reads the site addresses: each line at column 0 that ends in `{`.
    """

    def __init__(self, refuse: str = "") -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, str, bytes | None]] = []

    async def request(
        self, method: str, path: str, body: bytes | None = None, content_type: str = "application/json"
    ) -> tuple[int, bytes]:
        self.calls.append((method, path, body))
        if self.refuse and path in ("/load", "/adapt"):
            raise YardError(400, "bad_caddyfile", self.refuse)
        if path == "/adapt":
            return 200, json.dumps({"result": site_config(site_addresses((body or b"").decode()))}).encode()
        return 200, b"{}"

    @property
    def loads(self) -> int:
        return sum(1 for _method, path, _body in self.calls if path == "/load")


def site_addresses(text: str) -> list[str]:
    hosts: list[str] = []
    for line in text.splitlines():
        if line[:1] not in ("", " ", "\t", "#", "{") and line.rstrip().endswith("{"):
            hosts += [part.strip() for part in line.rstrip()[:-1].split(",") if part.strip()]
    return hosts


def site_config(hosts: list[str]) -> dict:
    """The JSON shape Caddy's adapter writes for one site block."""
    routes = [{"match": [{"host": hosts}], "handle": [{"handler": "subroute"}], "terminal": True}] if hosts else []
    return {"apps": {"http": {"servers": {"srv0": {"listen": [":443"], "routes": routes}}}}}
