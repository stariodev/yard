"""Fake programs that run in the test process. The server conftest patches the spawn."""

import asyncio
from collections.abc import Callable
from pathlib import Path

# Fake programs, by path: argv in, (stdout, stderr, exit code) out. Each one runs in the
# test process, because a /bin/sh or Python start costs 30-80 ms on each call.
FAKES: dict[str, Callable[[list[str]], tuple[bytes, bytes, int]]] = {}


def fake_program(path: Path, run: Callable[[list[str]], tuple[bytes, bytes, int]]) -> None:
    """Serve spawns of `path` from `run` in this process."""
    FAKES[str(path)] = run


class FakeProcess:
    """The part of asyncio.subprocess.Process that the server uses."""

    def __init__(self, out: bytes, err: bytes, code: int) -> None:
        self._out, self._err, self.returncode = out, err, code
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(out)
        self.stdout.feed_eof()

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._err

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass
