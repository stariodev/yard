from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from stario.testing import TestClient

from app.server.main import bootstrap


@asynccontextmanager
async def serve(
    tmp_path: Path, monkeypatch, *, journalctl: str = "journalctl-missing", journal_dir: str = "no-journal", timeout=5
):
    """The production bootstrap over TestClient. No podman, no boot steps, no deploys.

    A Linux runner has a real journal; the journal dir and socket always point into tmp_path.
    """
    monkeypatch.setenv("YARD_ROOT", str(tmp_path))
    monkeypatch.setenv("YARD_SKIP_DEPLOY", "1")
    monkeypatch.setenv("YARD_BOOT", "0")
    monkeypatch.setenv("YARD_PODMAN_BIN", "yard-podman-missing")
    monkeypatch.setenv("YARD_JOURNAL_DIR", str(tmp_path / journal_dir))
    monkeypatch.setenv("YARD_JOURNAL_SOCKET", str(tmp_path / "no-journal.sock"))
    monkeypatch.setenv("YARD_JOURNALCTL_BIN", journalctl)
    async with TestClient(bootstrap, request_timeout=timeout) as client:
        yield client, (tmp_path / "tokens" / "admin.token").read_text().strip()


@pytest.fixture
async def yard(tmp_path, monkeypatch):
    async with serve(tmp_path, monkeypatch) as (client, token):
        yield client, token, tmp_path


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def local(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "host": "127.0.0.1"}


NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


def stamp(**delta) -> str:
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_report() -> dict:
    return {
        "host": {
            "cpus": 4,
            "mem": 4_000_000_000,
            "disk": 200_000_000_000,
            "cpu_used": 2.0,
            "mem_used": 30_000_000,
            "data_used": 10_000_000,
            "forges": {"origin": True, "github": False},
        },
        "rows": [
            {
                "app": "heldso",
                "container": "heldso-app",
                "service": "app",
                "state": "running",
                "running": True,
                "health": "healthy",
                "exit_code": 0,
                "id": "aa11bb22cc33",
                "started": stamp(hours=2),
                "exited": "",
                "image": "localhost/heldso:abc1234",
                "sha": "abc1234",
                "tip": "f00ba12",
                "hosts": ["heldso.example.com"],
                "cpu": "1.5%",
                "mem": "20MB / 1GB",
                "data": 10_000_000,
                "deploy": {
                    "status": "ok",
                    "sha": "abc1234",
                    "started": stamp(minutes=120),
                    "ended": stamp(minutes=118),
                },
            },
            {
                "app": "shop",
                "container": "shop-app",
                "service": "app",
                "state": "exited",
                "running": False,
                "health": "",
                "exit_code": 1,
                "id": "dd44ee55ff66",
                "oom": False,
                "error": "",
                "started": "",
                "exited": stamp(hours=3),
                "image": "localhost/shop:def5678",
                "sha": "def5678",
                "tip": "",
                "hosts": [],
                "cpu": "",
                "mem": "",
                "data": 0,
                "deploy": {
                    "status": "failed",
                    "sha": "def5678",
                    "started": stamp(hours=3),
                    "ended": stamp(hours=3),
                },
            },
            {
                "app": "nightly",
                "container": "",
                "service": "",
                "state": "absent",
                "running": False,
                "health": "down",
                "exit_code": 0,
                "id": "",
                "oom": False,
                "error": "",
                "started": "",
                "exited": "",
                "image": "",
                "sha": "",
                "tip": "",
                "hosts": [],
                "trigger": "manual",
                "cpu": "",
                "mem": "",
                "data": 0,
                "deploy": None,
            },
            {
                "app": "",
                "container": "caddy",
                "service": "",
                "state": "running",
                "running": True,
                "health": "",
                "exit_code": 0,
                "id": "001122334455",
                "oom": False,
                "error": "",
                "started": stamp(days=1),
                "exited": "",
                "image": "docker.io/library/caddy:2",
                "sha": "",
                "tip": "",
                "hosts": [],
                "cpu": "0.5%",
                "mem": "10MB / 1GB",
                "data": 0,
                "deploy": None,
            },
        ],
    }
