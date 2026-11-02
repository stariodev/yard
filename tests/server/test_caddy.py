import asyncio
import contextlib
import json
import os
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import cast

import pytest

from app.server import sites
from app.server.caddy import Caddy, hosts
from app.server.caddy import hosts as caddy_hosts
from app.shared import YardError
from tests.server.conftest import FakeCaddy, make_yard

OPS = Path(__file__).resolve().parents[2] / "ops"


class _Admin(socketserver.UnixStreamServer):
    """A fake Caddy admin API on a unix socket. `answer` is the code and body of every reply."""

    answer: tuple[int, bytes] = (200, b"{}")
    calls: list = []


class _Handler(BaseHTTPRequestHandler):
    @property
    def _admin(self) -> _Admin:
        return cast("_Admin", self.server)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self._admin.calls.append((self.path, self.headers.get("content-type"), self.rfile.read(length)))
        code, body = self._admin.answer
        self.send_response(code)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


def _serve(answer: tuple[int, bytes]) -> tuple[Caddy, _Admin]:
    # AF_UNIX paths cap at ~104 characters. pytest's tmp_path is deeper than that.
    sock = Path(tempfile.mkdtemp(prefix="yd-")) / "s.sock"
    server = _Admin(str(sock), _Handler)
    server.answer = answer
    server.calls = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return Caddy(sock), server


async def test_load_imports_the_main_caddyfile():
    client, server = _serve((200, b""))
    await client.load(Path("/etc/caddy/Caddyfile"))
    assert server.calls == [("/load", "text/caddyfile", b"import /etc/caddy/Caddyfile\n")]
    server.shutdown()


async def test_adapt_returns_the_result():
    client, server = _serve((200, json.dumps({"result": {"apps": {}}}).encode()))
    assert await client.adapt("a.example.com {\n}\n") == {"apps": {}}
    assert server.calls[0][2] == b"a.example.com {\n}\n"
    server.shutdown()


async def test_caddy_400_is_the_callers_400():
    error = {"error": "adapting config using caddyfile adapter: /config/sites/a.caddy:2: unrecognized directive: x"}
    client, server = _serve((400, json.dumps(error).encode()))
    with pytest.raises(YardError) as caught:
        await client.load(Path("/etc/caddy/Caddyfile"))
    assert caught.value.status == 400
    assert caught.value.code == "bad_caddyfile"
    assert caught.value.message == "/config/sites/a.caddy:2: unrecognized directive: x"
    server.shutdown()


async def test_other_failures_are_a_502(tmp_path: Path):
    with pytest.raises(YardError) as caught:
        await Caddy(tmp_path / "missing.sock").request("GET", "/config/")
    assert (caught.value.status, caught.value.code) == (502, "caddy")

    client, server = _serve((500, b"boom"))
    with pytest.raises(YardError) as caught:
        await client.load(Path("/etc/caddy/Caddyfile"))
    assert (caught.value.status, caught.value.code) == (502, "caddy")
    server.shutdown()


def test_hosts_reads_the_top_routes_of_every_server():
    config = {
        "apps": {
            "http": {
                "servers": {
                    "srv0": {
                        "routes": [
                            {"match": [{"host": ["a.example.com", "b.example.com"]}], "handle": []},
                            {"handle": []},
                        ]
                    },
                    "srv1": {"routes": [{"match": [{"host": ["c.example.com", "a.example.com"]}]}]},
                }
            }
        }
    }
    assert hosts(config) == ["a.example.com", "b.example.com", "c.example.com"]
    assert hosts({}) == []


async def test_set_site_writes_the_file_and_loads_caddy(tmp_path: Path):
    yard = make_yard(tmp_path)
    fake = FakeCaddy()
    yard.caddy = fake
    found = await sites.set_site(yard, "heldso", "heldso.example.com, www.heldso.example.com {\n}\n")
    assert found == ["heldso.example.com", "www.heldso.example.com"]
    assert yard.config.site("heldso").read_text() == "heldso.example.com, www.heldso.example.com {\n}\n"
    assert fake.loads == 1
    assert sites.get_site(yard, "heldso").startswith("heldso.example.com")


async def test_a_refused_site_puts_the_old_file_back(tmp_path: Path):
    yard = make_yard(tmp_path)
    yard.caddy = FakeCaddy()
    await sites.set_site(yard, "heldso", "heldso.example.com {\n}\n")
    yard.caddy = FakeCaddy(refuse="ambiguous site definition: heldso.example.com")
    with pytest.raises(YardError) as caught:
        await sites.set_site(yard, "heldso", "bad {\n")
    assert caught.value.status == 400
    assert yard.config.site("heldso").read_text() == "heldso.example.com {\n}\n"

    with pytest.raises(YardError):
        await sites.set_site(yard, "other", "other.example.com {\n}\n")
    assert not yard.config.site("other").exists()


async def test_empty_text_removes_the_site(tmp_path: Path):
    yard = make_yard(tmp_path)
    yard.caddy = FakeCaddy()
    await sites.set_site(yard, "heldso", "heldso.example.com {\n}\n")
    assert await sites.set_site(yard, "heldso", "  \n") == []
    assert not yard.config.site("heldso").exists()
    assert sites.get_site(yard, "heldso") == ""


async def test_a_site_file_has_a_size_limit(tmp_path: Path):
    yard = make_yard(tmp_path)
    yard.caddy = FakeCaddy()
    with pytest.raises(YardError) as caught:
        await sites.set_site(yard, "heldso", "#" * (sites.MAX_BYTES + 1))
    assert caught.value.code == "too_large"


async def test_remove_site_reports_a_refused_load(tmp_path: Path):
    yard = make_yard(tmp_path)
    yard.caddy = FakeCaddy()
    assert await sites.remove_site(yard, "heldso") == ""
    await sites.set_site(yard, "heldso", "heldso.example.com {\n}\n")
    yard.caddy = FakeCaddy(refuse="no socket")
    assert await sites.remove_site(yard, "heldso") == "no socket"
    assert not yard.config.site("heldso").exists()


def test_the_committed_caddyfile():
    text = (OPS / "Caddyfile").read_text()
    assert "admin unix//run/caddy/admin.sock" in text
    assert "auto_https disable_redirects" in text
    assert "disable_http_challenge" in text
    assert "protocols h1 h2 h3" in text
    assert text.rstrip().endswith("import /config/sites/*.caddy")


@pytest.mark.skipif(shutil.which("caddy") is None, reason="needs the caddy binary")
def test_the_committed_caddyfile_adapts():
    proc = subprocess.run(
        ["caddy", "adapt", "--config", str(OPS / "Caddyfile")], capture_output=True, text=True, check=True
    )
    config = json.loads(proc.stdout)
    assert config["admin"] == {"listen": "unix//run/caddy/admin.sock"}
    issuer = config["apps"]["tls"]["automation"]["policies"][0]["issuers"][0]
    assert issuer["challenges"]["http"]["disabled"] is True
    # A glob that matches no site file is a warning, not an error.
    assert "No files matching import glob pattern" in proc.stderr


def test_compose_spec_matches_the_document():
    spec = (OPS / "compose.yml").read_text()
    for part in (
        "  app:",
        "  caddy:",
        "${YARD_ROOT}/apps/yard/caddy/config:/config",
        "${YARD_ROOT}/apps/yard/caddy/data:/data",
        "${YARD_ROOT}/apps/yard/caddy/run:/run/caddy",
        "${CHECKOUT}/ops/Caddyfile:/etc/caddy/Caddyfile:ro",
        'command: ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile", "--resume"]',
        "8443:443/tcp",
        "8443:443/udp",
        "CADDY_ADMIN: unix//run/caddy/admin.sock",
        "127.0.0.1:8000:8000",
        "CONTAINER_HOST: unix:///run/podman/podman.sock",
    ):
        assert part in spec
    assert "8443:8443" not in spec
    assert "COMPOSE_ENV_FILE" not in spec
    # Compose names the containers yard-app-1 and yard-caddy-1 like every app.
    assert "container_name" not in spec
    # Compose's own labels name the project and the service. No yard.* label is needed.
    assert "labels:" not in spec


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def _caddy(autosave: dict | None = None) -> Iterator[tuple[Path, Caddy, int]]:
    """A real Caddy on a short temp root. The main Caddyfile imports the Yard sites directory.

    With `autosave`, Caddy starts with --resume from that config, as a 0.10 host does after the upgrade.
    """
    root = Path(tempfile.mkdtemp(prefix="yc-"))
    sites_dir = root / "apps" / "yard" / "caddy" / "config" / "sites"
    sites_dir.mkdir(parents=True)
    sock, port = root / "a.sock", _free_port()
    main = root / "Caddyfile"
    main.write_text(
        "{\n"
        f"\tadmin unix/{sock}\n"
        f"\thttp_port {port}\n"
        f"\thttps_port {_free_port()}\n"
        "\tauto_https off\n"
        "}\n\n"
        f"import {sites_dir}/*.caddy\n"
    )
    command = ["caddy", "run", "--config", str(main), "--adapter", "caddyfile"]
    if autosave is not None:
        (root / "xdg" / "caddy").mkdir(parents=True)
        (root / "xdg" / "caddy" / "autosave.json").write_text(json.dumps(autosave))
        command.append("--resume")
    # Caddy keeps its autosave and its data under these, never in the user's own Caddy directories.
    env = {**os.environ, "XDG_CONFIG_HOME": str(root / "xdg"), "XDG_DATA_HOME": str(root / "data")}
    # ops/compose.yml sets the same: a resumed autosave without an admin block uses it.
    env["CADDY_ADMIN"] = f"unix/{sock}"
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        for _ in range(100):
            if sock.exists():
                break
            proc.poll()
            assert proc.returncode is None, "caddy exited at start"
            threading.Event().wait(0.05)
        assert sock.exists(), "caddy opened no admin socket"
        yield root, Caddy(sock), port
    finally:
        proc.terminate()
        proc.wait(5)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def real_caddy() -> Iterator[tuple[Path, Caddy, int]]:
    with _caddy() as found:
        yield found


def _get(port: int, host: str) -> tuple[int, str]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Host": host})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except urllib.error.URLError:
        # No site left: Caddy closes the listener.
        return 0, ""


@pytest.mark.skipif(shutil.which("caddy") is None, reason="needs the caddy binary")
async def test_real_caddy_loads_checks_and_rolls_back(real_caddy: tuple[Path, Caddy, int]):
    root, client, port = real_caddy
    yard = make_yard(root)
    yard.config = replace(yard.config, caddyfile=root / "Caddyfile")
    yard.caddy = client

    site = f'http://a.localhost:{port} {{\n\trespond "A"\n}}\n'
    assert await sites.set_site(yard, "heldso", site) == ["a.localhost"]
    assert await asyncio.to_thread(_get, port, "a.localhost") == (200, "A")

    # Another app claims the same host: Caddy refuses, and the old config keeps serving.
    with pytest.raises(YardError) as caught:
        await sites.set_site(yard, "other", f'http://a.localhost:{port} {{\n\trespond "B"\n}}\n')
    assert caught.value.code == "bad_caddyfile"
    assert "ambiguous site definition" in caught.value.message
    assert not yard.config.site("other").exists()
    assert await asyncio.to_thread(_get, port, "a.localhost") == (200, "A")

    # A bad directive names the file and the line.
    with pytest.raises(YardError) as caught:
        await sites.set_site(yard, "heldso", f"http://a.localhost:{port} {{\n\tnot_a_directive\n}}\n")
    assert "heldso.caddy:2" in caught.value.message
    assert yard.config.site("heldso").read_text() == site

    assert (await sites.check(yard))[0]
    assert await sites.remove_site(yard, "heldso") == ""
    assert (await asyncio.to_thread(_get, port, "a.localhost"))[1] != "A"


def _bind_route(host: str, dial: str, window: object = "10s") -> dict:
    """One 0.10 bind route, as Yard wrote it through the admin API."""
    return {
        "@id": f"yard-{host}",
        "match": [{"host": [host]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "load_balancing": {"try_duration": window, "try_interval": "250ms"},
                "upstreams": [{"dial": dial}],
            }
        ],
    }


class _Resumed(FakeCaddy):
    """A Caddy that resumed the 0.10 config. `down` answers that many calls with a 502 first."""

    def __init__(self, routes: list, down: int = 0, refuse: str = "") -> None:
        super().__init__()
        self.config = {"apps": {"http": {"servers": {"yard": {"listen": [":443"], "routes": routes}}}}}
        self.down = down
        self.refuse_load = refuse

    async def request(self, method, path, body=None, content_type="application/json"):
        if self.down:
            self.down -= 1
            raise YardError(502, "caddy", "caddy GET /config/: no socket")
        if path == "/config/":
            return 200, json.dumps(self.config).encode()
        if path == "/load" and self.refuse_load:
            raise YardError(400, "bad_caddyfile", self.refuse_load)
        return await super().request(method, path, body, content_type)


def _resumed_yard(tmp_path: Path, caddy: FakeCaddy):
    yard = make_yard(tmp_path)
    for name in ("yard", "honorata", "bobowski-site"):
        yard.store.create_app(name, "")
    yard.config.caddy_sites().mkdir(parents=True)
    yard.caddy = caddy
    return yard


ROUTES = [
    _bind_route("yard.bobowski.dev", "yard-app-1:8000"),
    _bind_route("honorata.cc", "honorata-app-1:8000"),
    _bind_route("www.honorata.cc", "honorata-app-1:8000"),
    _bind_route("bobowski.dev", "bobowski-site-app-1:8000", 30_000_000_000),
    _bind_route("gone.example.com", "gone-app-1:8000"),
    {"match": [{"host": ["by-hand.example.com"]}], "handle": []},
]


async def test_boot_imports_the_binds_once(tmp_path: Path):
    caddy = _Resumed(ROUTES, down=2)
    yard = _resumed_yard(tmp_path, caddy)
    await sites.boot(yard, pause=0)

    assert sorted(path.name for path in yard.config.caddy_sites().iterdir()) == [
        "bobowski-site.caddy",
        "honorata.caddy",
        "yard.caddy",
    ]
    assert sites.get_site(yard, "honorata") == (
        "honorata.cc, www.honorata.cc {\n"
        "\treverse_proxy honorata-app-1:8000 {\n"
        "\t\tlb_try_duration 10s\n"
        "\t\tlb_try_interval 250ms\n"
        "\t}\n"
        "}\n"
    )
    assert "lb_try_duration 30s" in sites.get_site(yard, "bobowski-site")
    assert caddy.loads == 1

    # The files are the state now: a second boot loads them and imports nothing.
    yard.config.site("honorata").write_text("honorata.cc {\n}\n")
    await sites.boot(yard, pause=0)
    assert sites.get_site(yard, "honorata") == "honorata.cc {\n}\n"
    assert caddy.loads == 2


async def test_boot_removes_the_import_when_caddy_refuses_it(tmp_path: Path):
    yard = _resumed_yard(tmp_path, _Resumed(ROUTES, refuse="ambiguous site definition: honorata.cc"))
    await sites.boot(yard, pause=0)
    assert list(yard.config.caddy_sites().iterdir()) == []


async def test_boot_gives_up_when_caddy_never_answers(tmp_path: Path):
    caddy = _Resumed(ROUTES, down=10)
    yard = _resumed_yard(tmp_path, caddy)
    await sites.boot(yard, tries=3, pause=0)
    assert list(yard.config.caddy_sites().iterdir()) == []
    assert caddy.down == 7


@pytest.mark.skipif(shutil.which("caddy") is None, reason="needs the caddy binary")
async def test_real_caddy_moves_from_the_binds_with_no_gap():
    port = _free_port()
    old = {
        "apps": {
            "http": {
                "servers": {
                    "yard": {
                        "listen": [f":{port}"],
                        "automatic_https": {"disable": True},
                        "routes": [
                            {
                                "@id": "yard-a.localhost",
                                "match": [{"host": ["a.localhost"]}],
                                "handle": [{"handler": "static_response", "body": "OLD"}],
                            },
                            _bind_route("b.localhost", "heldso-app-1:8000"),
                        ],
                    }
                }
            }
        }
    }
    with _caddy(autosave=old) as (root, client, _site_port):
        yard = make_yard(root)
        yard.store.create_app("heldso", "")
        yard.config = replace(yard.config, caddyfile=root / "Caddyfile")
        yard.caddy = client
        # Before Yard boots, the resumed Caddy still serves the 0.10 routes.
        assert await asyncio.to_thread(_get, port, "a.localhost") == (200, "OLD")

        await sites.boot(yard, pause=0)
        assert sites.get_site(yard, "heldso").startswith("b.localhost {\n\treverse_proxy heldso-app-1:8000 {")
        live = json.loads((await client.request("GET", "/config/"))[1])
        assert list(live["apps"]["http"]["servers"]) == ["srv0"]
        assert caddy_hosts(live) == ["b.localhost"]
        saved = json.loads((root / "xdg" / "caddy" / "autosave.json").read_text())
        assert caddy_hosts(saved) == ["b.localhost"]
