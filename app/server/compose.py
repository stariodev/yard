"""Compose projects. Yard sets the env; the apply image runs docker-compose on the files as they are."""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.server.store import App, read_deployed
from app.shared import YardError, deploy_container, ensure_file, short_sha

if TYPE_CHECKING:
    from app.server.store import Config

log = logging.getLogger("yard")

APPLY_DIR = "/usr/local/share/yard/apply"
if not Path(APPLY_DIR).is_dir():
    APPLY_DIR = str(Path(__file__).resolve().parents[2] / "ops" / "apply")
APP_COMPOSE = f"{APPLY_DIR}/app.compose.yml"
BUILD_COMPOSE = f"{APPLY_DIR}/build.compose.yml"
COMPOSE_NAMES = ("compose.yml", "compose.yaml", "ops/compose.yml", "ops/compose.yaml")
DOCKERFILES = ("Containerfile", "Dockerfile", "ops/Containerfile", "ops/Dockerfile")


def image_tag(name: str, sha: str, template: str = "") -> str:
    tag = f"localhost/{name}:{short_sha(sha)}"
    return f"{tag}-{template}" if template else tag


@dataclass(slots=True)
class Project:
    name: str
    files: list[str]
    env: dict[str, str]


def run_env(config: Config, app: App, checkout: Path, image: str, sha: str) -> dict[str, str]:
    """The values the compose files interpolate. The same names reach the apply container as process env."""
    data = config.data(app.name)
    data.mkdir(parents=True, exist_ok=True)
    env = {
        "PROJECT_NAME": app.name,
        "IMAGE": image,
        "SHA": sha,
        "CHECKOUT": str(checkout),
        "DATA_DIR": str(data),
        "APP_ENV": str(config.env(app.name)),
    }
    if app.name == "yard":
        # ops/compose.yml binds these host paths.
        sock = config.podman_socket
        env["YARD_ROOT"] = str(config.root)
        env["YARD_PODMAN_SOCK"] = sock
        env["YARD_PODMAN_DIR"] = str(Path(sock).parent)
    return env


def project(
    config: Config,
    app: App,
    kind: str,
    runs: str,
    revision: str,
    image: str,
    checkout: Path,
    dockerfile: str = "",
) -> Project:
    """The project a deploy or purge runs.

    kind "containerfile" composes Yard's app+build files; "image" composes app
    only; "compose" runs the file itself: the clone path or the fetched copy.
    """
    env = run_env(config, app, checkout, image, revision)
    ensure_file(config.env(app.name))
    if kind == "containerfile":
        files = [APP_COMPOSE, BUILD_COMPOSE]
        env["DOCKERFILE"] = dockerfile
        env["YARD_FILES"] = "1"
    elif kind == "image":
        files = [APP_COMPOSE]
        env["YARD_FILES"] = "1"
    elif runs.startswith("https://"):
        files = [str(config.compose_copy(app.name, revision))]
    else:
        files = [str(checkout / runs)]
    return Project(app.name, files, env)


def saved_project(config: Config, app: App) -> Project | None:
    """Rebuild the project for a purge from the deployed file; None when there is none."""
    deployed = read_deployed(config, app.name)
    if deployed is None or not deployed.revision:
        return None
    if deployed.kind == "image":
        image = f"{deployed.runs.split('@', 1)[0]}@{deployed.revision}"
        return project(config, app, "image", "", deployed.revision, image, config.app_dir(app.name))
    if deployed.kind == "compose" and deployed.runs.startswith("https://"):
        copy = config.compose_copy(app.name, deployed.revision)
        if not copy.is_file():
            return None
        image = f"compose:{deployed.revision[7:19]}"
        return project(config, app, "compose", deployed.runs, deployed.revision, image, config.app_dir(app.name))
    checkout = config.clone(app.name)
    if not (checkout / ".git").exists():
        return None
    image = image_tag(app.name, deployed.revision, deployed.template)
    dockerfile = str(config.app_dir(app.name) / "Containerfile") if deployed.runs.startswith("yard:") else deployed.runs
    return project(config, app, deployed.kind, deployed.runs, deployed.revision, image, checkout, dockerfile)


async def apply(
    config: Config,
    app: str,
    command: str,
    project: Project,
    *,
    labels: dict[str, str] | None = None,
    detach: bool = False,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Run the apply script as one Podman container. A non-zero exit is the deploy log, not an exception."""
    argv = _podman(config) + [
        "run",
        *_record_args(app, labels or {}),
        "--network",
        "yard",
        "-v",
        f"{config.podman_socket}:/run/podman/podman.sock",
        "-v",
        f"{config.root}:{config.root}",
        "-e",
        f"COMPOSE_PROJECT_NAME={app}",
        "-e",
        f"COMPOSE_FILE={':'.join(project.files)}",
    ]
    for key in sorted(project.env):
        argv += ["-e", f"{key}={project.env[key]}"]
    if detach:
        argv.append("-d")
    argv += ["localhost/yard-apply", command]
    return await _run(argv, _process_env(config), on_line=on_line)


async def record_failure(config: Config, app: str, labels: dict[str, str], lines: list[str]) -> None:
    """A deploy that failed before the apply: a deploy container that prints its lines and exits 1."""
    argv = _podman(config) + [
        "run",
        *_record_args(app, labels),
        "--entrypoint",
        "sh",
        "-e",
        "YARD_LOG=" + "\n".join(lines),
        "localhost/yard-apply",
        "-c",
        'printf "%s\\n" "$YARD_LOG"; exit 1',
    ]
    try:
        await _run(argv, _process_env(config))
    except Exception as exc:
        log.warning("deploy record %s: %s", app, exc)


def _record_args(app: str, labels: dict[str, str]) -> list[str]:
    """The fixed name and the labels. The name makes `podman logs deploy-<name>` the last deploy;
    --replace drops the one before. `status.deploy_record` reads the labels back."""
    args = ["--replace", "--name", deploy_container(app)]
    for key in sorted(labels):
        args += ["--label", f"yard.{key}={labels[key]}"]
    return args


async def build_apply(config: Config) -> tuple[int, str]:
    """Rebuild the apply image. The layer cache makes a no-change build fast, and each yard upgrade is a new process."""
    argv = _podman(config) + [
        "build",
        "-t",
        "localhost/yard-apply",
        "-f",
        f"{APPLY_DIR}/Containerfile",
        APPLY_DIR,
    ]
    return await _run(argv, _process_env(config))


def _podman(config: Config) -> list[str]:
    args = [config.podman_bin or "podman"]
    if config.remote:
        args.append("--remote")
    return args


def _process_env(config: Config) -> dict[str, str]:
    """The env the host-side podman needs to find the socket."""
    env = dict(os.environ)
    host = env.get("CONTAINER_HOST") or f"unix://{config.podman_socket}"
    env["CONTAINER_HOST"] = host
    env["DOCKER_HOST"] = env.get("DOCKER_HOST") or host
    return env


async def _run(
    argv: list[str],
    env: dict[str, str],
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env or None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=16 * 1024 * 1024,
        )
    except FileNotFoundError as exc:
        raise YardError(
            502,
            "compose",
            f"{argv[0]} is not on PATH: {exc}",
            help_text="The yard image ships docker-compose and podman.",
        ) from exc
    text = ""
    assert proc.stdout is not None
    while chunk := await proc.stdout.readline():
        decoded = chunk.decode(errors="replace")
        text += decoded
        if on_line is not None:
            on_line(decoded.rstrip("\n"))
    await proc.wait()
    return proc.returncode or 0, text
