"""Terminal text for the yard command. Pure functions; the tests render them with a fixed now."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

from app.shared import ago, app_container, duration, format_bytes, parse

# --- the tables ------------------------------------------------------------


def format_status(report: dict, now: datetime, *, color: bool = False) -> str:
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    cells = [_status_cells(row, now) for row in rows]
    headers = ["name", "state", "cpu", "mem"]
    indexes = [0, 1, 2, 3]
    for index, header in ((4, "data"), (5, "rev"), (6, "host"), (7, "deploy")):
        if any(cell[index] for cell in cells):
            headers.append(header)
            indexes.append(index)
    right = {i for i, header in enumerate(headers) if header in {"cpu", "mem", "data"}}
    deploy_index = headers.index("deploy") if "deploy" in headers else -1
    text = _render(
        headers,
        [[cell[i] for i in indexes] for cell in cells],
        right,
        color=color,
        paint=partial(_paint, state=1, deploy=deploy_index),
    )
    host = _host_line(report.get("host") or {})
    return text + host if host else text


def _status_cells(row: dict, now: datetime) -> list[str]:
    app = str(row.get("app") or "")
    # The container name is the row: bobowski-site-app-1, yard-caddy-1.
    # A project with nothing running shows its name.
    name = str(row.get("container") or "") or app
    mem = str(row.get("mem") or "").split("/", 1)[0].strip()
    return [
        name,
        _state_text(row, now),
        str(row.get("cpu") or ""),
        mem,
        format_bytes(int(row.get("data") or 0)) if row.get("data") else "",
        _rev(str(row.get("sha") or ""), str(row.get("tip") or "")),
        ", ".join(str(host) for host in row.get("hosts") or []),
        _deploy_text(row.get("deploy")),
    ]


def _state_text(row: dict, now: datetime) -> str:
    state = str(row.get("state") or "")
    health = str(row.get("health") or "")
    code = row.get("exit_code") or 0
    if state == "absent":
        return "down"
    if state in {"exited", "dead"}:
        age = _ago(str(row.get("exited") or ""), now)
        if row.get("oom"):
            word = "OOM"
        elif code:
            word = f"exit {code}"
        elif state == "exited":
            word = "down"
        else:
            word = "failed"
        return f"{word} {age}".rstrip()
    if state == "running":
        age = _ago(str(row.get("started") or ""), now)
        word = health if health in {"unhealthy", "starting"} else "up"
        text = f"{word} {age}".rstrip()
        restarts = int(row.get("restarts") or 0)
        if restarts:
            text += f" · {restarts} restart" + ("s" if restarts != 1 else "")
        return text
    return state


def _short(sha: str) -> str:
    """A git sha reads as 7 chars; a sha256: digest as the 12 after the prefix."""
    hexed = sha.removeprefix("sha256:")
    return hexed[:12] if len(hexed) > 40 else hexed[:7]


def _rev(sha: str, tip: str) -> str:
    short = _short(sha)
    want = _short(tip)
    if not want or short == want:
        return short
    if not short:
        return f"→ {want}"
    return f"{short} → {want}"


def _deploy_text(deploy: Any) -> str:
    if not isinstance(deploy, dict):
        return ""
    status = str(deploy.get("status") or "")
    took = _format_span(str(deploy.get("started") or ""), str(deploy.get("ended") or ""))
    text = f"{status} {took}".rstrip()
    when = _when(str(deploy.get("started") or ""))
    return f"{text} · {when}" if when else text


def _host_line(host: dict) -> str:
    parts = []
    cpu = host.get("cpu_used") or 0
    cpus = host.get("cpus") or 0
    if cpu and cpus:
        parts.append(f"{format_percent(cpu)} of {cpus} cores")
    mem = host.get("mem_used") or 0
    total = host.get("mem") or 0
    if mem and total:
        parts.append(f"{format_bytes(mem)} of {format_bytes(total)}")
    data = host.get("data_used") or 0
    disk = host.get("disk") or 0
    if data and disk:
        parts.append(f"{format_bytes(data)} of {format_bytes(disk)}")
    return f"host: {' · '.join(parts)}\n" if parts else ""


def status_hint(report: dict) -> str:
    """The one command worth typing next. A failed deploy comes before a down app."""
    hint = ""
    for row in report.get("rows") or []:
        if not isinstance(row, dict):
            continue
        app = str(row.get("app") or "")
        if not app:
            continue
        deploy = row.get("deploy")
        if isinstance(deploy, dict) and deploy.get("status") == "failed":
            return f"yard app logs --deploy --app {app}"
        # The state word without its age drives the hint.
        word = _state_text(row, datetime.now(UTC)).split(" ", 1)[0]
        if not hint and word in {"failed", "down", "unhealthy", "exit", "OOM"}:
            container = str(row.get("container") or "") or app_container(app)
            hint = f"yard logs {container}"
    return hint


def format_app_status(report: dict, now: datetime, *, color: bool = False) -> str:
    """The one-app page: header, containers, deploys, log."""
    record_raw = report.get("app")
    record: dict = record_raw if isinstance(record_raw, dict) else {}
    name = str(record.get("name") or "")
    lines = [name]
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]

    sha = str(report.get("sha") or "")
    tip = next((str(row.get("tip") or "") for row in rows if row.get("tip")), "")
    rev = f"rev {_short(sha)}" if sha else ""
    if tip and _short(tip) != _short(sha):
        rev += f" · tip {_short(tip)}"
    if report.get("template_moved"):
        rev += " · template moved"
    image = str(report.get("image") or "")
    if image:
        rev += f" · image {image}"
    trigger = str(record.get("trigger") or "")
    if trigger:
        rev += f" · trigger {trigger}"
    if rev:
        lines.append(rev)
    data = max((int(row.get("data") or 0) for row in rows), default=0)
    lines.append(f"data {format_bytes(data)} · env {report.get('env_file') or ''}")
    lines.append(f"runs  {_runs_text(record)}")

    hosts = [str(host) for host in report.get("hosts") or []]
    if hosts:
        lines.append(f"caddy {', '.join(hosts)}")

    # The id column shows only when a row carries one.
    has_id = any(row.get("id") for row in rows)
    state_index = 2 if has_id else 1
    containers = _align(
        [
            [
                str(row.get("container") or row.get("app") or name),
                *([str(row.get("id") or "")] if has_id else []),
                _state_text(row, now),
                str(row.get("cpu") or ""),
                str(row.get("mem") or "").split("/", 1)[0].strip(),
            ]
            for row in rows
        ],
        {3, 4} if has_id else {2, 3},
        partial(_paint, state=state_index) if color else None,
    )
    lines += ["", "containers"] + [f"  {line}" for line in containers]

    deploys = [row for row in report.get("deploys") or [] if isinstance(row, dict)]
    lines += ["", "deploys"]
    if deploys:
        shown = _align(
            [
                [
                    str(row.get("status") or ""),
                    _format_span(str(row.get("started") or ""), str(row.get("ended") or "")),
                    _ago(str(row.get("started") or ""), now),
                    _short(str(row.get("sha") or "")),
                ]
                for row in deploys
            ],
            {1, 2},
            partial(_paint, deploy=0) if color else None,
        )
        lines += [f"  {line}" for line in shown]
    else:
        lines.append("  no deploys yet")

    log = str(report.get("log") or "").rstrip("\n")
    lines += ["", "log"]
    if log:
        lines += [f"  {line}" for line in log.splitlines()]
    else:
        lines.append("  no log")
    return "\n".join(lines) + "\n"


def _align(rows: list[list[str]], right: set[int], paint: Callable[[int, str], str] | None = None) -> list[str]:
    """Columns padded to the widest cell, like _render but without the header."""
    width = [0] * max((len(row) for row in rows), default=0)
    for row in rows:
        for index, cell in enumerate(row):
            width[index] = max(width[index], len(cell))
    lines = []
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            text = cell.rjust(width[index]) if index in right else cell.ljust(width[index])
            if paint is not None:
                text = paint(index, text)
            cells.append(text)
        lines.append("  ".join(cells).rstrip())
    return lines


def format_apps(rows: list, *, color: bool = False) -> str:
    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("repo") or row.get("runs") or "auto")
        ref = str(row.get("ref") or "") if row.get("repo") else ""
        body.append([str(row.get("name") or ""), source, ref, str(row.get("trigger") or "")])
    return _render(["app", "source", "ref", "trigger"], body, color=color)


def format_events(rows: list, *, color: bool = False) -> str:
    """The merged podman/yard timeline. time, source, event, app, container, detail."""
    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        container = str(row.get("container") or "")
        ident = str(row.get("container_id") or "")
        if ident:
            container = f"{container} {ident[:12]}".strip()
        detail = " ".join(
            part
            for part in (
                f"exit {row['exit_code']}" if str(row.get("exit_code") or "") else "",
                "oom" if row.get("oom") else "",
                str(row.get("status") or ""),
                _short(str(row.get("sha") or "")),
                str(row.get("actor") or ""),
                str(row.get("message") or ""),
            )
            if part
        )
        when = str(row.get("time") or "")
        clock = when[11:19] if "T" in when else when
        body.append(
            [
                clock,
                str(row.get("source") or ""),
                str(row.get("event") or ""),
                str(row.get("app") or ""),
                container,
                detail,
            ]
        )
    return _render(["time", "source", "event", "app", "container", "detail"], body, color=color)


def format_forges(rows: list, *, color: bool = False) -> str:
    body = [
        [
            str(row.get("name") or ""),
            "yes" if row.get("connected") else "no",
            str(row.get("app_id") or ""),
            ", ".join(
                f"{entry.get('account')}:{entry.get('id')}"
                for entry in row.get("installations") or []
                if isinstance(entry, dict)
            ),
            ", ".join(
                str(entry.get("selection") or "") for entry in row.get("installations") or [] if isinstance(entry, dict)
            ),
            _when(str(row.get("last_delivery") or "")),
        ]
        for row in rows
        if isinstance(row, dict)
    ]
    return _render(["forge", "connected", "app", "installation", "selection", "delivery"], body, color=color)


def format_tokens(rows: list, *, color: bool = False) -> str:
    return _name_when("token", rows, color=color)


def _name_when(header: str, rows: list, *, color: bool) -> str:
    body = [[str(row.get("name") or ""), _when(str(row.get("created") or ""))] for row in rows if isinstance(row, dict)]
    return _render([header, "when"], body, {1}, color=color)


def format_record(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body if body.endswith("\n") or not body else body + "\n"
    if not isinstance(data, dict):
        return body if body.endswith("\n") else body + "\n"
    if "token" in data:
        name = str(data.get("name") or "")
        secret = str(data.get("token") or "")
        return f"{name}\n{secret}\n" if name else f"{secret}\n"
    if "name" in data and set(data) <= {"name", "created"}:
        return f"{data['name']}\n"
    if not data.get("repo"):
        data.pop("ref", None)
    runs = _runs_text(data)
    lines = [
        f"{key}  {value}"
        for key, value in data.items()
        if key not in ("runs", "deployed") and value not in ("", None, [], {})
    ]
    lines.append(f"runs  {runs}")
    return ("\n".join(lines) + "\n") if lines else ""


def _runs_text(record: dict) -> str:
    """The runs line: the record's value plus what the last deploy answered."""
    shown = str(record.get("runs") or "") or "auto"
    deployed = record.get("deployed")
    if not isinstance(deployed, dict):
        return f"{shown} (not deployed yet)"
    answer = str(deployed.get("runs") or "")
    text = shown if answer in ("", "auto", shown) else f"{shown} → {answer}"
    if str(deployed.get("template") or ""):
        text += f" @{deployed['template']}"
    inner = str(deployed.get("kind") or "")
    revision = str(deployed.get("revision") or "")
    if revision.startswith("sha256:"):
        inner += f" @{revision[:19]}"
    reason = str(deployed.get("reason") or "")
    if reason:
        inner += f"; {reason}"
    return f"{text} ({inner})" if inner else text


# --- render and time -------------------------------------------------------


def _render(
    headers: list[str],
    rows: list[list[str]],
    right: set[int] | None = None,
    *,
    color: bool = False,
    paint: Callable[[int, str], str] | None = None,
) -> str:
    numeric = right or set()
    width = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(width):
                width[index] = max(width[index], len(cell))

    def line(cells: list[str], *, header: bool = False) -> str:
        shown = []
        for index, cell in enumerate(cells):
            pad = width[index] if index < len(width) else 0
            text = cell.rjust(pad) if index in numeric else cell.ljust(pad)
            if color and not header and paint is not None:
                text = paint(index, text)
            shown.append(text)
        text = "  ".join(shown).rstrip()
        if color and header:
            return ansi(text, "2")
        return text

    lines = [line(headers, header=True)]
    if not rows:
        lines.append("(none)")
    lines.extend(line(row) for row in rows)
    return "\n".join(lines) + "\n"


def _paint(index: int, text: str, *, state: int = -1, deploy: int = -1) -> str:
    """Color the word inside a padded cell; the padding stays outside the escape."""
    code = ""
    if index == state:
        code = _attention(text.strip())
    elif index == deploy:
        code = _deploy_attention(text.strip().split(" ", 1)[0])
    if not code:
        return text
    head = len(text) - len(text.lstrip())
    tail = len(text.rstrip())
    return text[:head] + ansi(text[head:tail], code) + text[tail:]


def _attention(state: str) -> str:
    head = state.split(" ", 1)[0]
    if head in {"failed", "down", "absent"}:
        return "31"
    if head in {"unhealthy", "starting"}:
        return "33"
    return ""


def _deploy_attention(status: str) -> str:
    if status == "failed":
        return "31"
    if status == "running":
        return "33"
    return ""


def format_prune(report: dict) -> str:
    images = [row for row in report.get("images") or [] if isinstance(row, dict)]
    refused = [row for row in images if row.get("kept")]
    word = "would remove" if report.get("dry") else "removed"
    days = report.get("days") or 7
    lines = [
        f"{word} {len(images) - len(refused)} images, {format_bytes(int(report.get('freed') or 0))}",
        f"kept: an image a container still uses, and anything newer than {days} days",
    ]
    for row in refused:
        lines.append(f"kept {row.get('tag')} ({row.get('kept')})")
    return "\n".join(lines) + "\n"


def format_doctor(checks: list[dict[str, Any]], color: bool = False) -> str:
    width = max((len(str(check.get("name") or "")) for check in checks if isinstance(check, dict)), default=0)
    lines = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        word = "ok" if check.get("ok") else "FAIL"
        if check.get("ok") and check.get("warn"):
            word = "warn"
        # Pad the plain word first; the escape wraps the word, not the spaces.
        cell = f"{word:<5}"
        if word == "FAIL" and color:
            cell = ansi(word, "31") + cell[len(word) :]
        elif word == "warn" and color:
            cell = ansi(word, "33") + cell[len(word) :]
        lines.append(f"{cell}{str(check.get('name') or ''):<{width}}  {check.get('detail') or ''}")
    return "\n".join(lines) + ("\n" if lines else "")


def ansi(text: str, code: str) -> str:
    if not text or not code:
        return text
    return f"\033[{code}m{text}\033[0m"


def _format_span(started: str, ended: str) -> str:
    start = parse(started)
    stop = parse(ended)
    if start is None or stop is None or stop <= start:
        return ""
    return duration(stop - start)


def _ago(stamp: str, now: datetime) -> str:
    return ago(stamp, now)


def _when(stamp: str) -> str:
    return ago(stamp, datetime.now(UTC))


def format_percent(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"
