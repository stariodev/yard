"""Compose projects. Yard sets the env; the apply image runs docker-compose on the files as they are."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.server.podman import Podman
from app.server.store import App, Deployed
from app.shared import deploy_container, ensure_file, short_sha

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
    image: str = ""


def plan_image(app: App, plan: Deployed) -> str:
    """The image a plan runs: a local tag for a repo app, the pinned digest for an image app,
    and a compose:<12 hex> name for a compose file from a URL."""
    if app.repo:
        return image_tag(app.name, plan.revision, plan.template)
    if plan.kind == "image":
        return f"{plan.runs.split('@', 1)[0]}@{plan.revision}"
    return f"compose:{plan.revision[7:19]}"


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


def project(config: Config, app: App, plan: Deployed) -> Project:
    """The project a deploy or purge runs, with the image it runs.

    kind "containerfile" composes Yard's app+build files; "image" composes app
    only; "compose" runs the file itself: the clone path or the fetched copy.
    """
    checkout = config.clone(app.name) if app.repo else config.app_dir(app.name)
    image = plan_image(app, plan)
    env = run_env(config, app, checkout, image, plan.revision)
    ensure_file(config.env(app.name))
    if plan.kind == "containerfile":
        files = [APP_COMPOSE, BUILD_COMPOSE]
        # A written Containerfile lives beside the clone; a repo one is a path in it.
        written = plan.runs.startswith("yard:")
        env["DOCKERFILE"] = str(config.app_dir(app.name) / "Containerfile") if written else plan.runs
        env["YARD_FILES"] = "1"
    elif plan.kind == "image":
        files = [APP_COMPOSE]
        env["YARD_FILES"] = "1"
    elif not app.repo:
        files = [str(config.compose_copy(app.name, plan.revision))]
    else:
        files = [str(checkout / plan.runs)]
    return Project(app.name, files, env, image)


def saved_project(config: Config, app: App, plan: Deployed | None) -> Project | None:
    """The project for a purge from the running plan; None when its files are gone."""
    if plan is None or not plan.revision:
        return None
    if app.repo and not (config.clone(app.name) / ".git").exists():
        return None
    if not app.repo and plan.kind == "compose" and not config.compose_copy(app.name, plan.revision).is_file():
        return None
    return project(config, app, plan)


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
    await _remove_record(config, app)
    argv = [
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
    return await _podman(config).stream(*argv, on_line=on_line)


async def record_failure(config: Config, app: str, labels: dict[str, str], lines: list[str]) -> None:
    """A deploy that failed before the apply: a deploy container that prints its lines and exits 1."""
    try:
        await _remove_record(config, app)
    except Exception as exc:
        log.warning("deploy record %s: %s", app, exc)
        return
    argv = [
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
        await _podman(config).stream(*argv)
    except Exception as exc:
        log.warning("deploy record %s: %s", app, exc)


async def _remove_record(config: Config, app: str) -> None:
    """Remove the last deploy container. Without -f, Podman refuses a running one: a running deploy
    container is the deploy lock, and the `run` after this then fails on the name."""
    await _podman(config).stream("rm", deploy_container(app))


async def follow(config: Config, app: str, on_line: Callable[[str], None]) -> None:
    """Stream the log of the deploy container from its first line until it stops."""
    await _podman(config).stream("logs", "-f", deploy_container(app), on_line=on_line)


def _record_args(app: str, labels: dict[str, str]) -> list[str]:
    """The fixed name and the labels. The name makes `podman logs deploy-<name>` the last deploy.
    `status.deploy_record` reads the labels back."""
    args = ["--name", deploy_container(app)]
    for key in sorted(labels):
        args += ["--label", f"yard.{key}={labels[key]}"]
    return args


async def build_apply(config: Config) -> tuple[int, str]:
    """Rebuild the apply image. The layer cache makes a no-change build fast, and each yard upgrade is a new process."""
    return await _podman(config).stream(
        "build", "-t", "localhost/yard-apply", "-f", f"{APPLY_DIR}/Containerfile", APPLY_DIR
    )


def _podman(config: Config) -> Podman:
    return Podman(config.podman_bin, remote=config.remote)
