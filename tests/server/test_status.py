import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from app.server import status
from app.server.podman import Podman
from app.server.store import Config, Store
from app.server.yard import Yard
from tests.server.conftest import FakeCaddy


def _yard(tmp_path: Path, ps: list[dict], stats: str = "[]", *, sock: bool = False, logs: str = "") -> Yard:
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        skip_deploy=True,
        boot=sock,
        remote=False,
        journal_dir=tmp_path / "no-journal",
        journalctl_bin="journalctl-missing",
        journal_socket=tmp_path / "no-journal.sock",
    )
    store = Store.open(tmp_path)
    yard = Yard(config, store)

    class _Fake(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            if args and args[0] == "ps":
                return json.dumps(ps)
            if args and args[0] == "stats":
                return stats
            if args and args[0] == "info":
                return '{"host":{"cpus":4,"memTotal":1000},"store":{"graphRootAllocated":2000}}'
            if args and args[0] == "logs":
                return logs
            return "[]"

    yard.podman = _Fake("podman", False)
    return yard


def _app_container(app: str, name: str, **extra):
    # An app container carries compose's own labels. The project is the app.
    return {
        "Names": [name],
        "Image": f"localhost/{app}:abc",
        "State": "running",
        "Labels": {"com.docker.compose.project": app, "com.docker.compose.service": "app"},
        **extra,
    }


def _site(yard: Yard, name: str, *hosts: str) -> None:
    yard.caddy = FakeCaddy()
    path = yard.config.site(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{', '.join(hosts)} {{\n\treverse_proxy {name}-app-1:8000\n}}\n")


def test_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ps = [
        _app_container("heldso", "heldso-app-1", Status="Up 2 minutes (healthy)", StartedAt=1710000000),
        _app_container("heldso", "heldso-worker", State="exited", ExitCode=0),
        {
            "Names": ["deploy-nightly-job"],
            "State": "exited",
            "ExitCode": 0,
        },
        {
            "Names": ["deploy-heldso"],
            "State": "exited",
        },
        {"Names": ["caddy"], "State": "running"},
    ]
    stats = '[{"Name":"heldso-app-1","CPUPerc":"1.5%","MemUsage":"20MB / 1GB"}]'
    yard = _yard(tmp_path, ps, stats)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    yard.store.create_app("nightly", "https://origin.cursor.com/org/app", "main", "manual")
    yard.store.create_app("gone", "https://origin.cursor.com/org/app", "main")
    _site(yard, "heldso", "heldso.example.com")
    data = tmp_path / "apps" / "heldso" / "data"
    data.mkdir(parents=True)
    (data / "f").write_bytes(b"x" * 10)

    from app.server.yard import Tip

    yard.tips["heldso"] = Tip(revision="tip9")
    report = asdict(asyncio.run(status.report(yard)))

    assert report["host"] == {
        "cpus": 4,
        "mem": 1000,
        "disk": 2000,
        "cpu_used": 1.5,
        "mem_used": 20_000_000,
        "data_used": 20,  # each heldso row reports the same 10-byte dir
        "forges": {"origin": False, "github": False},
    }
    rows = {row["container"]: row for row in report["rows"]}
    assert rows["heldso-app-1"]["app"] == "heldso"
    assert rows["heldso-app-1"]["service"] == "app"
    assert rows["heldso-app-1"]["running"]
    assert rows["heldso-app-1"]["health"] == "healthy"
    assert rows["heldso-app-1"]["cpu"] == "1.5%"
    assert rows["heldso-app-1"]["sha"] == "abc"
    assert rows["heldso-app-1"]["tip"] == "tip9"
    assert rows["heldso-app-1"]["hosts"] == ["heldso.example.com"]
    assert rows["heldso-worker"]["hosts"] == []
    assert rows["heldso-app-1"]["data"] == 10
    assert rows["heldso-app-1"]["deploy"]["status"] == "ok"
    assert rows["heldso-app-1"]["started"] == "2024-03-09T16:00:00Z"
    assert rows["heldso-worker"]["state"] == "exited"

    # Apply containers are skipped.
    assert "deploy-nightly-job" not in rows
    assert "deploy-heldso" not in rows
    nightly = [row for row in report["rows"] if row["app"] == "nightly"]
    assert len(nightly) == 1
    assert nightly[0]["state"] == "absent"
    assert nightly[0]["trigger"] == "manual"

    # An app with no container gets one absent row.
    absent = [row for row in report["rows"] if row["app"] == "gone"]
    assert len(absent) == 1
    assert absent[0]["state"] == "absent"
    assert absent[0]["container"] == ""
    assert absent[0]["health"] == "down"

    # A container with no labels still shows.
    assert rows["caddy"]["app"] == ""
    assert rows["caddy"]["running"]


def test_report_running_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ps = [
        _app_container("heldso", "heldso-app-1"),
        _app_container("heldso", "heldso-dead", State="exited"),
        {"Names": ["caddy"], "State": "exited"},
    ]
    yard = _yard(tmp_path, ps)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    yard.store.create_app("gone", "https://origin.cursor.com/org/app", "main")
    yard.tips.clear()
    report = asdict(asyncio.run(status.report(yard, running_only=True)))
    names = [row["container"] for row in report["rows"]]
    assert names == ["heldso-app-1"]


def test_app_report_is_the_app_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log = "2026-10-05T12:00:00Z one\n2026-10-05T12:00:01Z two\n"
    yard = _yard(tmp_path, [_app_container("heldso", "heldso-app-1")], sock=True, logs=log)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    _site(yard, "heldso", "heldso.example.com")

    from app.server.yard import Tip

    yard.tips["heldso"] = Tip(revision="tip9")
    found = asdict(asyncio.run(status.app_report(yard, "heldso")))

    assert found["app"]["name"] == "heldso"
    assert "env" not in found["app"]
    assert found["env_file"].endswith("apps/heldso/env")
    assert found["hosts"] == ["heldso.example.com"]
    assert [row["container"] for row in found["rows"]] == ["heldso-app-1"]
    assert found["deploy"] is None
    assert "two" in found["log"]


def test_two_hosts_stay_on_the_app_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(
        tmp_path,
        [
            _app_container("heldso", "heldso-app-1"),
            _app_container("heldso", "heldso-worker"),
        ],
    )
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    _site(yard, "heldso", "heldso.example.com", "www.heldso.example.com")
    yard.tips.clear()
    report = asdict(asyncio.run(status.report(yard)))
    rows = {row["container"]: row for row in report["rows"]}
    assert rows["heldso-app-1"]["hosts"] == ["heldso.example.com", "www.heldso.example.com"]
    assert rows["heldso-worker"]["hosts"] == []


def test_url_row_shows_the_deployed_revision(tmp_path):
    """A URL app's container runs the compose's image; the rev is the deployed file."""
    from app.server.podman import Container
    from app.server.store import Deployed, Store, write_deployed
    from app.server.yard import Tip

    store = Store.open(tmp_path)
    app = store.create_app("umami", "", runs="https://example.com/umami.yml")
    revision = "sha256:" + "b" * 64
    config = __import__("app.server.store", fromlist=["Config"]).Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="",
        skip_deploy=True,
        boot=False,
        remote=False,
        journal_dir=tmp_path / "j",
        journalctl_bin="",
        journal_socket=tmp_path / "s.sock",
    )
    write_deployed(config, "umami", Deployed(revision=revision, runs=app.runs, kind="compose"))
    item = Container(name="umami-app-1", image="postgres:17", app="umami", running=True, state="running")
    row = status._row(
        config, item, app, None, Tip(revision="sha256:" + "c" * 64), read_dep(config, "umami"), None, [], {}
    )
    assert row.sha == revision
    assert row.tip == "sha256:" + "c" * 64


def read_dep(config, name):
    from app.server.store import read_deployed

    return read_deployed(config, name)


@pytest.mark.parametrize(
    ("state", "code", "status_", "error"),
    [
        ("running", 0, "running", ""),
        ("exited", 0, "ok", ""),
        ("exited", 3, "reverted", "unhealthy; reverted to localhost/heldso:old"),
        ("exited", 1, "failed", "apply exited 1"),
    ],
)
def test_deploy_record_reads_the_labels_and_the_exit_code(state, code, status_, error):
    from app.server.podman import parse_ps

    labels = {"yard.sha": "abc123", "yard.image": "localhost/heldso:abc123", "yard.previous": "localhost/heldso:old"}
    raw = json.dumps([{"Names": ["deploy-heldso"], "State": state, "ExitCode": code, "Labels": labels}])
    record = status.deploy_record(parse_ps(raw), "heldso")
    assert record is not None
    assert record["sha"] == "abc123" and record["image"] == "localhost/heldso:abc123"
    assert record["status"] == status_
    assert record["error"] == error
