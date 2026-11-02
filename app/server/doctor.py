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

from app.server import compose, journal, podman, sites, status
from app.server.forges import Forge
from app.server.store import Config
from app.shared import YardError, age, duration, format_bytes, parse

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
    checks = [
        _check("podman", await _podman(yard)),
        _check("network yard", await _exists(yard, "network", "yard", "rerun install.sh")),
        _check("apply image", await _exists(yard, "image", "localhost/yard-apply", "rerun install.sh")),
    ]

    if not config.boot:
        # A laptop run has no host network; the proxy checks do not apply.
        hosts: list[str] = []
        yard_hosts: set[str] = set()
    else:
        checks.append(_check("caddy", await _caddy_up(yard)))
        checks.append(_check("caddy admin", await _caddy_admin(yard)))
        checks.append(_check("caddy config", await sites.check(yard)))
        checks.append(_check("podman journald", await _podman_journald(yard)))
        checks.append(_check("journal", await _journal(yard)))
        checks.append(_check("container logs", await _container_logs(yard)))
        checks.append(_check("yard events", _yard_events(config)))
        checks += await _healthchecks(yard)
        hosts, yard_hosts = [], set()
        for app in yard.store.list_apps():
            found = await sites.list_hosts(yard, app.name)
            hosts += [host for host in found if host not in hosts]
            if app.name == "yard":
                yard_hosts.update(found)
    if config.boot:
        for host in hosts:
            checks.append(_check(f"dns {host}", await _dns(host)))
            checks.append(_check(f"certificate {host}", _certificate(config, host)))
            path = "/health" if host in yard_hosts else "/"
            checks.append(_check(f"https {host}", await _https(host, path)))

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
    """Each forge: app, installation, token, and the last delivery and check.

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
            _check(f"{forge.name} app", await _forge_app(forge)),
            *await _forge_installation_rows(forge),
            _check(f"{forge.name} token", await _forge_token(forge)),
            _check(f"{forge.name} webhook", (True, _ago(forge.last_delivery))),
            _check(f"{forge.name} check", (True, _ago(forge.last_check))),
        ]
    return rows


async def _forge_app(forge: Forge) -> tuple[bool, str]:
    try:
        body = await forge.get_app()
    except YardError as exc:
        return False, f"{exc}; check the app on {forge.host}"
    name = body.get("displayName") or body.get("display_name") or body.get("name") or ""
    return True, str(name) if name else "ok"


async def _forge_installation_rows(forge: Forge) -> list[Check]:
    """One row per installation of the app, named by account."""
    try:
        installations = await forge.find_installations()
    except YardError as exc:
        return [_check(f"{forge.name} installation", (False, f"{exc}; check the installation on {forge.host}"))]
    if not installations:
        return [_check(f"{forge.name} installation", (False, "no installation; reinstall it from the forge app page"))]
    rows = []
    for installation in installations:
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


async def _podman(yard: Yard) -> tuple[bool, str]:
    try:
        info = podman.parse_info(await yard.podman.run("info", "--format", "json"))
    except YardError as exc:
        return False, f"{exc}; run sudo -u yard podman info on the host"
    return True, f"podman {info.version}" if info.version else "podman"


async def _exists(yard: Yard, kind: str, name: str, hint: str = "") -> tuple[bool, str]:
    try:
        await yard.podman.run(kind, "exists", name)
    except YardError:
        return False, f"{name} missing" + (f"; {hint}" if hint else "")
    return True, f"{name} exists"


async def _caddy_up(yard: Yard) -> tuple[bool, str]:
    try:
        items = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json")).values()
        found = next((item for item in items if item.app == "yard" and item.service == "caddy"), None)
    except YardError as exc:
        return False, str(exc)
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


async def _podman_journald(yard: Yard) -> tuple[bool, str]:
    """Podman must write container logs and engine events to the journal."""
    try:
        info = podman.parse_info(await yard.podman.run("info", "--format", "json"))
    except YardError as exc:
        return False, f"{exc}; check podman on the host"
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


async def _container_logs(yard: Yard) -> tuple[bool, str]:
    """Every running container must already log to journald."""
    try:
        listed = podman.parse_ps(await yard.podman.run("ps", "--format", "json"))
        running = [item.name for item in listed.values() if item.running]
        if not running:
            return True, "0 containers"
        types = podman.parse_log_types(await yard.podman.run("inspect", *running))
    except YardError as exc:
        return False, f"{exc}; check podman on the host"
    stale = [name for name in running if types.get(name, "") != "journald"]
    if stale:
        return False, f"redeploy to switch: {', '.join(sorted(stale))}"
    return True, f"{len(running)} containers"


async def _healthchecks(yard: Yard) -> list[Check]:
    """One row per app container's HEALTHCHECK. The yard app has none on purpose."""
    checks = []
    for app in yard.store.list_apps():
        if app.name == "yard":
            continue
        container = compose.app_container(app.name)
        try:
            found = podman.parse_health(await yard.podman.run("inspect", container, "--format", "json"))
        except YardError:
            continue  # no app container yet, or podman is down (its own row covers that)
        health = found.get(container)
        if health is not None:
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
            "none; a broken build goes live. Add HEALTHCHECK to the Containerfile:"
            " stario.dev/yard/compose#healthchecks",
            warn=True,
        )
    if health.interval > 60_000_000_000:
        return Check(
            f"healthcheck {app}",
            True,
            f"every {_ns_text(health.interval)}; Podman runs the first check one interval"
            " after start and a deploy waits 120 s, so use --interval=10s",
            warn=True,
        )
    detail = f"every {_ns_text(health.interval)}"
    if health.status:
        detail += f" · {health.status}"
    return Check(f"healthcheck {app}", True, detail)


def _ns_text(nanos: int) -> str:
    """A healthcheck interval: 10s, 5m. Whole minutes drop the 0s."""
    seconds = nanos / 1e9
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


async def _https(host: str, path: str) -> tuple[bool, str]:
    # Yard shares Caddy's network, so the check dials the service name.
    request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()

    def round_trip() -> bytes:
        with socket.create_connection(("caddy", 443), 10) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                tls.sendall(request)
                return tls.recv(4096).split(b"\r\n", 1)[0]

    try:
        line = await asyncio.to_thread(round_trip)
    except (OSError, ssl.SSLError) as exc:
        return False, f"{exc}; yard logs yard-caddy-1"
    if line.startswith(b"HTTP/"):
        return True, line.decode(errors="replace").strip()
    return False, (line.decode(errors="replace").strip() or "no answer") + "; yard logs yard-caddy-1"


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
