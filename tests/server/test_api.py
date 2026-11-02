import asyncio
import hashlib
import hmac
import json
import stat

from app.server import apps
from app.server.caddy import Caddy
from app.server.forges import GitHub, Origin
from app.server.podman import Podman
from app.shared import YardError
from tests.conftest import auth
from tests.server.conftest import FakeCaddy


async def test_health(yard):
    client, _token, _root = yard
    res = await client.get("/health")
    assert res.status_code == 200


async def test_apps_need_a_token(yard):
    client, _token, _root = yard
    res = await client.get("/api/v1/apps")
    assert res.status_code == 401
    assert "unauthorized" in res.text


async def test_app_create_needs_a_name_and_a_repo(yard):
    client, token, _root = yard
    for body in (
        {"name": "", "repo": "https://origin.cursor.com/org/heldso"},
        {"name": " ", "repo": "https://origin.cursor.com/org/heldso"},
        {"repo": "https://origin.cursor.com/org/heldso"},
        {"name": "heldso"},
        {"name": "heldso", "repo": " "},
    ):
        res = await client.post("/api/v1/apps", json=body, headers=auth(token))
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "bad_json"

    res = await client.post(
        "/api/v1/apps",
        json={"name": "yard-sampler", "repo": "https://origin.cursor.com/org/heldso"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "bad_name" in res.text

    res = await client.post(
        "/api/v1/apps",
        json={"name": "caddy", "repo": "https://origin.cursor.com/org/heldso"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "bad_name" in res.text

    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "not-a-repo"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "bad_repo" in res.text


async def test_app_round_trip(yard, monkeypatch):
    client, token, _root = yard

    async def fake_run(self, *args, timeout=None, env=None, stdin=None):
        return "[]"

    monkeypatch.setattr(Podman, "run", fake_run)
    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://origin.cursor.com/org/heldso"},
        headers=auth(token),
    )
    assert res.status_code == 200
    app = res.json()["app"]
    assert app["repo"] == "https://origin.cursor.com/org/heldso"
    assert app["ref"] == "main"
    assert app["trigger"] == "push"
    assert "env" not in app

    res = await client.post(
        "/api/v1/apps",
        json={"name": "other", "repo": "https://origin.cursor.com/org/heldso", "ref": "side", "trigger": "manual"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["app"]["trigger"] == "manual"

    res = await client.patch("/api/v1/apps/heldso", json={"ref": "release"}, headers=auth(token))
    assert res.status_code == 200
    assert res.json()["app"]["ref"] == "release"

    res = await client.patch("/api/v1/apps/heldso", json={}, headers=auth(token))
    assert res.status_code == 400
    assert "nothing to change" in res.text

    page = (await client.get("/api/v1/apps/heldso", headers=auth(token))).json()
    assert page["app"]["name"] == "heldso"
    assert page["app"]["ref"] == "release"
    assert "env" not in page["app"]

    listed = (await client.get("/api/v1/apps", headers=auth(token))).json()
    assert [row["name"] for row in listed] == ["heldso", "other"]
    assert "env" not in listed[0]

    res = await client.delete("/api/v1/apps/heldso", headers=auth(token))
    assert res.status_code == 200
    res = await client.get("/api/v1/apps/heldso", headers=auth(token))
    assert res.status_code == 404


def _fake_caddy(monkeypatch, refuse: str = "") -> FakeCaddy:
    """Every Caddy client in the app answers like FakeCaddy."""
    fake = FakeCaddy(refuse)

    async def request(self, method, path, body=None, content_type="application/json"):
        return await fake.request(method, path, body, content_type)

    monkeypatch.setattr(Caddy, "request", request)
    return fake


async def test_app_delete_answers_the_site_hosts(yard, monkeypatch):
    client, token, root = yard

    async def fake_run(self, *args, **kw):
        return "[]"

    monkeypatch.setattr(Podman, "run", fake_run)
    fake = _fake_caddy(monkeypatch)
    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://origin.cursor.com/org/heldso"},
        headers=auth(token),
    )
    assert res.status_code == 200
    res = await client.put("/api/v1/apps/heldso/caddy", content="heldso.example.com {\n}\n", headers=auth(token))
    assert res.status_code == 200

    res = await client.delete("/api/v1/apps/heldso", headers=auth(token))
    assert res.status_code == 200
    assert res.json() == {"name": "heldso", "hosts": ["heldso.example.com"]}
    assert not (root / "apps" / "yard" / "caddy" / "config" / "sites" / "heldso.caddy").exists()
    assert fake.loads == 2


async def test_app_delete_refuses_the_yard_app(yard):
    client, token, _root = yard
    res = await client.delete("/api/v1/apps/yard", headers=auth(token))
    assert res.status_code == 400
    error = res.json()["error"]
    assert error["code"] == "forbidden"
    assert "yard-app-1" in error["help"]


async def test_app_trigger_is_push_or_manual(yard):
    client, token, _root = yard
    res = await client.post(
        "/api/v1/apps",
        json={"name": "backup", "repo": "https://origin.cursor.com/org/app", "trigger": "every 5m"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "bad_trigger" in res.text

    res = await client.post(
        "/api/v1/apps",
        json={"name": "backup", "repo": "https://origin.cursor.com/org/app", "trigger": "manual"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["app"]["trigger"] == "manual"

    res = await client.patch("/api/v1/apps/backup", json={"trigger": ""}, headers=auth(token))
    assert res.status_code == 200
    assert res.json()["app"]["trigger"] == "push"


async def test_env_api(yard):
    client, token, root = yard
    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://origin.cursor.com/org/heldso"},
        headers=auth(token),
    )
    assert res.status_code == 200

    env_file = root / "apps" / "heldso" / "env"
    assert env_file.read_text() == ""
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    res = await client.patch(
        "/api/v1/apps/heldso/env",
        json={"API_TOKEN": "secret", "GREETING": "hi there"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json() == {"keys": ["API_TOKEN", "GREETING"]}
    assert env_file.read_text() == 'API_TOKEN=secret\nGREETING="hi there"\n'
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # The atomic write leaves no tmp file behind.
    assert sorted(path.name for path in env_file.parent.iterdir()) == ["app", "env"]

    # A null value removes the key.
    res = await client.patch(
        "/api/v1/apps/heldso/env",
        json={"GREETING": None},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json() == {"keys": ["API_TOKEN"]}
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    # PUT replaces the whole file.
    res = await client.put(
        "/api/v1/apps/heldso/env",
        json={"ONLY": "this"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json() == {"keys": ["ONLY"]}

    res = await client.get("/api/v1/apps/heldso/env", headers=auth(token))
    assert res.json() == {"ONLY": "this"}

    res = await client.patch(
        "/api/v1/apps/heldso/env",
        json={"1BAD": "x"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "bad_key" in res.text

    # Deleting the app removes the directory.
    assert (await client.delete("/api/v1/apps/heldso", headers=auth(token))).status_code == 200
    assert not env_file.exists()


async def test_app_deploy_reports_the_outcome(yard):
    client, token, _root = yard
    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://origin.cursor.com/org/heldso"},
        headers=auth(token),
    )
    assert res.status_code == 200
    res = await client.post("/api/v1/apps/heldso/deploy", headers=auth(token))
    assert res.status_code == 202
    body = res.json()
    assert body["deploy"]["app"] == "heldso"
    # YARD_SKIP_DEPLOY is set in the fixture, so the job ends "skipped".
    await asyncio.sleep(0.2)
    res = await client.get("/api/v1/apps/heldso/deploy", headers=auth(token))
    assert res.json()["deploy"]["status"] == "skipped"

    res = await client.post("/api/v1/apps/nope/deploy", headers=auth(token))
    assert res.status_code == 404


async def test_tokens(yard):
    client, admin, _root = yard
    res = await client.post("/api/v1/tokens", json={"name": "ada"}, headers=auth(admin))
    assert res.status_code == 200
    ada = res.json()["token"]
    res = await client.get("/api/v1/tokens", headers=auth(admin))
    names = {row["name"] for row in res.json()}
    assert names == {"admin", "ada"}
    assert (await client.get("/api/v1/apps", headers=auth(ada))).status_code == 200

    res = await client.delete("/api/v1/tokens/admin", headers=auth(admin))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"
    res = await client.delete("/api/v1/tokens/nobody", headers=auth(admin))
    assert res.status_code == 404

    res = await client.delete("/api/v1/tokens/ada", headers=auth(admin))
    assert res.status_code == 200
    assert (await client.get("/api/v1/apps", headers=auth(ada))).status_code == 401


async def test_podman_route_is_gone(yard):
    client, token, _root = yard
    res = await client.post("/api/v1/podman", json={"args": ["ps"]}, headers=auth(token))
    assert res.status_code == 404
    res = await client.post("/api/v1/podman", json={"args": ["ps"]})
    assert res.status_code == 404


async def test_prune(yard, monkeypatch):
    client, token, _root = yard

    async def fake_run(self, *args, **kw):
        if args and args[0] == "images":
            return "[]"
        return ""

    monkeypatch.setattr(Podman, "run", fake_run)
    res = await client.post("/api/v1/prune", json={"days": 0}, headers=auth(token))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_json"

    res = await client.post("/api/v1/prune", json={"days": 3, "dry": True}, headers=auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["days"] == 3
    assert body["dry"]
    assert body["images"] == []


async def test_caddy_site_round_trip(yard, monkeypatch):
    client, token, root = yard
    fake = _fake_caddy(monkeypatch)
    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://origin.cursor.com/org/heldso"},
        headers=auth(token),
    )
    assert res.status_code == 200

    res = await client.get("/api/v1/apps/heldso/caddy", headers=auth(token))
    assert (res.status_code, res.text) == (200, "")

    site = "heldso.example.com, www.heldso.example.com {\n\treverse_proxy heldso-app-1:8000\n}\n"
    res = await client.put("/api/v1/apps/heldso/caddy", content=site, headers=auth(token))
    assert res.status_code == 200
    assert res.json() == {"hosts": ["heldso.example.com", "www.heldso.example.com"]}
    assert (root / "apps" / "yard" / "caddy" / "config" / "sites" / "heldso.caddy").read_text() == site
    assert ("POST", "/load", b"import /etc/caddy/Caddyfile\n") in fake.calls

    res = await client.get("/api/v1/apps/heldso/caddy", headers=auth(token))
    assert res.text == site

    res = await client.put("/api/v1/apps/heldso/caddy", content="", headers=auth(token))
    assert res.json() == {"hosts": []}
    assert not (root / "apps" / "yard" / "caddy" / "config" / "sites" / "heldso.caddy").exists()


async def test_caddy_site_refusals(yard, monkeypatch):
    client, token, root = yard
    _fake_caddy(monkeypatch, refuse="/config/sites/heldso.caddy:2: unrecognized directive: nope")
    res = await client.put("/api/v1/apps/nope/caddy", content="a.example.com {\n}\n", headers=auth(token))
    assert res.status_code == 404
    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://origin.cursor.com/org/heldso"},
        headers=auth(token),
    )
    assert res.status_code == 200

    res = await client.put("/api/v1/apps/heldso/caddy", content="a.example.com {\n\tnope\n}\n", headers=auth(token))
    assert res.status_code == 400
    error = res.json()["error"]
    assert error["code"] == "bad_caddyfile"
    assert "heldso.caddy:2" in error["message"]
    assert not (root / "apps" / "yard" / "caddy" / "config" / "sites" / "heldso.caddy").exists()

    res = await client.put("/api/v1/apps/heldso/caddy", content=b"\xff\xfe", headers=auth(token))
    assert res.json()["error"]["code"] == "bad_caddyfile"
    res = await client.put("/api/v1/apps/heldso/caddy", content="#" * (64 << 10 | 1), headers=auth(token))
    assert res.json()["error"]["code"] == "too_large"


async def test_caddy_site_fails_when_caddy_is_down(yard):
    client, token, _root = yard
    await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://origin.cursor.com/org/heldso"},
        headers=auth(token),
    )
    res = await client.put("/api/v1/apps/heldso/caddy", content="a.example.com {\n}\n", headers=auth(token))
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "caddy"


async def test_origin_hook_status_codes(yard):
    client, _token, _root = yard
    payload = {
        "event": {
            "type": "repository.pushed",
            "payload": {"repository": {"owner": {"slug": "org"}, "name": "heldso"}},
        }
    }
    res = await client.post("/hooks/origin", content=json.dumps(payload))
    assert res.status_code == 204
    res = await client.post(
        "/hooks/origin",
        content=json.dumps({"event": {"type": "installation.created", "payload": {}}}),
    )
    assert res.status_code == 204
    res = await client.post(
        "/hooks/origin",
        content=json.dumps({"event": {"type": "something.else", "payload": {}}}),
    )
    assert res.status_code == 204

    res = await client.post("/hooks/origin", content="not json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_json"

    res = await client.post("/hooks/origin", content="x" * (1 << 20 | 1))
    assert res.status_code == 413


async def test_origin_hook_marks_the_delivery(yard, monkeypatch):
    """last_delivery shows in the doctor origin webhook row."""
    client, token, _root = yard
    monkeypatch.setattr(Origin, "connected", lambda self: True)

    async def fake_get_app(self):
        return {"displayName": "yard"}

    async def fake_find(self):
        return [{"id": "ins_1", "account": "org", "selection": "all"}]

    async def fake_token(self, repo: str = ""):
        return "tok"

    monkeypatch.setattr(Origin, "get_app", fake_get_app)
    monkeypatch.setattr(Origin, "find_installations", fake_find)
    monkeypatch.setattr(Origin, "token", fake_token)

    checks = {check["name"]: check for check in (await client.get("/api/v1/doctor", headers=auth(token))).json()}
    assert checks["origin webhook"]["detail"] == "none since boot"

    payload = {
        "event": {
            "type": "repository.pushed",
            "payload": {"repository": {"owner": {"slug": "org"}, "name": "heldso"}},
        }
    }
    assert (await client.post("/hooks/origin", content=json.dumps(payload))).status_code == 204
    await client.drain_tasks()
    checks = {check["name"]: check for check in (await client.get("/api/v1/doctor", headers=auth(token))).json()}
    assert checks["origin webhook"]["detail"] != "none since boot"


async def test_app_status_and_doctor(yard, monkeypatch):
    client, token, _root = yard

    async def fake_run(self, *args, timeout=None, env=None, stdin=None):
        if args and args[0] == "ps":
            labels = {"com.docker.compose.project": "heldso", "com.docker.compose.service": "app"}
            return json.dumps([{"Names": ["heldso-app-1"], "State": "running", "Labels": labels}])
        if args and args[0] == "stats":
            return "[]"
        if args and args[0] == "info":
            return '{"host":{"cpus":4,"memTotal":1000},"version":{"Version":"5.3.1"}}'
        if args and args[0] == "logs":
            return "2026-10-05T12:00:00Z one\n"
        return ""

    monkeypatch.setattr(Podman, "run", fake_run)
    _fake_caddy(monkeypatch)
    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://origin.cursor.com/org/heldso", "ref": "main"},
        headers=auth(token),
    )
    assert res.status_code == 200
    res = await client.put("/api/v1/apps/heldso/caddy", content="heldso.example.com {\n}\n", headers=auth(token))
    assert res.status_code == 200

    page = (await client.get("/api/v1/apps/heldso", headers=auth(token))).json()
    assert page["app"]["name"] == "heldso"
    assert "env" not in page["app"]
    assert page["hosts"] == ["heldso.example.com"]
    assert [row["container"] for row in page["rows"]] == ["heldso-app-1"]
    assert "one" in page["log"]
    res = await client.get("/api/v1/apps/nope", headers=auth(token))
    assert res.status_code == 404

    checks = {check["name"]: check for check in (await client.get("/api/v1/doctor", headers=auth(token))).json()}
    assert checks["podman"] == {"name": "podman", "ok": True, "detail": "podman 5.3.1", "warn": False}
    assert "caddy" not in checks
    assert checks["origin"]["ok"] is False


async def test_status_reports_the_forges(yard, monkeypatch):
    client, token, _root = yard

    async def fake_run(self, *args, timeout=None, env=None, stdin=None):
        if args and args[0] == "info":
            return '{"host":{"cpus":4,"memTotal":1000}}'
        return "[]"

    monkeypatch.setattr(Podman, "run", fake_run)
    res = await client.get("/api/v1/status", headers=auth(token))
    assert res.status_code == 200
    assert res.json()["host"]["forges"] == {"origin": False, "github": False}


async def test_github_hook(yard, monkeypatch):
    """The signature gates everything; ping and push are the known events."""
    client, token, root = yard
    secret = "w" * 64
    state = root / "apps" / "yard" / "data" / "github" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"webhook_secret": secret}))

    seen: list[str] = []

    async def _noop():
        return None

    monkeypatch.setattr(apps, "check_repo", lambda _yard, repo: seen.append(repo) or _noop())
    monkeypatch.setattr(apps, "check_installation", lambda _yard, _forge: seen.append("install") or _noop())

    def sig(body: str) -> str:
        digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    body = json.dumps({"zen": "keep it"})
    # Missing and bad signatures get a 401 and nothing runs.
    res = await client.post("/hooks/github", content=body, headers={"x-github-event": "ping"})
    assert res.status_code == 401
    res = await client.post(
        "/hooks/github",
        content=body,
        headers={"x-github-event": "ping", "x-hub-signature-256": "sha256=" + "0" * 64},
    )
    assert res.status_code == 401
    assert seen == []

    res = await client.post(
        "/hooks/github",
        content=body,
        headers={"x-github-event": "ping", "x-hub-signature-256": sig(body)},
    )
    assert res.status_code == 204
    assert seen == []

    push = json.dumps({"repository": {"full_name": "org/heldso"}})
    res = await client.post(
        "/hooks/github",
        content=push,
        headers={"x-github-event": "push", "x-hub-signature-256": sig(push)},
    )
    assert res.status_code == 204
    await client.drain_tasks()
    assert seen == ["https://github.com/org/heldso"]

    install = json.dumps({"action": "created"})
    res = await client.post(
        "/hooks/github",
        content=install,
        headers={"x-github-event": "installation", "x-hub-signature-256": sig(install)},
    )
    assert res.status_code == 204
    await client.drain_tasks()
    assert seen[-1] == "install"


async def test_github_hook_needs_a_secret(yard, monkeypatch):
    """A bad signature does not touch last_delivery; a good one does."""
    client, token, root = yard
    monkeypatch.setattr(GitHub, "connected", lambda self: True)

    async def fake_get_app(self):
        return {"name": "yard"}

    async def fake_find(self):
        return [{"id": "101", "account": "org", "selection": "all"}]

    async def fake_token(self, repo: str = ""):
        return "tok"

    monkeypatch.setattr(GitHub, "get_app", fake_get_app)
    monkeypatch.setattr(GitHub, "find_installations", fake_find)
    monkeypatch.setattr(GitHub, "token", fake_token)

    async def row() -> dict:
        found = (await client.get("/api/v1/doctor", headers=auth(token))).json()
        return {check["name"]: check for check in found}

    secret = "w" * 64
    state = root / "apps" / "yard" / "data" / "github" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"webhook_secret": secret}))

    res = await client.post(
        "/hooks/github",
        content=json.dumps({}),
        headers={"x-github-event": "ping", "x-hub-signature-256": "sha256=" + "0" * 64},
    )
    assert res.status_code == 401
    assert (await row())["github webhook"]["detail"] == "none since boot"

    body = json.dumps({})
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    res = await client.post(
        "/hooks/github",
        content=body,
        headers={"x-github-event": "ping", "x-hub-signature-256": f"sha256={digest}"},
    )
    assert res.status_code == 204
    assert (await row())["github webhook"]["detail"] != "none since boot"


async def test_forge_routes_redact_secrets(yard, monkeypatch):
    client, token, _root = yard
    res = await client.get("/api/v1/forges", headers=auth(token))
    assert res.status_code == 200
    rows = {row["name"]: row for row in res.json()}
    assert list(rows) == ["origin", "github"]
    assert all(not row["connected"] for row in rows.values())
    assert all("installations" in row for row in rows.values())
    # The public key is a summary field; first read creates the keypair.
    key = rows["origin"]["public_key"]
    assert key.startswith("-----BEGIN PUBLIC KEY-----")
    assert "webhook_secret" not in res.text and "PRIVATE" not in res.text

    secret = "w" * 64
    pem = "-----BEGIN RSA PRIVATE KEY-----\nSECRET-PEM\n-----END RSA PRIVATE KEY-----\n"

    async def fake_connect(self, app_id, private_key, webhook_secret):
        return {"name": "yard-host", "slug": "yard-host", "events": ["push"]}

    monkeypatch.setattr(GitHub, "connect", fake_connect)
    res = await client.put(
        "/api/v1/forges/github",
        json={"app_id": "42", "private_key": pem, "webhook_secret": secret},
        headers=auth(token),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["app"] == {"name": "yard-host", "slug": "yard-host", "events": ["push"]}
    # The key and the secret went in; they do not come back out.
    assert "SECRET-PEM" not in res.text
    assert secret not in res.text
    assert body["forge"]["name"] == "github"


async def test_forge_origin_key_and_put(yard, monkeypatch):
    client, token, _root = yard
    # The first read of the summary creates the keypair, idempotently.
    rows = (await client.get("/api/v1/forges", headers=auth(token))).json()
    key = next(row["public_key"] for row in rows if row["name"] == "origin")
    assert key.startswith("-----BEGIN PUBLIC KEY-----")
    rows = (await client.get("/api/v1/forges", headers=auth(token))).json()
    assert next(row["public_key"] for row in rows if row["name"] == "origin") == key

    async def fake_connect(self, app_id):
        self.save(app_id=app_id)
        return {
            "displayName": "yard",
            "webhookUrl": "https://yard.example.com/hooks/origin",
            "events": ["repository.pushed"],
        }

    monkeypatch.setattr(Origin, "connect", fake_connect)
    res = await client.put("/api/v1/forges/origin", json={"app_id": "app_1"}, headers=auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["forge"]["app_id"] == "app_1"
    assert body["app"]["name"] == "yard"


async def test_forges_find_the_installation_on_read(yard, monkeypatch):
    client, token, root = yard
    github = next(forge for forge in _yard_forges(root) if forge.name == "github")
    github.save(app_id="42")

    async def missing(self):
        return []

    monkeypatch.setattr(GitHub, "find_installations", missing)
    rows = (await client.get("/api/v1/forges", headers=auth(token))).json()
    found = next(row for row in rows if row["name"] == "github")
    assert found["installations"] == []

    async def found_one(self):
        self.save(installations=[{"id": "101", "account": "org", "selection": "all"}])
        return [{"id": "101", "account": "org", "selection": "all"}]

    monkeypatch.setattr(GitHub, "find_installations", found_one)
    rows = (await client.get("/api/v1/forges", headers=auth(token))).json()
    found = next(row for row in rows if row["name"] == "github")
    assert found["installations"] == [
        {
            "id": "101",
            "account": "org",
            "selection": "all",
            "settings_url": "https://github.com/settings/installations/101",
        }
    ]
    state = json.loads((root / "apps" / "yard" / "data" / "github" / "state.json").read_text())
    assert state["installations"] == [{"id": "101", "account": "org", "selection": "all"}]


async def test_forges_swallow_installation_errors(yard, monkeypatch):
    client, token, root = yard
    github = next(forge for forge in _yard_forges(root) if forge.name == "github")
    github.save(app_id="42")

    async def broken(self):
        raise YardError(502, "github", "github GET /app/installations: refused")

    monkeypatch.setattr(GitHub, "find_installations", broken)
    res = await client.get("/api/v1/forges", headers=auth(token))
    assert res.status_code == 200
    found = next(row for row in res.json() if row["name"] == "github")
    assert found["error"] == "github GET /app/installations: refused"


async def test_installation_hook_keeps_every_account(yard, monkeypatch):
    """A second installation on another account must not evict the first."""
    client, token, root = yard
    secret = "w" * 64
    github = next(forge for forge in _yard_forges(root) if forge.name == "github")
    github.save(
        app_id="42",
        webhook_secret=secret,
        installations=[{"id": "101", "account": "Bobowski", "selection": "all"}],
    )

    async def fake_call(self, method, path, bearer, body=None):
        assert path.startswith("/app/installations")
        return [
            {"id": 202, "account": {"login": "stariodev"}, "repository_selection": "all"},
            {"id": 101, "account": {"login": "Bobowski"}, "repository_selection": "all"},
        ]

    monkeypatch.setattr(GitHub, "app_jwt", lambda self: "jwt")
    monkeypatch.setattr(GitHub, "_call", fake_call)

    body = json.dumps({"action": "created"})
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    res = await client.post(
        "/hooks/github",
        content=body,
        headers={"x-github-event": "installation", "x-hub-signature-256": f"sha256={digest}"},
    )
    assert res.status_code == 204
    await client.drain_tasks()
    state = json.loads((root / "apps" / "yard" / "data" / "github" / "state.json").read_text())
    assert state["installations"] == [
        {"account": "stariodev", "id": "202", "selection": "all", "type": ""},
        {"account": "Bobowski", "id": "101", "selection": "all", "type": ""},
    ]
    assert "installation_id" not in state
    # Both accounts keep an installation.
    assert github.installation_for("https://github.com/Bobowski/yard")
    assert github.installation_for("https://github.com/stariodev/yard")


def _yard_forges(root):
    """Reach the running app's forge objects through its data dir."""
    from app.server.forges import GitHub, Origin

    return (Origin(root / "apps" / "yard" / "data" / "origin"), GitHub(root / "apps" / "yard" / "data" / "github"))


async def test_events_log_actors_and_never_secrets(yard, capsys):
    """No journal socket under tests: events land on stdout as JSON."""
    client, token, _root = yard
    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://origin.cursor.com/org/app"},
        headers=auth(token),
    )
    assert res.status_code == 200
    res = await client.patch(
        "/api/v1/apps/heldso/env",
        json={"PASSWORD": "hunter2"},
        headers=auth(token),
    )
    assert res.status_code == 200
    out = capsys.readouterr().out
    assert "hunter2" not in out
    rows = [json.loads(line) for line in out.splitlines() if '"yard"' in line]
    create = next(row for row in rows if row["yard"] == "app.create")
    assert create["actor"] == "admin" and create["app"] == "heldso"
    env = next(row for row in rows if row["yard"] == "env.set")
    assert env["keys"] == "PASSWORD" and env["actor"] == "admin"


async def test_app_unknown_fields_are_400(yard):
    client, token, _root = yard
    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://github.com/org/heldso", "containerfile": "Dockerfile"},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "unknown_field"
    assert "containerfile" in res.text

    res = await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://github.com/org/heldso", "runs": "ops/stack.yml"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json()["app"]["runs"] == "ops/stack.yml"
    assert res.json()["app"]["deployed"] is None

    res = await client.patch("/api/v1/apps/heldso", json={"compose": "x"}, headers=auth(token))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "unknown_field"


async def test_app_files_route(yard, tmp_path):
    client, token, _root = yard
    await client.post(
        "/api/v1/apps",
        json={"name": "heldso", "repo": "https://github.com/org/heldso", "runs": "compose.yml"},
        headers=auth(token),
    )
    res = await client.get("/api/v1/apps/heldso/files/compose", headers=auth(token))
    assert res.status_code == 404

    from app.server.store import Deployed, write_deployed

    clone = tmp_path / "apps" / "heldso" / "repo"
    clone.mkdir(parents=True)
    (clone / "compose.yml").write_text("services: {}\n")
    write_deployed(_config(tmp_path), "heldso", Deployed(revision="abc", runs="compose.yml", kind="compose"))
    res = await client.get("/api/v1/apps/heldso/files/compose", headers=auth(token))
    assert res.status_code == 200
    assert res.text == "services: {}\n"

    res = await client.get("/api/v1/apps/heldso/files/containerfile", headers=auth(token))
    assert res.status_code == 404
    assert "compose file" in res.text


def _config(tmp_path):
    from app.server.store import Config

    return Config(
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


async def test_app_files_the_yard_files(yard, tmp_path):
    client, token, _root = yard
    await client.post(
        "/api/v1/apps",
        json={"name": "db", "runs": "postgres:17"},
        headers=auth(token),
    )
    res = await client.get("/api/v1/apps/db/files/containerfile", headers=auth(token))
    assert res.status_code == 404
    write_dep(tmp_path, "db", "image")
    res = await client.get("/api/v1/apps/db/files/containerfile", headers=auth(token))
    assert res.status_code == 404
    assert "runs an image" in res.text
    res = await client.get("/api/v1/apps/db/files/compose", headers=auth(token))
    assert res.status_code == 200
    assert "# app.compose.yml" in res.text and "image: ${IMAGE}" in res.text


def write_dep(tmp_path, name, kind):
    from app.server.store import Deployed, write_deployed

    write_deployed(_config(tmp_path), name, Deployed(revision="sha256:" + "a" * 64, runs="postgres:17", kind=kind))


async def test_deploy_body_with_sha_is_400(yard):
    client, token, _root = yard
    await client.post("/api/v1/apps", json={"name": "db", "runs": "postgres:17"}, headers=auth(token))
    res = await client.post("/api/v1/apps/db/deploy", json={"sha": "abc1234"}, headers=auth(token))
    assert res.status_code == 400
    assert "deploys what the source says" in res.text


async def test_patch_repo_on_an_image_app_is_400(yard):
    client, token, _root = yard
    await client.post("/api/v1/apps", json={"name": "db", "runs": "postgres:17"}, headers=auth(token))
    res = await client.patch("/api/v1/apps/db", json={"repo": "https://github.com/org/db"}, headers=auth(token))
    assert res.status_code == 400
    assert "runs a file from it" in res.text


async def test_page_record_is_the_record():
    """app on the status page is the record; the running image and sha are top level."""
    from app.server.api import page_json
    from app.server.status import Page
    from app.server.store import App

    page = Page(
        app=App("heldso", "https://github.com/org/heldso", "main", "push", "t", "ops/Dockerfile"),
        image="localhost/heldso:abc1234",
        sha="abc1234",
        env_file="/env",
        rows=[],
        hosts=[],
        deploys=[],
        log="",
    )
    data = page_json(page)
    assert data["app"]["runs"] == "ops/Dockerfile"
    assert data["app"]["deployed"] is None
    assert data["image"] == "localhost/heldso:abc1234"
    assert data["sha"] == "abc1234"


async def test_deploy_is_a_job(yard):
    client, token, _root = yard
    await client.post("/api/v1/apps", json={"name": "db", "runs": "postgres:17"}, headers=auth(token))
    res = await client.post("/api/v1/apps/db/deploy", json={}, headers=auth(token))
    assert res.status_code == 202
    job = res.json()["deploy"]
    assert job["app"] == "db" and job["cause"] == "manual"
    fields = {
        "id",
        "app",
        "status",
        "cause",
        "actor",
        "started",
        "ended",
        "image",
        "error",
        "previous",
        "port",
    }
    assert fields <= set(job)
    res = await client.post("/api/v1/apps/db/deploy", json={"sha": "abc"}, headers=auth(token))
    assert res.status_code == 400 and "Yard deploys what the source says now" in res.text
    res = await client.post("/api/v1/apps/db/deploy", json={"other": 1}, headers=auth(token))
    assert res.status_code == 400 and res.json()["error"]["code"] == "unknown_field"


async def test_deploy_get_and_follow(yard):
    client, token, _root = yard
    res = await client.get("/api/v1/apps/db/deploy", headers=auth(token))
    assert res.status_code == 404
    await client.post("/api/v1/apps", json={"name": "db", "runs": "postgres:17"}, headers=auth(token))
    await client.post("/api/v1/apps/db/deploy", json={}, headers=auth(token))
    await asyncio.sleep(0.2)
    res = await client.get("/api/v1/apps/db/deploy", headers=auth(token))
    job = res.json()["deploy"]
    assert job["status"] == "skipped"
    assert "lines" in job
    text = (
        await client.get("/api/v1/apps/db/deploy?follow=1", headers={**auth(token), "accept-encoding": "identity"})
    ).text
    rows = [json.loads(line) for line in text.splitlines() if line]
    assert all("line" in row or "deploy" in row for row in rows)
    assert rows[-1]["deploy"]["status"] == "skipped"


async def test_deploy_second_post_returns_the_running_job(yard, monkeypatch):
    client, token, _root = yard
    await client.post("/api/v1/apps", json={"name": "db", "runs": "postgres:17"}, headers=auth(token))
    gate = asyncio.Event()

    async def slow(yard_, name, **kw):
        await gate.wait()

    monkeypatch.setattr(apps, "_finish", slow)
    first = await client.post("/api/v1/apps/db/deploy", json={}, headers=auth(token))
    second = await client.post("/api/v1/apps/db/deploy", json={}, headers=auth(token))
    gate.set()
    assert first.json()["deploy"]["id"] == second.json()["deploy"]["id"]
    assert second.json()["deploy"]["status"] == "running"


def _gh_state(root, installations: list) -> None:
    state = root / "apps" / "yard" / "data" / "github" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"app_id": "42", "installations": installations}))


def _journal_event(out: str, name: str) -> dict:
    return next(json.loads(line) for line in out.splitlines() if line.startswith("{") and f'"{name}"' in line)


async def test_repo_create(yard, monkeypatch, capsys):
    client, token, root = yard
    monkeypatch.setattr(GitHub, "connected", lambda self: True)
    made: list[tuple[str, str]] = []

    async def fake_create(self, owner, name):
        made.append((owner, name))
        return f"https://github.com/{owner}/{name}"

    monkeypatch.setattr(GitHub, "create_repo", fake_create)
    _gh_state(root, [{"id": "101", "account": "org", "selection": "all", "type": "Organization"}])
    res = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert res.status_code == 200
    assert res.json() == {"repo": "https://github.com/org/heldso"}
    # The one connected forge and its one installation are the defaults.
    assert made == [("org", "heldso")]
    event = _journal_event(capsys.readouterr().out, "repo.create")
    assert event["repo"] == "https://github.com/org/heldso"
    assert event["forge"] == "github"
    assert event["actor"] == "admin"


async def test_repo_create_checks_the_body(yard):
    client, token, _root = yard
    res = await client.post("/api/v1/repos", json={"name": "heldso", "owner": 42}, headers=auth(token))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_json"
    res = await client.post("/api/v1/repos", json={"name": "heldso"})
    assert res.status_code == 401


async def test_repo_create_rejects_a_bad_name(yard, monkeypatch):
    client, token, _root = yard
    monkeypatch.setattr(GitHub, "connected", lambda self: True)
    res = await client.post("/api/v1/repos", json={"name": "Bad Name"}, headers=auth(token))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_name"


async def test_repo_create_needs_a_connected_forge(yard):
    client, token, _root = yard
    res = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert res.status_code == 502
    assert "no forge is connected" in res.text
    res = await client.post("/api/v1/repos", json={"name": "heldso", "forge": "github"}, headers=auth(token))
    assert res.status_code == 502
    assert "github is not connected" in res.text


async def test_repo_create_needs_the_forge_when_two_are_connected(yard, monkeypatch):
    client, token, _root = yard
    monkeypatch.setattr(GitHub, "connected", lambda self: True)
    monkeypatch.setattr(Origin, "connected", lambda self: True)
    res = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "forge_required"


async def test_repo_create_needs_an_owner_when_accounts_differ(yard, monkeypatch):
    client, token, root = yard
    monkeypatch.setattr(GitHub, "connected", lambda self: True)
    _gh_state(
        root,
        [
            {"id": "101", "account": "Bobowski", "selection": "all", "type": "User"},
            {"id": "202", "account": "stariodev", "selection": "all", "type": "Organization"},
        ],
    )
    res = await client.post("/api/v1/repos", json={"name": "heldso"}, headers=auth(token))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "owner_required"
    assert "Bobowski" in res.text and "stariodev" in res.text


async def test_repo_create_origin(yard, monkeypatch):
    client, token, _root = yard
    monkeypatch.setattr(GitHub, "connected", lambda self: True)
    monkeypatch.setattr(Origin, "connected", lambda self: True)

    async def fake_create(self, owner, name):
        return f"https://origin.cursor.com/{owner}/{name}"

    monkeypatch.setattr(Origin, "create_repo", fake_create)
    res = await client.post(
        "/api/v1/repos",
        json={"name": "heldso", "owner": "org", "forge": "origin"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json() == {"repo": "https://origin.cursor.com/org/heldso"}


async def test_repo_token(yard, monkeypatch, capsys):
    client, token, _root = yard
    monkeypatch.setattr(GitHub, "connected", lambda self: True)
    asked: list[str] = []

    async def fake_repo_token(self, repo):
        asked.append(repo)
        return "ghs_secret", "2026-10-30T13:00:00Z"

    monkeypatch.setattr(GitHub, "repo_token", fake_repo_token)
    res = await client.post(
        "/api/v1/repos/token",
        json={"repo": "https://github.com/org/heldso.git"},
        headers=auth(token),
    )
    assert res.status_code == 200
    assert res.json() == {"username": "x-access-token", "token": "ghs_secret", "expires_at": "2026-10-30T13:00:00Z"}
    assert asked == ["https://github.com/org/heldso"]
    out = capsys.readouterr().out
    event = _journal_event(out, "repo.token")
    assert event["repo"] == "https://github.com/org/heldso"
    assert event["actor"] == "admin"
    # The journal never carries the token.
    assert "ghs_secret" not in out


async def test_repo_token_checks_the_repo(yard):
    client, token, _root = yard
    res = await client.post("/api/v1/repos/token", json={"repo": 42}, headers=auth(token))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_json"
    res = await client.post("/api/v1/repos/token", json={"repo": "https://example.com/a/b"}, headers=auth(token))
    assert res.status_code == 400
    res = await client.post("/api/v1/repos/token", json={"repo": "https://github.com/org/heldso"}, headers=auth(token))
    assert res.status_code == 502
    res = await client.post("/api/v1/repos/token", json={"repo": "https://github.com/org/heldso"})
    assert res.status_code == 401
