"""journalctl and the journald socket as one adapter: argv, send, /logs, /events."""

import json
import socket
import struct
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from stario.testing import TestClient

from app.server import journal
from app.server.main import bootstrap
from app.server.store import Config
from app.shared import YardError
from tests.conftest import auth

WHEN = datetime(2025, 10, 5, 21, 0, 0, tzinfo=UTC)
MICROS = str(int(WHEN.timestamp() * 1_000_000))
ENTRY = json.dumps({"__REALTIME_TIMESTAMP": MICROS, "CONTAINER_NAME": "heldso-app-1", "MESSAGE": "hello"})
BYTES = json.dumps({"__REALTIME_TIMESTAMP": MICROS, "CONTAINER_NAME": "heldso-app-1", "MESSAGE": [104, 105, 255]})

FULL_ID = "abc123def456" + "0" * 52
PODMAN_DIED = json.dumps(
    {
        "__REALTIME_TIMESTAMP": MICROS,
        "SYSLOG_IDENTIFIER": "podman",
        "PODMAN_TYPE": "container",
        "PODMAN_EVENT": "died",
        "PODMAN_NAME": "heldso-app-1",
        "PODMAN_ID": FULL_ID,
        "PODMAN_IMAGE": "localhost/heldso:abc123def456",
        "PODMAN_EXIT_CODE": "1",
        "PODMAN_LABELS": json.dumps({"com.docker.compose.project": "heldso"}),
        "MESSAGE": "container died heldso-app-1",
    }
)
PODMAN_SYSTEM = json.dumps(
    {
        "__REALTIME_TIMESTAMP": MICROS,
        "SYSLOG_IDENTIFIER": "podman",
        "PODMAN_TYPE": "podman",
        "PODMAN_EVENT": "refresh",
        "MESSAGE": "system refresh",
    }
)
YARD_DEPLOY = json.dumps(
    {
        "__REALTIME_TIMESTAMP": MICROS,
        "SYSLOG_IDENTIFIER": "yard",
        "YARD_EVENT": "deploy.end",
        "YARD_APP": "heldso",
        "YARD_STATUS": "ok",
        "YARD_SHA": "abc123def456",
        "YARD_ACTOR": "admin",
        "YARD_CAUSE": "manual",
        "MESSAGE": "deploy heldso ok",
    }
)


def _config(tmp_path: Path, journalctl: Path, journal_socket: Path | None = None) -> Config:
    return Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="yard-podman-missing",
        skip_deploy=True,
        boot=False,
        remote=False,
        journal_dir=tmp_path / "journal",
        journalctl_bin=str(journalctl),
        journal_socket=journal_socket or (tmp_path / "no-socket"),
    )


def _journalctl(tmp_path: Path, *, out: str = "", err: str = "", code: int = 0) -> tuple[Path, Path]:
    """A fake journalctl. It records argv and prints canned output."""
    argv_log = tmp_path / "argv"
    script = tmp_path / "journalctl"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{argv_log}"\n'
        f'cat "{tmp_path / "out"}" 2>/dev/null\n'
        f'cat "{tmp_path / "err"}" >&2 2>/dev/null\n'
        f"exit {code}\n"
    )
    script.chmod(0o755)
    (tmp_path / "out").write_text(out)
    (tmp_path / "err").write_text(err)
    (tmp_path / "journal").mkdir()
    return script, argv_log


async def test_read_builds_the_argv(tmp_path: Path):
    script, argv_log = _journalctl(tmp_path, out=ENTRY + "\n" + BYTES + "\n" + "not json\n")
    config = _config(tmp_path, script)
    text = await journal.read(
        config,
        ["CONTAINER_NAME=a-app-1", "CONTAINER_NAME=b-app-1"],
        lines=50,
        since="-15min",
        until="2025-10-05 12:00",
    )
    assert argv_log.read_text().splitlines() == [
        "-D",
        str(config.journal_dir),
        "--no-pager",
        "-o",
        "json",
        "-n",
        "50",
        "--since",
        "-15min",
        "--until",
        "2025-10-05 12:00",
        "CONTAINER_NAME=a-app-1",
        "CONTAINER_NAME=b-app-1",
    ]
    assert "2025-10-05T21:00:00Z heldso-app-1 hello" in text
    assert "2025-10-05T21:00:00Z heldso-app-1 hi�" in text


async def test_read_puts_each_entry_on_its_own_line(tmp_path: Path):
    two = ENTRY + "\n" + ENTRY.replace("hello", "again") + "\n"
    script, _argv_log = _journalctl(tmp_path, out=two)
    text = await journal.read(_config(tmp_path, script), ["CONTAINER_NAME=heldso-app-1"], lines=50)
    assert text.splitlines() == [
        "2025-10-05T21:00:00Z heldso-app-1 hello",
        "2025-10-05T21:00:00Z heldso-app-1 again",
    ]


async def test_read_after_a_cursor_starts_at_the_oldest(tmp_path: Path):
    script, argv_log = _journalctl(tmp_path, out=ENTRY + "\n")
    config = _config(tmp_path, script)
    await journal.read(config, ["CONTAINER_NAME=heldso-app-1"], lines=50, after="s=abc;n=2;i=3")
    argv = argv_log.read_text().splitlines()
    assert argv[argv.index("-n") + 1] == "+50"
    assert argv[argv.index("--after-cursor") + 1] == "s=abc;n=2;i=3"


async def test_follow_passes_dash_f(tmp_path: Path):
    script, argv_log = _journalctl(tmp_path, out=ENTRY + "\n")
    config = _config(tmp_path, script)
    proc = await journal.follow(config, ["CONTAINER_NAME=heldso-app-1"], lines=20)
    try:
        assert proc.stdout is not None
        assert b"hello" in await proc.stdout.read()
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    assert "-f" in argv_log.read_text().splitlines()


async def test_a_missing_journal_dir_is_a_502(tmp_path: Path):
    script, _ = _journalctl(tmp_path)
    (tmp_path / "journal").rmdir()
    with pytest.raises(YardError) as caught:
        await journal.read(_config(tmp_path, script), ["CONTAINER_NAME=heldso-app-1"], lines=20)
    assert caught.value.status == 502
    assert caught.value.code == "journal"
    assert "install.sh" in (caught.value.help_text or "")


async def test_a_failed_journalctl_is_a_502(tmp_path: Path):
    script, _ = _journalctl(tmp_path, err="cannot read", code=1)
    with pytest.raises(YardError) as caught:
        await journal.read(_config(tmp_path, script), ["CONTAINER_NAME=heldso-app-1"], lines=20)
    assert caught.value.status == 502
    assert "cannot read" in str(caught.value)


async def test_a_missing_journalctl_is_a_502(tmp_path: Path):
    (tmp_path / "journal").mkdir()
    config = _config(tmp_path, tmp_path / "no-such-journalctl")
    with pytest.raises(YardError) as caught:
        await journal.read(config, ["CONTAINER_NAME=heldso-app-1"], lines=20)
    assert caught.value.status == 502


def test_format_line():
    entry: dict = {"__REALTIME_TIMESTAMP": MICROS, "CONTAINER_NAME": "x", "MESSAGE": "m"}
    assert journal.format_line(entry) == "2025-10-05T21:00:00Z x m"
    entry["MESSAGE"] = [104, 105]
    assert journal.format_line(entry).endswith("x hi")
    assert journal.format_line({}) == ""
    assert "x" in journal.format_line({"CONTAINER_NAME": "x", "MESSAGE": "m"})


# --- journal.send ------------------------------------------------------------


def test_send_writes_a_native_datagram(tmp_path: Path):
    """A real AF_UNIX dgram socket gets the framed fields."""
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    sock_path = directory / "journal.sock"
    recv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    recv.bind(str(sock_path))
    try:
        config = _config(tmp_path, Path("/bin/false"), journal_socket=sock_path)
        journal.send(
            config,
            "deploy.end",
            "deploy heldso ok",
            priority=3,
            app="heldso",
            error="boom\nmore",
        )
        recv.settimeout(2)
        data, _peer = recv.recvfrom(65535)
    finally:
        recv.close()
        sock_path.unlink(missing_ok=True)
        directory.rmdir()
    assert b"SYSLOG_IDENTIFIER=yard\n" in data
    assert b"PRIORITY=3\n" in data
    assert b"MESSAGE=deploy heldso ok\n" in data
    assert b"YARD_EVENT=deploy.end\n" in data
    assert b"YARD_APP=heldso\n" in data
    marker = b"YARD_ERROR\n" + struct.pack("<Q", len(b"boom\nmore"))
    assert marker in data
    assert data.endswith(b"boom\nmore\n")


def test_send_falls_back_to_stdout(tmp_path: Path, capsys):
    """No socket: one JSON line on stdout, which still lands in the journal."""
    config = _config(tmp_path, Path("/bin/false"), journal_socket=tmp_path / "missing.sock")
    journal.send(config, "app.create", "app heldso created", app="heldso", actor="admin")
    row = json.loads(capsys.readouterr().out)
    assert row == {
        "yard": "app.create",
        "message": "app heldso created",
        "app": "heldso",
        "actor": "admin",
    }


def test_send_never_raises(tmp_path: Path, capsys):
    """A path that exists but is not a socket fails the same way."""
    blocker = tmp_path / "regular"
    blocker.write_text("x")
    config = _config(tmp_path, Path("/bin/false"), journal_socket=blocker)
    journal.send(config, "check.moved", "x")
    assert "check.moved" in capsys.readouterr().out


# --- the /logs and /events routes --------------------------------------------


@pytest.fixture
async def yard_journal(tmp_path, monkeypatch):
    script, argv_log = _journalctl(tmp_path, out=ENTRY + "\n")
    monkeypatch.setenv("YARD_ROOT", str(tmp_path))
    monkeypatch.setenv("YARD_SKIP_DEPLOY", "1")
    monkeypatch.setenv("YARD_BOOT", "0")
    monkeypatch.setenv("YARD_PODMAN_BIN", "yard-podman-missing")
    monkeypatch.setenv("YARD_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("YARD_JOURNALCTL_BIN", str(script))
    async with TestClient(bootstrap, request_timeout=5) as client:
        token = (tmp_path / "tokens" / "admin.token").read_text().strip()
        yield client, token, argv_log, tmp_path / "out"


async def test_logs_reads_a_container_by_name(yard_journal):
    client, token, argv_log, _out = yard_journal
    res = await client.get("/api/v1/logs?container=heldso-app-1", headers=auth(token))
    assert res.status_code == 200
    assert "heldso-app-1 hello" in res.text
    argv = argv_log.read_text().splitlines()
    assert argv[-1] == "CONTAINER_NAME=heldso-app-1"
    assert "-n" in argv and "100" in argv


async def test_logs_reads_a_container_by_id(yard_journal):
    client, token, argv_log, _out = yard_journal
    res = await client.get("/api/v1/logs?container=abc123def456&since=-15m&lines=5", headers=auth(token))
    assert res.status_code == 200
    argv = argv_log.read_text().splitlines()
    assert "CONTAINER_ID=abc123def456" in argv
    assert argv[argv.index("--since") + 1] == "-15min"
    res = await client.get(f"/api/v1/logs?container={FULL_ID}", headers=auth(token))
    assert res.status_code == 200
    assert f"CONTAINER_ID_FULL={FULL_ID}" in argv_log.read_text().splitlines()


async def test_logs_rejects_a_bad_container_and_query(yard_journal):
    client, token, _, _out = yard_journal
    for path, code in (
        ("/api/v1/logs", "bad_container"),
        ("/api/v1/logs?container=heldso-app-1;rm", "bad_container"),
        ("/api/v1/logs?container=x&since=tomorrow", "bad_time"),
        ("/api/v1/logs?container=x&lines=0", "bad_lines"),
        ("/api/v1/logs?container=x&lines=20000", "bad_lines"),
        ("/api/v1/logs?container=x&lines=abc", "bad_lines"),
    ):
        res = await client.get(path, headers=auth(token))
        assert res.status_code == 400, path
        assert res.json()["error"]["code"] == code


async def test_logs_follow_streams(yard_journal):
    client, token, argv_log, _out = yard_journal
    res = await client.get("/api/v1/logs?container=heldso-app-1&follow=1", headers=auth(token))
    assert res.status_code == 200
    assert "heldso-app-1 hello" in res.text
    assert "-f" in argv_log.read_text().splitlines()


_CURSOR_JOURNALCTL = """\
import json
import sys

argv = sys.argv[1:]
with open({argv_log!r}, "a") as log:
    log.write(" ".join(argv) + "\\n")

lines, after, matches = "", "", []
pos = 0
while pos < len(argv):
    arg = argv[pos]
    if arg in ("-D", "-o", "--since", "--until"):
        pos += 2
    elif arg in ("-n", "--lines"):
        lines, pos = argv[pos + 1], pos + 2
    elif arg == "--after-cursor":
        after, pos = argv[pos + 1], pos + 2
    elif arg == "-f":
        pos += 1
    else:
        matches.append(arg)
        pos += 1

want = {{}}
for match in matches:
    field, _, value = match.partition("=")
    want.setdefault(field, set()).add(value)
entries = [
    entry
    for entry in (json.loads(line) for line in open({data!r}) if line.strip())
    if all(str(entry.get(field) or "") in values for field, values in want.items())
]
if after:
    found = [i for i, entry in enumerate(entries) if str(entry.get("__CURSOR") or "") == after]
    entries = entries[found[0] + 1 :] if found else entries
if lines:
    count = int(lines.lstrip("+"))
    entries = entries[:count] if lines.startswith("+") else entries[-count:]
for entry in entries:
    print(json.dumps(entry))
"""


def _cursor_journalctl(tmp_path: Path, entries: list[dict]) -> tuple[Path, Path]:
    """A fake journalctl that honors matches, --after-cursor, and -n [+]N."""
    data = tmp_path / "journal.jsonl"
    data.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    argv_log = tmp_path / "argv"
    script = tmp_path / "journalctl"
    script.write_text(f"#!{sys.executable}\n" + _CURSOR_JOURNALCTL.format(argv_log=str(argv_log), data=str(data)))
    script.chmod(0o755)
    (tmp_path / "journal").mkdir()
    return script, argv_log


@pytest.fixture
async def yard_cursor(tmp_path, monkeypatch):
    """The API over a journalctl that honors --after-cursor and -n [+]N."""
    micros = int(WHEN.timestamp() * 1_000_000)
    entries = [
        {
            "__CURSOR": f"cursor-{n}",
            "__REALTIME_TIMESTAMP": str(micros + n * 1_000_000),
            "CONTAINER_NAME": "heldso-app-1",
            "MESSAGE": f"line {n}",
        }
        for n in range(5)
    ]
    script, argv_log = _cursor_journalctl(tmp_path, entries)
    monkeypatch.setenv("YARD_ROOT", str(tmp_path))
    monkeypatch.setenv("YARD_SKIP_DEPLOY", "1")
    monkeypatch.setenv("YARD_BOOT", "0")
    monkeypatch.setenv("YARD_PODMAN_BIN", "yard-podman-missing")
    monkeypatch.setenv("YARD_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("YARD_JOURNALCTL_BIN", str(script))
    async with TestClient(bootstrap, request_timeout=5) as client:
        token = (tmp_path / "tokens" / "admin.token").read_text().strip()
        yield client, token, argv_log


async def test_logs_json_is_a_page_of_entries(yard_cursor):
    client, token, _argv_log = yard_cursor
    res = await client.get(
        "/api/v1/logs?container=heldso-app-1&lines=1",
        headers={**auth(token), "accept": "application/json"},
    )
    assert res.status_code == 200
    assert res.json() == {
        "entries": [
            {
                "cursor": "cursor-4",
                "container": "heldso-app-1",
                "time": "2025-10-05T21:00:04Z",
                "message": "line 4",
            }
        ],
        "cursor": "cursor-4",
    }


async def test_logs_json_batches_do_not_overlap(yard_cursor):
    client, token, argv_log = yard_cursor
    res = await client.get(
        "/api/v1/logs?container=heldso-app-1&lines=2&since=-1h&after=cursor-0",
        headers={**auth(token), "accept": "application/json"},
    )
    assert res.status_code == 200
    batch = res.json()
    assert [entry["message"] for entry in batch["entries"]] == ["line 1", "line 2"]
    assert batch["cursor"] == "cursor-2"
    argv = argv_log.read_text()
    assert "--after-cursor cursor-0" in argv
    assert "--since -1h" in argv
    assert "-n +2" in argv

    res = await client.get(
        f"/api/v1/logs?container=heldso-app-1&lines=2&after={batch['cursor']}",
        headers={**auth(token), "accept": "application/json"},
    )
    assert res.status_code == 200
    batch = res.json()
    assert [entry["message"] for entry in batch["entries"]] == ["line 3", "line 4"]
    assert batch["cursor"] == "cursor-4"


async def test_logs_json_after_the_last_entry_is_empty(yard_cursor):
    client, token, _argv_log = yard_cursor
    res = await client.get(
        "/api/v1/logs?container=heldso-app-1&after=cursor-4",
        headers={**auth(token), "accept": "application/json"},
    )
    assert res.status_code == 200
    assert res.json() == {"entries": [], "cursor": ""}


async def test_logs_text_takes_after_too(yard_cursor):
    client, token, _argv_log = yard_cursor
    # No Accept header and */* — what curl sends — both answer text lines.
    for headers in (auth(token), {**auth(token), "accept": "*/*"}):
        res = await client.get("/api/v1/logs?container=heldso-app-1&after=cursor-1", headers=headers)
        assert res.status_code == 200
        assert "line 1" not in res.text
        assert "line 2" in res.text and "line 4" in res.text
    # A stale json=1 in the query still answers text.
    res = await client.get("/api/v1/logs?container=heldso-app-1&json=1&after=cursor-1", headers=auth(token))
    assert res.status_code == 200
    assert "line 2" in res.text and '"entries"' not in res.text


async def test_logs_text_is_one_line_per_entry(yard_cursor):
    client, token, _argv_log = yard_cursor
    res = await client.get("/api/v1/logs?container=heldso-app-1&lines=2", headers=auth(token))
    assert res.status_code == 200
    assert res.text.splitlines() == [
        "2025-10-05T21:00:03Z heldso-app-1 line 3",
        "2025-10-05T21:00:04Z heldso-app-1 line 4",
    ]


async def test_logs_needs_a_token(yard_journal):
    client, _token, _argv, _out = yard_journal
    res = await client.get("/api/v1/logs?container=heldso-app-1")
    assert res.status_code == 401


async def test_events_merge_podman_and_yard(yard_journal):
    client, token, argv_log, out = yard_journal
    out.write_text(PODMAN_DIED + "\n" + PODMAN_SYSTEM + "\n" + YARD_DEPLOY + "\n")
    res = await client.get("/api/v1/events", headers=auth(token))
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 2
    died, deploy = rows
    # A podman event on the engine itself (PODMAN_TYPE=podman) is not a container event.
    assert died["source"] == "podman" and died["event"] == "died"
    assert died["app"] == "heldso" and died["container"] == "heldso-app-1"
    assert died["container_id"] == FULL_ID and died["exit_code"] == "1"
    assert deploy["source"] == "yard" and deploy["event"] == "deploy.end"
    assert deploy["status"] == "ok" and deploy["actor"] == "admin"
    assert deploy["fields"] == {"cause": "manual"}
    argv = argv_log.read_text().splitlines()
    assert "SYSLOG_IDENTIFIER=podman" in argv and "SYSLOG_IDENTIFIER=yard" in argv
    assert argv[argv.index("--since") + 1] == "-1d"


def _deploy_event(event: str, code: str = "") -> str:
    """A Podman event of the apply container. Its labels are the deploy record."""
    labels = {
        "yard.sha": "b3550ab" + "0" * 33,
        "yard.image": "localhost/stario-site:b3550ab00000",
        "yard.runs": "yard:stario",
        "yard.kind": "containerfile",
        "yard.cause": "push",
        "yard.actor": "webhook:github",
        "yard.previous": "localhost/stario-site:f5f6ab300000",
    }
    entry = {
        "__REALTIME_TIMESTAMP": MICROS,
        "SYSLOG_IDENTIFIER": "podman",
        "PODMAN_TYPE": "container",
        "PODMAN_EVENT": event,
        "PODMAN_NAME": "deploy-stario-site",
        "PODMAN_ID": FULL_ID,
        "PODMAN_IMAGE": "localhost/yard-apply:latest",
        "PODMAN_LABELS": json.dumps(labels),
        "MESSAGE": f"container {event} deploy-stario-site",
    }
    if code:
        entry["PODMAN_EXIT_CODE"] = code
    return json.dumps(entry)


async def test_deploy_events_come_from_the_deploy_container(yard_journal):
    """Podman's create and died events of `deploy-<name>` are the deploy.start and deploy.end rows.

    They come from the container, so a Yard restart in the middle of a deploy loses neither.
    """
    client, token, _argv, out = yard_journal
    lines = [_deploy_event("create"), _deploy_event("start"), _deploy_event("died", "0"), _deploy_event("died", "3")]
    out.write_text("\n".join(lines) + "\n")
    res = await client.get("/api/v1/events?app=stario-site&event=deploy.", headers=auth(token))
    assert res.status_code == 200
    start, end, reverted = res.json()
    sha = "b3550ab" + "0" * 33
    assert start == {
        "time": start["time"],
        "source": "yard",
        "event": "deploy.start",
        "app": "stario-site",
        "container": "deploy-stario-site",
        "actor": "webhook:github",
        "sha": sha,
        "message": "deploy stario-site b3550ab00000",
        "fields": {
            "image": "localhost/stario-site:b3550ab00000",
            "runs": "yard:stario",
            "kind": "containerfile",
            "cause": "push",
        },
    }
    assert end["event"] == "deploy.end" and end["status"] == "ok" and end["sha"] == sha
    assert end["message"] == "deploy stario-site ok"
    assert reverted["status"] == "reverted"
    assert reverted["fields"]["error"] == "unhealthy; reverted to localhost/stario-site:f5f6ab300000"
    # The other events of the apply container stay Podman rows.
    res = await client.get("/api/v1/events?app=stario-site&source=podman", headers=auth(token))
    assert [row["event"] for row in res.json()] == ["start"]


async def test_events_filters(yard_journal):
    client, token, _argv, out = yard_journal
    out.write_text(PODMAN_DIED + "\n" + YARD_DEPLOY + "\n")

    async def events(query: str) -> list:
        res = await client.get(f"/api/v1/events?{query}", headers=auth(token))
        assert res.status_code == 200
        return res.json()

    assert len(await events("app=heldso")) == 2
    assert await events("app=other") == []
    assert [row["source"] for row in await events("source=yard")] == ["yard"]
    assert [row["event"] for row in await events("event=deploy.")] == ["deploy.end"]
    assert [row["container"] for row in await events("container=heldso-app-1")] == ["heldso-app-1"]
    # A 12-char prefix of the full id matches too.
    assert [row["container"] for row in await events("container=abc123def456")] == ["heldso-app-1"]
    assert len(await events("lines=1")) == 1


async def test_events_follow_streams_ndjson(yard_journal):
    client, token, argv_log, out = yard_journal
    out.write_text(YARD_DEPLOY + "\n")
    res = await client.get("/api/v1/events?follow=1&source=yard", headers=auth(token))
    assert res.status_code == 200
    row = json.loads(res.text.splitlines()[0])
    assert row["event"] == "deploy.end"
    assert "SYSLOG_IDENTIFIER=yard" in argv_log.read_text().splitlines()


# --- hide/only filters --------------------------------------------------------


def _filter_entries() -> list[dict]:
    micros = int(WHEN.timestamp() * 1_000_000)

    def entry(n: int, message: str) -> dict:
        return {
            "__CURSOR": f"cursor-{n}",
            "__REALTIME_TIMESTAMP": str(micros + n * 1_000_000),
            "CONTAINER_NAME": "heldso-app-1",
            "MESSAGE": message,
        }

    health_child = {
        "name": "render",
        "trace_id": "t-health",
        "span_id": "s2",
        "parent_id": "s1",
        "status": "ok",
        "duration_ns": 100,
    }
    health_root = {
        "name": "GET /health",
        "trace_id": "t-health",
        "span_id": "s1",
        "status": "ok",
        "duration_ns": 5000,
        "attributes": {"request.path": "/health"},
    }
    notes_child = {
        "name": "db.query",
        "trace_id": "t-notes",
        "span_id": "s4",
        "parent_id": "s3",
        "status": "error",
        "duration_ns": 700,
    }
    notes_root = {
        "name": "GET /notes",
        "trace_id": "t-notes",
        "span_id": "s3",
        "status": "ok",
        "duration_ns": 9000,
        "attributes": {"request.path": "/notes"},
    }
    return [
        entry(0, json.dumps(health_child)),
        entry(1, json.dumps(health_root)),
        entry(2, json.dumps(notes_child)),
        entry(3, "boom traceback"),
        entry(4, json.dumps(notes_root)),
        entry(5, json.dumps({"name": "tick", "status": "ok", "duration_ns": 50})),
    ]


@pytest.fixture
async def yard_filtered(tmp_path, monkeypatch):
    """The API over a journal of two traces, a text line, and a lone JSON line."""
    script, argv_log = _cursor_journalctl(tmp_path, _filter_entries())
    monkeypatch.setenv("YARD_ROOT", str(tmp_path))
    monkeypatch.setenv("YARD_SKIP_DEPLOY", "1")
    monkeypatch.setenv("YARD_BOOT", "0")
    monkeypatch.setenv("YARD_PODMAN_BIN", "yard-podman-missing")
    monkeypatch.setenv("YARD_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("YARD_JOURNALCTL_BIN", str(script))
    async with TestClient(bootstrap, request_timeout=5) as client:
        token = (tmp_path / "tokens" / "admin.token").read_text().strip()
        yield client, token, argv_log


async def _page(client, token, query: str) -> dict:
    res = await client.get(
        f"/api/v1/logs?container=heldso-app-1&{query}",
        headers={**auth(token), "accept": "application/json"},
    )
    assert res.status_code == 200
    return res.json()


def _messages(page: dict) -> list[str]:
    return [entry["message"] for entry in page["entries"]]


async def test_logs_no_filters_returns_every_line(yard_filtered):
    client, token, _argv = yard_filtered
    page = await _page(client, token, "lines=50")
    assert [json.loads(m)["name"] if m.startswith("{") else m for m in _messages(page)] == [
        "render",
        "GET /health",
        "db.query",
        "boom traceback",
        "GET /notes",
        "tick",
    ]
    assert page["cursor"] == "cursor-5"
    res = await client.get("/api/v1/logs?container=heldso-app-1", headers=auth(token))
    assert res.status_code == 200
    # No filter: the same six entries, joined exactly as journal.read renders.
    assert res.text.count("heldso-app-1") == 6


async def test_logs_hide_a_field_drops_the_whole_trace(yard_filtered):
    client, token, _argv = yard_filtered
    page = await _page(client, token, "hide=request.path%3D%2Fhealth")
    # The child has no request.path; the trace hides as one group.
    assert "t-health" not in "".join(_messages(page))
    names = [json.loads(m)["name"] if m.startswith("{") else m for m in _messages(page)]
    assert names == ["db.query", "boom traceback", "GET /notes", "tick"]


async def test_logs_only_keeps_matching_traces_and_text(yard_filtered):
    client, token, _argv = yard_filtered
    page = await _page(client, token, "only=status%3Derror")
    names = [json.loads(m)["name"] if m.startswith("{") else m for m in _messages(page)]
    # The notes child's error keeps root and child; the text line is not judged
    # by a field rule; /health and the lone status-ok JSON line are gone.
    assert names == ["db.query", "boom traceback", "GET /notes"]


async def test_logs_hide_a_text_rule_drops_a_plain_line(yard_filtered):
    client, token, _argv = yard_filtered
    res = await client.get("/api/v1/logs?container=heldso-app-1&hide=boom", headers=auth(token))
    assert res.status_code == 200
    assert "boom" not in res.text
    assert "t-health" in res.text and "tick" in res.text


async def test_logs_number_rules_read_group_lines(yard_filtered):
    client, token, _argv = yard_filtered
    page = await _page(client, token, "only=duration_ns%3E800")
    names = [json.loads(m)["name"] if m.startswith("{") else m for m in _messages(page)]
    assert names == ["render", "GET /health", "db.query", "boom traceback", "GET /notes"]


async def test_logs_page_cursor_is_the_last_raw_entry(yard_filtered):
    client, token, _argv = yard_filtered
    page = await _page(client, token, "only=status%3Derror")
    # cursor-4 and cursor-5 are hidden, but the poller must resume after them.
    assert page["cursor"] == "cursor-5"


async def test_logs_a_bad_filter_is_a_400_before_the_read(yard_filtered):
    client, token, argv_log = yard_filtered
    argv_log.unlink(missing_ok=True)
    for query in ("hide=duration_ns%3Eslow", "hide=", "only="):
        res = await client.get(f"/api/v1/logs?container=heldso-app-1&{query}", headers=auth(token))
        assert res.status_code == 400, query
        assert res.json()["error"]["code"] == "bad_filter"
    assert not argv_log.exists()


async def test_logs_follow_filters_with_the_same_rules(yard_filtered):
    client, token, _argv = yard_filtered
    res = await client.get(
        "/api/v1/logs?container=heldso-app-1&follow=1&only=status%3Derror",
        headers=auth(token),
    )
    assert res.status_code == 200
    names = [
        json.loads(message)["name"] if message.startswith("{") else message
        for message in (line.split(" ", 2)[2] for line in res.text.splitlines())
    ]
    # The text line decides at once and may print ahead of the held trace.
    assert names == ["boom traceback", "db.query", "GET /notes"]


_TRICKLE_JOURNALCTL = """\
import json
import sys
import time

with open({argv_log!r}, "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
lines = open({data!r}).read().splitlines()
print(lines[0])
sys.stdout.flush()
for line in lines[1:]:
    time.sleep({delay})
    print(line)
    sys.stdout.flush()
"""


@pytest.fixture
async def yard_trickle(tmp_path, monkeypatch):
    """The API over a journalctl that emits entries with a delay between them."""
    micros = int(WHEN.timestamp() * 1_000_000)
    entries = [
        {
            "__CURSOR": f"cursor-{n}",
            "__REALTIME_TIMESTAMP": str(micros + n * 1_000_000),
            "CONTAINER_NAME": "heldso-app-1",
            "MESSAGE": message,
        }
        for n, message in enumerate(
            [
                json.dumps({"name": "slow.child", "trace_id": "t-slow", "span_id": "c1", "parent_id": "r1"}),
                *[json.dumps({"name": f"tick {n}"}) for n in range(1, 9)],
            ]
        )
    ]
    data = tmp_path / "journal.jsonl"
    data.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    argv_log = tmp_path / "argv"
    script = tmp_path / "journalctl"
    script.write_text(
        f"#!{sys.executable}\n" + _TRICKLE_JOURNALCTL.format(argv_log=str(argv_log), data=str(data), delay=0.45)
    )
    script.chmod(0o755)
    (tmp_path / "journal").mkdir()
    monkeypatch.setenv("YARD_ROOT", str(tmp_path))
    monkeypatch.setenv("YARD_SKIP_DEPLOY", "1")
    monkeypatch.setenv("YARD_BOOT", "0")
    monkeypatch.setenv("YARD_PODMAN_BIN", "yard-podman-missing")
    monkeypatch.setenv("YARD_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("YARD_JOURNALCTL_BIN", str(script))
    async with TestClient(bootstrap, request_timeout=15) as client:
        token = (tmp_path / "tokens" / "admin.token").read_text().strip()
        yield client, token, argv_log


async def test_logs_filtered_text_keeps_one_entry_per_line(yard_filtered):
    client, token, _argv = yard_filtered
    res = await client.get("/api/v1/logs?container=heldso-app-1&hide=t-health", headers=auth(token))
    assert res.status_code == 200
    lines = res.text.splitlines()
    assert len(lines) == 4
    assert all(line.count("heldso-app-1") == 1 for line in lines)


async def test_logs_follow_expires_a_held_trace_during_traffic(yard_trickle):
    client, token, _argv = yard_trickle
    res = await client.get(
        "/api/v1/logs?container=heldso-app-1&follow=1&hide=zzz",
        headers=auth(token),
    )
    assert res.status_code == 200
    names = [json.loads(line.split(" ", 2)[2])["name"] for line in res.text.splitlines()]
    assert "slow.child" in names
    # The held trace ages out mid-stream: the last line is a tick, not the
    # child a drain would have flushed after every other line.
    assert names[-1] == "tick 8"
