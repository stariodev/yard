"""Podman CLI and the JSON it prints. The socket stays on the host."""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from stario import json

from app.shared import YardError
from app.shared import parse as _parse_time


@dataclass(slots=True)
class Container:
    id: str = ""
    name: str = ""
    image: str = ""
    app: str = ""
    service: str = ""
    role: str = ""
    state: str = ""
    running: bool = False
    restarts: int = 0
    exit_code: int = 0
    health: str = ""
    created: datetime | None = None
    started: datetime | None = None
    exited: datetime | None = None
    # The yard.* labels of a deploy container, without the prefix.
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Sample:
    name: str
    cpu: str
    mem: str
    cpu_percent: float = 0.0
    mem_bytes: int = 0


@dataclass(slots=True)
class Host:
    cpus: int = 0
    mem: int = 0
    disk: int = 0
    version: str = ""
    log_driver: str = ""
    event_logger: str = ""


@dataclass(slots=True)
class Image:
    name: str
    created: datetime | None = None
    size: int = 0


@dataclass(slots=True)
class Healthcheck:
    """Config.Healthcheck plus the live State.Health status. Interval is nanoseconds."""

    test: list[str] = field(default_factory=list)
    interval: int = 0
    status: str = ""


class Podman:
    def __init__(self, binary: str = "podman", remote: bool = False) -> None:
        self.binary = binary or "podman"
        self.remote = remote

    def _args(self, args: tuple[str, ...]) -> list[str]:
        head = [self.binary]
        if self.remote:
            head.append("--remote")
        head.extend(args)
        return head

    async def run(
        self,
        *args: str,
        timeout: float | None = 20,
    ) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._args(args),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise YardError(
                502,
                "podman",
                f"podman {' '.join(args)}: {exc}",
                help_text="The podman command has to be on PATH.",
            ) from exc
        try:
            if timeout is None:
                out, err = await proc.communicate()
            else:
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError as exc:
            proc.kill()
            raise YardError(
                502,
                "podman",
                f"podman {' '.join(args)}: timed out",
                help_text="The command did not finish. The container may still be starting.",
            ) from exc
        text = ((out or b"") + (err or b"")).decode(errors="replace").strip()
        if proc.returncode:
            raise YardError(
                502,
                "podman",
                f"podman {' '.join(args)}: {text}",
                exit_code=proc.returncode,
                help_text="Yard talks to Podman through the socket on this host.",
            )
        return text

    async def stream(self, *args: str, on_line: Callable[[str], None] | None = None) -> tuple[int, str]:
        """Run with no timeout; stderr joins stdout, each line reaches on_line. A non-zero exit is not an error."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._args(args),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=16 * 1024 * 1024,
            )
        except FileNotFoundError as exc:
            raise YardError(
                502,
                "podman",
                f"podman {' '.join(args)}: {exc}",
                help_text="The podman command has to be on PATH.",
            ) from exc
        lines: list[str] = []
        assert proc.stdout is not None
        while chunk := await proc.stdout.readline():
            line = chunk.decode(errors="replace").rstrip("\n")
            lines.append(line)
            if on_line is not None:
                on_line(line)
        await proc.wait()
        return proc.returncode or 0, "".join(line + "\n" for line in lines)


def parse_info(raw: str) -> Host:
    try:
        info = json.loads(raw)
    except ValueError:
        return Host()
    if not isinstance(info, dict):
        return Host()
    host = info.get("host")
    store = info.get("store")
    cpus = mem = disk = 0
    log_driver = event_logger = ""
    if isinstance(host, dict):
        cpus = _int(host.get("cpus"))
        mem = _int(host.get("memTotal"))
        log_driver = str(host.get("logDriver") or "")
        event_logger = str(host.get("eventLogger") or "")
    if isinstance(store, dict):
        disk = _int(store.get("graphRootAllocated"))
    version = info.get("version")
    return Host(
        cpus,
        mem,
        disk,
        str(version.get("Version") or "") if isinstance(version, dict) else "",
        log_driver,
        event_logger,
    )


def parse_log_types(raw: str) -> dict[str, str]:
    """podman inspect: container name -> log driver."""
    found: dict[str, str] = {}
    for row in _rows(raw):
        name = str(row.get("Name") or row.get("name") or "").lstrip("/")
        config = row.get("HostConfig")
        log = config.get("LogConfig") if isinstance(config, dict) else None
        kind = log.get("Type") if isinstance(log, dict) else ""
        if name:
            found[name] = str(kind or "")
    return found


def parse_health(raw: str) -> dict[str, Healthcheck]:
    """podman inspect: container name -> its healthcheck."""
    found: dict[str, Healthcheck] = {}
    for row in _rows(raw):
        name = str(row.get("Name") or row.get("name") or "").lstrip("/")
        if not name:
            continue
        config = row.get("Config")
        check = config.get("Healthcheck") if isinstance(config, dict) else None
        state = row.get("State")
        health = state.get("Health") if isinstance(state, dict) else None
        test = check.get("Test") if isinstance(check, dict) else []
        found[name] = Healthcheck(
            [str(item) for item in test] if isinstance(test, list) else [],
            _int(check.get("Interval")) if isinstance(check, dict) else 0,
            str(health.get("Status") or "") if isinstance(health, dict) else "",
        )
    return found


def parse_states(raw: str) -> dict[str, tuple[bool, str]]:
    """podman inspect: container name -> (oom killed, error text)."""
    found: dict[str, tuple[bool, str]] = {}
    for row in _rows(raw):
        name = str(row.get("Name") or row.get("name") or "").lstrip("/")
        state = row.get("State")
        if name and isinstance(state, dict):
            found[name] = (bool(state.get("OOMKilled")), str(state.get("Error") or ""))
    return found


def parse_port(raw: str) -> int:
    """podman inspect --format json: the one exposed tcp port, or 0."""
    rows = _rows(raw)
    if not rows:
        return 0
    config = rows[0].get("Config")
    ports = config.get("ExposedPorts") if isinstance(config, dict) else None
    found = []
    for key in ports or {}:
        number, _, proto = str(key).partition("/")
        if proto == "tcp" and number.isdigit():
            found.append(int(number))
    return found[0] if len(found) == 1 else 0


# Podman --format json prints snake_case keys; the table header spellings are a fallback.
def parse_stats(raw: str) -> dict[str, Sample]:
    found: dict[str, Sample] = {}
    for row in _rows(raw):
        name = str(row.get("name") or row.get("Name") or "")
        if not name:
            continue
        cpu = str(row.get("cpu_percent") or row.get("CPUPerc") or "")
        mem = str(row.get("mem_usage") or row.get("MemUsage") or "")
        found[name] = Sample(name, cpu, mem, _percent(cpu), _size(mem.split("/", 1)[0]))
    return found


def parse_images(raw: str) -> list[Image]:
    """One row per tag. Podman 4.9 gives Names, unix Created, and Size in bytes."""
    found: list[Image] = []
    for row in _rows(raw):
        names = row.get("Names")
        if not isinstance(names, list):
            continue
        created = _stamp(row.get("Created"))
        size = _int(row.get("Size"))
        for name in names:
            if isinstance(name, str) and name:
                found.append(Image(name, created, size))
    return found


def parse_system_df(raw: str) -> tuple[int, int] | None:
    """podman system df: the Images row as (raw size, raw reclaimable), or None."""
    try:
        rows = _rows(raw)
    except YardError, ValueError:
        return None
    for row in rows:
        if str(row.get("Type") or "") == "Images":
            return _int(row.get("RawSize")), _int(row.get("RawReclaimable"))
    return None


def parse_ps(raw: str) -> dict[str, Container]:
    found: dict[str, Container] = {}
    for row in _rows(raw):
        names = row.get("Names")
        if not isinstance(names, list):
            names = []
        names = [str(name) for name in names if name]
        if not names:
            continue
        health = str(row.get("Health") or "")
        if not health:
            health = _health_text(str(row.get("Status") or ""))

        # The deploy container is deploy-<name>.
        name = names[0]
        labels: dict[str, str] = {}
        if name.startswith("deploy-"):
            role = "deploy"
            app = name.split("-", 1)[1]
            service = ""
            given = row.get("Labels")
            if isinstance(given, dict):
                labels = {
                    str(key)[5:]: str(value) for key, value in given.items() if str(key).startswith("yard.") and value
                }
        else:
            role = "app"
            app = _label(row, "com.docker.compose.project")
            service = _label(row, "com.docker.compose.service")

        item = Container(
            id=str(row.get("Id") or ""),
            name=name,
            image=str(row.get("Image") or ""),
            app=app,
            service=service,
            role=role,
            state=str(row.get("State") or ""),
            restarts=_int(row.get("Restarts")),  # podman >= 4.3 exposes Restarts in ps --format json
            exit_code=_int(row.get("ExitCode")),
            health=health,
            created=_stamp(row.get("Created")),
            started=_stamp(row.get("StartedAt")),
            exited=_stamp(row.get("ExitedAt")),
            labels=labels,
        )
        item.running = item.state == "running"
        found[item.name] = item
    return found


def _rows(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    if raw in {"", "null", "[]"}:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise YardError(
            502,
            "podman",
            "podman json is not a list",
            help_text="Podman --format json has to return a list of objects.",
        )
    return [row for row in data if isinstance(row, dict)]


_SIZE = re.compile(r"([0-9.]+)\s*([kmgtp]?i?b?|bytes?)", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "b": 1, "byte": 1, "bytes": 1}
for _index, _prefix in enumerate("kmgtp", 1):
    _SIZE_UNITS[_prefix] = 1000**_index
    _SIZE_UNITS[f"{_prefix}b"] = 1000**_index
    _SIZE_UNITS[f"{_prefix}ib"] = 1024**_index


def _percent(text: str) -> float:
    try:
        return float(text.strip().rstrip("%"))
    except ValueError:
        return 0.0


def _size(text: str) -> int:
    match = _SIZE.search(text.strip())
    if not match:
        return 0
    unit = _SIZE_UNITS.get(match.group(2).lower())
    if unit is None:
        return 0
    return int(float(match.group(1)) * unit)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _label(row: dict[str, Any], key: str) -> str:
    labels = row.get("Labels")
    if not isinstance(labels, dict):
        return ""
    value = labels.get(key, "")
    return value if isinstance(value, str) else ""


def _stamp(value: Any) -> datetime | None:
    if isinstance(value, str) and value:
        return _parse_time(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(int(value), UTC)
    return None


def _health_text(status: str) -> str:
    if "(healthy)" in status:
        return "healthy"
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(starting)" in status:
        return "starting"
    return ""
