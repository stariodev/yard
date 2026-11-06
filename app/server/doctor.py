"""Host checks for `yard status --full`. Each returns ok and a one-line detail."""

import asyncio
import shutil
import socket
import ssl
import stat
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.server import journal, podman, sites, status
from app.server.forges import Forge
from app.server.store import Config
from app.shared import YardError, age, app_container, duration, format_bytes, parse

if TYPE_CHECKING:
    from app.server.yard import Yard


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False


async def run(yard: Yard) -> list[Check]:
    config = yard.config
    # One look at Podman feeds every row: info once, ps once, inspect once.
    try:
        info: podman.Host | YardError = podman.parse_info(await yard.podman.run("info", "--format", "json"))
    except YardError as exc:
        info = exc
    checks = [
        _check("podman", _podman(info)),
        _check("network yard", await _exists(yard, "network", "yard", "rerun install.sh")),
        _check("apply image", await _exists(yard, "image", "localhost/yard-apply", "rerun install.sh")),
    ]

    if config.boot:
        try:
            listed: dict[str, podman.Container] | YardError = podman.parse_ps(
                await yard.podman.run("ps", "-a", "--format", "json")
            )
        except YardError as exc:
            listed = exc
        inspected = await _inspect(yard, listed)
        checks.append(_check("caddy", _caddy_up(listed)))
        checks.append(_check("caddy admin", await _caddy_admin(yard)))
        checks.append(_check("caddy config", await sites.check(yard)))
        checks.append(_check("podman journald", _podman_journald(info)))
        checks.append(_check("journal", await _journal(yard)))
        checks.append(_check("container logs", _container_logs(listed, inspected)))
        checks.append(_check("yard events", _yard_events(config)))
        checks += _healthchecks(yard, inspected)
        # The first app that names a host owns it. A laptop run has no host network: no proxy rows.
        hosts: dict[str, str] = {}
        for app in yard.store.list_apps():
            for host in await sites.list_hosts(yard, app.name):
                hosts.setdefault(host, app.name)
        for host, name in hosts.items():
            checks.append(_check(f"dns {host}", await _dns(host)))
            checks.append(_check(f"certificate {host}", _certificate(config, host)))
            checks.append(_check(f"https {host}", await _https(host, name)))

    checks += await _forge_rows(yard)
    checks.append(_check("prune", _prune(yard)))
    checks.append(_check("disk", await _disk(yard)))
    for name in sorted(yard.tips):
        error = yard.tips[name].error
        if not error:
            continue
        checks.append(
            Check(
                f"source {name}",
                True,
                f"{error}; Yard keeps running the last version and tries again at the next check.",
                warn=True,
            )
        )
    return checks


async def _forge_rows(yard: Yard) -> list[Check]:
    """Each forge: installation, token, and the last delivery and check.

    The installations are the saved ones: the installation webhook keeps them.
    The token is cached for an hour, so a sweep asks the forge nothing new.
    A forge nothing uses and nothing connects to earns no row at all.
    """
    used = {urlsplit(app.repo).hostname or "" for app in yard.store.list_apps()}
    rows = []
    for forge in yard.forges.values():
        if not forge.connected():
            if forge.host in used:
                rows.append(_check(forge.name, (False, f"not connected; run yard forge setup {forge.name}")))
            continue
        rows += [
            *_forge_installation_rows(forge),
            _check(f"{forge.name} token", await _forge_token(forge)),
            _check(f"{forge.name} webhook", (True, _ago(forge.last_delivery))),
            _check(f"{forge.name} check", (True, _ago(forge.last_check))),
        ]
    return rows


def _forge_installation_rows(forge: Forge) -> list[Check]:
    """One row per saved installation of the app, named by account."""
    rows = []
    for installation in forge.installations:
        account = str(installation.get("account") or "")
        name = f"{forge.name} installation {account}" if account else f"{forge.name} installation"
        if str(installation.get("selection") or "") != "all":
            # Deploys work on a selected-repos install; yard repo new refuses it.
            where = forge.settings_url(installation) or f"the installation page on {forge.host}"
            detail = f"selected repos only; yard repo new needs all repositories: {where}"
            rows.append(_check(name, (True, detail, True)))
        else:
            rows.append(_check(name, (True, "all repos")))
    return rows


async def _forge_token(forge: Forge) -> tuple[bool, str]:
    try:
        await forge.token()
    except YardError as exc:
        return False, f"{exc}; rerun yard forge setup {forge.name}"
    return True, "ok"


def _ago(stamp: str) -> str:
    moment = parse(stamp)
    if moment is None:
        return "none since boot"
    return _since(time.time() - moment.timestamp())


def _since(seconds: float) -> str:
    text = age(max(0, seconds))
    return "just now" if text == "now" else f"{text} ago"


def _check(name: str, pair: tuple[bool, str] | tuple[bool, str, bool]) -> Check:
    ok, detail, *warn = pair
    return Check(name, ok, detail, bool(warn and warn[0]))


def _podman(info: podman.Host | YardError) -> tuple[bool, str]:
    if isinstance(info, YardError):
        return False, f"{info}; run sudo -u yard podman info on the host"
    return True, f"podman {info.version}" if info.version else "podman"


async def _inspect(yard: Yard, listed: dict[str, podman.Container] | YardError) -> str | YardError:
    """One inspect of the running containers and the app containers: the log driver and the healthcheck."""
    if isinstance(listed, YardError):
        return listed
    apps = {app_container(app.name) for app in yard.store.list_apps()}
    names = sorted(name for name, item in listed.items() if item.running or name in apps)
    if not names:
        return "[]"
    try:
        return await yard.podman.run("inspect", *names, "--format", "json")
    except YardError as exc:
        return exc


async def _exists(yard: Yard, kind: str, name: str, hint: str = "") -> tuple[bool, str]:
    try:
        await yard.podman.run(kind, "exists", name)
    except YardError:
        return False, f"{name} missing" + (f"; {hint}" if hint else "")
    return True, f"{name} exists"


def _caddy_up(listed: dict[str, podman.Container] | YardError) -> tuple[bool, str]:
    if isinstance(listed, YardError):
        return False, str(listed)
    found = next((item for item in listed.values() if item.app == "yard" and item.service == "caddy"), None)
    if found is None:
        return False, "absent; rerun install.sh"
    if not found.running:
        return False, f"{found.state} not running; sudo -u yard podman start yard-caddy-1"
    return True, "running"


async def _caddy_admin(yard: Yard) -> tuple[bool, str]:
    try:
        await yard.caddy.request("GET", "/config/")
    except YardError as exc:
        return False, f"{exc}; sudo -u yard podman restart yard-caddy-1"
    return True, "ok"


def _podman_journald(info: podman.Host | YardError) -> tuple[bool, str]:
    """Podman must write container logs and engine events to the journal."""
    if isinstance(info, YardError):
        return False, f"{info}; check podman on the host"
    detail = f"logs {info.log_driver or 'unset'} · events {info.event_logger or 'unset'}"
    if info.log_driver != "journald" or info.event_logger != "journald":
        return False, f"{detail}; rerun install.sh"
    return True, detail


def _yard_events(config: Config) -> tuple[bool, str]:
    """Yard's own events go to the journald socket the container mounts."""
    path = config.journal_socket
    try:
        mode = path.stat().st_mode
    except OSError:
        return False, f"no socket at {path}; events fall back to stdout; rerun install.sh"
    if not stat.S_ISSOCK(mode):
        return False, f"{path} is not a socket; rerun install.sh"
    return True, str(path)


async def _journal(yard: Yard) -> tuple[bool, str]:
    try:
        await journal.read(yard.config, [], lines=0)
    except YardError as exc:
        return False, f"{exc}; check journald on the host"
    return True, str(yard.config.journal_dir)


def _container_logs(listed: dict[str, podman.Container] | YardError, inspected: str | YardError) -> tuple[bool, str]:
    """Every running container must already log to journald."""
    if isinstance(listed, YardError) or isinstance(inspected, YardError):
        return False, f"{listed if isinstance(listed, YardError) else inspected}; check podman on the host"
    running = [name for name, item in listed.items() if item.running]
    if not running:
        return True, "0 containers"
    types = podman.parse_log_types(inspected)
    stale = [name for name in running if types.get(name, "") != "journald"]
    if stale:
        return False, f"redeploy to switch: {', '.join(sorted(stale))}"
    return True, f"{len(running)} containers"


def _healthchecks(yard: Yard, inspected: str | YardError) -> list[Check]:
    """One row per app container's HEALTHCHECK. An app with no container earns none."""
    if isinstance(inspected, YardError):
        return []  # the podman and container logs rows already fail
    found = podman.parse_health(inspected)
    checks = []
    for app in yard.store.list_apps():
        container = app_container(app.name)
        if (health := found.get(container)) is not None:
            checks.append(_healthcheck(app.name, container, health))
    return checks


def _healthcheck(app: str, container: str, health: podman.Healthcheck) -> Check:
    """Absent or slow checks warn; a failing one fails."""
    if health.status == "unhealthy":
        return Check(f"healthcheck {app}", False, f"unhealthy; read yard logs {container}")
    if not health.test or health.test == ["NONE"]:
        return Check(
            f"healthcheck {app}",
            True,
            "none; a broken app stays live. Add HEALTHCHECK to the Containerfile: stario.dev/yard/healthchecks",
            warn=True,
        )
    # Podman runs the first check at start. A check that fails there because the app is not up yet
    # runs again one interval later. A check with a timeout over 30 s can wait for the app itself.
    if health.interval > 60_000_000_000 and health.timeout <= 30_000_000_000:
        return Check(
            f"healthcheck {app}",
            True,
            f"every {_ns_text(health.interval)}; when the first check runs before the app is up,"
            " the deploy waits one interval and fails after 120 s. Use --interval=10s, or let the check"
            " wait for the app: curl --retry-connrefused with a --timeout over 30s",
            warn=True,
        )
    detail = f"every {_ns_text(health.interval)}"
    if health.status:
        detail += f" · {health.status}"
    return Check(f"healthcheck {app}", True, detail)


def _ns_text(nanos: int) -> str:
    """A healthcheck interval: 10s, 5m, 1h. Whole minutes and hours drop the zeros."""
    seconds = nanos / 1e9
    hours, rest = divmod(seconds, 3600)
    if hours and not rest:
        return f"{int(hours)}h"
    minutes, rest = divmod(seconds, 60)
    if minutes and not rest:
        return f"{int(minutes)}m"
    return duration(timedelta(seconds=seconds))


def _prune(yard: Yard) -> tuple[bool, str, bool]:
    """The daily auto-prune. A failed run is a warning: `yard prune` still works by hand."""
    days = yard.config.prune_days
    if days <= 0:
        return True, "off (YARD_PRUNE_DAYS=0); run yard prune by hand", False
    base = f"daily, keeps {days} days"
    if yard.last_prune_error:
        return True, f"{base}; {yard.last_prune_error}", True
    if not yard.last_prune:
        return True, f"{base}; not run yet", False
    return True, f"{base}; last {_ago(yard.last_prune)}, freed {format_bytes(yard.last_prune_freed)}", False


async def _https(host: str, app: str) -> tuple[bool, str]:
    """One GET through Caddy: /health for Yard, / for an app. A 5xx fails."""
    # Yard shares Caddy's network, so the check dials the service name.
    path = "/health" if app == "yard" else "/"
    request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()

    def round_trip() -> bytes:
        with socket.create_connection(("caddy", 443), 10) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                tls.sendall(request)
                return tls.recv(4096).split(b"\r\n", 1)[0]

    try:
        line = await asyncio.to_thread(round_trip)
    except OSError as exc:  # ssl.SSLError is an OSError
        return False, f"{exc}; yard logs yard-caddy-1"
    text = line.decode(errors="replace").strip()
    if not line.startswith(b"HTTP/"):
        return False, (text or "no answer") + "; yard logs yard-caddy-1"
    # Caddy answers 502 for an app that is down: a 5xx is the app's failure, not an answer.
    code = text.split(" ", 2)[1] if text.count(" ") else ""
    if code.startswith("5"):
        return False, f"{text}; yard logs {app_container(app)}"
    return True, text


async def _dns(host: str) -> tuple[bool, str]:
    try:
        found = await asyncio.to_thread(socket.getaddrinfo, host, 443)
    except OSError as exc:
        return False, f"{exc}; point the A record at this host"
    return True, str(found[0][4][0])


def _certificate(config: Config, host: str) -> tuple[bool, str]:
    # Caddy's /data volume is lib/, and it writes under a caddy/ directory in it.
    root = config.caddy_data() / "caddy" / "certificates"
    found = list(root.glob(f"*/{host}/{host}.crt")) if root.is_dir() else []
    if not found:
        return False, "not issued yet; check dns, then yard logs yard-caddy-1"
    return True, f"issued {_since(time.time() - found[0].stat().st_mtime)}"


async def _disk(yard: Yard) -> tuple[bool, str]:
    """Filesystem free, then where the space went: images, app data, the journal."""
    config = yard.config
    usage = shutil.disk_usage(config.root)
    parts = [f"{config.root} {format_bytes(usage.free)} free of {format_bytes(usage.total)}"]
    try:
        df = podman.parse_system_df(await yard.podman.run("system", "df", "--format", "json"))
    except YardError:
        df = None
    if df is not None:
        size, reclaimable = df
        parts.append(f"images {format_bytes(size)} ({format_bytes(reclaimable)} reclaimable)")
    data = sum(status.dir_size(config.data(app.name)) for app in yard.store.list_apps())
    parts.append(f"data {format_bytes(data)}")
    if config.journal_dir.is_dir():
        parts.append(f"journal {format_bytes(status.dir_size(config.journal_dir))}")
    detail = " · ".join(parts)
    if usage.free < usage.total * 0.1:
        return False, f"{detail}; try yard prune"
    return True, detail
