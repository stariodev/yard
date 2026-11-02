"""What the host shows: the app table, one app's page, and container logs."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.server import journal, podman, sites
from app.server.podman import Container, Sample
from app.server.store import App, Config, Deployed, read_deployed
from app.server.yard import Tip
from app.shared import YardError, app_container, deploy_container, image_sha, stamp

if TYPE_CHECKING:
    from app.server.yard import Yard


@dataclass(slots=True)
class Row:
    app: str = ""
    container: str = ""
    service: str = ""
    state: str = ""
    running: bool = False
    health: str = ""
    restarts: int = 0
    exit_code: int = 0
    started: str = ""
    exited: str = ""
    id: str = ""
    oom: bool = False
    error: str = ""
    image: str = ""
    sha: str = ""
    tip: str = ""
    hosts: list[str] = field(default_factory=list)
    trigger: str = ""
    cpu: str = ""
    mem: str = ""
    data: int = 0
    deploy: dict[str, Any] | None = None
    template_moved: bool = False


@dataclass(slots=True)
class Host:
    cpus: int = 0
    mem: int = 0
    disk: int = 0
    cpu_used: float = 0.0
    mem_used: int = 0
    data_used: int = 0
    forges: dict[str, bool] = field(default_factory=dict)


@dataclass(slots=True)
class Report:
    host: Host
    rows: list[Row]


@dataclass(slots=True)
class Page:
    """One app's page: its rows, its hosts, its recent deploys, its log tail."""

    app: App
    image: str
    sha: str
    env_file: str
    rows: list[Row]
    hosts: list[str]
    deploys: list[dict[str, Any]]
    log: str
    template_moved: bool = False


async def report(yard: Yard, running_only: bool = False) -> Report:
    config, store, client = yard.config, yard.store, yard.podman
    apps = store.list_apps()
    listed = podman.parse_ps(await client.run("ps", "-a", "--format", "json"))
    try:
        samples = podman.parse_stats(await client.run("stats", "--no-stream", "--format", "json"))
    except YardError:
        samples = {}
    host = podman.parse_info(await client.run("info", "--format", "json"))
    if not host.disk:
        host.disk = disk_capacity(config.root)
    crashes = await _crashes(yard, listed)

    # Apply containers in a deploy step are noise. Everything else shows.
    grouped: dict[str, list[Container]] = {}
    other: list[Container] = []
    for item in listed.values():
        if item.role == "deploy":
            continue
        if item.app:
            grouped.setdefault(item.app, []).append(item)
        else:
            other.append(item)

    rows: list[Row] = []
    for app in apps:
        items = [item for item in grouped.pop(app.name, []) if item.role == "app"]
        tip = yard.tips.get(app.name)
        deployed = read_deployed(config, app.name)
        note = deploy_record(listed, app.name)
        hosts = await sites.list_hosts(yard, app.name)
        rows += _app_rows(config, app, items, samples, tip, deployed, note, hosts, crashes, running_only)
    for name in sorted(grouped):
        for item in sorted(grouped[name], key=_age):
            if running_only and not item.running:
                continue
            rows.append(_row(config, item, None, samples.get(item.name), None, None, None, [], crashes))
    for item in sorted(other, key=lambda one: one.name):
        if running_only and not item.running:
            continue
        rows.append(_row(config, item, None, samples.get(item.name), None, None, None, [], crashes))
    shown = [samples.get(row.container) for row in rows]
    return Report(
        host=Host(
            cpus=host.cpus,
            mem=host.mem,
            disk=host.disk,
            cpu_used=sum(sample.cpu_percent for sample in shown if sample),
            mem_used=sum(sample.mem_bytes for sample in shown if sample),
            data_used=sum(row.data for row in rows),
            forges={forge.name: forge.connected() for forge in yard.forges.values()},
        ),
        rows=rows,
    )


async def _crashes(yard: Yard, listed: dict[str, Container]) -> dict[str, tuple[bool, str]]:
    """OOM and the exit error for each stopped container, in one inspect call."""
    down = [item.name for item in listed.values() if not item.running]
    if not down:
        return {}
    try:
        return podman.parse_states(await yard.podman.run("inspect", *down))
    except YardError:
        return {}


def deploy_record(listed: dict[str, Container], name: str) -> dict[str, Any] | None:
    """The deploy container is the deploy. One row, or none."""
    item = listed.get(deploy_container(name))
    if item is None:
        return None
    if item.running:
        state = "running"
    elif item.exit_code == 0:
        state = "ok"
    else:
        state = "failed"
    return {
        "app": name,
        "sha": image_sha(item.image),
        "image": item.image,
        "status": state,
        "started": stamp(item.started) if item.started else "",
        "ended": stamp(item.exited) if item.exited else "",
    }


def _site_target(app: App, items: list[Container]) -> str:
    """The app container `<name>-app-1` carries the hosts of the site file."""
    want = app_container(app.name)
    return want if any(item.name == want for item in items) else ""


def _app_rows(
    config: Config,
    app: App,
    items: list[Container],
    samples: dict[str, Sample],
    tip: Tip | None,
    deployed: Deployed | None,
    note: dict[str, Any] | None,
    hosts: list[str],
    crashes: dict[str, tuple[bool, str]],
    running_only: bool,
) -> list[Row]:
    shown = [item for item in items if not running_only or item.running]
    target = _site_target(app, shown)
    rows: list[Row] = []
    for item in sorted(shown, key=_age):
        rows.append(
            _row(
                config,
                item,
                app,
                samples.get(item.name),
                tip,
                deployed,
                note,
                hosts if item.name == target else [],
                crashes,
            )
        )
    if not items and not running_only:
        rows.append(_row(config, None, app, None, tip, deployed, note, hosts, crashes))
    return rows


async def app_report(yard: Yard, name: str) -> Page:
    """The one-app page: its rows, its hosts, its recent deploys, its log tail."""
    config, store, client = yard.config, yard.store, yard.podman
    app = store.app(name)
    listed = podman.parse_ps(await client.run("ps", "-a", "--format", "json"))
    try:
        samples = podman.parse_stats(await client.run("stats", "--no-stream", "--format", "json"))
    except YardError:
        samples = {}
    tip = yard.tips.get(app.name)
    deployed = read_deployed(config, name)
    items = [item for item in listed.values() if item.app == name and item.role == "app"]
    hosts = await sites.list_hosts(yard, app.name)
    note = deploy_record(listed, name)
    crashes = await _crashes(yard, listed)
    rows = _app_rows(config, app, items, samples, tip, deployed, note, hosts, crashes, False)
    page_log = await _container_tail(yard, app_container(name))
    image = items[0].image if items else ""
    return Page(
        app=app,
        image=image,
        sha=deployed.revision if deployed else image_sha(image),
        env_file=str(config.env(name)),
        rows=rows,
        hosts=hosts,
        deploys=[note] if note else [],
        log=page_log,
        template_moved=any(row.template_moved for row in rows),
    )


def _age(item: Container) -> tuple[float, str]:
    return (-(item.created.timestamp() if item.created else 0), item.name)


def _health(item: Container) -> str:
    if item.health in {"healthy", "unhealthy", "starting"}:
        return item.health
    if item.state == "dead" or (item.state == "exited" and item.exit_code != 0):
        return "failed"
    return ""


def _row(
    config: Config,
    item: Container | None,
    app: App | None,
    sample: Sample | None,
    tip: Tip | None,
    deployed: Deployed | None,
    note: dict[str, Any] | None,
    hosts: list[str],
    crashes: dict[str, tuple[bool, str]],
) -> Row:
    """One rendered row. app None means the container has no app row behind it."""
    image = (item.image if item and item.image else "") or ""
    if item:
        health = _health(item)
    else:
        health = "down"
    key = app.name if app else ((item.app or item.name) if item else "")
    oom, error = crashes.get(item.name, (False, "")) if item and not item.running else (False, "")
    return Row(
        app=app.name if app else (item.app if item else ""),
        container=item.name if item else "",
        service=item.service if item else "",
        state=item.state if item else "absent",
        running=item.running if item else False,
        restarts=item.restarts if item else 0,
        health=health,
        exit_code=item.exit_code if item else 0,
        started=stamp(item.started) if item and item.started else "",
        exited=stamp(item.exited) if item and item.exited else "",
        id=(item.id[:12] if item else ""),
        oom=oom,
        error=error,
        image=image,
        sha=(deployed.revision if deployed and deployed.revision else image_sha(image)) if app else image_sha(image),
        tip=tip.revision if tip else "",
        template_moved=bool(tip and tip.template and tip.template != (deployed.template if deployed else "")),
        hosts=hosts,
        trigger=app.trigger if app else "",
        cpu=sample.cpu if sample else "",
        mem=sample.mem if sample else "",
        data=dir_size(config.data(key)),
        deploy=note,
    )


def dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            file = Path(root) / name
            try:
                total += file.stat().st_size
            except OSError:
                continue
    return total


def disk_capacity(path: Path) -> int:
    current = path
    while True:
        try:
            stats = os.statvfs(current)
        except OSError:
            if current == current.parent:
                return 0
            current = current.parent
            continue
        return int(stats.f_blocks) * int(stats.f_frsize)


async def _container_tail(yard: Yard, name: str) -> str:
    """The journal first; a host without journald falls back to podman logs."""
    try:
        return await journal.read(yard.config, [f"CONTAINER_NAME={name}"], lines=20)
    except YardError:
        try:
            return await yard.podman.run("logs", "--tail", "20", name)
        except YardError:
            return ""
