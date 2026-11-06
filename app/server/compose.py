"""Compose projects. Yard sets the env; the apply image runs docker-compose on the files as they are."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.server.podman import Podman
from app.server.store import App, Deployed
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
# 4 bytes a character at most: the tail stays under the 128 KiB of one argv string on Linux.
_LOG_CHARS = 30_000


def image_tag(name: str, sha: str, template: str = "") -> str:
    tag = f"localhost/{name}:{short_sha(sha)}"
    return f"{tag}-{template}" if template else tag


@dataclass(slots=True)
class Project:
    name: str
    files: list[str]
    env: dict[str, str]
    image: str = ""


def plan_image(app: App, revision: str, template: str = "") -> str:
    """The image a deploy runs: a local tag for a repo app, the digest for an image app, none for a URL app."""
    if app.repo:
        return image_tag(app.name, revision, template)
    if app.image:
        return f"{app.image.split('@', 1)[0]}@{revision}"
    return ""


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
    env = run_env(config, app, checkout, plan.image, plan.revision)
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
    return Project(app.name, files, env, plan.image)


def saved_project(config: Config, app: App, plan: Deployed | None) -> Project | None:
    """The project for a purge from the running plan; None when its files are gone."""
    if plan is None or not plan.revision:
        return None
    if app.repo and not (config.clone(app.name) / ".git").exists():
        return None
    if not app.repo and plan.kind == "compose" and not config.compose_copy(app.name, plan.revision).is_file():
        return None
    return project(config, app, plan)


async def run(
    config: Config,
    app: str,
    command: str,
    project: Project | None,
    *,
    labels: dict[str, str] | None = None,
    log: list[str] | None = None,
    on_line: Callable[[str], None] | None = None,
) -> int:
    """Start the deploy container, follow its log, and return its exit code."""
    skip = await start(config, app, command, project, labels=labels or {}, log=log or [])
    await follow(config, app, on_line or (lambda _line: None), skip=skip)
    return await wait(config, app)


async def start(
    config: Config, app: str, command: str, project: Project | None, *, labels: dict[str, str], log: list[str]
) -> int:
    """Start `deploy-<name>` detached: `up`, `purge`, or `fail` (print the log, exit 1).

    The container prints `log` first, so its own log is the whole deploy. Returns how many lines
    of `log` it prints, so a follower can skip the ones it has.
    """
    podman = _podman(config)
    # Without -f, Podman refuses a running container: a running deploy container is the deploy lock,
    # and the `run` below then fails on the name.
    await podman.stream("rm", deploy_container(app))
    text = "\n".join(log)[-_LOG_CHARS:]
    argv = ["run", "-d", "--name", deploy_container(app)]
    for key in sorted(labels):
        if labels[key]:
            argv += ["--label", f"yard.{key}={labels[key]}"]
    argv += ["--network", "yard", "-v", f"{config.podman_socket}:/run/podman/podman.sock"]
    argv += ["-v", f"{config.root}:{config.root}", "-e", f"YARD_LOG={text}"]
    if project is not None:
        argv += ["-e", f"COMPOSE_PROJECT_NAME={app}", "-e", f"COMPOSE_FILE={':'.join(project.files)}"]
        argv += [arg for key in sorted(project.env) for arg in ("-e", f"{key}={project.env[key]}")]
    await podman.run(*argv, "localhost/yard-apply", command, timeout=60)
    return text.count("\n") + 1 if text else 0


async def follow(config: Config, app: str, on_line: Callable[[str], None], *, skip: int = 0) -> None:
    """Stream the log of the deploy container from its first line until it stops. The first `skip` lines stay out."""
    seen = 0

    def shown(line: str) -> None:
        nonlocal seen
        seen += 1
        if seen > skip:
            on_line(line)

    await _podman(config).stream("logs", "-f", deploy_container(app), on_line=shown)


async def wait(config: Config, app: str) -> int:
    """The exit code of the deploy container, once it stops."""
    code, text = await _podman(config).stream("wait", deploy_container(app))
    try:
        return int(text.strip().splitlines()[-1])
    except ValueError, IndexError:
        raise YardError(502, "podman", f"podman wait {deploy_container(app)}: {text.strip() or code}") from None


async def build_apply(config: Config) -> tuple[int, str]:
    """Rebuild the apply image. The layer cache makes a no-change build fast, and each yard upgrade is a new process."""
    return await _podman(config).stream(
        "build", "-t", "localhost/yard-apply", "-f", f"{APPLY_DIR}/Containerfile", APPLY_DIR
    )


def _podman(config: Config) -> Podman:
    return Podman(config.podman_bin, remote=config.remote)
