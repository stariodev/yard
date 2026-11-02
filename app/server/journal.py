"""Journald, both directions. Podman writes container logs and engine events; Yard
writes its own events to the native socket; journalctl reads them all back.

A container's log survives its removal, so `yard logs` still answers after
a redeploy. A missing journal or journalctl is a 502 pointing at install.sh.
A missing journald socket is not an error: the event lands on stdout instead.
"""

import asyncio
import socket
import struct
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime

from stario import json

from app.server.store import Config
from app.shared import YardError

_HELP = "Rerun install.sh to enable journald logs."


def _argv(
    config: Config,
    matches: list[str],
    lines: int,
    since: str = "",
    until: str = "",
    after: str = "",
    follow: bool = False,
) -> list[str]:
    argv = [
        config.journalctl_bin,
        "-D",
        str(config.journal_dir),
        "--no-pager",
        "-o",
        "json",
        "-n",
        # "+N" takes the first lines after the cursor; a tail of the window
        # would silently drop the backlog a poller has not seen yet.
        f"+{lines}" if after else str(lines),
    ]
    if since:
        argv += ["--since", since]
    if until:
        argv += ["--until", until]
    if after:
        argv += ["--after-cursor", after]
    if follow:
        argv.append("-f")
    # Repeating one field name is OR for journalctl.
    return argv + matches


async def _spawn(config: Config, argv: list[str]) -> asyncio.subprocess.Process:
    if not config.journal_dir.is_dir():
        raise YardError(502, "journal", f"no journal at {config.journal_dir}", help_text=_HELP)
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise YardError(502, "journal", f"journalctl: {exc}", help_text=_HELP) from exc


async def _run(config: Config, argv: list[str]) -> bytes:
    proc = await _spawn(config, argv)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 30)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise YardError(502, "journal", "journalctl timed out", help_text=_HELP) from exc
    if proc.returncode:
        detail = (err or out or b"").decode(errors="replace").strip()
        raise YardError(502, "journal", f"journalctl: {detail}", help_text=_HELP)
    return out or b""


async def read(
    config: Config,
    matches: list[str],
    *,
    lines: int,
    since: str = "",
    until: str = "",
    after: str = "",
) -> str:
    """Formatted lines, oldest first. journalctl -n already picks the tail."""
    out = await _run(config, _argv(config, matches, lines, since, until, after))
    return "\n".join(line for entry in entries(out) if (line := format_line(entry)))


async def read_entries(
    config: Config,
    matches: list[str],
    *,
    lines: int,
    since: str = "",
    until: str = "",
    after: str = "",
) -> list[dict]:
    """The raw entries, oldest first."""
    out = await _run(config, _argv(config, matches, lines, since, until, after))
    return list(entries(out))


async def follow(
    config: Config,
    matches: list[str],
    *,
    lines: int,
    since: str = "",
) -> asyncio.subprocess.Process:
    """The follow process. stdout yields one JSON entry per line."""
    return await _spawn(config, _argv(config, matches, lines, since=since, follow=True))


async def follow_entries(proc: asyncio.subprocess.Process, *, tick: float | None = None) -> AsyncGenerator[dict | None]:
    """The entries of a follow process, then None at each `tick` seconds with no new line.

    The process ends when the iterator closes: wrap it in contextlib.aclosing.
    """
    assert proc.stdout is not None
    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), tick)
            except TimeoutError:
                yield None
                continue
            if not raw:
                return
            for entry in entries(raw):
                yield entry
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


def send(config: Config, event: str, message: str, *, priority: int = 6, **fields: str) -> None:
    """One journald datagram with native fields. Never raises.

    A missing or failing socket falls back to one JSON line on stdout, which
    lands in the journal as the yard container's own output.
    """
    pairs = [
        ("SYSLOG_IDENTIFIER", "yard"),
        ("MESSAGE", message),
        ("PRIORITY", str(priority)),
        ("YARD_EVENT", event),
        *((f"YARD_{key.upper()}", str(value)) for key, value in fields.items() if str(value)),
    ]
    datagram = b""
    for key, value in pairs:
        raw = value.encode(errors="replace")[:4096]
        if b"\n" in raw:
            # The native protocol frames a multi-line value with a length prefix.
            datagram += key.encode() + b"\n" + struct.pack("<Q", len(raw)) + raw + b"\n"
        else:
            datagram += key.encode() + b"=" + raw + b"\n"
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(datagram, str(config.journal_socket))
        finally:
            sock.close()
    except OSError:
        print(json.dumps({"yard": event, "message": message, **fields}))


def entries(raw: bytes) -> Iterator[dict]:
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def timestamp(entry: dict) -> str:
    """__REALTIME_TIMESTAMP microseconds as ISO-8601 UTC, to the second."""
    micros = entry.get("__REALTIME_TIMESTAMP")
    if micros is None or not str(micros).isdigit():
        return ""
    moment = datetime.fromtimestamp(int(micros) / 1_000_000, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def message_text(entry: dict) -> str:
    message = entry.get("MESSAGE")
    if isinstance(message, list):
        # Non-UTF-8 output arrives as a list of byte values.
        try:
            return bytes(int(part) for part in message).decode("utf-8", errors="replace")
        except TypeError, ValueError:
            return ""
    return str(message or "")


def format_line(entry: dict) -> str:
    """<ISO-8601 UTC, seconds> <CONTAINER_NAME> <MESSAGE>; "" on missing fields."""
    return f"{timestamp(entry)} {entry.get('CONTAINER_NAME') or ''} {message_text(entry)}".strip()
