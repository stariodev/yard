import json
import re
from datetime import UTC, datetime, timedelta

from app.cli.render import (
    _state_text,
    format_app_status,
    format_apps,
    format_doctor,
    format_status,
    format_tokens,
    status_hint,
)
from app.shared import ago, format_bytes
from tests.conftest import NOW, stamp, status_report


def test_format_status():
    text = format_status(status_report(), NOW)
    lines = text.split("\n")
    assert lines[0].startswith("name")
    header = lines[0].split()
    assert header == ["name", "state", "cpu", "mem", "data", "rev", "host", "deploy"]
    assert "heldso-app" in lines[1]
    assert "up 2h" in lines[1]
    assert "abc1234 → f00ba12" in lines[1]
    assert "heldso.example.com" in lines[1]
    assert "ok 2m0s" in lines[1]
    assert "shop-app" in lines[2]
    assert "exit 1 3h" in lines[2]
    assert "down" in lines[3]
    assert "caddy" in lines[4]
    assert "up 24h" in lines[4]
    assert "host:" in lines[-2]
    assert "2% of 4 cores" in lines[-2]
    assert "30MB of 4GB" in lines[-2]
    assert "10MB of 200GB" in lines[-2]


def test_format_status_drops_empty_columns():
    report = status_report()
    for row in report["rows"]:
        row["data"] = 0
        row["sha"] = ""
        row["tip"] = ""
        row["hosts"] = []
        row["deploy"] = None
    text = format_status(report, NOW)
    assert text.split("\n")[0].split() == ["name", "state", "cpu", "mem"]


def test_format_status_deploy_failed_hint():
    report = status_report()
    assert status_hint(report) == "yard app logs --deploy --app shop"
    for row in report["rows"]:
        row["deploy"] = None
    assert status_hint(report) == "yard logs shop-app"


def test_state_text_marks_health_and_restarts():
    row = {"state": "running", "health": "", "started": stamp(hours=2), "restarts": 3}
    assert _state_text(row, NOW) == "up 2h · 3 restarts"
    row["restarts"] = 1
    assert _state_text(row, NOW) == "up 2h · 1 restart"
    row["health"] = "starting"
    row["restarts"] = 0
    assert _state_text(row, NOW) == "starting 2h"
    row["health"] = "unhealthy"
    assert _state_text(row, NOW) == "unhealthy 2h"
    row["health"] = "healthy"
    assert _state_text(row, NOW) == "up 2h"


def test_status_hint_points_an_unhealthy_container_at_logs():
    report = status_report()
    for row in report["rows"]:
        row["deploy"] = None
        row["state"] = "running"
        row["health"] = "unhealthy"
    assert status_hint(report) == "yard logs heldso-app"


def test_format_app_status():
    report = status_report()
    row = report["rows"][0]
    page = {
        "app": {
            "name": "heldso",
            "repo": "heldso",
            "ref": "main",
            "dir": "",
            "domain": "heldso.example.com",
            "trigger": "",
            "created": stamp(days=2),
            "env": {"A": "1", "B": "2", "C": "3"},
        },
        "image": "localhost/heldso:abc1234",
        "sha": "abc1234",
        "env_file": "/srv/yard/env/heldso.env",
        "rows": [row],
        "hosts": ["heldso.example.com", "www.heldso.example.com"],
        "deploy": {
            "app": "heldso",
            "sha": "abc1234",
            "status": "ok",
            "started": stamp(days=4, minutes=2),
            "ended": stamp(days=4),
        },
        "log": "one\ntwo\n",
    }
    assert format_app_status(page, NOW) == (
        "heldso\n"
        "rev abc1234 · tip f00ba12 · image localhost/heldso:abc1234\n"
        "data 10MB · env /srv/yard/env/heldso.env\n"
        "runs  auto (not deployed yet)\n"
        "caddy heldso.example.com, www.heldso.example.com\n"
        "\n"
        "containers\n"
        "  heldso-app  aa11bb22cc33  up 2h  1.5%  20MB\n"
        "\n"
        "deploy\n"
        "  ok  2m0s  4d  abc1234\n"
        "\n"
        "log\n"
        "  one\n"
        "  two\n"
    )


def test_format_app_status_minimal():
    page = {
        "app": {"name": "heldso", "env": {}},
        "env_file": "/srv/yard/env/heldso.env",
        "rows": [{"app": "heldso", "state": "absent", "health": "down"}],
        "hosts": [],
        "deploy": None,
        "log": "",
    }
    assert format_app_status(page, NOW) == (
        "heldso\n"
        "data 0B · env /srv/yard/env/heldso.env\n"
        "runs  auto (not deployed yet)\n"
        "\n"
        "containers\n"
        "  heldso  down\n"
        "\n"
        "deploy\n"
        "  no deploy yet\n"
        "\n"
        "log\n"
        "  no log\n"
    )


def test_format_bytes():
    assert format_bytes(0) == "0B"
    assert format_bytes(10) == "10B"
    assert format_bytes(1500) == "1.5kB"
    assert format_bytes(4_000_000_000) == "4GB"


def test_ago():
    now = datetime.now(UTC)
    assert ago("", now) == ""
    assert ago((now - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), now) == "now"
    assert ago((now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), now) == "3h"
    assert ago("1999-01-01T00:00:00Z", now) == ""


def test_format_status_host_cell_lists_the_hosts():
    report = status_report()
    report["rows"][0]["hosts"] = ["heldso.example.com", "www.heldso.example.com"]
    assert "heldso.example.com, www.heldso.example.com" in format_status(report, NOW)


def test_format_resource_tables():
    apps = format_apps(
        [
            {"name": "heldso", "repo": "org/app", "ref": "main", "trigger": "push"},
            {"name": "docs", "repo": "org/app", "ref": "dev", "trigger": "manual"},
        ]
    )
    assert apps.split("\n")[0].split() == ["app", "source", "ref", "trigger"]
    assert "org/app" in apps
    assert "manual" in apps

    tokens = format_tokens([{"name": "ada", "created": stamp(days=1)}])
    assert tokens.split("\n")[0].split()[0] == "token"


def test_format_doctor_marks_warnings():
    checks = [
        {"name": "podman", "ok": True, "detail": "podman 5.3.1", "warn": False},
        {"name": "healthcheck heldso", "ok": True, "detail": "none", "warn": True},
        {"name": "caddy", "ok": False, "detail": "absent", "warn": False},
    ]
    text = format_doctor(checks)
    assert text.split("\n")[0].startswith("ok")
    assert text.split("\n")[1].startswith("warn")
    assert text.split("\n")[2].startswith("FAIL")

    colored = format_doctor(checks, color=True)
    assert "\033[33mwarn\033[0m" in colored
    assert "\033[31mFAIL\033[0m" in colored


def _plain(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def test_colored_doctor_words_keep_the_padding():
    checks = [
        {"name": "podman", "ok": True, "detail": "podman 5.7.0", "warn": False},
        {"name": "healthcheck heldso", "ok": True, "detail": "none", "warn": True},
        {"name": "dns x", "ok": False, "detail": "no host", "warn": False},
    ]
    colored = format_doctor(checks, color=True)
    # Once the escapes are stripped, every line aligns like the plain output.
    assert _plain(colored) == format_doctor(checks)
    # The escape wraps the word; the padding spaces stay outside it.
    assert "\033[33mwarn\033[0m healthcheck" in colored
    assert "\033[31mFAIL\033[0m dns" in colored


def test_colored_table_cells_keep_the_padding():
    report = status_report()
    # "unhealthy 3h" is the widest state cell, "down" gets padding after it.
    report["rows"][0]["health"] = "unhealthy"
    report["rows"][1]["state"] = "absent"
    colored = format_status(report, NOW, color=True)
    assert _plain(colored) == format_status(report, NOW)
    assert "\033[33munhealthy 2h\033[0m" in colored
    assert "\033[31mdown\033[0m " in colored


def test_colored_app_status_keeps_the_padding():
    report = status_report()
    row = report["rows"][0]
    row["health"] = "unhealthy"
    page = {
        "app": {"name": "heldso", "repo": "heldso", "ref": "main", "trigger": "", "env": {}},
        "env_file": "",
        "rows": [row],
        "hosts": [],
        "deploy": None,
        "log": "",
    }
    colored = format_app_status(page, NOW, color=True)
    assert _plain(colored) == format_app_status(page, NOW)
    assert "\033[33munhealthy 2h\033[0m" in colored


def test_format_record_repoless_hides_ref():
    import json

    from app.cli.render import format_record

    body = json.dumps({"name": "db", "repo": "", "ref": "main", "trigger": "push", "runs": "postgres:17"})
    out = format_record(body)
    assert "runs  postgres:17 (not deployed yet)" in out
    assert "ref" not in out


def test_format_apps_repoless_shows_source(tmp_path):
    from app.cli.render import format_apps

    rows = [
        {"name": "db", "repo": "", "ref": "main", "trigger": "push", "runs": "postgres:17"},
        {"name": "umami", "repo": "", "ref": "main", "trigger": "push", "runs": "https://x.example/umami.yml"},
        {"name": "heldso", "repo": "https://github.com/org/heldso", "ref": "main", "trigger": "push"},
    ]
    out = format_apps(rows)
    lines = out.splitlines()
    assert "db" in lines[1] and "postgres:17" in lines[1]
    assert "https://x.example/umami.yml" in lines[2]
    # repo-less rows have an empty ref column; the repo app keeps main
    assert "main" not in lines[1]
    assert "main" not in lines[2]
    assert "main" in lines[3]


def test_the_runs_line_renders_all_kinds():
    from app.cli.render import format_record

    """The five runs renderings: auto with a template, a path, an image, a URL, undeployed."""
    template = {
        "runs": "",
        "deployed": {
            "runs": "yard:python-uv",
            "kind": "containerfile",
            "reason": "stario: pyproject.toml declares stario",
            "revision": "abc1234",
            "template": "3f9a1c2e",
        },
    }
    out = format_record(json.dumps(template))
    assert "runs  auto → yard:python-uv @3f9a1c2e (containerfile; stario: pyproject.toml declares stario)" in out

    pinned = {**template, "runs": "yard:python-uv"}
    out = format_record(json.dumps(pinned))
    assert "runs  yard:python-uv @3f9a1c2e (containerfile; stario: pyproject.toml declares stario)" in out

    path = {"runs": "ops/stack.yml", "deployed": {"runs": "ops/stack.yml", "kind": "compose", "revision": "abc"}}
    assert "runs  ops/stack.yml (compose)" in format_record(json.dumps(path))

    image = {
        "runs": "postgres:17",
        "deployed": {"runs": "postgres:17", "kind": "image", "revision": "sha256:1a2b3c4d5e6f" + "0" * 52},
    }
    assert "runs  postgres:17 (image @sha256:1a2b3c4d5e6f)" in format_record(json.dumps(image))

    url = {
        "runs": "https://x.example/umami.yml",
        "deployed": {
            "runs": "https://x.example/umami.yml",
            "kind": "compose",
            "revision": "sha256:9f8e7d6c5b4a" + "0" * 52,
        },
    }
    assert "runs  https://x.example/umami.yml (compose @sha256:9f8e7d6c5b4a)" in format_record(json.dumps(url))

    fresh = {"runs": ""}
    assert "runs  auto (not deployed yet)" in format_record(json.dumps(fresh))
