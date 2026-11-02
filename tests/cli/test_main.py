import argparse
import html
import http.client
import io
import json
import re
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import app.cli.main as cli
from app.cli.main import main
from tests.conftest import stamp, status_report


def _forge_rows(fake):
    """POST /forges/NAME/installations answers that forge's row from the fake's GET /forges."""

    def wrapped(client, method, path, body, timeout=None):
        found = re.fullmatch(r"/api/v1/forges/(\w+)/installations", path)
        if method != "POST" or not found:
            return fake(client, method, path, body, timeout)
        code, raw = fake(client, "GET", "/api/v1/forges", None, timeout)
        rows = json.loads(raw)
        return code, json.dumps(next((row for row in rows if row.get("name") == found.group(1)), {}))

    return wrapped


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YARD_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("YARD_TOKEN", "token")


def test_yard_prints_help(capsys: pytest.CaptureFixture):
    main([])
    out = capsys.readouterr().out
    assert "usage: yard" in out
    assert "status" in out


def test_resource_ls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    bodies = {
        "/api/v1/apps": (
            200,
            json.dumps(
                [{"name": "heldso", "repo": "https://origin.cursor.com/org/app", "ref": "main", "trigger": "push"}]
            ),
        ),
        "/api/v1/tokens": (200, json.dumps([{"name": "ada", "created": stamp(days=1)}])),
    }

    def fake_api_try(client, method, path, body, timeout=None):
        return bodies[path]

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)

    main(["app", "ls"])
    assert "heldso" in capsys.readouterr().out
    main(["token", "ls"])
    assert "ada" in capsys.readouterr().out


def test_main_end_to_end(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    bodies = {
        "/api/v1/status": (200, json.dumps(status_report())),
        "/api/v1/apps/heldso": (
            200,
            json.dumps(
                {
                    "app": {
                        "name": "heldso",
                        "repo": "https://origin.cursor.com/org/heldso",
                        "ref": "main",
                        "trigger": "push",
                        "image": "localhost/heldso:a1b2c3d",
                        "sha": "a1b2c3d",
                        "created": stamp(days=2),
                    },
                    "env_file": "",
                    "rows": [],
                    "binds": [],
                    "deploy": None,
                    "log": "",
                }
            ),
        ),
    }

    def fake_api_try(client, method, path, body, timeout=None):
        return bodies[path.split("?")[0]]

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)

    main(["status"])
    out = capsys.readouterr().out
    assert "heldso-app" in out
    assert "host:" in out

    main(["app", "status", "--app", "heldso"])
    out = capsys.readouterr().out
    assert "heldso" in out

    main(["--version"])
    out = capsys.readouterr().out
    assert out.strip()


def test_app_status_and_doctor(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    report = status_report()
    page = {
        "app": {
            "name": "heldso",
            "repo": "https://origin.cursor.com/org/heldso",
            "ref": "main",
            "trigger": "push",
            "image": "localhost/heldso:a1b2c3d",
            "sha": "a1b2c3d",
            "created": stamp(days=2),
        },
        "env_file": "/srv/yard/apps/heldso/env",
        "rows": [report["rows"][0]],
        "hosts": ["heldso.example.com"],
        "deploy": None,
        "log": "one\ntwo\n",
    }
    bodies = {
        "/api/v1/status": (200, json.dumps(report)),
        "/api/v1/apps/heldso": (200, json.dumps(page)),
        "/api/v1/doctor": (
            200,
            json.dumps(
                [
                    {"name": "podman", "ok": True, "detail": "podman 5.3.1"},
                    {"name": "caddy config", "ok": False, "detail": "ambiguous site definition: shop.example.com"},
                ]
            ),
        ),
    }

    def fake_api_try(client, method, path, body, timeout=None):
        return bodies[path.split("?")[0]]

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)

    main(["app", "status", "--app", "heldso"])
    out = capsys.readouterr().out
    assert "caddy heldso.example.com" in out
    assert "containers" in out
    assert "no deploy yet" in out
    assert "  two" in out

    with pytest.raises(SystemExit) as exited:
        main(["status", "--full"])
    assert exited.value.code == 1
    out = capsys.readouterr().out
    assert "ok   podman" in out
    assert "FAIL caddy config  ambiguous site definition: shop.example.com" in out

    # --json keeps the status report's shape; --full adds the doctor rows under "doctor".
    main(["status", "--json"])
    plain = json.loads(capsys.readouterr().out)
    assert "doctor" not in plain
    with pytest.raises(SystemExit):
        main(["status", "--full", "--json"])
    full = json.loads(capsys.readouterr().out)
    assert full["rows"] == plain["rows"]
    assert [row["name"] for row in full["doctor"]] == ["podman", "caddy config"]


def test_app_deploy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        if path.endswith("/deploy"):
            return 200, json.dumps(
                {"app": {"name": "nightly"}, "deploy": {"status": "ok", "image": "localhost/app:abc", "log": "ready\n"}}
            )
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    note = {"status": "ok", "image": "localhost/app:abc"}
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: (print("ready"), note)[1])

    main(["app", "deploy", "--app", "nightly"])
    assert seen[("POST", "/api/v1/apps/nightly/deploy")] == {}
    assert "ready" in capsys.readouterr().out


class _TTY:
    """sys.stdin for the confirmation tests."""

    def isatty(self) -> bool:
        return True


def _rm_api(page: dict, deleted: dict):
    def fake_api_try(client, method, path, body, timeout=None):
        base = path.split("?")[0]
        if method == "GET" and base == "/api/v1/apps/heldso":
            return 200, json.dumps(page)
        if method == "DELETE" and base == "/api/v1/apps/heldso":
            return 200, json.dumps(deleted)
        raise AssertionError((method, path))

    return fake_api_try


_RM_PAGE = {
    "app": {"name": "heldso"},
    "env_file": "/srv/yard/apps/heldso/env",
    "dir": "/srv/yard/apps/heldso",
    "rows": [{"container": "heldso-app-1", "data": 1_200_000_000}],
    "hosts": ["heldso.example.com"],
    "deploy": None,
    "log": "",
}


def test_app_rm_refuses_a_non_tty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "rm", "--app", "heldso"])
    err = capsys.readouterr().err
    assert "needs a confirmation" in err
    assert "--yes" in err


def test_app_rm_confirms_and_names_the_site(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    deleted = {"name": "heldso", "hosts": ["heldso.example.com"]}
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_rm_api(_RM_PAGE, deleted)))
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "heldso")
    _env(monkeypatch)
    main(["app", "rm", "--app", "heldso"])
    out = capsys.readouterr().out
    assert "This deletes the app heldso:" in out
    assert "heldso-app-1" in out
    assert "/srv/yard/apps/heldso/data (1.2GB)" in out
    assert "caddy site  heldso.example.com" in out
    assert "removed heldso" in out
    assert "removed the Caddy site: heldso.example.com" in out


def test_app_rm_a_wrong_answer_removes_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path))
        return 200, json.dumps(_RM_PAGE)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "other")
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "rm", "--app", "heldso"])
    assert "Not removed: you typed 'other', the app is 'heldso'" in capsys.readouterr().err
    assert [call for call in calls if call[0] == "DELETE"] == []


def test_app_rm_yes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path))
        return 200, json.dumps({"name": "heldso", "hosts": []})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    main(["app", "rm", "--app", "heldso", "--yes"])
    assert calls == [("DELETE", "/api/v1/apps/heldso")]
    assert capsys.readouterr().out == "removed heldso\n"


def test_app_rm_warns_when_caddy_refuses(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    deleted = {"name": "heldso", "hosts": ["heldso.example.com"], "caddy_error": "caddy POST /load: no socket"}

    def fake_api_try(client, method, path, body, timeout=None):
        return 200, json.dumps(deleted)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    main(["app", "rm", "--app", "heldso", "--yes"])
    out = capsys.readouterr().out
    assert "Caddy did not load the config without the site: caddy POST /load: no socket" in out
    assert "yard status --full" in out


def test_not_logged_in_points_at_login(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    monkeypatch.delenv("YARD_URL", raising=False)
    monkeypatch.delenv("YARD_TOKEN", raising=False)
    monkeypatch.setattr(cli, "config_path", lambda: Path("/nonexistent/yard/config.json"))
    with pytest.raises(SystemExit):
        main(["app", "ls"])
    err = capsys.readouterr().err
    assert "not logged in" in err
    assert "yard login --url https://<your yard host> --token <token>" in err
    assert "/srv/yard/tokens/admin.token" in err


def test_api_unreachable_names_the_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "ls"])
    err = capsys.readouterr().err
    assert "the Yard API at http://127.0.0.1:8000 did not answer: " in err
    assert "yard login" in err
    assert "sudo -u yard podman ps" in err


def test_a_deploy_timeout_points_at_the_deploy_log(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    def boom(request, timeout=None):
        raise TimeoutError

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "deploy", "--app", "heldso"])
    err = capsys.readouterr().err
    assert "did not answer" in err
    assert "The deploy may still be running" in err
    assert "yard app logs --deploy --app heldso" in err


def test_env_set_needs_key_value(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["env", "set", "notakey", "--app", "heldso"])
    err = capsys.readouterr().err
    assert "need KEY=VALUE, got notakey" in err
    assert "Example: yard env set DATABASE_PATH=/data/app.sqlite3" in err


def test_env_edit_an_editor_failure_saves_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    class _Proc:
        returncode = 3

    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda *a, **k: (200, "{}")))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(sys, "stdin", _TTY())
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["env", "edit", "--app", "heldso"])
    err = capsys.readouterr().err
    assert "the editor exited with 3; nothing was saved" in err


def test_app_deploy_failed_reads_the_log_hint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    wrap = {
        "app": {"name": "heldso", "trigger": "manual"},
        "deploy": {"status": "failed", "image": "localhost/heldso:abc", "log": "== boom\n", "error": "apply exited 1"},
    }
    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda *a, **k: (200, json.dumps(wrap))))
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: wrap["deploy"])
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "deploy", "--app", "heldso"])
    out = capsys.readouterr().out
    assert "failed" in out
    assert "Read the full log: yard app logs --deploy --app heldso" in out


def test_app_deploy_reverted_explains_the_old_image(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    wrap = {
        "app": {"name": "heldso", "trigger": "manual"},
        "deploy": {
            "status": "reverted",
            "image": "localhost/heldso:abc",
            "log": "",
            "previous": "localhost/heldso:old",
        },
    }
    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda *a, **k: (200, json.dumps(wrap))))
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: wrap["deploy"])
    _env(monkeypatch)
    main(["app", "deploy", "--app", "heldso"])
    out = capsys.readouterr().out
    assert "The new container never got healthy, so the previous image still runs." in out
    assert "yard app logs --deploy --app heldso, then yard app logs --app heldso" in out


def test_app_deploy_a_server_500_keeps_the_log_hint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    body = {
        "error": {
            "code": "deploy_failed",
            "message": "apply exited 1",
            "help": "Read the full log: yard app logs --deploy --app heldso",
        }
    }
    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda *a, **k: (500, json.dumps(body))))
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "deploy", "--app", "heldso"])
    assert "yard app logs --deploy --app heldso" in capsys.readouterr().err


def test_app_new(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: list[tuple] = []

    def fake_api_try(client, method, path, body, timeout=None):
        seen.append((method, path, body))
        if path == "/api/v1/apps":
            return 200, json.dumps(
                {"app": {"name": body["name"], "repo": body["repo"], "ref": body["ref"], "trigger": "push"}}
            )
        if path == "/api/v1/apps/web/caddy":
            return 200, json.dumps({"hosts": ["heldso.example.com", "www.heldso.example.com"]})
        if path == "/api/v1/apps/web/deploy":
            return 200, json.dumps(
                {
                    "app": {"name": "web"},
                    "deploy": {"status": "ok", "image": "localhost/web:abc", "log": "== started\n"},
                }
            )
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(
        cli,
        "_follow_deploy",
        lambda *a: {"status": "ok", "image": "localhost/web:abc", "log": "== started\n", "port": 8080},
    )
    _env(monkeypatch)

    main(
        [
            "app",
            "new",
            "--repo",
            "https://origin.cursor.com/org/heldso",
            "--ref",
            "main",
            "--app",
            "web",
            "--caddy",
            "heldso.example.com",
            "--caddy",
            "www.heldso.example.com",
        ]
    )
    assert seen[0] == (
        "POST",
        "/api/v1/apps",
        {"name": "web", "repo": "https://origin.cursor.com/org/heldso", "ref": "main"},
    )
    # A new app deploys right away. The site file follows, with the port the deploy found.
    assert seen[1] == ("POST", "/api/v1/apps/web/deploy", {})
    assert seen[2] == (
        "PUT",
        "/api/v1/apps/web/caddy",
        b"heldso.example.com, www.heldso.example.com {\n"
        b"\treverse_proxy web-app-1:8080 {\n"
        b"\t\tlb_try_duration 10s\n"
        b"\t\tlb_try_interval 250ms\n"
        b"\t}\n"
        b"}\n",
    )
    out = capsys.readouterr().out
    assert "Saved. Caddy serves heldso.example.com, www.heldso.example.com for web." in out
    assert "https://origin.cursor.com/org/heldso" in out
    assert "Deploying web now…" in out
    assert "ok  localhost/web:abc" in out
    assert "Run yard app deploy to deploy now" not in out


def test_app_new_no_deploy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: list[tuple] = []

    def fake_api_try(client, method, path, body, timeout=None):
        seen.append((method, path, body))
        return 200, json.dumps({"app": {"name": "heldso", "repo": body["repo"], "ref": body["ref"]}})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    main(["app", "new", "--repo", "https://origin.cursor.com/org/heldso", "--no-deploy"])
    assert seen == [
        ("POST", "/api/v1/apps", {"name": "heldso", "repo": "https://origin.cursor.com/org/heldso", "ref": "main"})
    ]
    out = capsys.readouterr().out
    assert "Not deployed yet. Deploy it: yard app deploy --app heldso" in out


def test_app_new_a_failed_deploy_keeps_the_app(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    def fake_api_try(client, method, path, body, timeout=None):
        if path == "/api/v1/apps":
            return 200, json.dumps({"app": {"name": "heldso"}})
        if path == "/api/v1/apps/heldso/deploy":
            return 500, json.dumps(
                {
                    "error": {
                        "code": "deploy_failed",
                        "message": "apply exited 1",
                        "help": "Read the full log: yard app logs --deploy --app heldso",
                    }
                }
            )
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    with pytest.raises(SystemExit) as caught:
        main(["app", "new", "--repo", "https://origin.cursor.com/org/heldso"])
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "heldso was created, but the first deploy failed." in err
    assert "apply exited 1" in err
    assert "yard app logs --deploy --app heldso" in err


def test_app_rm_ctrl_c_says_not_removed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda *a, **k: (200, json.dumps(_RM_PAGE))))
    monkeypatch.setattr(sys, "stdin", _TTY())

    def _interrupt(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _interrupt)
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "rm", "--app", "heldso"])
    err = capsys.readouterr().err
    assert err.strip() == "Not removed."
    assert "stopped" not in err


def test_app_new_trigger(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        if path == "/api/v1/apps":
            return 200, json.dumps(
                {
                    "app": {
                        "name": body["name"],
                        "repo": body["repo"],
                        "ref": body["ref"],
                        "trigger": body.get("trigger", "push"),
                    }
                }
            )
        return 200, json.dumps({"deploy": {"status": "ok", "image": "localhost/app:dev", "log": ""}})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: {"status": "ok"})

    main(["app", "new", "--repo", "https://origin.cursor.com/org/app", "--ref", "dev", "--trigger", "manual"])
    body = seen[("POST", "/api/v1/apps")]
    assert body["trigger"] == "manual"
    assert body["name"] == "app-dev"


def test_app_new_ref_follows_the_checkout_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = tmp_path / "work"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "feature/x")
    monkeypatch.chdir(repo)
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        if path == "/api/v1/apps":
            return 200, json.dumps(
                {"app": {"name": body["name"], "repo": body["repo"], "ref": body["ref"], "trigger": "push"}}
            )
        return 200, json.dumps({"deploy": {"status": "ok", "image": "localhost/app:main", "log": ""}})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: {"status": "ok"})
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: {"status": "ok"})

    # A repo that is not the checkout's gets main, not the checkout branch.
    main(["app", "new", "--repo", "https://origin.cursor.com/org/other"])
    assert seen[("POST", "/api/v1/apps")] == {
        "name": "other",
        "repo": "https://origin.cursor.com/org/other",
        "ref": "main",
    }

    # The checkout's own repo, spelled with other case, takes the branch.
    main(["app", "new", "--repo", "https://origin.cursor.com/ORG/HELDSO"])
    assert seen[("POST", "/api/v1/apps")] == {
        "name": "heldso-feature-x",
        "repo": "https://origin.cursor.com/ORG/HELDSO",
        "ref": "feature/x",
    }


def test_app_set_message(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    state = {
        "name": "heldso",
        "repo": "https://origin.cursor.com/org/app",
        "ref": "main",
        "trigger": "push",
        "created": "",
    }

    def fake_api_try(client, method, path, body, timeout=None):
        if method == "GET":
            return 200, json.dumps({"app": dict(state)})
        if method == "PATCH":
            state.update(body)
            return 200, json.dumps({"app": dict(state)})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)

    # A ref move on a push app points at the next check.
    main(["app", "set", "--app", "heldso", "--ref", "dev"])
    out = capsys.readouterr().out
    assert "The next check deploys it. Deploy now" in out
    assert "Deploy now: yard app deploy --app heldso" in out

    # A trigger change does not move the code.
    main(["app", "set", "--app", "heldso", "--trigger", "manual"])
    assert capsys.readouterr().out == "Saved.\n"

    # The same ref is not a move.
    main(["app", "set", "--app", "heldso", "--ref", "dev"])
    assert capsys.readouterr().out == "Saved.\n"

    # A ref move on a manual app names the deploy command.
    main(["app", "set", "--app", "heldso", "--ref", "main"])
    assert capsys.readouterr().out == (
        "Saved. heldso still runs the old repo and ref until the next deploy.\n"
        "Deploy now: yard app deploy --app heldso\n"
    )


def test_env_commands(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen: dict = {}
    env = {"A": "1"}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        if method == "GET":
            return 200, json.dumps(dict(env))
        if body is not None:
            if method == "PUT":
                env.clear()
                env.update(body)
            else:
                for key, value in body.items():
                    if value is None:
                        env.pop(key, None)
                    else:
                        env[key] = value
            return 200, json.dumps({"keys": sorted(env)})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)

    main(["env", "set", "B=2", "C=3", "--app", "heldso"])
    assert seen[("PATCH", "/api/v1/apps/heldso/env")] == {"B": "2", "C": "3"}
    assert capsys.readouterr().out == (
        "Saved. heldso keeps its old settings until the next deploy.\nApply now: yard app deploy --app heldso\n"
    )

    main(["env", "unset", "A", "--app", "heldso"])
    assert seen[("PATCH", "/api/v1/apps/heldso/env")] == {"A": None}

    # capsys is not a terminal: a pipe still gets the dotenv text, not JSON.
    main(["env", "cat", "--app", "heldso"])
    out = capsys.readouterr().out
    assert "B=2" in out
    assert not out.startswith("{")

    main(["env", "get", "B", "--app", "heldso"])
    assert capsys.readouterr().out == "2\n"
    with pytest.raises(SystemExit):
        main(["env", "get", "NOPE", "--app", "heldso"])
    assert "NOPE is not set for heldso" in capsys.readouterr().err


def _caddy_api(site: dict, seen: list, refuse: str = ""):
    """GET and PUT on the site file. A set `refuse` answers each PUT with Caddy's 400."""

    def fake_api_try(client, method, path, body, timeout=None):
        assert path == "/api/v1/apps/heldso/caddy"
        seen.append((method, body))
        if method == "GET":
            # _api_try strips the body, like the real client.
            return 200, site.get("text", "").strip()
        if refuse:
            return 400, json.dumps({"error": {"code": "bad_caddyfile", "message": refuse}})
        site["text"] = body.decode()
        return 200, json.dumps({"hosts": ["heldso.example.com"] if site["text"].strip() else []})

    return fake_api_try


def test_caddy_cat_and_edit_from_stdin(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    site: dict = {}
    seen: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_caddy_api(site, seen)))
    _env(monkeypatch)

    main(["caddy", "cat", "--app", "heldso"])
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"heldso.example.com {\n}\n")))
    main(["caddy", "edit", "--app", "heldso"])
    assert seen[-1] == ("PUT", b"heldso.example.com {\n}\n")
    assert capsys.readouterr().out == "Saved. Caddy serves heldso.example.com for heldso.\n"

    main(["caddy", "cat", "--app", "heldso"])
    assert capsys.readouterr().out == "heldso.example.com {\n}\n"


def test_caddy_edit_in_the_editor(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    site: dict = {}
    seen: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_caddy_api(site, seen)))
    monkeypatch.setattr(sys, "stdin", _TTY())
    _env(monkeypatch)

    # An unchanged example saves nothing.
    monkeypatch.setattr(cli, "_edit_text", lambda text, suffix="": text)
    main(["caddy", "edit", "--app", "heldso"])
    assert capsys.readouterr().out == "No change.\n"
    assert [method for method, _body in seen] == ["GET"]

    monkeypatch.setattr(cli, "_edit_text", lambda text, suffix="": "heldso.example.com {\n}\n")
    main(["caddy", "edit", "--app", "heldso"])
    assert site["text"] == "heldso.example.com {\n}\n"
    assert "Saved. Caddy serves heldso.example.com" in capsys.readouterr().out


def test_caddy_edit_a_refused_file_opens_the_editor_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    seen: list = []
    edits: list[str] = []
    monkeypatch.setattr(
        cli, "_api_try", _forge_rows(_caddy_api({}, seen, refuse="heldso.caddy:2: unrecognized directive: x"))
    )
    monkeypatch.setattr(sys, "stdin", _TTY())

    def fake_edit(text: str, suffix: str = "") -> str:
        edits.append(text)
        return "heldso.example.com {\n\tx\n}\n"

    monkeypatch.setattr(cli, "_edit_text", fake_edit)
    answers = iter(["", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["caddy", "edit", "--app", "heldso"])
    err = capsys.readouterr().err
    assert "heldso.caddy:2: unrecognized directive: x" in err
    assert "Not saved. Caddy keeps the site it had." in err
    # The second editor opens on the refused text, so the change is not lost.
    assert edits[1] == "heldso.example.com {\n\tx\n}\n"
    assert [method for method, _body in seen] == ["GET", "PUT", "PUT"]


def test_prune(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    seen = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        return 200, json.dumps(
            {
                "images": [
                    {"tag": "localhost/app:st4le", "bytes": 2000, "kept": ""},
                    {"tag": "localhost/busy:t4g", "bytes": 3000, "kept": "image is in use by heldso-app-1"},
                ],
                "freed": 3000,
                "days": body["days"],
                "dry": body["dry"],
            }
        )

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)

    main(["prune"])
    assert seen[("POST", "/api/v1/prune")] == {"days": 7, "dry": False}
    out = capsys.readouterr().out
    assert "removed 1 images, 3kB" in out
    assert "kept localhost/busy:t4g (image is in use by heldso-app-1)" in out

    main(["prune", "--days", "1", "--dry-run"])
    assert seen[("POST", "/api/v1/prune")] == {"days": 1, "dry": True}
    assert "would remove" in capsys.readouterr().out


def test_login_warns_when_no_forge_is_connected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "cfg" / "config.json")

    def fake_api_try(client, method, path, body, timeout=None):
        report = status_report()
        report["host"]["forges"] = {"origin": False, "github": False}
        return 200, json.dumps(report)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    main(["login", "--url", "http://127.0.0.1:8000", "--token", "tok"])
    err = capsys.readouterr().err
    assert cli.FORGE_HINT in err


def _init_commit(path: Path, branch: str = "main") -> None:
    env = {
        "GIT_AUTHOR_NAME": "Yard",
        "GIT_AUTHOR_EMAIL": "yard@example.com",
        "GIT_COMMITTER_NAME": "Yard",
        "GIT_COMMITTER_EMAIL": "yard@example.com",
    }
    subprocess.run(["git", "init", "-b", branch, str(path)], check=True, capture_output=True, env={**env})
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "one"], check=True, capture_output=True, env=env)


def _checkout(path: Path, remote: str, branch: str = "main") -> None:
    _init_commit(path, branch)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True, capture_output=True)


def _bare_repo(path: Path) -> str:
    """A local bare repo the push can really reach. Returns the URL without .git."""
    bare = f"{path}.git"
    subprocess.run(["git", "init", "--bare", bare], check=True, capture_output=True)
    return f"{path}"


def _fake_push(calls: list, ok: bool = True):
    """subprocess.run with the git push stubbed; every other call runs for real."""
    real_run = subprocess.run

    def fake_run(argv, *a, **k):
        if list(argv)[:2] == ["git", "push"]:
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0 if ok else 1)
        return real_run(argv, *a, **k)

    return fake_run


def test_remote_repo_spellings():
    for host in ("origin.cursor.com", "github.com"):
        url = f"https://{host}/org/heldso"
        assert cli._remote_repo(f"https://{host}/org/heldso") == url
        assert cli._remote_repo(f"https://{host}/org/heldso.git") == url
        assert cli._remote_repo(f"ssh://git@{host}/org/heldso.git") == url
        assert cli._remote_repo(f"git@{host}:org/heldso.git") == url
    assert cli._remote_repo("https://gitlab.com/org/heldso.git") == ""
    assert cli._remote_repo("https://github.com/org/heldso/extra.git") == ""


def test_app_deduction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = tmp_path / "work"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "main")
    monkeypatch.chdir(repo)

    def fake_api_try(client, method, path, body, timeout=None):
        return 200, json.dumps([{"name": "heldso", "repo": "https://origin.cursor.com/org/heldso", "ref": "main"}])

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    assert cli._app_name(cli.Client("http://x", "t"), argparse_ns()) == "heldso"


def argparse_ns(**kw):
    import argparse

    return argparse.Namespace(app=kw.get("app", ""))


def test_app_deduction_feature_branch_names_the_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    repo = tmp_path / "work"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "feature/x")
    monkeypatch.chdir(repo)
    seen: dict = {}

    def fake_api_try(client, method, path, body, timeout=None):
        seen[(method, path)] = body
        if path == "/api/v1/apps":
            return 200, json.dumps(
                {"app": {"name": body["name"], "repo": body["repo"], "ref": body["ref"], "trigger": "push"}}
            )
        return 200, json.dumps({"deploy": {"status": "ok", "image": "localhost/app:main", "log": ""}})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: {"status": "ok"})
    main(["app", "new"])
    assert seen[("POST", "/api/v1/apps")] == {
        "name": "heldso-feature-x",
        "repo": "https://origin.cursor.com/org/heldso",
        "ref": "feature/x",
    }


def test_app_deduction_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture):
    _env(monkeypatch)
    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda client, method, path, body, timeout=None: (200, "[]")))

    # Not a git cli.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        cli._app_name(cli.Client("http://x", "t"), argparse_ns())
    assert "--app" in capsys.readouterr().err

    # A remote that is not on Origin or GitHub.
    repo = tmp_path / "gh"
    _checkout(repo, "https://gitlab.com/org/heldso.git", "main")
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit):
        cli._app_name(cli.Client("http://x", "t"), argparse_ns())
    assert "--app" in capsys.readouterr().err

    # A detached HEAD has no branch to match.
    repo = tmp_path / "det"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "main")
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repo), "checkout", sha], check=True, capture_output=True)
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit):
        cli._app_name(cli.Client("http://x", "t"), argparse_ns())
    assert "--app" in capsys.readouterr().err

    # On a branch no app follows.
    repo = tmp_path / "miss"
    _checkout(repo, "https://origin.cursor.com/org/heldso.git", "dev")
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit):
        cli._app_name(cli.Client("http://x", "t"), argparse_ns())
    err = capsys.readouterr().err
    assert "--app" in err
    assert "no app follows https://origin.cursor.com/org/heldso dev" in err
    assert "Create one here: yard app new" in err


def test_forge_ls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    rows = [
        {
            "name": "origin",
            "host": "origin.cursor.com",
            "connected": False,
            "app_id": "",
            "installations": [],
            "last_delivery": "",
            "last_check": "",
        },
        {
            "name": "github",
            "host": "github.com",
            "connected": True,
            "app_id": "42",
            "installations": [
                {"id": "101", "account": "org", "selection": "all"},
                {"id": "202", "account": "other", "selection": "all"},
            ],
            "last_delivery": "",
            "last_check": "",
        },
    ]
    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda *a, **k: (200, json.dumps(rows))))
    _env(monkeypatch)
    main(["forge", "ls"])
    out = capsys.readouterr().out
    assert "origin" in out and "github" in out and "yes" in out
    assert "selection" in out and "all" in out
    assert "org:101, other:202" in out


def _fake_setup_api(calls: list):
    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            return 200, json.dumps(
                [
                    {
                        "name": "origin",
                        "public_key": "-----BEGIN PUBLIC KEY-----\nPUB\n-----END PUBLIC KEY-----\n",
                        "installations": [{"id": "ins_1", "account": "org", "selection": "all"}],
                    },
                    {"name": "github", "installations": [{"id": "101", "account": "org", "selection": "all"}]},
                ]
            )
        if path == "/api/v1/forges/origin" and method == "PUT":
            return 200, json.dumps(
                {
                    "forge": {"name": "origin"},
                    "app": {
                        "name": "yard",
                        "webhook_url": f"{client.url}/hooks/origin",
                        "events": ["repository.pushed"],
                    },
                }
            )
        if path == "/api/v1/forges/github" and method == "PUT":
            return 200, json.dumps(
                {"forge": {"name": "github"}, "app": {"name": "yard-host", "slug": "yard-host", "events": ["push"]}}
            )
        raise AssertionError(path)

    return fake_api_try


def _answers(*answers: str):
    it = iter(answers)
    return lambda _prompt="": next(it)


def _eof_after(*answers: str):
    it = iter(answers)

    def fake(_prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return fake


PEM = "-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----\n"


def _pem(directory: Path, name: str = "key.pem") -> Path:
    path = directory / name
    path.write_text(PEM)
    return path


def _config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "cfg"
    monkeypatch.setattr(cli, "config_path", lambda: cfg / "config.json")
    return cfg


def test_forge_setup_origin(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_fake_setup_api(calls)))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("app_123"))
    _env(monkeypatch)
    main(["forge", "setup", "origin", "--no-browser"])
    out = capsys.readouterr().out
    assert "PUB" in out
    assert "http://127.0.0.1:8000/hooks/origin" in out
    assert "repository.pushed" in out
    assert "scope: repository:contents:read namespace:repositories:create" in out
    assert "install?client_id=app_123" in out
    assert "namespace%3Arepositories%3Acreate" in out
    assert "Done." in out
    assert ("PUT", "/api/v1/forges/origin", {"app_id": "app_123"}) in calls


def test_forge_setup_polls_and_warns(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """The poll waits for the installation; a partial selection and a summary error warn."""
    reads = []

    def fake_api_try(client, method, path, body, timeout=None):
        if path == "/api/v1/forges":
            reads.append(1)
            count = len(reads)
            return 200, json.dumps(
                [
                    {
                        "name": "origin",
                        "public_key": "PUB\n",
                        "installations": (
                            [{"id": "ins_1", "account": "org", "selection": "selected"}] if count > 2 else []
                        ),
                        "error": "origin GET /app/installations: refused" if count == 2 else "",
                    }
                ]
            )
        if path == "/api/v1/forges/origin" and method == "PUT":
            return 200, json.dumps(
                {
                    "forge": {"name": "origin"},
                    "app": {
                        "name": "yard",
                        "webhook_url": f"{client.url}/hooks/origin",
                        "events": ["repository.pushed"],
                    },
                }
            )
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("app_123"))
    _env(monkeypatch)
    main(["forge", "setup", "origin", "--no-browser"])
    captured = capsys.readouterr()
    assert "Done." in captured.out
    assert len(reads) >= 3
    assert "warning: origin GET /app/installations: refused" in captured.err
    assert "installation covers selected repos only; yard repo new needs all repositories" in captured.err


def test_forge_setup_origin_reprompts_a_bad_app_id(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_fake_setup_api(calls)))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("app_a b", "app_123"))
    _env(monkeypatch)
    main(["forge", "setup", "origin", "--no-browser"])
    assert "not an app ID" in capsys.readouterr().err
    puts = [call for call in calls if call[0] == "PUT"]
    assert puts == [("PUT", "/api/v1/forges/origin", {"app_id": "app_123"})]


def test_forge_setup_github_existing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path):
    """Every value as a flag: no prompt, one PUT, then the install step."""
    pem = _pem(tmp_path, "app.pem")
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_fake_setup_api(calls)))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers())
    _env(monkeypatch)
    argv = ["forge", "setup", "github", "--app-id", "42", "--key", str(pem), "--webhook-secret", "s" * 64]
    main([*argv, "--no-browser"])
    captured = capsys.readouterr()
    assert "http://127.0.0.1:8000/hooks/github" in captured.out
    assert "https://github.com/apps/yard-host/installations/new" in captured.out
    assert "Done." in captured.out
    # The key goes to the API but never to the terminal.
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [
        (
            "PUT",
            "/api/v1/forges/github",
            {"app_id": "42", "private_key": PEM, "webhook_secret": "s" * 64},
        )
    ]
    assert "PRIVATE KEY" not in captured.out
    assert "PRIVATE KEY" not in captured.err


def test_forge_setup_github_refuses_http_to_a_remote(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    called: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda *a, **k: called.append(a) or (200, "{}")))
    monkeypatch.setenv("YARD_URL", "http://yard.example.com")
    monkeypatch.setenv("YARD_TOKEN", "token")
    with pytest.raises(SystemExit):
        main(["forge", "setup", "github", "--no-browser"])
    assert "refusing to send the private key over http" in capsys.readouterr().err
    assert called == []


def test_forge_setup_github_accepts_quoted_and_escaped_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pem = _pem(tmp_path)
    spaced = _pem(tmp_path, "my key.pem")
    _config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: "s" * 64)
    _env(monkeypatch)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt="": "s" * 64)
    for answer in (f"'{pem}'", str(spaced).replace(" ", "\\ ")):
        calls: list = []
        monkeypatch.setattr(cli, "_api_try", _forge_rows(_fake_setup_api(calls)))
        monkeypatch.setattr("builtins.input", _answers("42", answer))
        main(["forge", "setup", "github", "--existing", "--no-browser"])
        put = [call for call in calls if call[0] == "PUT"]
        assert put == [
            (
                "PUT",
                "/api/v1/forges/github",
                {"app_id": "42", "private_key": PEM, "webhook_secret": "s" * 64},
            )
        ]


def test_forge_setup_github_reprompts_a_missing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    pem = _pem(tmp_path)
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_fake_setup_api(calls)))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("42", str(tmp_path / "missing.pem"), str(pem)))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt="": "s" * 64)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--existing", "--no-browser"])
    assert "cannot read the key" in capsys.readouterr().err
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [("PUT", "/api/v1/forges/github", {"app_id": "42", "private_key": PEM, "webhook_secret": "s" * 64})]


def test_forge_setup_github_reprompts_a_bad_app_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    pem = _pem(tmp_path)
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_fake_setup_api(calls)))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("42", str(pem)))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt="": "s" * 64)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--app-id", "forty-two", "--no-browser"])
    assert "not an app ID" in capsys.readouterr().err
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [("PUT", "/api/v1/forges/github", {"app_id": "42", "private_key": PEM, "webhook_secret": "s" * 64})]


def test_forge_setup_github_retries_after_a_client_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    pem = _pem(tmp_path)
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    attempts = 0

    def fake_api_try(client, method, path, body, timeout=None):
        nonlocal attempts
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            return 200, json.dumps(
                [{"name": "github", "installations": [{"id": "101", "account": "org", "selection": "all"}]}]
            )
        if path == "/api/v1/forges/github" and method == "PUT":
            attempts += 1
            if attempts == 1:
                return 400, json.dumps({"error": {"message": "the app id does not match the key"}})
            return 200, json.dumps({"forge": {"name": "github"}, "app": {"slug": "yard-host"}})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    # The API refuses the first App ID, key, and secret; the wizard asks all three again.
    monkeypatch.setattr("builtins.input", _answers("41", str(pem), "42", str(pem)))
    secrets_given = iter(["s" * 64, "t" * 64])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt="": next(secrets_given))
    _env(monkeypatch)
    main(["forge", "setup", "github", "--existing", "--no-browser"])
    assert "the app id does not match the key" in capsys.readouterr().err
    put = [call for call in calls if call[0] == "PUT"]
    assert [(call[2]["app_id"], call[2]["webhook_secret"]) for call in put] == [("41", "s" * 64), ("42", "t" * 64)]


def test_forge_setup_github_continues_to_the_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    reads = 0

    def fake_api_try(client, method, path, body, timeout=None):
        nonlocal reads
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            reads += 1
            row = {
                "name": "github",
                "app_id": "42",
                "slug": "yard-host",
                "installations": [] if reads == 1 else [{"id": "101", "account": "org", "selection": "all"}],
            }
            return 200, json.dumps([row])
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--no-browser"])
    out = capsys.readouterr().out
    assert "https://github.com/apps/yard-host/installations/new" in out
    assert "Done." in out
    assert not [call for call in calls if call[0] == "PUT"]


def test_forge_setup_github_already_connected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    _config_dir(monkeypatch, tmp_path)
    calls: list = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            return 200, json.dumps(
                [
                    {
                        "name": "github",
                        "connected": True,
                        "app_id": "42",
                        "slug": "yard-host",
                        "installations": [{"id": "101", "account": "Bobowski", "selection": "all"}],
                    }
                ]
            )
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    main(["forge", "setup", "github", "--no-browser"])
    out = capsys.readouterr().out
    assert "already connected" in out
    assert "Bobowski (101)" in out
    assert "Add another account: https://github.com/apps/yard-host/installations/new" in out
    assert "--new" in out
    assert calls == [("GET", "/api/v1/forges", None)]


def test_forge_setup_github_new_ignores_a_connected_forge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    """--new runs the setup even when the forge row says connected."""
    pem = _pem(tmp_path)
    _config_dir(monkeypatch, tmp_path)
    calls: list = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            return 200, json.dumps(
                [
                    {
                        "name": "github",
                        "connected": True,
                        "app_id": "42",
                        "installations": [{"id": "101", "account": "org", "selection": "all"}],
                    }
                ]
            )
        if path == "/api/v1/forges/github" and method == "PUT":
            return 200, json.dumps({"forge": {"name": "github"}, "app": {"slug": "yard-host"}})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    monkeypatch.setattr("builtins.input", _answers("42", str(pem)))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt="": "n" * 64)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--existing", "--new", "--no-browser"])
    out = capsys.readouterr().out
    assert "Done." in out
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [("PUT", "/api/v1/forges/github", {"app_id": "42", "private_key": PEM, "webhook_secret": "n" * 64})]


MANIFEST_APP = {
    "id": 4242,
    "slug": "yard-127-0-0-1",
    "pem": PEM,
    "webhook_secret": "w" * 40,
    "html_url": "https://github.com/apps/yard-127-0-0-1",
}


def _local_get(url: str) -> tuple[int, str]:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port, timeout=10)
    conn.request("GET", parts.path + ("?" + parts.query if parts.query else ""))
    response = conn.getresponse()
    body = response.read().decode()
    conn.close()
    return response.status, body


def _drive_manifest(opened: list, bad_state_first: bool = False):
    """The browser: open the local page, send GitHub's callback."""

    def fake_open(url: str) -> None:
        opened.append(url)
        if not url.startswith("http://127.0.0.1"):
            return
        status, page = _local_get(url)
        assert status == 200
        state = re.search(r'state=([^"&]+)', page)
        assert state
        if bad_state_first:
            status, body = _local_get(f"{url}callback?code=manifest-code&state=bogus")
            assert status == 400
            assert "does not belong" in body
        status, body = _local_get(f"{url}callback?code=manifest-code&state={state.group(1)}")
        assert status == 200
        assert "Go back to the terminal" in body

    return fake_open


class _ConversionReply:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self) -> bytes:
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_conversion(request, timeout=None):
    assert request.full_url == "https://api.github.com/app-manifests/manifest-code/conversions"
    assert request.get_method() == "POST"
    return _ConversionReply(json.dumps(MANIFEST_APP).encode())


def test_forge_setup_github_manifest_page():
    session = cli.Client("https://yard.a-rather-long-host-name.example.com", "t")
    page = cli._manifest_page(session, argparse.Namespace(org="", public=False), "http://127.0.0.1:9", "STATE")
    action = re.search(r'action="([^"]*)"', page)
    assert action and html.unescape(action.group(1)) == "https://github.com/settings/apps/new?state=STATE"
    hidden = re.search(r'name="manifest" value="([^"]*)"', page)
    assert hidden
    manifest = json.loads(html.unescape(hidden.group(1)))
    assert manifest["name"] == "yard-yard.a-rather-long-host-name.example.com"[:34]
    assert len(manifest["name"]) == 34
    assert manifest["url"] == "https://yard.a-rather-long-host-name.example.com"
    assert manifest["hook_attributes"] == {
        "url": "https://yard.a-rather-long-host-name.example.com/hooks/github",
        "active": True,
    }
    assert manifest["redirect_url"] == "http://127.0.0.1:9/callback"
    assert manifest["public"] is False
    assert manifest["default_permissions"] == {
        "contents": "write",
        "metadata": "read",
        "repository_creation": "write",
    }
    assert manifest["default_events"] == ["push"]
    assert "document.forms[0].submit()" in page

    org_page = cli._manifest_page(session, argparse.Namespace(org="my-org", public=True), "http://127.0.0.1:9", "S")
    org_action = re.search(r'action="([^"]*)"', org_page)
    assert org_action and html.unescape(org_action.group(1)).startswith(
        "https://github.com/organizations/my-org/settings/apps/new"
    )
    org_manifest = re.search(r'name="manifest" value="([^"]*)"', org_page)
    assert org_manifest and json.loads(html.unescape(org_manifest.group(1)))["public"] is True


def test_forge_setup_github_manifest(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path):
    cfg = _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_fake_setup_api(calls)))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    opened: list = []
    monkeypatch.setattr(cli.webbrowser, "open", _drive_manifest(opened, bad_state_first=True))
    monkeypatch.setattr(cli.urllib.request, "urlopen", _fake_conversion)
    _env(monkeypatch)
    main(["forge", "setup", "github"])
    captured = capsys.readouterr()
    out = captured.out
    assert "Opening the GitHub App form. Keep the name or change it" in out
    assert "Install the app on your account and select all repositories." in out
    assert "https://github.com/apps/yard-127-0-0-1/installations/new" in out
    assert "Done." in out
    put = [call for call in calls if call[0] == "PUT"]
    assert put == [
        (
            "PUT",
            "/api/v1/forges/github",
            {"app_id": "4242", "private_key": PEM, "webhook_secret": "w" * 40},
        )
    ]
    assert not (cfg / "github-manifest.json").exists()
    # The key and the secret never reach the terminal.
    assert "PRIVATE KEY" not in out + captured.err
    assert "w" * 40 not in out + captured.err


def test_forge_setup_github_manifest_no_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_fake_setup_api(calls)))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    opened: list = []

    def fake_open(args, url):
        opened.append(url)
        print(url)
        if url.startswith("http://127.0.0.1"):
            _drive_manifest([], False)(url)

    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.urllib.request, "urlopen", _fake_conversion)
    _env(monkeypatch)
    main(["forge", "setup", "github", "--no-browser"])
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out
    assert "Open it in a browser on this computer; GitHub sends you back to it." in out
    assert "Done." in out


def test_forge_setup_github_manifest_times_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    _config_dir(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_fake_setup_api(calls)))
    monkeypatch.setattr(cli, "SETUP_LIMIT", 0.05)
    monkeypatch.setattr(cli, "_open", lambda args, url: None)
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["forge", "setup", "github", "--no-browser"])
    assert "No answer from GitHub in ten minutes" in capsys.readouterr().err


def test_forge_setup_github_manifest_keeps_a_failed_put(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
):
    cfg = _config_dir(monkeypatch, tmp_path)
    calls: list = []
    puts = 0

    def fake_api_try(client, method, path, body, timeout=None):
        nonlocal puts
        calls.append((method, path, body))
        if path == "/api/v1/forges":
            return 200, json.dumps(
                [{"name": "github", "installations": [{"id": "101", "account": "org", "selection": "all"}]}]
            )
        if path == "/api/v1/forges/github" and method == "PUT":
            puts += 1
            if puts == 1:
                return 400, json.dumps({"error": {"message": "the secret is too short"}})
            return 200, json.dumps({"forge": {"name": "github"}, "app": {"slug": "yard-127-0-0-1"}})
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(cli, "SETUP_POLL", 0.0)
    opened: list = []
    monkeypatch.setattr(cli.webbrowser, "open", _drive_manifest(opened))
    monkeypatch.setattr(cli.urllib.request, "urlopen", _fake_conversion)
    _env(monkeypatch)

    with pytest.raises(SystemExit):
        main(["forge", "setup", "github"])
    err = capsys.readouterr().err
    assert "the secret is too short" in err
    assert "The key is kept in" in err
    pending = cfg / "github-manifest.json"
    saved = json.loads(pending.read_text())
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert saved["pem"] == PEM and saved["app_id"] == "4242"

    # The rerun goes straight to the PUT; the browser opens the install page only.
    opened.clear()
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    main(["forge", "setup", "github"])
    out = capsys.readouterr().out
    assert "Finishing the app created at" in out
    assert "saving its key in Yard" in out
    assert "Done." in out
    assert opened == ["https://github.com/apps/yard-127-0-0-1/installations/new"]
    assert not pending.exists()
    put = [call for call in calls if call[0] == "PUT"]
    assert len(put) == 2


def test_app_logs_builds_the_query(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        if path == "/api/v1/apps/heldso/deploy":
            return 404, "no deploy since Yard started"
        return 200, "line one\n"

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    main(["app", "logs", "--app", "heldso", "--since", "-1h", "--deploy", "--tail", "50"])
    assert calls == [
        ("GET", "/api/v1/logs?lines=50&container=deploy-heldso&since=-1h", None),
    ]
    assert "line one" in capsys.readouterr().out


def test_app_logs_follow_streams(monkeypatch: pytest.MonkeyPatch):
    seen = []
    monkeypatch.setattr(cli, "_stream_get", lambda client, path: seen.append(path))
    _env(monkeypatch)
    main(["app", "logs", "-f", "--app", "heldso", "--container", "heldso-worker-1"])
    assert seen == ["/api/v1/logs?lines=100&container=heldso-worker-1&follow=1"]


def test_logs_by_name_or_id(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda *a, **k: calls.append(a[2]) or (200, "x\n")))
    _env(monkeypatch)
    main(["logs", "yard-caddy-1", "--tail", "10"])
    main(["logs", "abc123def456"])
    assert calls == [
        "/api/v1/logs?lines=10&container=yard-caddy-1",
        "/api/v1/logs?lines=100&container=abc123def456",
    ]


def test_logs_sends_hide_and_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(lambda *a, **k: calls.append(a[2]) or (200, "x\n")))
    _env(monkeypatch)
    main(["logs", "heldso-app-1", "--hide", "a=b", "--hide", "noise", "--only", "status=error"])
    assert calls == ["/api/v1/logs?lines=100&container=heldso-app-1&hide=a%3Db&hide=noise&only=status%3Derror"]


def test_events_build_the_query(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    calls = []

    def fake_api_try(client, method, path, body, timeout=None):
        calls.append((method, path, body))
        return 200, json.dumps(
            [
                {
                    "time": "2026-10-05T12:00:00Z",
                    "source": "podman",
                    "event": "died",
                    "app": "heldso",
                    "container": "heldso-app-1",
                    "container_id": "abc123def456" + "0" * 52,
                    "exit_code": "1",
                }
            ]
        )

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    main(["events", "--app", "heldso", "--source", "podman", "--since", "-1h"])
    assert calls[-1] == ("GET", "/api/v1/events?lines=200&since=-1h&app=heldso&source=podman", None)
    out = capsys.readouterr().out
    assert "died" in out and "heldso-app-1 abc123def456" in out and "exit 1" in out


def test_app_cat(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    def fake_api_try(client, method, path, body, timeout=None):
        if path == "/api/v1/apps/heldso/files/containerfile":
            return 200, "FROM scratch\n"
        raise AssertionError(path)

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    main(["app", "cat", "containerfile", "--app", "heldso"])
    assert capsys.readouterr().out == "FROM scratch\n"


def test_source_name():
    assert cli._source_name("image", "ghcr.io/you/api:1.4") == "api"
    assert cli._source_name("image", "postgres:17") == "postgres"
    assert cli._source_name("url", "https://example.com/ops/umami.yml") == "umami"
    assert cli._source_name("url", "https://example.com/ops/site.yaml") == "site"
    assert cli._source_name("url", "https://example.com/examples/uptime-kuma.compose.yml") == "uptime-kuma"


def test_app_new_image(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    sent = []

    def fake_api_try(client, method, path, body, timeout=None):
        sent.append((method, path, body))
        if method == "POST" and path == "/api/v1/apps":
            return 200, json.dumps({"app": {"name": "api", "runs": "ghcr.io/you/api:1.4"}})
        if method == "POST" and path.endswith("/deploy"):
            return 200, json.dumps({"deploy": {"status": "ok", "port": 3000}})
        return 200, "{}"

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: {"status": "ok"})
    main(["app", "new", "--runs", "ghcr.io/you/api:1.4"])
    method, path, body = sent[0]
    assert body["name"] == "api"
    assert body["repo"] == ""
    assert body["runs"] == "ghcr.io/you/api:1.4"


def test_app_new_recipe_caddy_port(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    sent = []

    def fake_api_try(client, method, path, body, timeout=None):
        sent.append((method, path, body))
        if method == "POST" and path == "/api/v1/apps":
            return 200, json.dumps({"app": {"name": "umami"}})
        if method == "POST" and path.endswith("/deploy"):
            return 200, json.dumps({"deploy": {"status": "ok", "port": 3000}})
        return 200, "{}"

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: {"status": "ok", "port": 3000})
    main(
        [
            "app",
            "new",
            "--runs",
            "https://example.com/umami.yml",
            "--caddy",
            "umami.example.com",
        ]
    )
    site = next(call for call in sent if call[0] == "PUT")
    assert site[1] == "/api/v1/apps/umami/caddy"
    assert b"reverse_proxy umami-app-1:3000 {" in site[2]


def test_app_set_runs_push_message(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    def fake_api_try(client, method, path, body, timeout=None):
        if method == "GET":
            return 200, json.dumps({"app": {"name": "db", "runs": "postgres:16", "trigger": "push"}})
        return 200, json.dumps({"app": {"name": "db", "runs": "postgres:17", "trigger": "push"}})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    main(["app", "set", "--app", "db", "--runs", "postgres:17"])
    out = capsys.readouterr().out
    assert "The next check deploys it. Deploy now" in out


def test_app_set_runs_manual_message(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    def fake_api_try(client, method, path, body, timeout=None):
        if method == "GET":
            return 200, json.dumps({"app": {"name": "db", "runs": "postgres:16", "trigger": "manual"}})
        return 200, json.dumps({"app": {"name": "db", "runs": "postgres:17", "trigger": "manual"}})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    main(["app", "set", "--app", "db", "--runs", "postgres:17"])
    out = capsys.readouterr().out
    assert "still runs the old way until the next deploy" in out
    assert "Deploy now: yard app deploy --app db" in out


def _site_calls(sent):
    return [call for call in sent if call[0] == "PUT" and call[1].endswith("/caddy")]


def _new_web(monkeypatch: pytest.MonkeyPatch, sent: list, port: int, deploy_code: int = 200) -> None:
    def fake_api_try(client, method, path, body, timeout=None):
        sent.append((method, path, body))
        if path == "/api/v1/apps":
            return 200, json.dumps({"app": {"name": "web"}})
        if path.endswith("/deploy"):
            if deploy_code >= 300:
                return deploy_code, json.dumps({"error": {"message": "build failed"}})
            return 200, json.dumps({"deploy": {"status": "ok", "port": port}})
        return 200, json.dumps({"hosts": ["web.example.com"]})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: {"status": "ok", "port": port})
    _env(monkeypatch)


NEW_WEB = ["app", "new", "--repo", "https://origin.cursor.com/org/web", "--caddy", "web.example.com"]


def test_app_new_caddy_writes_the_exposed_port(monkeypatch: pytest.MonkeyPatch):
    sent: list = []
    _new_web(monkeypatch, sent, 3000)
    main(NEW_WEB)
    site = _site_calls(sent)[0]
    assert b"web.example.com {\n\treverse_proxy web-app-1:3000 {" in site[2]
    # The site file comes after the deploy.
    assert sent.index(("POST", "/api/v1/apps/web/deploy", {})) < sent.index(site)


def test_app_new_caddy_repo_app_falls_back_to_8000(monkeypatch: pytest.MonkeyPatch):
    # The Yard templates have no EXPOSE line: a repo app gets 8000.
    sent: list = []
    _new_web(monkeypatch, sent, 0)
    main(NEW_WEB)
    assert b"reverse_proxy web-app-1:8000 {" in _site_calls(sent)[0][2]


def test_app_new_caddy_image_without_a_port_prints_the_template(monkeypatch: pytest.MonkeyPatch, capsys):
    sent: list = []
    _new_web(monkeypatch, sent, 0)
    with pytest.raises(SystemExit) as caught:
        main(["app", "new", "--runs", "postgres:17", "--app", "web", "--caddy", "web.example.com"])
    assert caught.value.code == 1
    assert _site_calls(sent) == []
    err = capsys.readouterr().err
    assert "web-app-1 does not expose exactly one port" in err
    assert "yard caddy edit --app web" in err
    assert "reverse_proxy web-app-1:PORT {" in err


def test_app_new_caddy_writes_nothing_after_a_failed_deploy(monkeypatch: pytest.MonkeyPatch):
    sent: list = []
    _new_web(monkeypatch, sent, 3000, deploy_code=500)
    with pytest.raises(SystemExit):
        main(NEW_WEB)
    assert _site_calls(sent) == []


def test_app_new_caddy_needs_the_first_deploy(monkeypatch: pytest.MonkeyPatch, capsys):
    sent: list = []
    _new_web(monkeypatch, sent, 3000)
    with pytest.raises(SystemExit):
        main([*NEW_WEB, "--no-deploy"])
    assert sent == []
    assert "--caddy needs the first deploy" in capsys.readouterr().err


def test_app_new_caddy_checks_each_host(monkeypatch: pytest.MonkeyPatch):
    sent: list = []
    _new_web(monkeypatch, sent, 3000)
    with pytest.raises(SystemExit):
        main([*NEW_WEB, "--caddy", "NOT A HOST"])
    assert sent == []


def _new_env_fakes(monkeypatch, sent):
    def fake_api_try(client, method, path, body, timeout=None):
        sent.append((method, path, body))
        return 200, json.dumps({"app": {"name": "heldso"}})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: {"status": "ok"})
    _env(monkeypatch)


def test_app_new_env_flag_puts_the_env(monkeypatch: pytest.MonkeyPatch):
    sent = []
    _new_env_fakes(monkeypatch, sent)
    main(["app", "new", "--repo", "https://origin.cursor.com/org/x", "-e", "A=1", "-e", "B=two"])
    put = next(c for c in sent if c[0] == "PUT" and c[1].endswith("/env"))
    assert put[2] == {"A": "1", "B": "two"}
    assert sent.index(put) < sent.index(next(c for c in sent if c[1].endswith("/deploy")))


def test_app_new_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sent = []
    dotenv = tmp_path / "x.env"
    dotenv.write_text("A=from-file\nB=file-two\n")
    _new_env_fakes(monkeypatch, sent)
    main(["app", "new", "--repo", "https://origin.cursor.com/org/x", "--env-file", str(dotenv)])
    put = next(c for c in sent if c[0] == "PUT" and c[1].endswith("/env"))
    assert put[2] == {"A": "from-file", "B": "file-two"}


def test_app_new_env_merge_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sent = []
    dotenv = tmp_path / "x.env"
    dotenv.write_text("A=file\nB=file\n")
    _new_env_fakes(monkeypatch, sent)
    main(
        [
            "app",
            "new",
            "--repo",
            "https://origin.cursor.com/org/x",
            "--env-file",
            str(dotenv),
            "-e",
            "A=flag",
        ]
    )
    put = next(c for c in sent if c[0] == "PUT" and c[1].endswith("/env"))
    assert put[2] == {"A": "flag", "B": "file"}


def test_app_new_env_edit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sent = []
    _new_env_fakes(monkeypatch, sent)
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr(cli, "_edit_text", lambda text: "A=edited\n")
    main(["app", "new", "--repo", "https://origin.cursor.com/org/x", "-e", "A=1", "--edit-env"])
    put = next(c for c in sent if c[0] == "PUT" and c[1].endswith("/env"))
    assert put[2] == {"A": "edited"}


def test_app_new_edit_env_starts_from_the_checkout_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
):
    sent = []
    shown = []
    _new_env_fakes(monkeypatch, sent)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "env.example").write_text("# Secrets\nA=\nSECRET=\n# OPT=x\nMODEL=small\n")
    monkeypatch.setattr(sys, "stdin", _TTY())

    def fake_edit(text: str) -> str:
        shown.append(text)
        return text.replace("SECRET=", "SECRET=s3")

    monkeypatch.setattr(cli, "_edit_text", fake_edit)
    main(["app", "new", "--repo", "https://origin.cursor.com/org/x", "-e", "A=1", "--edit-env"])
    assert shown == ["A=1\n\n# Secrets\nSECRET=\n# OPT=x\nMODEL=small\n"]
    put = next(c for c in sent if c[0] == "PUT" and c[1].endswith("/env"))
    assert put[2] == {"A": "1", "SECRET": "s3", "MODEL": "small"}
    assert "These keys are empty" not in capsys.readouterr().err


def test_app_new_edit_env_takes_a_path_and_drops_empty_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
):
    sent = []
    _new_env_fakes(monkeypatch, sent)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("IGNORED=1\n")
    (tmp_path / "prod.env").write_text("KEY=\nMODEL=big\n")
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr(cli, "_edit_text", lambda text: text)
    main(["app", "new", "--repo", "https://origin.cursor.com/org/x", "--edit-env", "prod.env"])
    put = next(c for c in sent if c[0] == "PUT" and c[1].endswith("/env"))
    assert put[2] == {"MODEL": "big"}
    err = capsys.readouterr().err
    assert "These keys are empty, so they are not saved: KEY" in err
    assert "yard env set --app x KEY=" in err


def test_app_new_edit_env_missing_path_fails_before_post(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sent = []
    _new_env_fakes(monkeypatch, sent)
    monkeypatch.setattr(sys, "stdin", _TTY())
    with pytest.raises(SystemExit):
        main(["app", "new", "--repo", "https://origin.cursor.com/org/x", "--edit-env", str(tmp_path / "no")])
    assert sent == []


def test_app_new_bad_env_fails_before_post(monkeypatch: pytest.MonkeyPatch):
    sent = []
    _new_env_fakes(monkeypatch, sent)
    with pytest.raises(SystemExit):
        main(["app", "new", "--repo", "https://origin.cursor.com/org/x", "-e", "NOTAPAIR"])
    assert sent == []


def test_app_new_failed_first_deploy_exits_1(monkeypatch: pytest.MonkeyPatch, capsys):
    sent = []

    def fake_api_try(client, method, path, body, timeout=None):
        sent.append((method, path, body))
        if path == "/api/v1/apps":
            return 200, json.dumps({"app": {"name": "web"}})
        return 200, json.dumps({"deploy": {"status": "ok"}})

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    monkeypatch.setattr(cli, "_follow_deploy", lambda *a: {"status": "failed", "log": "== boom\n"})
    _env(monkeypatch)
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "app",
                "new",
                "--repo",
                "https://origin.cursor.com/org/web",
                "--caddy",
                "web.example.com",
            ]
        )
    assert caught.value.code == 1
    assert _site_calls(sent) == []
    captured = capsys.readouterr()
    assert "web was created, but the first deploy failed." in captured.err
    assert "yard caddy edit --app web" in captured.out


def test_app_new_env_put_failure_dies_cleanly(monkeypatch: pytest.MonkeyPatch, capsys):
    def fake_api_try(client, method, path, body, timeout=None):
        if path == "/api/v1/apps":
            return 200, json.dumps({"app": {"name": "x"}})
        if "/env" in path:
            return 500, json.dumps({"error": {"message": "disk full"}})
        return 200, "{}"

    monkeypatch.setattr(cli, "_api_try", _forge_rows(fake_api_try))
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "new", "--repo", "https://origin.cursor.com/org/x", "-e", "A=1"])
    err = capsys.readouterr().err
    assert "x was created, but its env was not saved" in err
    assert "yard env edit --app x" in err


def _work_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A committed checkout with no origin, made the cwd."""
    repo = tmp_path / "work"
    _init_commit(repo, "main")
    monkeypatch.chdir(repo)
    return repo


def _repo_api(seen: list | None = None, *, repo="https://github.com/org/work"):
    """An _api_try fake for the repo POST."""

    def fake_api_try(client, method, path, body, timeout=None):
        if seen is not None:
            seen.append((method, path, body))
        return 200, json.dumps({"repo": repo})

    return fake_api_try


def test_repo_new(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture):
    repo = _work_checkout(monkeypatch, tmp_path)
    bare = _bare_repo(tmp_path / "bare")
    seen: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_repo_api(seen, repo=bare)))
    _env(monkeypatch)
    main(["repo", "new"])
    # The server picks the forge and the owner.
    assert seen == [("POST", "/api/v1/repos", {"name": "work"})]
    remote = subprocess.check_output(["git", "-C", str(repo), "remote", "get-url", "origin"], text=True).strip()
    assert remote == f"{bare}.git"
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    pushed = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "origin/main"], text=True).strip()
    assert pushed == head
    out = capsys.readouterr().out
    assert f"created {bare}" in out
    assert "pushed main" in out


@pytest.mark.parametrize(
    "case, texts",
    [
        ("no_checkout", ["not a git checkout here"]),
        ("no_commit", ["no commit yet"]),
        ("detached", ["detached HEAD"]),
        ("origin_exists", ["origin is already https://github.com/org/heldso", "yard app new uses it"]),
    ],
)
def test_repo_new_die_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys, case, texts):
    repo = tmp_path / "work"
    if case == "no_commit":
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        monkeypatch.chdir(repo)
    elif case == "detached":
        _init_commit(repo, "main")
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(repo), "checkout", sha], check=True, capture_output=True)
        monkeypatch.chdir(repo)
    elif case == "origin_exists":
        _checkout(repo, "https://github.com/org/heldso.git", "main")
        monkeypatch.chdir(repo)
    else:
        monkeypatch.chdir(tmp_path)
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["repo", "new"])
    err = capsys.readouterr().err
    for text in texts:
        assert text in err


def test_repo_new_a_push_failure_keeps_origin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    repo = _work_checkout(monkeypatch, tmp_path)
    pushes: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_repo_api(repo="https://github.com/org/heldso")))
    monkeypatch.setattr(cli.subprocess, "run", _fake_push(pushes, ok=False))
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["repo", "new"])
    err = capsys.readouterr().err
    assert "the push failed" in err
    assert "git push -u origin main" in err
    assert pushes == [["git", "push", "-u", "origin", "main"]]
    remote = subprocess.check_output(["git", "-C", str(repo), "remote", "get-url", "origin"], text=True).strip()
    assert remote == "https://github.com/org/heldso.git"


def test_repo_new_warns_about_uncommitted_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    repo = _work_checkout(monkeypatch, tmp_path)
    bare = _bare_repo(tmp_path / "bare")
    (repo / "dirty.txt").write_text("dirty\n")
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_repo_api(repo=bare)))
    _env(monkeypatch)
    main(["repo", "new"])
    assert "warning: uncommitted changes are not pushed" in capsys.readouterr().err


def test_repo_new_json_prints_repo_and_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    _work_checkout(monkeypatch, tmp_path)
    bare = _bare_repo(tmp_path / "bare")
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_repo_api(repo=bare)))
    _env(monkeypatch)
    main(["repo", "new", "--json"])
    assert json.loads(capsys.readouterr().out) == {"repo": bare, "branch": "main"}


def test_repo_new_passes_the_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    _work_checkout(monkeypatch, tmp_path)
    bare = _bare_repo(tmp_path / "bare")
    seen: list = []
    monkeypatch.setattr(cli, "_api_try", _forge_rows(_repo_api(seen, repo=bare)))
    _env(monkeypatch)
    main(["repo", "new", "heldso", "--owner", "apps", "--forge", "origin"])
    assert seen == [("POST", "/api/v1/repos", {"name": "heldso", "owner": "apps", "forge": "origin"})]


def test_app_new_no_repo_names_repo_new(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    _work_checkout(monkeypatch, tmp_path)
    _env(monkeypatch)
    with pytest.raises(SystemExit):
        main(["app", "new"])
    err = capsys.readouterr().err
    assert "no repo" in err
    assert "yard repo new" in err


def test_json_asks_for_json_and_logs_print_the_page(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """--json sends Accept: application/json, so yard logs prints the entries page Medic reads."""
    _env(monkeypatch)
    monkeypatch.setattr(cli, "_json", True)
    request = cli._request(cli.Client("http://127.0.0.1:8000", "token"), "GET", "/api/v1/logs", None)
    assert request.get_header("Accept") == "application/json"
    monkeypatch.setattr(cli, "_json", False)
    request = cli._request(cli.Client("http://127.0.0.1:8000", "token"), "GET", "/api/v1/logs", None)
    assert request.get_header("Accept") is None

    page = {"entries": [{"cursor": "c1", "container": "heldso-app-1", "time": "t", "message": "hi"}], "cursor": "c1"}
    monkeypatch.setattr(cli, "_api_try", lambda client, method, path, body, timeout=None: (200, json.dumps(page)))
    main(["logs", "heldso-app-1", "--json"])
    assert json.loads(capsys.readouterr().out) == page


def test_events_follow_prints_table_lines(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    _env(monkeypatch)
    rows = [
        {"time": "2026-11-02T10:00:00Z", "source": "yard", "event": "deploy.end", "app": "heldso", "status": "ok"},
        {"time": "2026-11-02T10:00:01Z", "source": "podman", "event": "died", "app": "heldso", "container": "x"},
    ]

    body = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda request: io.BytesIO(body))
    main(["events", "-f"])
    out = capsys.readouterr().out
    assert "deploy.end" in out and "died" in out
    assert "{" not in out
