"""The Caddy site file of each app. Yard stores the text; Caddy checks it, loads it, and serves it."""

import asyncio
import logging
from pathlib import Path

from stario import json

from app.server import caddy, journal
from app.server.yard import Yard
from app.shared import YardError, atomic_write, default_site

log = logging.getLogger("yard")

MAX_BYTES = 64 << 10


def get_site(yard: Yard, name: str) -> str:
    """The site file text, or "" when the app has none."""
    path = yard.config.site(name)
    return path.read_text() if path.is_file() else ""


async def set_site(yard: Yard, name: str, text: str, *, actor: str = "") -> list[str]:
    """Write the site file and load Caddy. A refused load puts the old file back. Empty text removes it."""
    if len(text.encode()) > MAX_BYTES:
        raise YardError(400, "too_large", f"a site file is at most {MAX_BYTES >> 10} KiB")
    path = yard.config.site(name)
    async with yard.sites_lock:
        old = path.read_text() if path.is_file() else None
        _write(path, text)
        try:
            await yard.caddy.load(yard.config.caddyfile)
        except YardError:
            _write(path, old or "")
            raise
    journal.send(yard.config, "caddy.set", f"{name} caddy", app=name, actor=actor)
    return await list_hosts(yard, name)


async def remove_site(yard: Yard, name: str, *, actor: str = "") -> str:
    """Remove the site file and load Caddy. The error text, or "" when Caddy took it."""
    path = yard.config.site(name)
    if not path.is_file():
        return ""
    async with yard.sites_lock:
        path.unlink()
        try:
            await yard.caddy.load(yard.config.caddyfile)
        except YardError as exc:
            return exc.message
    journal.send(yard.config, "caddy.set", f"{name} caddy", app=name, actor=actor)
    return ""


async def list_hosts(yard: Yard, name: str) -> list[str]:
    """The hosts in the app's site file, as Caddy reads them. Empty when Caddy does not answer.

    Caddy adapts each file once; the answer stays until the file changes.
    """
    path = yard.config.site(name)
    try:
        info = path.stat()
    except OSError:
        return []
    stamp = (info.st_mtime_ns, info.st_size)
    cached = yard.site_hosts.get(name)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    text = path.read_text()
    if not text.strip():
        return []
    try:
        hosts = caddy.hosts(await yard.caddy.adapt(text))
    except YardError:
        return []
    yard.site_hosts[name] = (stamp, hosts)
    return hosts


async def check(yard: Yard) -> tuple[bool, str]:
    """Caddy adapts the main Caddyfile and every site file on disk, and loads nothing."""
    try:
        config = await yard.caddy.adapt(f"import {yard.config.caddyfile}\n")
    except YardError as exc:
        return False, exc.message
    count = sum(1 for path in yard.config.caddy_sites().glob("*.caddy"))
    return True, f"{count} site files · {len(caddy.hosts(config))} hosts"


async def boot(yard: Yard, *, tries: int = 60, pause: float = 2.0) -> None:
    """Load Caddy once Yard starts, so the site files are what Caddy serves. Caddy can start later than Yard."""
    for _ in range(tries):
        try:
            async with yard.sites_lock:
                written = await _import_binds(yard)
                try:
                    await yard.caddy.load(yard.config.caddyfile)
                except YardError:
                    for path in written:
                        path.unlink(missing_ok=True)
                    raise
        except YardError as exc:
            if exc.status != 502:
                log.warning("caddy refused the site files: %s", exc.message)
                return
            await asyncio.sleep(pause)
            continue
        if written:
            names = ",".join(path.stem for path in written)
            journal.send(yard.config, "caddy.import", f"imported the binds of {names}", apps=names)
        return
    log.warning("caddy did not answer; the site files load at the next change")


async def _import_binds(yard: Yard) -> list[Path]:
    """The move from 0.10 binds: with no site file yet, each app gets one from the routes Caddy resumed.

    A bind route has an `@id` of yard-<host> and one upstream. The dial `<app>-<service>-1` names the app.
    """
    if any(yard.config.caddy_sites().glob("*.caddy")):
        return []
    _code, raw = await yard.caddy.request("GET", "/config/")
    servers = _dict(_dict(_dict(json.loads(raw or b"null")).get("apps")).get("http")).get("servers")
    names = {app.name for app in yard.store.list_apps()}
    found: dict[str, dict[tuple[str, str], list[str]]] = {}
    for route in _dict(_dict(servers).get("yard")).get("routes") or []:
        host, dial, window = _bind(route)
        name = _owner(dial, names)
        if not host or not name:
            if host:
                log.warning("bind %s -> %s names no app; it is not imported", host, dial)
            continue
        found.setdefault(name, {}).setdefault((dial, window), []).append(host)
    written = []
    for name, blocks in found.items():
        path = yard.config.site(name)
        text = "\n".join(default_site(hosts, dial, window) for (dial, window), hosts in blocks.items())
        atomic_write(path, text, 0o644)
        written.append(path)
    return written


def _bind(route: object) -> tuple[str, str, str]:
    """(host, dial, window) of one bind route; empty strings for any other route."""
    row = _dict(route)
    if not str(row.get("@id") or "").startswith("yard-"):
        return "", "", ""
    match = (row.get("match") or [{}])[0]
    proxy = (row.get("handle") or [{}])[0]
    hosts = _dict(match).get("host") or [""]
    upstream = (_dict(proxy).get("upstreams") or [{}])[0]
    window = _dict(_dict(proxy).get("load_balancing")).get("try_duration") or "10s"
    if isinstance(window, int):
        window = f"{window // 1_000_000_000}s"
    return str(hosts[0]), str(_dict(upstream).get("dial") or ""), str(window)


def _owner(dial: str, names: set[str]) -> str:
    """The app a dial points at: `yard` (the old alias), or `<app>-<service>-1` for a known app."""
    host = dial.rpartition(":")[0] or dial
    if host == "yard":
        return "yard"
    stem = host.removesuffix("-1").rpartition("-")[0]
    return stem if stem in names else ""


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _write(path: Path, text: str) -> None:
    if text.strip():
        atomic_write(path, text, 0o644)
    else:
        path.unlink(missing_ok=True)
