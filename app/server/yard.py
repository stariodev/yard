"""Yard's dependencies and runtime state. The services do the work."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.server.caddy import Caddy
from app.server.forges import Forge, GitHub, Origin
from app.server.podman import Podman
from app.server.store import Config, Store
from app.shared import YardError

if TYPE_CHECKING:
    from stario import App as StarioApp


JOB_LINES = 5000


@dataclass(slots=True)
class Job:
    """One deploy, memory only. `lines` is the live log; `changed` wakes followers."""

    id: str
    app: str
    cause: str
    actor: str
    started: str
    status: str = "running"
    ended: str = ""
    outcome: dict | None = None
    lines: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    pushed: int = 0

    def push(self, line: str) -> None:
        self.lines.append(line)
        del self.lines[:-JOB_LINES]
        self.pushed += 1
        self.changed.set()
        self.changed.clear()


@dataclass(slots=True)
class Tip:
    """What a check last saw for one app."""

    revision: str = ""
    template: str = ""
    error: str = ""


class Yard:
    """Config, store, the adapters, and what the running server remembers."""

    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.podman = Podman(config.podman_bin, remote=config.remote)
        self.caddy = Caddy(config.caddy_socket)
        self.forges: dict[str, Forge] = {
            forge.host: forge
            for forge in (
                Origin(config.data("yard") / "origin"),
                GitHub(config.data("yard") / "github"),
            )
        }
        self.tips: dict[str, Tip] = {}
        self.deploys: dict[str, Job] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        # One lock for every site file write and Caddy load.
        self.sites_lock = asyncio.Lock()
        self.checked: dict[str, float] = {}
        self.queued: dict[str, Callable[[], Awaitable[object]]] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.stario: StarioApp | None = None
        self.last_prune = ""
        self.last_prune_freed = 0
        self.last_prune_error = ""

    def attach(self, app: StarioApp) -> None:
        self.stario = app

    def spawn(self, coro) -> asyncio.Task:
        """Background work from a request. The app tracks it when it can."""
        if self.stario is not None:
            return self.stario.create_task(coro)
        return asyncio.get_running_loop().create_task(coro)

    def lock(self, name: str) -> asyncio.Lock:
        """One lock per app: deploys serialize on it."""
        return self.locks.setdefault(name, asyncio.Lock())

    def forge(self, repo: str) -> Forge:
        """The forge that hosts a repo URL."""
        forge = self.forges.get(urlsplit(repo).hostname or "")
        if forge is None:
            raise YardError(
                400,
                "bad_repo",
                f"repo must be a forge URL: {repo}",
                help_text="Use https://<forge>/<owner>/<name>.",
                example="https://github.com/org/heldso",
            )
        return forge

    def forge_named(self, name: str) -> Forge:
        """The forge by its short name, for the /forges/{name} routes."""
        for forge in self.forges.values():
            if forge.name == name:
                return forge
        raise YardError(404, "not_found", f"unknown forge {name}", help_text="The forges are origin and github.")
