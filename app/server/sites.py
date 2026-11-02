"""The Caddy site file of each app. Yard stores the text; Caddy checks it, loads it, and serves it."""

from pathlib import Path

from app.server import caddy, journal
from app.server.yard import Yard
from app.shared import YardError, atomic_write

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
    """The hosts in the app's site file, as Caddy reads them. Empty when Caddy does not answer."""
    text = get_site(yard, name)
    if not text.strip():
        return []
    try:
        return caddy.hosts(await yard.caddy.adapt(text))
    except YardError:
        return []


async def check(yard: Yard) -> tuple[bool, str]:
    """Caddy adapts the main Caddyfile and every site file on disk, and loads nothing."""
    try:
        config = await yard.caddy.adapt(f"import {yard.config.caddyfile}\n")
    except YardError as exc:
        return False, exc.message
    count = sum(1 for path in yard.config.caddy_sites().glob("*.caddy"))
    return True, f"{count} site files · {len(caddy.hosts(config))} hosts"


def _write(path: Path, text: str) -> None:
    if text.strip():
        atomic_write(path, text, 0o644)
    else:
        path.unlink(missing_ok=True)
