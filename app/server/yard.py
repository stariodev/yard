"""Yard's dependencies and runtime state. The services do the work."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.server.caddy import Caddy
from app.server.forges import Forge, GitHub, Origin
from app.server.podman import Podman
from app.server.store import Config, Store
from app.shared import YardError, now

if TYPE_CHECKING:
    from stario import App as StarioApp


JOB_LINES = 5000


@dataclass(slots=True)
class Job:
    """One deploy while Yard runs it: the live log for `yard app deploy`. `changed` wakes followers.

    The deploy container keeps the same log and the record; the job ends with that record.
    """

    id: str
    app: str
    cause: str
    actor: str
    started: str
    status: str = "running"
    ended: str = ""
    record: dict | None = None
    lines: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    pushed: int = 0

    def push(self, line: str) -> None:
        self.lines.append(line)
        del self.lines[:-JOB_LINES]
        self.pushed += 1
        self.changed.set()
        self.changed.clear()

    def end(self, record: dict) -> None:
        self.status = str(record.get("status") or "failed")
        self.ended = now()
        self.record = record
        self.changed.set()


@dataclass(slots=True)
class Tip:
    """What a check last saw for one app."""

    revision: str = ""
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
        # The hosts Caddy read from each site file, keyed by the file's (mtime_ns, size).
        self.site_hosts: dict[str, tuple[tuple[int, int], list[str]]] = {}
        # The apps the check worker should check, with the cause and actor of the request.
        self.wanted: dict[str, tuple[str, str]] = {}
        self.checking: dict[str, asyncio.Task] = {}
        self.wake = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []
        self.stario: StarioApp | None = None
        self.last_prune = ""
        self.last_prune_freed = 0
        self.last_prune_error = ""

    def attach(self, app: StarioApp) -> None:
        self.stario = app

    async def pause(self, seconds: float, wake: asyncio.Event | None = None) -> bool:
        """Sleep `seconds`, or until `wake` is set. False when the app shuts down first: the caller stops.

        A background loop that sleeps with plain `asyncio.sleep` holds the shutdown for the whole
        grace period before Stario cancels it.
        """
        if self.stario is not None and self.stario.shutting_down:
            return False
        waits: list[asyncio.Future] = [] if self.stario is None else [self.stario.shutdown]
        waker = asyncio.ensure_future(wake.wait()) if wake is not None else None
        if waker is not None:
            waits.append(waker)
        try:
            if waits:
                await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(seconds)
        finally:
            if waker is not None:
                waker.cancel()
        return self.stario is None or not self.stario.shutting_down

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
            )
        return forge

    def forge_named(self, name: str) -> Forge:
        """The forge by its short name, for the /forges/{name} routes."""
        for forge in self.forges.values():
            if forge.name == name:
                return forge
        raise YardError(404, "not_found", f"unknown forge {name}", help_text="The forges are origin and github.")
