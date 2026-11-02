import asyncio
import collections
import os
import shutil
import socket
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from app.server import doctor
from app.server.caddy import Caddy
from app.server.forges import GitHub, Origin
from app.server.podman import Podman
from app.server.store import Config, Store
from app.server.yard import Yard
from app.shared import YardError, now
from tests.server.conftest import FakeCaddy

CADDY_PS = (
    '[{"Names":["yard-caddy-1"],"State":"running",'
    '"Labels":{"com.docker.compose.project":"yard","com.docker.compose.service":"caddy"}}]'
)


def _journal_sock() -> Path:
    """A real AF_UNIX socket file at a short path. It stays a socket after close."""
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    path = directory / "journal.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(str(path))
    return path


def _yard(
    tmp_path: Path,
    *,
    sock: bool = True,
    info: str = "",
    ps: str = "[]",
    inspect: str = "[]",
    df: str = "",
    prune_days: int = 7,
    journal_socket: Path | None = None,
) -> Yard:
    (tmp_path / "journal").mkdir(exist_ok=True)
    journalctl = tmp_path / "journalctl"
    journalctl.write_text("#!/bin/sh\nexit 0\n")
    journalctl.chmod(0o755)
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        skip_deploy=True,
        boot=sock,
        remote=False,
        prune_days=prune_days,
        journal_dir=tmp_path / "journal",
        journalctl_bin=str(journalctl),
        journal_socket=journal_socket or _journal_sock(),
    )
    yard = Yard(config, Store.open(tmp_path))

    class _Fake(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            if args and args[0] == "info":
                return info or (
                    '{"host":{"cpus":4,"memTotal":1000,"logDriver":"journald",'
                    '"eventLogger":"journald"},"version":{"Version":"5.3.1"}}'
                )
            if args and args[0] == "ps":
                return ps
            if args and args[0] == "inspect":
                return inspect
            if args[:2] == ("system", "df"):
                return df
            return ""

    yard.podman = _Fake("podman", False)
    yard.caddy = FakeCaddy()
    return yard


def _site(yard: Yard, name: str, host: str) -> None:
    path = yard.config.site(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{host} {{\n\treverse_proxy {name}-app-1:8000\n}}\n")


def _run(yard: Yard) -> dict[str, dict]:
    return {check["name"]: check for check in map(asdict, asyncio.run(doctor.run(yard)))}


def test_doctor_all_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS, inspect=JOURNALD_INSPECT)
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert found["podman"]["detail"] == "podman 5.3.1"
    assert found["network yard"]["ok"]
    assert found["apply image"]["ok"]
    assert found["caddy"]["detail"] == "running"
    assert found["caddy admin"]["detail"] == "ok"
    assert found["caddy config"]["detail"] == "0 site files · 0 hosts"
    assert found["podman journald"]["detail"] == "logs journald · events journald"
    assert found["journal"]["ok"]
    assert found["container logs"]["detail"] == "1 containers"
    assert found["yard events"]["ok"]
    assert found["disk"]["detail"] == f"{tmp_path} 100B free of 200B · data 0B · journal 0B"
    # A forge nothing uses earns no row.
    assert "origin" not in found
    assert "github" not in found


def test_doctor_marks_a_used_forge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.store.create_app("heldso", "https://github.com/org/app", "main")
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert found["github"]["ok"] is False
    assert found["github"]["detail"] == "not connected; run yard forge setup github"
    assert "origin" not in found


def test_doctor_forge_rows_when_connected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    monkeypatch.setattr(Origin, "connected", lambda self: True)

    async def fake_app(self):
        return {"displayName": "yard"}

    async def fake_find(self):
        return [{"id": "ins_1", "account": "org", "selection": "all"}]

    async def fake_token(self, repo: str = ""):
        return "tok"

    monkeypatch.setattr(Origin, "get_app", fake_app)
    monkeypatch.setattr(Origin, "find_installations", fake_find)
    monkeypatch.setattr(Origin, "token", fake_token)
    found = _run(yard)
    assert found["origin app"]["detail"] == "yard"
    assert found["origin installation org"]["detail"] == "all repos"
    assert found["origin token"]["ok"]
    assert found["origin webhook"]["detail"] == "none since boot"
    assert found["origin check"]["ok"]


def test_doctor_disk_says_where_the_space_went(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(
        tmp_path,
        ps=CADDY_PS,
        df='[{"Type":"Images","RawSize":2000,"RawReclaimable":500,"Size":"2kB"}]',
    )
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    data = yard.config.data("heldso")
    data.mkdir(parents=True)
    (data / "db.sqlite").write_bytes(b"x" * 100)
    (tmp_path / "journal" / "system.journal").write_bytes(b"y" * 50)
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert found["disk"]["ok"]
    assert found["disk"]["detail"] == (
        f"{tmp_path} 100B free of 200B · images 2kB (500B reclaimable) · data 100B · journal 50B"
    )


def test_doctor_disk_omits_missing_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No podman and no journal dir: the disk row keeps only the filesystem and data parts."""
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        skip_deploy=True,
        boot=False,
        remote=False,
        journal_dir=tmp_path / "no-journal",
        journalctl_bin="journalctl-missing",
        journal_socket=tmp_path / "no-journal.sock",
    )
    yard = Yard(config, Store.open(tmp_path))

    class _Dead(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            raise YardError(502, "podman", "podman is down")

    yard.podman = _Dead("podman", False)
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert found["disk"]["ok"]
    assert found["disk"]["detail"] == f"{tmp_path} 100B free of 200B · data 0B"


def test_doctor_skips_the_proxy_checks_without_a_host(tmp_path: Path):
    yard = _yard(tmp_path, sock=False)
    found = _run(yard)
    assert "caddy" not in found
    assert "caddy admin" not in found
    assert "caddy config" not in found
    assert not any(name.startswith("dns ") for name in found)


def test_doctor_flags_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")

    class _Dead(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            raise YardError(502, "podman", "podman is down")

    yard.podman = _Dead("podman", False)
    usage = collections.namedtuple("usage", "total used free")(100, 95, 5)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert not found["podman"]["ok"]
    assert not found["network yard"]["ok"]
    assert not found["caddy"]["ok"]
    assert not found["disk"]["ok"]
    assert "try yard prune" in found["disk"]["detail"]
    assert not found["origin"]["ok"]
    assert "github" not in found


def test_doctor_flags_a_dead_admin_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.caddy = FakeCaddy()
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)

    class _Down(Caddy):
        def __init__(self) -> None:
            pass

        async def request(self, method: str, path: str, body=None, content_type: str = "application/json"):
            raise YardError(502, "caddy", "caddy GET /config/: no socket")

    yard.caddy = _Down()
    found = _run(yard)
    assert found["caddy admin"]["ok"] is False
    assert found["caddy config"]["ok"] is False


def test_doctor_routes_and_certificates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    _site(yard, "heldso", "heldso.example.com")
    monkeypatch.setattr(doctor.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("10.0.0.1", 443))])

    def refused(addr, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.socket, "create_connection", refused)
    found = _run(yard)
    assert found["caddy config"]["ok"]
    assert found["dns heldso.example.com"]["detail"] == "10.0.0.1"
    assert found["certificate heldso.example.com"]["detail"] == "not issued yet; check dns, then yard logs yard-caddy-1"
    assert not found["certificate heldso.example.com"]["ok"]

    # The certificate file ages out of the not-issued state.
    cert = yard.config.caddy_data() / "caddy" / "certificates" / "acme" / "heldso.example.com"
    cert.mkdir(parents=True)
    issued = cert / "heldso.example.com.crt"
    issued.write_text("pem")
    found = _run(yard)
    assert found["certificate heldso.example.com"]["ok"]

    # The age uses the short form, not a span.
    old = time.time() - 15 * 86400
    os.utime(issued, (old, old))
    found = _run(yard)
    assert found["certificate heldso.example.com"]["detail"] == "issued 15d ago"

    # A removed site file stops the host checks.
    yard.config.site("heldso").unlink()
    found = _run(yard)
    assert found["caddy config"]["ok"]
    assert "dns heldso.example.com" not in found


def test_doctor_https(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.store.create_app("yard", "https://origin.cursor.com/org/yard", "main")
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    _site(yard, "yard", "yard.example.com")
    _site(yard, "heldso", "heldso.example.com")
    monkeypatch.setattr(doctor.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("10.0.0.1", 443))])
    sent: list[bytes] = []

    class _Sock:
        def sendall(self, data):
            sent.append(data)

        def recv(self, n):
            return b"HTTP/1.1 200 OK\r\n\r\n"

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _TLS:
        def wrap_socket(self, raw, server_hostname=""):
            return raw

    monkeypatch.setattr(doctor.socket, "create_connection", lambda addr, timeout=0: _Sock())
    monkeypatch.setattr(doctor.ssl, "create_default_context", lambda: _TLS())
    found = _run(yard)
    assert found["https yard.example.com"]["detail"] == "HTTP/1.1 200 OK"
    assert found["https heldso.example.com"]["ok"]
    assert sorted(line.split(b" ")[1] for line in sent) == [b"/", b"/health"]


def test_doctor_https_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    _site(yard, "heldso", "heldso.example.com")
    monkeypatch.setattr(doctor.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("10.0.0.1", 443))])

    def refused(addr, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.socket, "create_connection", refused)
    found = _run(yard)
    assert not found["https heldso.example.com"]["ok"]


JOURNALD_INSPECT = '[{"Name":"/yard-caddy-1","HostConfig":{"LogConfig":{"Type":"journald"}}}]'
K8S_INSPECT = '[{"Name":"/yard-caddy-1","HostConfig":{"LogConfig":{"Type":"k8s-file"}}}]'


def test_doctor_log_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS, inspect=JOURNALD_INSPECT)
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert found["podman journald"]["detail"] == "logs journald · events journald"
    assert found["journal"]["ok"]
    assert found["container logs"]["detail"] == "1 containers"


def test_doctor_flags_a_wrong_log_driver(tmp_path: Path):
    yard = _yard(
        tmp_path,
        info='{"host":{"logDriver":"k8s-file"}}',
        ps=CADDY_PS,
        inspect=K8S_INSPECT,
    )
    found = _run(yard)
    assert found["podman journald"]["ok"] is False
    assert found["podman journald"]["detail"] == "logs k8s-file · events unset; rerun install.sh"
    assert found["container logs"]["ok"] is False
    assert found["container logs"]["detail"] == "redeploy to switch: yard-caddy-1"


def test_doctor_flags_a_missing_event_logger(tmp_path: Path):
    yard = _yard(tmp_path, info='{"host":{"logDriver":"journald","eventLogger":"file"}}', ps=CADDY_PS)
    found = _run(yard)
    assert found["podman journald"]["ok"] is False
    assert found["podman journald"]["detail"] == "logs journald · events file; rerun install.sh"


def test_doctor_flags_a_missing_journal_socket(tmp_path: Path):
    yard = _yard(tmp_path, ps=CADDY_PS, journal_socket=tmp_path / "no-socket")
    found = _run(yard)
    assert found["yard events"]["ok"] is False
    assert "no socket" in found["yard events"]["detail"]
    plain = tmp_path / "not-a-socket"
    plain.write_text("x")
    yard = _yard(tmp_path, ps=CADDY_PS, journal_socket=plain)
    found = _run(yard)
    assert "not a socket" in found["yard events"]["detail"]


HEALTH_NONE = '[{"Name":"/heldso-app-1","Config":{"Healthcheck":{"Test":["NONE"]}},"State":{"Health":{}}}]'
HEALTH_SLOW = (
    '[{"Name":"/heldso-app-1","Config":{"Healthcheck":{"Test":["CMD-SHELL","curl -fs localhost:8000"],'
    '"Interval":300000000000}},"State":{"Health":{"Status":"starting"}}}]'
)
HEALTH_BAD = (
    '[{"Name":"/heldso-app-1","Config":{"Healthcheck":{"Test":["CMD-SHELL","curl -fs localhost:8000"],'
    '"Interval":10000000000}},"State":{"Health":{"Status":"unhealthy"}}}]'
)
HEALTH_OK = (
    '[{"Name":"/heldso-app-1","Config":{"Healthcheck":{"Test":["CMD-SHELL","curl -fs localhost:8000"],'
    '"Interval":10000000000}},"State":{"Health":{"Status":"healthy"}}}]'
)


def _healthcheck_yard(tmp_path: Path, inspect: str) -> Yard:
    yard = _yard(tmp_path, ps=CADDY_PS, inspect=inspect)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    return yard


def test_doctor_warns_on_a_missing_healthcheck(tmp_path: Path):
    found = _run(_healthcheck_yard(tmp_path, HEALTH_NONE))
    row = found["healthcheck heldso"]
    assert row["ok"] and row["warn"]
    assert row["detail"].startswith("none; a broken build goes live")
    assert "stario.dev/yard/compose#healthchecks" in row["detail"]


def test_doctor_warns_on_a_slow_healthcheck(tmp_path: Path):
    found = _run(_healthcheck_yard(tmp_path, HEALTH_SLOW))
    row = found["healthcheck heldso"]
    assert row["ok"] and row["warn"]
    assert row["detail"].startswith("every 5m; Podman runs the first check one interval after start")
    assert "use --interval=10s" in row["detail"]


def test_doctor_fails_an_unhealthy_container(tmp_path: Path):
    found = _run(_healthcheck_yard(tmp_path, HEALTH_BAD))
    row = found["healthcheck heldso"]
    assert row["ok"] is False
    assert row["detail"] == "unhealthy; read yard logs heldso-app-1"


def test_doctor_healthcheck_ok(tmp_path: Path):
    found = _run(_healthcheck_yard(tmp_path, HEALTH_OK))
    row = found["healthcheck heldso"]
    assert row == {"name": "healthcheck heldso", "ok": True, "detail": "every 10s · healthy", "warn": False}


def test_doctor_skips_the_yard_healthcheck(tmp_path: Path):
    """The yard compose has no healthcheck on purpose, so it earns no row."""
    yard = _yard(tmp_path, ps=CADDY_PS, inspect=HEALTH_OK)
    yard.store.create_app("yard", "https://origin.cursor.com/org/yard", "main")
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    found = _run(yard)
    assert "healthcheck yard" not in found
    assert found["healthcheck heldso"]["ok"]


def test_doctor_skips_a_healthcheck_for_an_app_without_a_container(tmp_path: Path):
    yard = _yard(tmp_path, ps=CADDY_PS, inspect="[]")
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    found = _run(yard)
    assert "healthcheck heldso" not in found


def test_doctor_prune_row(tmp_path: Path):
    yard = _yard(tmp_path)
    found = _run(yard)
    assert found["prune"]["detail"] == "daily, keeps 7 days; not run yet"

    yard.last_prune = now()
    yard.last_prune_freed = 1_200_000_000
    found = _run(yard)
    assert found["prune"]["detail"].startswith("daily, keeps 7 days; last ")
    assert found["prune"]["detail"].endswith("just now, freed 1.2GB")
    assert found["prune"]["warn"] is False

    yard.last_prune_error = "podman is down"
    found = _run(yard)
    assert found["prune"]["ok"] and found["prune"]["warn"]
    assert "podman is down" in found["prune"]["detail"]


def test_doctor_prune_row_when_off(tmp_path: Path):
    yard = _yard(tmp_path, prune_days=0)
    found = _run(yard)
    assert found["prune"]["detail"] == "off (YARD_PRUNE_DAYS=0); run yard prune by hand"
    assert found["prune"]["warn"] is False


def test_doctor_a_selected_installation_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A selected-repos install is legitimate; new repos just need a grant."""
    yard = _yard(tmp_path, ps=CADDY_PS)
    monkeypatch.setattr(GitHub, "connected", lambda self: True)

    async def fake_app(self):
        return {"name": "yard"}

    async def fake_find(self):
        return [{"id": "101", "account": "org", "selection": "selected", "type": "Organization"}]

    async def fake_token(self, repo: str = ""):
        return "tok"

    monkeypatch.setattr(GitHub, "get_app", fake_app)
    monkeypatch.setattr(GitHub, "find_installations", fake_find)
    monkeypatch.setattr(GitHub, "token", fake_token)
    found = _run(yard)
    row = found["github installation org"]
    assert row["ok"] and row["warn"]
    assert row["detail"] == (
        "selected repos only; yard repo new needs all repositories:"
        " https://github.com/organizations/org/settings/installations/101"
    )


def test_doctor_one_installation_row_per_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    monkeypatch.setattr(GitHub, "connected", lambda self: True)

    async def fake_app(self):
        return {"name": "yard"}

    async def fake_find(self):
        return [
            {"id": "101", "account": "Bobowski", "selection": "all"},
            {"id": "202", "account": "stariodev", "selection": "selected"},
        ]

    async def fake_token(self, repo: str = ""):
        return "tok"

    monkeypatch.setattr(GitHub, "get_app", fake_app)
    monkeypatch.setattr(GitHub, "find_installations", fake_find)
    monkeypatch.setattr(GitHub, "token", fake_token)
    found = _run(yard)
    assert found["github installation Bobowski"]["detail"] == "all repos"
    selected = found["github installation stariodev"]
    assert selected["ok"] and selected["warn"]


def test_doctor_flags_a_missing_journal(tmp_path: Path):
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        skip_deploy=True,
        boot=True,
        remote=False,
        journal_dir=tmp_path / "no-journal",
        journalctl_bin="journalctl-missing",
    )
    yard = Yard(config, Store.open(tmp_path))
    found = _run(yard)
    assert found["journal"]["ok"] is False
    assert "no journal" in found["journal"]["detail"]
