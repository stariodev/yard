"""Apps: records, deploys, checks and their debounce, the migration, and prune. Adapters stay stubbed."""

import asyncio
import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from app.server import apps, compose, git, journal, sources
from app.server.forges import Forge, GitHub, Origin
from app.server.podman import Podman
from app.server.store import Config, Deployed, Store, read_deployed
from app.server.yard import Yard
from app.shared import YardError
from tests.server.conftest import FakeCaddy, make_yard


def _clone(yard: Yard, name: str) -> Path:
    """A clone dir with one commit. The HEAD is the deploy sha."""
    root = yard.config.clone(name)
    root.mkdir(parents=True)
    (root / "Containerfile").write_text("FROM scratch\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Yard",
        "GIT_AUTHOR_EMAIL": "yard@example.com",
        "GIT_COMMITTER_NAME": "Yard",
        "GIT_COMMITTER_EMAIL": "yard@example.com",
    }
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "one"], check=True, capture_output=True, env=env)
    return root


OK_LOG = "== build localhost/heldso:abc\nbuilt 2026-10-05T10:00:00Z\n== started localhost/heldso:abc\n"


class _Apply:
    """Stands in for compose.apply. Records the call and returns (code, log)."""

    def __init__(self, code: int = 0, text: str = OK_LOG) -> None:
        self.calls: list[dict] = []
        self.code = code
        self.text = text

    async def __call__(self, config, app, command, project, *, detach=False, on_line=None) -> tuple[int, str]:
        self.calls.append({"command": command, "app": app, "project": project, "detach": detach})
        if on_line is not None:
            for line in self.text.splitlines():
                on_line(line)
        return self.code, self.text


def _connect(monkeypatch: pytest.MonkeyPatch, token: str = "oit_x") -> None:
    """Every forge connected and owning every repo, with a canned token."""
    monkeypatch.setattr(Forge, "connected", lambda self: True)
    monkeypatch.setattr(Forge, "owns", lambda self, repo: True)

    async def fake_token(self, repo: str = ""):
        return token

    monkeypatch.setattr(Forge, "token", fake_token)


def _deploy_yard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "heldso", **kw):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app(name, "https://origin.cursor.com/org/app")
    clone = _clone(yard, name)
    yard.podman = _Podman(**kw)
    _connect(monkeypatch)
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_sync(_clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    return yard, recorder


async def test_deploy_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, recorder = _deploy_yard(tmp_path, monkeypatch)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    assert outcome.image.startswith("localhost/heldso:")
    assert recorder.calls[0]["command"] == "up"
    deployed = read_deployed(yard.config, "heldso")
    assert deployed is not None
    assert deployed.revision == git.head(yard.config.clone("heldso"))
    assert deployed.kind == "containerfile" and deployed.runs == "Containerfile"


async def test_deploy_apply_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    _clone(yard, "heldso")
    _connect(monkeypatch)
    monkeypatch.setattr(compose, "apply", _Apply(code=1, text="build failed\n"))

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "failed"
    assert "apply exited 1" in outcome.error
    assert read_deployed(yard.config, "heldso") is None


async def test_deploy_yard_is_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, recorder = _deploy_yard(tmp_path, monkeypatch, name="yard")
    outcome = await apps.deploy(yard, "yard")
    assert outcome.status == "pending"
    assert recorder.calls[0]["detach"] is True


async def test_deploy_needs_a_repo(tmp_path: Path):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("yard", "")
    outcome = await apps.deploy(yard, "yard")
    assert outcome.status == "failed"
    assert "yard app set --repo" in "\n".join(yard.deploys["yard"].lines)


async def test_deploy_needs_a_forge(tmp_path: Path):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "failed"
    assert outcome.error == "origin is not connected. Run yard forge setup origin."


async def test_deploy_skips(tmp_path: Path):
    yard = make_yard(tmp_path, skip=True)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "skipped"


async def test_deploy_missing_app(tmp_path: Path):
    yard = make_yard(tmp_path)
    outcome = await apps.deploy(yard, "ghost")
    assert outcome.status == "failed"


async def test_deploy_reverted_when_the_gate_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    _clone(yard, "heldso")
    _connect(monkeypatch)
    monkeypatch.setattr(compose, "apply", _Apply(code=3, text="== revert localhost/heldso:old\n"))

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "reverted"


async def test_deploy_emits_start_and_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_yard(tmp_path, monkeypatch)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(journal, "send", lambda _c, event, _m, **kw: sent.append((event, kw)))
    outcome = await apps.deploy(yard, "heldso", cause="push", actor="webhook:origin")
    assert outcome.status == "ok"
    start = next(kw for event, kw in sent if event == "deploy.start")
    assert start["cause"] == "push" and start["actor"] == "webhook:origin"
    assert start["runs"] == "Containerfile" and start["kind"] == "containerfile"
    end = next(kw for event, kw in sent if event == "deploy.end")
    assert end["status"] == "ok" and end["container"] == "deploy-heldso"


async def test_auto_guard_pins_a_moved_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A deploy whose auto answer moves from a template to compose.yml fails with the pin message."""
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    clone = _stario_clone(yard, "heldso")
    _connect(monkeypatch)
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_sync(_c, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    found_dep = read_deployed(yard.config, "heldso")
    assert found_dep is not None and found_dep.runs == "yard:python-uv"
    # The repo grows a compose.yml: auto would switch kinds. Refuse and ask for a pin.
    (clone / "compose.yml").write_text("services: {}\n")
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "failed"
    assert "auto found compose.yml (compose); the last deploy ran yard:python-uv (containerfile)" in outcome.error
    assert "yard app set --runs compose.yml" in outcome.error
    assert not recorder.calls[1:]  # the guard fails before apply


async def test_first_deploy_writes_the_template_containerfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, recorder = _deploy_yard(tmp_path, monkeypatch)
    clone = _stario_clone(yard, "heldso")
    (clone / "Containerfile").unlink()

    async def fake_sync(_c, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    deployed = read_deployed(yard.config, "heldso")
    assert deployed is not None
    assert deployed.runs == "yard:python-uv" and deployed.template
    stored = (yard.config.app_dir("heldso") / "Containerfile").read_text()
    assert stored.startswith("# Written by Yard")
    assert recorder.calls[0]["project"].env["DOCKERFILE"] == str(yard.config.app_dir("heldso") / "Containerfile")
    assert f"-{deployed.template}" in outcome.image


async def test_template_image_tag_carries_the_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, recorder = _deploy_yard(tmp_path, monkeypatch)
    clone = _stario_clone(yard, "heldso")
    (clone / "Containerfile").unlink()

    async def fake_sync(_c, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    sha12 = git.head(clone)[:12]
    deployed = read_deployed(yard.config, "heldso")
    assert deployed is not None
    assert outcome.image == f"localhost/heldso:{sha12}-{deployed.template}"


async def test_deploy_runs_path_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", runs="deploy/x.yml")
    _clone(yard, "heldso")
    _connect(monkeypatch)
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "failed"
    assert "deploy/x.yml is not a file" in outcome.error


async def test_deploy_image_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", runs="postgres:17")
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_digest(ref):
        return "sha256:" + "d" * 64

    monkeypatch.setattr(sources, "digest", fake_digest)
    outcome = await apps.deploy(yard, "db")
    assert outcome.status == "ok"
    found = recorder.calls[0]["project"]
    assert found.files == [compose.APP_COMPOSE]
    assert found.env["IMAGE"] == f"postgres:17@sha256:{'d' * 64}"
    assert found.env["SKIP_BUILD"] == "1"
    deployed = read_deployed(yard.config, "db")
    assert deployed is not None
    assert deployed.revision == "sha256:" + "d" * 64
    assert deployed.runs == "postgres:17" and deployed.kind == "image"


async def test_deploy_image_registry_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", runs="postgres:17")
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fake_digest(ref):
        raise YardError(502, "registry", "postgres:17: not found")

    monkeypatch.setattr(sources, "digest", fake_digest)
    outcome = await apps.deploy(yard, "db")
    assert outcome.status == "failed"
    assert "not found" in outcome.error
    assert read_deployed(yard.config, "db") is None


URL_BODY = b"services:\n  app:\n    image: x\n    networks:\n      - yard\n"


async def test_deploy_url_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("umami", "", runs="https://example.com/umami.yml")
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_fetch(url):
        return URL_BODY

    monkeypatch.setattr(sources, "fetch_file", fake_fetch)
    outcome = await apps.deploy(yard, "umami")
    assert outcome.status == "ok"
    revision = "sha256:" + hashlib.sha256(URL_BODY).hexdigest()
    copy = tmp_path / "apps" / "umami" / "compose" / f"{revision[7:19]}.yml"
    assert copy.is_file()
    found = recorder.calls[0]["project"]
    assert found.files == [str(copy)]
    assert found.env["SKIP_BUILD"] == "1"
    assert "PREVIOUS_COMPOSE_FILE" not in found.env
    deployed = read_deployed(yard.config, "umami")
    assert deployed is not None
    assert deployed.revision == revision and deployed.kind == "compose"

    outcome = await apps.deploy(yard, "umami")
    assert outcome.status == "ok"
    assert recorder.calls[1]["project"].env["PREVIOUS_COMPOSE_FILE"] == str(copy)


async def test_deploy_url_refuses_a_containerfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("umami", "", runs="https://example.com/umami.yml")
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fake_fetch(url):
        return b"FROM scratch\n"

    monkeypatch.setattr(sources, "fetch_file", fake_fetch)
    outcome = await apps.deploy(yard, "umami")
    assert outcome.status == "failed"
    assert "a Containerfile needs a repo to build" in outcome.error


async def test_deploy_url_refuses_a_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("umami", "", runs="https://example.com/umami.yml")
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fake_fetch(url):
        return b"services:\n  app:\n    build: .\n"

    monkeypatch.setattr(sources, "fetch_file", fake_fetch)
    outcome = await apps.deploy(yard, "umami")
    assert outcome.status == "failed"
    assert "cannot build" in outcome.error


async def test_deploy_reads_the_exposed_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", runs="postgres:17")
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fake_digest(ref):
        return "sha256:" + "d" * 64

    async def fake_run(*args, **kw):
        if args[:1] == ("inspect",):
            return '[{"Config": {"ExposedPorts": {"5432/tcp": {}}}}]'
        return "[]"

    monkeypatch.setattr(sources, "digest", fake_digest)
    yard.podman = _InspectPodman(fake_run)
    outcome = await apps.deploy(yard, "db")
    assert outcome.status == "ok"
    assert outcome.port == 5432


class _InspectPodman(Podman):
    def __init__(self, run):
        super().__init__("podman", False)
        self._run = run

    async def run(self, *args, **kw):
        return await self._run(*args, **kw)


def test_parse_port():
    assert podman_port('[{"Config": {"ExposedPorts": {"3001/tcp": {}}}}]') == 3001
    assert podman_port('[{"Config": {"ExposedPorts": {"3001/tcp": {}, "80/tcp": {}}}}]') == 0
    assert podman_port("[]") == 0


def podman_port(raw: str) -> int:
    from app.server import podman

    return podman.parse_port(raw)


PS_HELDSO = (
    '[{"Names":["heldso-app-1"],"State":"running","Image":"localhost/heldso:abc",'
    '"Labels":{"com.docker.compose.project":"heldso","com.docker.compose.service":"app"}}]'
)


def _delete_yard(tmp_path: Path, *, ps: str = "[]") -> tuple[Yard, FakeCaddy]:
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    yard.podman = _Podman(ps=ps)
    caddy = FakeCaddy()
    yard.caddy = caddy
    return yard, caddy


async def test_delete_refuses_the_yard_app(tmp_path: Path):
    yard = make_yard(tmp_path)
    yard.store.create_app("yard", "")
    with pytest.raises(YardError) as caught:
        await apps.delete(yard, "yard")
    assert caught.value.status == 400
    assert caught.value.code == "forbidden"


async def test_delete_removes_the_site_first(tmp_path: Path):
    yard, caddy = _delete_yard(tmp_path, ps=PS_HELDSO)
    yard.config.site("heldso").parent.mkdir(parents=True)
    yard.config.site("heldso").write_text("heldso.example.com {\n}\n")
    yard.config.site("other").write_text("other.example.com {\n}\n")
    removed = await apps.delete(yard, "heldso")
    assert removed.name == "heldso"
    assert removed.hosts == ["heldso.example.com"]
    assert removed.caddy_error == ""
    assert not yard.config.site("heldso").exists()
    assert yard.config.site("other").exists()
    assert caddy.loads == 1


async def test_delete_keeps_going_when_caddy_refuses(tmp_path: Path):
    yard, _caddy = _delete_yard(tmp_path, ps=PS_HELDSO)
    yard.config.site("heldso").parent.mkdir(parents=True)
    yard.config.site("heldso").write_text("heldso.example.com {\n}\n")
    yard.caddy = FakeCaddy(refuse="caddy POST /load: no socket")
    removed = await apps.delete(yard, "heldso")
    assert "no socket" in removed.caddy_error
    assert yard.store.list_apps() == []


async def test_delete_purges_from_the_deployed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", runs="postgres:17")
    from app.server.store import write_deployed

    write_deployed(yard.config, "db", Deployed(revision="sha256:" + "a" * 64, runs="postgres:17", kind="image"))
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)
    await apps.delete(yard, "db")
    assert recorder.calls and recorder.calls[0]["command"] == "purge"
    assert recorder.calls[0]["project"].files == [compose.APP_COMPOSE]


class _Podman(Podman):
    """ps and `image exists` answers for the deploy."""

    def __init__(self, ps: str = "[]", has_image: bool = False) -> None:
        super().__init__("podman", False)
        self.ps = ps
        self.has_image = has_image

    async def run(self, *args: str, timeout=None, env=None, stdin: bytes | None = None) -> str:
        if args[:1] == ("ps",):
            return self.ps
        if args[:2] == ("image", "exists"):
            if not self.has_image:
                raise YardError(502, "podman", "podman image exists: no such image")
            return ""
        return "[]"


def _deploy_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "heldso", apply: _Apply | None = None, **kw
) -> tuple[Yard, _Apply]:
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app(name, "https://github.com/org/app")
    clone = _clone(yard, name)
    yard.podman = _Podman(**kw)
    _connect(monkeypatch)
    recorder = apply or _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_sync(_clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    return yard, recorder


async def test_a_yard_self_deploy_stays_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_github(tmp_path, monkeypatch, name="yard")
    outcome = await apps.deploy(yard, "yard")
    assert outcome.status == "pending"


async def test_deploy_reuses_a_local_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, recorder = _deploy_yard(tmp_path, monkeypatch, has_image=True)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    assert recorder.calls[0]["project"].env["SKIP_BUILD"] == "1"


async def test_deploy_without_the_image_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, recorder = _deploy_yard(tmp_path, monkeypatch, has_image=False)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    assert "SKIP_BUILD" not in recorder.calls[0]["project"].env


NOW = time.time()

IMAGES = json.dumps(
    [
        {"Names": ["localhost/app:ne11"], "Created": int(NOW - 30 * 86400), "Size": 100},
        {"Names": ["localhost/app:0ld"], "Created": int(NOW - 30 * 86400), "Size": 110},
        {"Names": ["localhost/app:st4le"], "Created": int(NOW - 30 * 86400), "Size": 400},
        {"Names": ["localhost/app:fr3sh"], "Created": int(NOW - 2 * 86400), "Size": 500},
        {"Names": ["localhost/busy:t4g"], "Created": int(NOW - 30 * 86400), "Size": 600},
        {"Names": ["docker.io/library/caddy:2"], "Created": int(NOW - 30 * 86400), "Size": 700},
        {"Names": ["localhost/yard-apply"], "Created": int(NOW - 30 * 86400), "Size": 800},
    ]
)


def _yard(tmp_path: Path) -> Yard:
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
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    return yard


class _Fake(Podman):
    def __init__(self) -> None:
        super().__init__("podman", False)
        self.removed: list[str] = []

    async def run(self, *args: str, timeout=None, env=None, stdin: bytes | None = None) -> str:
        if args[:1] == ("ps",):
            return (
                '[{"Names":["heldso-app-1"],"State":"running","Image":"localhost/app:ne11",'
                '"Labels":{"com.docker.compose.project":"heldso","com.docker.compose.service":"app"}}]'
            )
        if args[:1] == ("images",):
            return IMAGES
        if args[:1] == ("rmi",):
            self.removed.append(args[1])
            if args[1] == "localhost/busy:t4g":
                raise YardError(502, "podman", "image is in use")
            return ""
        return "[]"


def test_prune_removes_what_no_app_needs(tmp_path: Path):
    yard = _yard(tmp_path)
    fake = _Fake()
    yard.podman = fake
    found = asdict(asyncio.run(apps.prune(yard, 7, False)))
    images = {row["tag"]: row["kept"] for row in found["images"]}
    assert set(images) == {"localhost/app:0ld", "localhost/app:st4le", "localhost/busy:t4g"}
    assert fake.removed == ["localhost/app:0ld", "localhost/app:st4le", "localhost/busy:t4g"]
    assert found["freed"] == 110 + 400


def test_prune_dry_run_removes_nothing(tmp_path: Path):
    yard = _yard(tmp_path)
    yard.podman = _Fake()
    found = asdict(asyncio.run(apps.prune(yard, 7, True)))
    assert found["dry"]


def _stario_clone(yard: Yard, name: str) -> Path:
    clone = yard.config.clone(name)
    clone.mkdir(parents=True, exist_ok=True)
    (clone / "pyproject.toml").write_text('[project]\ndependencies = ["stario>=5"]\n')
    (clone / "uv.lock").write_text("lock")
    (clone / "app").mkdir()
    (clone / "app" / "main.py").write_text("")
    subprocess.run(["git", "-C", str(clone), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "one"],
        check=True,
        capture_output=True,
    )
    return clone


async def test_ensure_host_marks_the_boot_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A detached yard deploy would loop forever without the boot stamp."""
    yard = make_yard(tmp_path)
    yard.config = replace(yard.config, sha="c" * 40)
    clone = _clone(yard, "yard")
    (clone / "ops").mkdir()
    (clone / "ops" / "compose.yml").write_text("services: {}\n")
    apps.ensure_host(yard)
    deployed = read_deployed(yard.config, "yard")
    assert deployed is not None
    assert deployed.revision == "c" * 40
    assert deployed.runs == "ops/compose.yml" and deployed.kind == "compose"
    assert deployed.template == ""  # a compose answer never carries a template


async def test_check_forge_mints_a_token_per_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two accounts on one forge each get their owner's token to ls-remote."""
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://github.com/Bobowski/app", "main", "push")
    yard.store.create_app("other", "https://github.com/stariodev/tool", "main", "push")
    github = yard.forge_named("github")
    github.save(
        app_id="42",
        installations=[
            {"id": "101", "account": "Bobowski", "selection": "all"},
            {"id": "202", "account": "stariodev", "selection": "all"},
        ],
    )
    minted: list[str] = []

    async def fake_token(self, repo: str = ""):
        minted.append(repo)
        return f"tok:{repo}"

    seen: list[tuple[str, str]] = []

    async def fake_tip(_clone, url, ref, token=""):
        seen.append((url, token))
        return "same123"

    async def fake_deploy(host, name, **_):
        return apps.Outcome("ok")

    monkeypatch.setattr(GitHub, "token", fake_token)
    monkeypatch.setattr(git, "remote_tip", fake_tip)
    monkeypatch.setattr(apps, "deploy", fake_deploy)
    monkeypatch.setattr(Forge, "connected", lambda self: True)
    await apps.check_all(yard)
    assert minted == ["https://github.com/Bobowski/app", "https://github.com/stariodev/tool"]
    assert seen == [
        ("https://github.com/Bobowski/app.git", "tok:https://github.com/Bobowski/app"),
        ("https://github.com/stariodev/tool.git", "tok:https://github.com/stariodev/tool"),
    ]


async def test_check_all_deploys_a_moved_tip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    yard.store.create_app("manual-app", "https://origin.cursor.com/org/other", "main", "manual")
    _connect(monkeypatch)

    tips = {"heldso": "abc123"}
    deployed: list[str] = []

    async def fake_tip(clone, url, ref, token=""):
        return tips.get(clone.parent.name, "")

    async def fake_deploy(host, name, **_):
        deployed.append(name)
        return apps.Outcome("ok")

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    monkeypatch.setattr(apps, "deploy", fake_deploy)
    await apps.check_all(yard)
    assert deployed == ["heldso"]
    assert yard.tips["heldso"].revision == "abc123"


async def test_check_all_skips_an_unchanged_tip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    _connect(monkeypatch)

    async def fake_tip(clone, url, ref, token=""):
        return "same123"

    async def fake_deploy(host, name, **_):
        raise AssertionError("no deploy")

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    monkeypatch.setattr(apps, "deploy", fake_deploy)
    from app.server.store import write_deployed

    write_deployed(yard.config, "heldso", Deployed(revision="same123", runs="Containerfile", kind="containerfile"))
    await apps.check_all(yard)
    assert yard.tips["heldso"].revision == "same123"


async def test_check_all_without_origin_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    seen: list[str] = []

    async def fake_tip(clone, url, ref, token=""):
        seen.append("tip")
        return "abc"

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    await apps.check_all(yard)
    assert seen == []
    assert yard.forges["origin.cursor.com"].last_check == ""


async def test_check_repo_deploys_only_that_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    yard.store.create_app("other", "https://origin.cursor.com/org/other", "main", "push")
    _connect(monkeypatch)
    deployed: list[str] = []

    async def fake_tip(clone, url, ref, token=""):
        return "newsha"

    async def fake_deploy(host, name, **_):
        deployed.append(name)
        return apps.Outcome("ok")

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    monkeypatch.setattr(apps, "deploy", fake_deploy)
    await apps.check_repo(yard, "https://origin.cursor.com/org/app")
    assert deployed == ["heldso"]


async def test_check_repo_debounces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    _connect(monkeypatch)
    monkeypatch.setattr(apps, "_DEBOUNCE", 0.05)
    seen: list[str] = []

    async def fake_tip(clone, url, ref, token=""):
        seen.append("tip")
        return ""

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    await apps.check_repo(yard, "https://origin.cursor.com/org/app")
    await apps.check_repo(yard, "https://origin.cursor.com/org/app")
    await apps.check_repo(yard, "https://origin.cursor.com/org/app")
    assert seen == ["tip"]
    await asyncio.sleep(0.1)
    assert seen == ["tip", "tip"]


async def test_check_installation_debounces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    monkeypatch.setattr(apps, "_DEBOUNCE", 0.05)
    calls: list[str] = []

    async def fake_find(self):
        calls.append("install")
        return []

    monkeypatch.setattr(Origin, "find_installations", fake_find)
    for _ in range(3):
        await apps.check_installation(yard, yard.forge_named("origin"))
    assert calls == ["install"]
    await asyncio.sleep(0.1)
    assert calls == ["install", "install"]


async def test_check_template_moved_deploys_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A new template stamp deploys a push app once; the tried pair is not retried."""
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    clone = _stario_clone(yard, "heldso")
    _connect(monkeypatch)
    tip_sha = git.head(clone)
    from app.server.store import write_deployed

    write_deployed(
        yard.config,
        "heldso",
        Deployed(revision=tip_sha, runs="yard:python-uv", kind="containerfile", template="oldtpl12"),
    )

    async def fake_tip(_c, url, ref, token=""):
        return tip_sha

    monkeypatch.setattr(git, "remote_tip", fake_tip)

    async def fake_sync(_c, url, ref, token=""):
        return tip_sha

    monkeypatch.setattr(git, "sync", fake_sync)
    # The apply "fails" (code 1) so no deployed write; the tried pair still stops a retry.
    recorder = _Apply(code=1, text="boom")
    monkeypatch.setattr(compose, "apply", recorder)
    await apps.check_all(yard)  # template moved -> deploy -> fails
    assert len(recorder.calls) == 1
    await apps.check_all(yard)  # same (tip, template): tried, no second deploy
    assert len(recorder.calls) == 1
    assert yard.tips["heldso"].tried == (tip_sha, yard.tips["heldso"].template)


async def test_check_source_image_and_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", runs="postgres:17", trigger="push")
    yard.store.create_app("site", "", runs="https://example.com/x.yml", trigger="manual")
    calls = []

    async def fake_digest(ref):
        calls.append(ref)
        return "sha256:" + "e" * 64

    async def fake_fetch(url):
        calls.append(url)
        return b"body\n"

    monkeypatch.setattr(sources, "digest", fake_digest)
    monkeypatch.setattr(sources, "fetch_file", fake_fetch)
    monkeypatch.setattr(compose, "apply", _Apply())
    yard.forges = {}
    await apps.check_all(yard)
    assert yard.tips["db"].revision == "sha256:" + "e" * 64
    assert yard.tips["site"].revision.startswith("sha256:")
    # push app deployed; manual app only updated tips
    assert read_deployed(yard.config, "db") is not None
    assert read_deployed(yard.config, "site") is None


async def test_check_source_error_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", runs="postgres:17", trigger="push")
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fail(ref):
        raise YardError(502, "registry", "postgres:17: boom")

    monkeypatch.setattr(sources, "digest", fail)
    yard.forges = {}
    await apps.check_all(yard)
    assert yard.tips["db"].error == "postgres:17: boom"
    assert read_deployed(yard.config, "db") is None

    async def ok(ref):
        return "sha256:" + "a" * 64

    monkeypatch.setattr(sources, "digest", ok)
    await apps.check_all(yard)
    assert yard.tips["db"].error == ""


async def test_check_source_one_get_per_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("a", "", runs="https://example.com/x.yml", trigger="manual")
    yard.store.create_app("b", "", runs="https://example.com/x.yml", trigger="manual")
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        return b"body\n"

    monkeypatch.setattr(sources, "fetch_file", fake_fetch)
    yard.forges = {}
    await apps.check_all(yard)
    assert calls == ["https://example.com/x.yml"]


async def test_source_deploy_failed_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", runs="postgres:17", trigger="push")
    revision = "sha256:" + "a" * 64

    async def fake_digest(ref):
        return revision

    monkeypatch.setattr(sources, "digest", fake_digest)
    monkeypatch.setattr(compose, "apply", _Apply(code=1, text="boom"))
    yard.forges = {}
    await apps.check_all(yard)
    assert read_deployed(yard.config, "db") is None
    recorder = _Apply(code=0)
    monkeypatch.setattr(compose, "apply", recorder)
    await apps.check_all(yard)
    assert not recorder.calls
    newer = "sha256:" + "b" * 64

    async def fake_digest2(ref):
        return newer

    monkeypatch.setattr(sources, "digest", fake_digest2)
    await apps.check_all(yard)
    assert recorder.calls


async def test_check_plan_failure_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A push app whose plan fails deploys once; the same tip is not retried."""
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push", runs="missing.yml")
    clone = _clone(yard, "heldso")
    tip_sha = git.head(clone)
    _connect(monkeypatch)

    async def fake_tip(_c, url, ref, token=""):
        return tip_sha

    async def fake_sync(_c, url, ref, token=""):
        return tip_sha

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    monkeypatch.setattr(git, "sync", fake_sync)
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)
    await apps.check_all(yard)  # plan fails: missing.yml is not a file
    assert not recorder.calls
    outcome = await apps.deploy(yard, "heldso")
    assert "missing.yml is not a file" in outcome.error


async def test_check_auto_guard_failure_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A guard failure (compose -> template) deploys once; the tried pair stops the retry."""
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    clone = _stario_clone(yard, "heldso")
    tip_sha = git.head(clone)
    _connect(monkeypatch)
    from app.server.store import write_deployed

    write_deployed(yard.config, "heldso", Deployed(revision=tip_sha, runs="compose.yml", kind="compose"))

    async def fake_tip(_c, url, ref, token=""):
        return tip_sha

    async def fake_sync(_c, url, ref, token=""):
        return tip_sha

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    monkeypatch.setattr(git, "sync", fake_sync)
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)
    await apps.check_all(yard)
    assert not recorder.calls  # the guard failed before apply
    await apps.check_all(yard)
    assert not recorder.calls  # and it did not retry the tried pair
    assert yard.tips["heldso"].tried[0] == tip_sha


async def test_check_url_containerfile_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A URL body that is a Containerfile fails once; the same body is not retried."""
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("umami", "", runs="https://example.com/x.yml", trigger="push")
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        return b"FROM scratch\n"

    monkeypatch.setattr(sources, "fetch_file", fake_fetch)
    monkeypatch.setattr(compose, "apply", _Apply())
    yard.forges = {}
    await apps.check_all(yard)  # classify fails: a Containerfile needs a repo
    assert yard.tips["umami"].error == ""
    assert read_deployed(yard.config, "umami") is None
    tried = yard.tips["umami"].tried
    assert tried[0].startswith("sha256:")
    await apps.check_all(yard)
    assert yard.tips["umami"].tried == tried  # no new deploy attempt


async def test_a_deploy_writes_a_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_yard(tmp_path, monkeypatch)
    outcome = await apps.deploy(yard, "heldso")
    job = yard.deploys["heldso"]
    assert outcome.status == "ok"
    assert job.status == "ok" and job.ended and job.outcome is not None
    assert job.outcome["status"] == "ok"
    assert job.lines[0] == "== sync https://origin.cursor.com/org/app main"
    assert any(line.startswith("== runs ") for line in job.lines)
    assert job.lines[-1].startswith(("== ", "hint:", "  ", "Set ", "Or ", "Then "))
    assert job.id and job.cause == "manual"


async def test_start_deploy_returns_the_running_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_yard(tmp_path, monkeypatch)
    gate = asyncio.Event()

    async def slow(*a, **kw):
        gate.set()
        await asyncio.sleep(0.05)
        return (0, "== done\n")

    monkeypatch.setattr(compose, "apply", slow)
    job = apps.start_deploy(yard, "heldso")
    assert job.status == "running"
    await gate.wait()
    assert apps.start_deploy(yard, "heldso").id == job.id
    await asyncio.sleep(0.1)
    assert job.status == "ok"


async def test_apply_lines_reach_the_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_yard(tmp_path, monkeypatch)
    await apps.deploy(yard, "heldso")
    lines = yard.deploys["heldso"].lines
    assert "== build localhost/heldso:abc" in lines
    assert lines.index("== build localhost/heldso:abc") < lines.index("== started localhost/heldso:abc")


def test_job_lines_trim_keeps_the_offset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A follower mid-stream loses no line when the buffer trims at JOB_LINES."""
    import app.server.yard as yard_mod

    monkeypatch.setattr(yard_mod, "JOB_LINES", 5)
    job = apps._new_job(make_yard(tmp_path), "x", "manual", "")
    for index in range(7):
        job.push(f"l{index}")
    # Follower pass 1 reads every unseen line (only the last 5 are buffered).
    seen, got = 0, []
    start = max(0, len(job.lines) - (job.pushed - seen))
    got += job.lines[start:]
    seen = job.pushed
    for index in range(7, 12):
        job.push(f"l{index}")
    # Pass 2 emits exactly the five new lines — no repeats, tail complete.
    start = max(0, len(job.lines) - (job.pushed - seen))
    got += job.lines[start:]
    assert len(got) == len(set(got))
    assert got == [f"l{i}" for i in range(2, 12)]


async def test_a_very_long_apply_line_still_deploys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_yard(tmp_path, monkeypatch)
    long_line = "x" * (100 * 1024)

    async def apply(config, name, command, project, *, detach=False, on_line=None):
        if on_line:
            on_line(long_line)
        return 0, long_line + "\n"

    monkeypatch.setattr(compose, "apply", apply)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    assert long_line in yard.deploys["heldso"].lines
