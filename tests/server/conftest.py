"""Service-level helpers: a Yard on a temp root, and a fake Caddy admin."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.server.caddy import Caddy
from app.server.podman import Podman
from app.server.store import Config, Store
from app.server.yard import Yard
from app.shared import YardError
from tests.server import fakes


@pytest.fixture(autouse=True)
def _in_process_programs(monkeypatch):
    real = asyncio.create_subprocess_exec

    async def spawn(program, *args, **kwargs):
        fake = fakes.FAKES.get(str(program))
        if fake is None:
            return await real(program, *args, **kwargs)
        return fakes.FakeProcess(*fake([str(arg) for arg in args]))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    yield
    fakes.FAKES.clear()


def make_config(tmp_path: Path, **overrides) -> Config:
    """A Config on tmp_path. The journal dir and socket point at nowhere unless a test overrides them."""
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="yard-podman-missing",
        skip_deploy=True,
        boot=False,
        remote=False,
        journal_dir=tmp_path / "no-journal",
        journalctl_bin="journalctl-missing",
        journal_socket=tmp_path / "no-journal.sock",
    )
    return replace(config, **overrides)


def make_yard(tmp_path: Path, **overrides) -> Yard:
    return Yard(make_config(tmp_path, **overrides), Store.open(tmp_path))


class FakePodman(Podman):
    """Podman, faked. Each answer is keyed by a command prefix ("ps", "system df"); the longest prefix wins.

    An answer is the text podman prints, or an exception to raise. A command with no answer prints `default`.
    """

    def __init__(self, answers: dict[str, str | Exception] | None = None, default: str = "[]") -> None:
        super().__init__("podman", False)
        self.answers = answers or {}
        self.default = default
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *args: str, timeout: float | None = 20) -> str:
        self.calls.append(args)
        line = " ".join(args)
        keys = [key for key in self.answers if not key or line == key or line.startswith(key + " ")]
        answer = self.answers[max(keys, key=len)] if keys else self.default
        if isinstance(answer, Exception):
            raise answer
        return answer


def dead_podman() -> FakePodman:
    return FakePodman(default="", answers={"": YardError(502, "podman", "podman is down")})


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
