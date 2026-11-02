import asyncio
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.server import compose
from app.server.compose import APP_COMPOSE, BUILD_COMPOSE, image_tag, project, run_env, saved_project
from app.server.podman import Podman
from app.server.store import App, Config, Deployed
from app.shared import YardError
from tests.server.conftest import make_config

FULL = (
    "services:\n  app:\n    networks:\n      - yard\n"
    "    env_file:\n      - ${APP_ENV}\n    volumes:\n      - ${DATA_DIR}:/data\n"
)


def _config(root: Path) -> Config:
    return make_config(root, podman_sock="/run/podman.sock", podman_bin="podman", remote=True)


def _app(name: str, trigger: str = "") -> App:
    return App(name, "https://origin.cursor.com/org/heldso", "main", trigger or "push", "")


def _checkout(root: Path, files: dict[str, str], name: str = "heldso") -> Path:
    """The app's clone, where compose.project reads a repo app's files."""
    directory = root / "apps" / name / "repo"
    directory.mkdir(parents=True)
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return directory


def _git(checkout: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Yard",
        "GIT_AUTHOR_EMAIL": "yard@example.com",
        "GIT_COMMITTER_NAME": "Yard",
        "GIT_COMMITTER_EMAIL": "yard@example.com",
    }
    subprocess.run(["git", "-C", str(checkout), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-m", "one"], check=True, capture_output=True, env=env)


def _git_out(checkout: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()


def test_run_env_is_the_interpolation_env(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"Containerfile": "FROM scratch\n"})
    env = run_env(config, _app("heldso"), checkout, "localhost/heldso:abc", "abc")

    assert env == {
        "PROJECT_NAME": "heldso",
        "IMAGE": "localhost/heldso:abc",
        "SHA": "abc",
        "CHECKOUT": str(checkout),
        "DATA_DIR": str(tmp_path / "apps" / "heldso" / "data"),
        "APP_ENV": str(tmp_path / "apps" / "heldso" / "env"),
    }
    assert (tmp_path / "apps" / "heldso" / "data").is_dir()


def test_project_containerfile_composes_app_and_build(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"Containerfile": "FROM scratch\n"})
    found = project(config, _app("heldso"), Deployed(revision="abc", runs="Containerfile", kind="containerfile"))
    assert found.files == [APP_COMPOSE, BUILD_COMPOSE]
    assert found.env["DOCKERFILE"] == "Containerfile"
    assert found.env["CHECKOUT"] == str(checkout)
    assert found.image == "localhost/heldso:abc"
    written = project(
        config, _app("heldso"), Deployed(revision="abc", runs="yard:go", kind="containerfile", template="3f9a1c2e")
    )
    assert written.env["DOCKERFILE"] == str(tmp_path / "apps" / "heldso" / "Containerfile")
    assert written.image == "localhost/heldso:abc-3f9a1c2e"
    assert found.env["YARD_FILES"] == "1"
    env_file = tmp_path / "apps" / "heldso" / "env"
    assert env_file.read_text() == ""


def test_project_repo_compose_is_one_file(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"compose.yml": FULL})
    found = project(config, _app("heldso"), Deployed(revision="abc", runs="compose.yml", kind="compose"))
    assert found.files == [str(checkout / "compose.yml")]
    assert "YARD_FILES" not in found.env


def test_project_image_app_is_app_only(tmp_path: Path):
    config = _config(tmp_path)
    app = App("db", "", "main", "push", "", "postgres:17")
    found = project(config, app, Deployed(revision="sha256:" + "a" * 64, runs=app.runs, kind="image"))
    assert found.files == [APP_COMPOSE]
    assert found.image == f"postgres:17@sha256:{'a' * 64}"
    assert found.env["CHECKOUT"] == str(config.app_dir("db"))
    assert found.env["YARD_FILES"] == "1"


def test_project_url_compose_is_the_copy(tmp_path: Path):
    config = _config(tmp_path)
    app = App("u", "", "main", "push", "", "https://example.com/x.yml")
    rev = "sha256:" + "b" * 64
    found = project(config, app, Deployed(revision=rev, runs=app.runs, kind="compose"))
    assert found.image == f"compose:{rev[7:19]}"
    assert found.files == [str(tmp_path / "apps" / "u" / "compose" / f"{rev[7:19]}.yml")]


def test_the_yard_app_gets_its_host_paths_from_config(tmp_path: Path):
    config = _config(tmp_path)
    checkout = _checkout(tmp_path, {"ops/compose.yml": FULL}, "yard")
    found = project(config, _app("yard"), Deployed(revision="abc", runs="ops/compose.yml", kind="compose"))
    assert found.env["YARD_ROOT"] == str(tmp_path)
    assert found.env["YARD_PODMAN_SOCK"] == "/run/podman.sock"
    assert found.env["YARD_PODMAN_DIR"] == "/run"
    other = run_env(replace(config, podman_sock=""), _app("yard"), checkout, "x", "s")
    assert other["YARD_PODMAN_SOCK"] == "/run/podman/podman.sock"


def test_saved_project_rebuilds_from_the_running_plan(tmp_path: Path):
    config = _config(tmp_path)
    checkout = config.clone("heldso")
    checkout.mkdir(parents=True)
    (checkout / "Containerfile").write_text("FROM scratch\n")
    _git(checkout)
    sha = _git_out(checkout, "rev-parse", "HEAD")
    found = saved_project(config, _app("heldso"), Deployed(revision=sha, runs="Containerfile", kind="containerfile"))
    assert found is not None
    assert found.files == [APP_COMPOSE, BUILD_COMPOSE]
    assert found.env["IMAGE"] == image_tag("heldso", sha)
    assert found.env["DOCKERFILE"] == "Containerfile"

    (checkout / "compose.yml").write_text(FULL)
    found = saved_project(config, _app("heldso"), Deployed(revision=sha, runs="compose.yml", kind="compose"))
    assert found is not None and found.files == [str(checkout / "compose.yml")]

    found = saved_project(
        config, _app("heldso"), Deployed(revision=sha, runs="yard:python-uv", kind="containerfile", template="3f9a1c2e")
    )
    assert found is not None
    assert found.env["IMAGE"] == image_tag("heldso", sha, "3f9a1c2e")
    assert found.env["DOCKERFILE"] == str(tmp_path / "apps" / "heldso" / "Containerfile")

    assert saved_project(config, _app("other"), None) is None


def test_saved_project_image_app(tmp_path: Path):
    config = _config(tmp_path)
    app = App("db", "", "main", "push", "", "postgres:17")
    found = saved_project(config, app, Deployed(revision="sha256:" + "a" * 64, runs="postgres:17", kind="image"))
    assert found is not None
    assert found.files == [APP_COMPOSE]
    assert found.env["IMAGE"] == "postgres:17@sha256:" + "a" * 64


def test_saved_project_url_compose(tmp_path: Path):
    config = _config(tmp_path)
    rev = "sha256:" + "b" * 64
    copy = tmp_path / "apps" / "u" / "compose" / f"{rev[7:19]}.yml"
    copy.parent.mkdir(parents=True)
    copy.write_text("services: {}\n")
    app = App("u", "", "main", "push", "", "https://example.com/x.yml")
    found = saved_project(config, app, Deployed(revision=rev, runs=app.runs, kind="compose"))
    assert found is not None and found.files == [str(copy)]


def test_apply_runs_one_podman_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _config(tmp_path)
    calls: list[list[str]] = []

    async def fake_stream(self, *args, on_line=None):
        calls.append(self._args(args))
        return 0, "== build\n"

    monkeypatch.setattr(Podman, "stream", fake_stream)
    found = compose.Project(
        "heldso",
        ["/checkout/compose.yml"],
        {"DATA_DIR": f"{tmp_path}/data/heldso", "IMAGE": "localhost/heldso:abc", "PROJECT_NAME": "heldso"},
    )

    code, text = asyncio.run(compose.apply(config, "heldso", "up", found))
    assert code == 0
    # A plain rm first: Podman refuses to remove a running deploy container, so a running deploy is never replaced.
    assert calls[0] == ["podman", "--remote", "rm", "deploy-heldso"]
    argv = calls[1]
    assert "--replace" not in argv
    assert argv[:12] == [
        "podman",
        "--remote",
        "run",
        "--name",
        "deploy-heldso",
        "--network",
        "yard",
        "-v",
        "/run/podman.sock:/run/podman/podman.sock",
        "-v",
        f"{tmp_path}:{tmp_path}",
        "-e",
    ]
    assert "COMPOSE_FILE=/checkout/compose.yml" in argv

    asyncio.run(compose.apply(config, "yard", "up", found, detach=True))
    argv = calls[-1]
    assert argv[-3:] == ["-d", "localhost/yard-apply", "up"]


def test_install_sh_runs_the_first_apply():
    spec = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    for part in (
        'git clone --depth 1 "$origin" "$src"',
        "apps/yard/repo",
        "localhost/yard-apply",
        "yard forge setup origin",
        "admin.token",
    ):
        assert part in spec


def test_install_sh_configures_journald():
    spec = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    for part in (
        "/etc/systemd/journald.conf.d/yard.conf",
        "Storage=persistent",
        'log_driver = "journald"',
        'events_logger = "journald"',
        "systemctl --user restart podman.socket",
    ):
        assert part in spec


def test_install_sh_points_the_yard_app_at_the_origin():
    spec = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    for part in (
        "api/v1/apps/yard",
        "-X PATCH",
        '\\"repo\\":\\"${origin%.git}\\"',
        "yard app set --app yard --repo ${origin%.git}",
    ):
        assert part in spec


def test_install_sh_uses_the_distro_podman():
    spec = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    assert "apt-get install -y nftables git ca-certificates sudo iproute2 procps uidmap acl podman" in spec


def test_the_images_pin_the_host_podman_remote():
    ops = Path(__file__).resolve().parents[2] / "ops"
    sha = "0d1759e3fc42b0e3a4496176dabc7b65f0f334a0ab2b22eaed7c36afeece8b0c"
    yard = (ops / "Containerfile").read_text()
    apply = (ops / "apply" / "Containerfile").read_text()
    assert "download/v5.7.0/podman-remote-static-linux_amd64.tar.gz" in yard
    assert "PODMAN_VERSION=5.7.0" in apply
    assert f"PODMAN_SHA256={sha}" in apply


def test_install_sh_and_apply_pass_the_same_env_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    block = spec.split("podman run --replace", 1)[1].split("localhost/yard-apply", 1)[0]
    shell_names = set(re.findall(r"-e ([A-Z_]+)=", block))

    config = _config(tmp_path)
    calls: list[list[str]] = []

    async def fake_stream(self, *args, on_line=None):
        calls.append(self._args(args))
        return 0, ""

    monkeypatch.setattr(Podman, "stream", fake_stream)
    _checkout(tmp_path, {"ops/compose.yml": FULL}, "yard")
    found = project(config, _app("yard"), Deployed(revision="abc", runs="ops/compose.yml", kind="compose"))
    asyncio.run(compose.apply(config, "yard", "up", found))
    argv = calls[-1]
    python_names = {value.split("=", 1)[0] for flag, value in zip(argv, argv[1:], strict=False) if flag == "-e"}
    assert python_names == shell_names


def test_the_apply_files_meet_the_contract():
    ops = Path(__file__).resolve().parents[2] / "ops" / "apply"
    names = sorted(path.name for path in ops.iterdir())
    assert names == ["Containerfile", "app.compose.yml", "apply.sh", "build.compose.yml"]
    app = (ops / "app.compose.yml").read_text()
    for line in (
        "image: ${IMAGE}",
        "- ${PROJECT_NAME}",
        "- ${APP_ENV}",
        "- ${DATA_DIR}:/data",
        "PROJECT_NAME: ${PROJECT_NAME}",
        "PROJECT_DATA_DIR: /data",
        "SHA: ${SHA}",
        "restart: always",
        "yard:",
        "external: true",
    ):
        assert line in app
    build = (ops / "build.compose.yml").read_text()
    for line in (
        "context: ${CHECKOUT}",
        "dockerfile: ${DOCKERFILE}",
        'PORT: "8000"',
        "STARIO_HOST: 0.0.0.0",
        'STARIO_PORT: "8000"',
        "STARIO_TRACER: json",
        "expose:",
    ):
        assert line in build
    containerfile = (ops / "Containerfile").read_text()
    assert "app.compose.yml" in containerfile and "build.compose.yml" in containerfile


def test_image_tag():
    assert image_tag("mono", "abc1234def") == "localhost/mono:abc1234def"
    assert image_tag("yard", "abc", "3f9a1c2e") == "localhost/yard:abc-3f9a1c2e"


def test_record_failure_runs_a_deploy_container_that_prints_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _config(tmp_path)
    calls: list[list[str]] = []

    async def fake_stream(self, *args, on_line=None):
        calls.append(self._args(args))
        return 1, ""

    monkeypatch.setattr(Podman, "stream", fake_stream)
    asyncio.run(compose.record_failure(config, "heldso", {"sha": "abc", "error": "no"}, ["== sync", "!! no"]))
    assert calls[0][-2:] == ["rm", "deploy-heldso"]
    argv = calls[1]
    assert argv[argv.index("--name") + 1] == "deploy-heldso"
    assert "--replace" not in argv
    assert "yard.error=no" in argv and "yard.sha=abc" in argv
    assert "YARD_LOG=== sync\n!! no" in argv
    assert argv[-3:] == ["localhost/yard-apply", "-c", 'printf "%s\\n" "$YARD_LOG"; exit 1']


def test_record_failure_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def broken(self, *args, on_line=None):
        raise YardError(502, "podman", "podman is gone")

    monkeypatch.setattr(Podman, "stream", broken)
    asyncio.run(compose.record_failure(_config(tmp_path), "heldso", {}, []))
