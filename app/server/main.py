"""Yard. `python -m app.server.main` serves; `yard login --token <token>` talks to it."""

import asyncio
import logging

import orjson
import uvloop
from stario import App, Span, json, serve

from app.server import api, apps, compose, sites
from app.server.store import Config, Store, ensure_admin
from app.server.yard import Yard
from app.shared import YardError

log = logging.getLogger("yard")


async def bootstrap(app: App, span: Span):
    json.set_codec(orjson.dumps, orjson.loads)
    config = Config.from_env()
    span.attrs({"yard.root": str(config.root), "yard.boot": config.boot})
    store = Store.open(config.root)
    token = ensure_admin(store)
    if token:
        span.event("yard.admin_token", {"path": str(config.token_file)})
    yard = Yard(config, store)
    yard.attach(app)
    if config.boot:
        await boot(yard, app)
    api.register(app, yard)

    try:
        yield
    finally:
        await close(yard)


async def boot(yard: Yard, app: App) -> None:
    """Directories, the yard record, the network, the two background tasks."""
    config = yard.config
    (config.root / "apps").mkdir(parents=True, exist_ok=True)
    (config.root / "tokens").mkdir(mode=0o700, parents=True, exist_ok=True)
    caddy_root = config.app_dir("yard") / "caddy"
    for part in ("config", "data", "run"):
        (caddy_root / part).mkdir(parents=True, exist_ok=True)
    config.caddy_sites().mkdir(parents=True, exist_ok=True)
    for forge in yard.forges.values():
        forge.dir.mkdir(parents=True, exist_ok=True)
    try:
        apps.ensure_host(yard)
    except YardError as exc:
        log.warning("host app: %s", exc)

    await _network(yard)
    # The apply image build pulls a base image. The server answers while that build runs.
    yard.tasks.append(app.create_task(_apply_image(yard), name="apply-image"))
    yard.tasks.append(app.create_task(apps.loop(yard), name="checks"))
    yard.tasks.append(app.create_task(sites.boot(yard), name="caddy"))
    if config.prune_days > 0:
        yard.tasks.append(app.create_task(apps.prune_loop(yard), name="prune"))


async def _network(yard: Yard) -> None:
    """The `yard` bridge: apply containers and apps join it, Caddy resolves them on it."""
    try:
        await yard.podman.run("network", "exists", "yard")
    except YardError:
        try:
            await yard.podman.run("network", "create", "yard")
        except YardError as exc:
            log.warning("network: %s", exc)


async def _apply_image(yard: Yard) -> None:
    try:
        code, text = await compose.build_apply(yard.config)
        if code:
            log.warning("apply image: %s", text.strip().splitlines()[-1] if text.strip() else code)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("apply image: %s", exc)


async def close(yard: Yard) -> None:
    for task in yard.tasks:
        task.cancel()
    if yard.tasks:
        await asyncio.gather(*yard.tasks, return_exceptions=True)


def main() -> None:
    uvloop.run(serve(bootstrap))


if __name__ == "__main__":
    main()
