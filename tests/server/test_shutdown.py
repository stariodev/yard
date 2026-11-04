"""Background loops stop at shutdown instead of holding it for the grace period."""

import asyncio
from pathlib import Path

from stario import App

from app.server import apps
from tests.server.conftest import make_yard


async def test_pause_sleeps_without_an_app(tmp_path: Path):
    yard = make_yard(tmp_path)
    assert await yard.pause(0) is True


async def test_pause_ends_at_shutdown(tmp_path: Path):
    yard = make_yard(tmp_path)
    app = App()
    yard.attach(app)
    pausing = asyncio.create_task(yard.pause(3600))
    await asyncio.sleep(0)
    app.signal_shutdown()
    assert await asyncio.wait_for(pausing, 1) is False
    # Once the app shuts down, a new pause returns at once.
    assert await asyncio.wait_for(yard.pause(3600), 1) is False


async def test_pause_runs_out_before_shutdown(tmp_path: Path):
    yard = make_yard(tmp_path)
    yard.attach(App())
    assert await yard.pause(0.01) is True


async def test_check_and_prune_loops_stop_at_shutdown(tmp_path: Path):
    yard = make_yard(tmp_path)
    app = App()
    yard.attach(app)
    loops = [asyncio.create_task(apps.loop(yard)), asyncio.create_task(apps.prune_loop(yard))]
    await asyncio.sleep(0)
    app.signal_shutdown()
    await asyncio.wait_for(asyncio.gather(*loops), 1)
