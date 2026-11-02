"""The public HTTP API. The forge webhooks sit beside it and skip auth. Only this file writes JSON shapes."""

import asyncio
import re
import time
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import asdict, dataclass
from typing import Any, cast

from stario import App as StarioApp
from stario import Context, Handler, Middleware, Route, Writer, json
from stario.http.middleware import catch_errors
from stario.json import JsonValue

from app.server import apps, doctor, journal, repos, sites, status
from app.server.forges import Forge, GitHub, Origin
from app.server.logfilter import Held, LogFilter
from app.server.store import App, Config, Deployed, Store
from app.server.yard import Job, Yard
from app.shared import YardError, now

HEALTH = Route("GET /health")
HOOK_ORIGIN = Route("POST /hooks/origin")
HOOK_GITHUB = Route("POST /hooks/github")
STATUS = Route("GET /api/v1/status")
LOGS = Route("GET /api/v1/logs")
EVENTS = Route("GET /api/v1/events")
DOCTOR = Route("GET /api/v1/doctor")
FORGES = Route("GET /api/v1/forges")
FORGE_PUT = Route("PUT /api/v1/forges/{name}")
FORGE_INSTALLATIONS = Route("POST /api/v1/forges/{name}/installations")
REPOS = Route("POST /api/v1/repos")
REPO_TOKEN = Route("POST /api/v1/repos/token")
APPS = Route("GET /api/v1/apps")
APP_CREATE = Route("POST /api/v1/apps")
APP = Route("GET /api/v1/apps/{name}")
APP_PATCH = Route("PATCH /api/v1/apps/{name}")
APP_DELETE = Route("DELETE /api/v1/apps/{name}")
APP_DEPLOY = Route("POST /api/v1/apps/{name}/deploy")
APP_DEPLOY_GET = Route("GET /api/v1/apps/{name}/deploy")
APP_FILE = Route("GET /api/v1/apps/{name}/files/{which}")
ENV = Route("GET /api/v1/apps/{name}/env")
ENV_PATCH = Route("PATCH /api/v1/apps/{name}/env")
ENV_PUT = Route("PUT /api/v1/apps/{name}/env")
CADDY = Route("GET /api/v1/apps/{name}/caddy")
CADDY_PUT = Route("PUT /api/v1/apps/{name}/caddy")
PRUNE = Route("POST /api/v1/prune")
TOKENS = Route("GET /api/v1/tokens")
TOKEN_CREATE = Route("POST /api/v1/tokens")
TOKEN_DELETE = Route("DELETE /api/v1/tokens/{name}")


async def json_object(c: Context) -> dict:
    raw = await c.req.body()
    if not raw or not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.") from exc
    if not isinstance(body, dict):
        raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.")
    return body


def app_fields(body: dict, allowed: set[str]) -> dict[str, str]:
    """The fields an app PATCH or POST takes. An unknown key is a 400."""
    fields: dict[str, str] = {}
    for key, value in body.items():
        if key not in allowed:
            raise YardError(
                400,
                "unknown_field",
                f"unknown field {key}",
                context={"field": key},
                help_text="This Yard takes name, repo, ref, trigger, runs. Upgrade the CLI: uv tool upgrade staryard",
            )
        if key == "name":
            continue
        if not isinstance(value, str):
            raise YardError(400, "bad_json", "bad json", help_text=f"{key} is a string.")
        fields[key] = value
    return fields


def app_json(app: App, deployed: Deployed | None = None) -> dict[str, Any]:
    return {
        "name": app.name,
        "repo": app.repo,
        "ref": app.ref,
        "trigger": app.trigger,
        "created": app.created,
        "runs": app.runs,
        "deployed": asdict(deployed) if deployed else None,
    }


def page_json(page: status.Page) -> dict[str, Any]:
    return {
        "app": app_json(page.app, page.deployed),
        "image": page.image,
        "sha": page.sha,
        "env_file": page.env_file,
        "dir": page.dir,
        "rows": [asdict(row) for row in page.rows],
        "hosts": page.hosts,
        "deploy": asdict(page.deploy) if page.deploy else None,
        "log": page.log,
        "template_moved": page.template_moved,
    }


def _actor(c: Context) -> str:
    """The token name on the request. Webhook work passes its own actor."""
    return str(c.state.get("actor") or "")


async def health(_c: Context, w: Writer) -> None:
    w.empty(200)


async def _hook_body(c: Context, w: Writer) -> bytes | None:
    """One megabyte tops. Oversize gets a 413 and no processing."""
    raw = await c.req.body()
    if len(raw) > 1 << 20:
        w.empty(413)
        return None
    return raw


def _hook_received(config: Config, forge: Forge, repo: str, kind: str, c: Context) -> None:
    delivery = next((value for name in forge.delivery_headers if (value := c.req.headers.get(name))), "")
    journal.send(
        config,
        "hook.received",
        f"{forge.name} {kind}",
        forge=forge.name,
        repo=repo,
        kind=str(kind),
        delivery=delivery,
    )


def _job_json(job: Job) -> dict[str, Any]:
    """A deploy while it runs. Its end is the job's outcome, the same values the deploy container keeps."""
    outcome = job.outcome or {}
    return {
        "id": job.id,
        "app": job.app,
        "status": job.status,
        "cause": job.cause,
        "actor": job.actor,
        "started": job.started,
        "ended": job.ended,
        "image": outcome.get("image", ""),
        "error": outcome.get("error", ""),
        "previous": outcome.get("previous", ""),
    }


_LOG_CONTAINER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_LOG_ABSOLUTE = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?\Z")
_LOG_RELATIVE = re.compile(r"-(\d+)(s|m|h|d)\Z")
# journalctl reads -1h and -2d as they are; a bare m means months, so it maps.
_LOG_UNITS = {"s": "s", "m": "min", "h": "h", "d": "d"}
# Podman writes container events with SYSLOG_IDENTIFIER=podman; Yard's own events use yard.
_EVENT_MATCHES = ["SYSLOG_IDENTIFIER=podman", "SYSLOG_IDENTIFIER=yard"]
_YARD_TOP = {"YARD_EVENT", "YARD_APP", "YARD_CONTAINER", "YARD_ACTOR", "YARD_SHA", "YARD_STATUS"}


def _container_match(given: str) -> str:
    """A name is CONTAINER_NAME; 12 or 64 hex picks the short or full id."""
    if re.fullmatch(r"[0-9a-fA-F]{64}", given):
        return f"CONTAINER_ID_FULL={given}"
    if re.fullmatch(r"[0-9a-fA-F]{12}", given):
        return f"CONTAINER_ID={given}"
    if _LOG_CONTAINER.fullmatch(given):
        return f"CONTAINER_NAME={given}"
    raise YardError(
        400,
        "bad_container",
        "bad container",
        help_text="A container name, a 12-char id, or a full id.",
    )


def _event_row(entry: dict) -> dict | None:
    """One journald entry as a public event, or None when it carries no event."""
    when = journal.timestamp(entry)
    source = str(entry.get("SYSLOG_IDENTIFIER") or "")
    if source == "podman":
        if str(entry.get("PODMAN_TYPE") or "") != "container":
            return None
        container = str(entry.get("PODMAN_NAME") or "")
        app = ""
        labels = entry.get("PODMAN_LABELS")
        if isinstance(labels, str):
            try:
                labels = json.loads(labels)
            except ValueError:
                labels = None
        if not isinstance(labels, dict):
            labels = {}
        app = str(labels.get("com.docker.compose.project") or "")
        event = str(entry.get("PODMAN_EVENT") or "")
        if not app and container.startswith("deploy-"):
            # The apply container is not on a compose project.
            app = container.split("-", 1)[1]
            if event in ("create", "died"):
                return _deploy_row(entry, when, app, container, event, labels)
        row: dict[str, Any] = {
            "time": when,
            "source": "podman",
            "event": event,
            "app": app,
            "container": container,
            "container_id": str(entry.get("PODMAN_ID") or ""),
            "image": str(entry.get("PODMAN_IMAGE") or ""),
            "exit_code": str(entry.get("PODMAN_EXIT_CODE") or ""),
            "oom": str(entry.get("PODMAN_OOM_KILLED") or "").lower() in {"true", "1"},
            "message": journal.message_text(entry),
        }
    elif source == "yard":
        fields = {
            key[5:].lower(): str(value)
            for key, value in entry.items()
            if key.startswith("YARD_") and key not in _YARD_TOP and str(value)
        }
        row = {
            "time": when,
            "source": "yard",
            "event": str(entry.get("YARD_EVENT") or ""),
            "app": str(entry.get("YARD_APP") or ""),
            "container": str(entry.get("YARD_CONTAINER") or ""),
            "actor": str(entry.get("YARD_ACTOR") or ""),
            "sha": str(entry.get("YARD_SHA") or ""),
            "status": str(entry.get("YARD_STATUS") or ""),
            "message": journal.message_text(entry),
            "fields": fields,
        }
    else:
        return None
    return {key: value for key, value in row.items() if value not in ("", {}, False, None)}


def _deploy_row(entry: dict, when: str, app: str, container: str, event: str, labels: dict) -> dict:
    """The create and died events of `deploy-<name>` are the deploy.start and deploy.end rows.

    The labels are the record, so these rows survive a Yard restart in the middle of a deploy.
    """
    given = {str(key)[5:]: str(value) for key, value in labels.items() if str(key).startswith("yard.") and value}
    sha = given.get("sha", "")
    if event == "create":
        name, state, message = "deploy.start", "", f"deploy {app} {sha[:12]}".rstrip()
        fields = {key: given.get(key, "") for key in ("image", "runs", "kind", "cause")}
    else:
        raw_code = str(entry.get("PODMAN_EXIT_CODE") or "0")
        code = int(raw_code) if raw_code.isdigit() else 1
        state = status.deploy_status(code)
        error = given.get("error", "") or status.deploy_error(code, given.get("previous", ""))
        name, message = "deploy.end", f"deploy {app} {state}"
        fields = {"image": given.get("image", ""), "previous": given.get("previous", ""), "error": error}
    row = {
        "time": when,
        "source": "yard",
        "event": name,
        "app": app,
        "container": container,
        "actor": given.get("actor", ""),
        "sha": sha,
        "status": state,
        "message": message,
        "fields": {key: value for key, value in fields.items() if value},
    }
    return {key: value for key, value in row.items() if value not in ("", {})}


def _event_keeps(row: dict, app: str, container: str, source: str, event: str) -> bool:
    if source and row.get("source") != source:
        return False
    if app and row.get("app") != app:
        return False
    if container and (
        row.get("container") != container and not str(row.get("container_id") or "").startswith(container)
    ):
        return False
    if event and not str(row.get("event") or "").startswith(event):
        return False
    return True


def _lines(value: str) -> int:
    """The lines query of logs and events: 1 to 10000, 100 when it is not given."""
    if not value:
        return 100
    try:
        lines = int(value)
    except ValueError:
        lines = 0
    if not 1 <= lines <= 10_000:
        raise YardError(400, "bad_lines", "bad lines", help_text="lines is 1 to 10000.")
    return lines


def _log_time(value: str) -> str:
    if not value:
        return ""
    found = _LOG_RELATIVE.fullmatch(value)
    if found:
        return f"-{found.group(1)}{_LOG_UNITS[found.group(2)]}"
    if _LOG_ABSOLUTE.fullmatch(value):
        return value
    raise YardError(
        400,
        "bad_time",
        "bad time",
        help_text='since/until: -15m, -1h, -2d, or "2025-01-31 14:00".',
        context={"value": value},
    )


async def _stream_journal(
    config: Config,
    c: Context,
    w: Writer,
    matches: list[str],
    lines: int,
    log_filter: LogFilter | None,
) -> None:
    proc = await journal.follow(config, matches, lines=lines)
    w.headers.set("content-type", "text/plain; charset=utf-8")
    w.write_headers(200)
    held: Held[str] | None = Held(log_filter) if log_filter else None
    async with aclosing(journal.follow_entries(proc, tick=0.5 if held else None)) as stream:
        async for entry in stream:
            if c.disconnected:
                break
            out: list[str] = []
            if entry is not None and (line := journal.format_line(entry)):
                out = [line] if held is None else held.push(line, journal.message_text(entry), time.monotonic())
            if held is not None:
                # Expire on every tick: busy output must not starve a held trace.
                out += held.expire(time.monotonic())
            for line in out:
                w.write(line.encode() + b"\n")
                await w.drain()
    for line in held.drain() if held else []:
        w.write(line.encode() + b"\n")
        await w.drain()
    w.end()


async def _stream_events(
    config: Config,
    c: Context,
    w: Writer,
    lines: int,
    since: str,
    want: tuple[str, str, str, str],
) -> None:
    proc = await journal.follow(config, _EVENT_MATCHES, lines=lines, since=since)
    w.headers.set("content-type", "application/x-ndjson")
    w.write_headers(200)
    async with aclosing(journal.follow_entries(proc)) as stream:
        async for entry in stream:
            if c.disconnected:
                break
            row = _event_row(entry) if entry is not None else None
            if row and _event_keeps(row, *want):
                w.write(json.dumps(row).encode() + b"\n")
                await w.drain()
    w.end()


def _env_set_event(config: Config, c: Context, name: str, keys: list[str], removed: list[str]) -> None:
    # Key names only; a value never reaches the journal.
    journal.send(
        config,
        "env.set",
        f"{name} env",
        app=name,
        keys=",".join(keys),
        removed=",".join(removed),
        actor=_actor(c),
    )


async def _error_body(_c: Context, w: Writer, exc: BaseException) -> None:
    err = cast(YardError, exc)
    w.json({"error": err.payload()}, err.status)


def bearer(store: Store) -> Middleware:
    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            header = c.req.headers.get("authorization") or ""
            raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
            token = store.token_ok(raw)
            if token is None:
                w.json(
                    {
                        "error": YardError(
                            401,
                            "unauthorized",
                            "unauthorized",
                            help_text="Pass a live token, or run yard login.",
                            example="Authorization: Bearer <token>",
                        ).payload()
                    },
                    401,
                )
                return
            c.state["actor"] = token.name
            await handler(c, w)

        return wrapped

    return middleware


@dataclass(frozen=True, slots=True)
class HooksPage:
    """Hooks routes."""

    yard: Yard
    origin: Origin
    github: GitHub

    def register(self, app: StarioApp, middleware: Sequence[Middleware] = ()) -> None:
        app.add(HOOK_ORIGIN, self.origin_hook, middleware=middleware)
        app.add(HOOK_GITHUB, self.github_hook, middleware=middleware)

    async def origin_hook(self, c: Context, w: Writer) -> None:
        """An Origin event. No signature: Yard reads the tip from the forge and never trusts the body."""
        await self._hook(self.origin, c, w)

    async def github_hook(self, c: Context, w: Writer) -> None:
        """A GitHub delivery. X-Hub-Signature-256 is HMAC-SHA256 over the raw body with the stored
        webhook secret; a bad or missing signature gets a 401 and nothing else happens."""
        await self._hook(self.github, c, w)

    async def _hook(self, forge: Forge, c: Context, w: Writer) -> None:
        """A delivery only schedules a check, and the answer is always fast."""
        raw = await _hook_body(c, w)
        if raw is None:
            return
        if not forge.signed(raw, c.req.headers.get(forge.signature_header) or ""):
            w.json({"error": YardError(401, "unauthorized", "bad signature").payload()}, 401)
            return
        forge.last_delivery = now()
        if not raw.strip():
            w.empty(204)
            return
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise YardError(400, "bad_json", "bad json", help_text="Send one JSON object.") from exc
        event, action, repo = forge.delivery(
            body if isinstance(body, dict) else {}, c.req.headers.get(forge.event_header) or ""
        )
        if action == "push" and repo:
            _hook_received(self.yard.config, forge, repo, event, c)
            c.app.create_task(apps.check_repo(self.yard, repo))
        elif action == "installation":
            _hook_received(self.yard.config, forge, "", event, c)
            c.app.create_task(apps.check_installation(self.yard, forge))
        w.empty(204)


@dataclass(frozen=True, slots=True)
class HostPage:
    """Host routes."""

    yard: Yard

    def register(self, app: StarioApp, middleware: Sequence[Middleware] = ()) -> None:
        app.add(STATUS, self.host_status, middleware=middleware)
        app.add(LOGS, self.logs_get, middleware=middleware)
        app.add(EVENTS, self.events_get, middleware=middleware)
        app.add(DOCTOR, self.doctor_run, middleware=middleware)
        app.add(PRUNE, self.run_prune, middleware=middleware)
        app.add(FORGES, self.forges_list, middleware=middleware)
        app.add(FORGE_PUT, self.forge_put, middleware=middleware)
        app.add(FORGE_INSTALLATIONS, self.forge_installations, middleware=middleware)
        app.add(REPOS, self.repo_create, middleware=middleware)
        app.add(REPO_TOKEN, self.repo_token, middleware=middleware)

    async def host_status(self, c: Context, w: Writer) -> None:
        report = await status.report(self.yard, c.req.query.get("running") == "1")
        w.json(cast(JsonValue, asdict(report)))

    async def doctor_run(self, _c: Context, w: Writer) -> None:
        w.json([asdict(check) for check in await doctor.run(self.yard)])

    async def run_prune(self, c: Context, w: Writer) -> None:
        data = await json_object(c)
        days = data.get("days", 7)
        dry = data.get("dry", False)
        if not isinstance(days, int) or isinstance(days, bool) or days < 1 or not isinstance(dry, bool):
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text="days is a number 1 or more. dry is true or false.",
                example='{"days": 7, "dry": false}',
            )
        w.json(cast(JsonValue, asdict(await apps.prune(self.yard, days, dry, actor=_actor(c)))))

    async def forges_list(self, _c: Context, w: Writer) -> None:
        """The forge summaries, as saved. A read asks no forge."""
        w.json(cast(JsonValue, [forge.summary() for forge in self.yard.forges.values()]))

    async def forge_installations(self, c: Context, w: Writer) -> None:
        """Ask the forge for the app's installations and save them. A setup polls this until one lands."""
        forge = self.yard.forge_named(c.match.params["name"])
        summary = forge.summary()
        if forge.app_id:
            error = await apps.refresh_installations(self.yard, forge)
            summary = forge.summary()
            if error:
                summary["error"] = error
        w.json(cast(JsonValue, summary))

    async def forge_put(self, c: Context, w: Writer) -> None:
        """Save a forge app once its credentials pass the forge's own check."""
        forge = self.yard.forge_named(c.match.params["name"])
        data = await json_object(c)
        values = {key: data.get(key, "") for key in forge.connect_fields}
        if not all(isinstance(value, str) and value for value in values.values()):
            names = forge.connect_fields
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f", and {names[-1]}"
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text=f"{listed} {'is a string' if len(names) == 1 else 'are strings'}.",
            )
        info = await forge.connect_with(values)
        journal.send(
            self.yard.config,
            "forge.connect",
            f"{forge.name} connected",
            forge=forge.name,
            app_id=values["app_id"],
            actor=_actor(c),
        )
        app = forge.app_info(info)
        w.json(cast(JsonValue, {"forge": forge.summary(), "app": app}))

    async def repo_create(self, c: Context, w: Writer) -> None:
        """A private repo on a forge."""
        data = await json_object(c)
        name, owner, forge = data.get("name", ""), data.get("owner", ""), data.get("forge", "")
        if not isinstance(name, str) or not isinstance(owner, str) or not isinstance(forge, str):
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text="name, owner, and forge are strings.",
                example='{"name": "heldso"}',
            )
        w.json(cast(JsonValue, await repos.create_repo(self.yard, name, owner, forge, actor=_actor(c))))

    async def repo_token(self, c: Context, w: Writer) -> None:
        """A token that can clone, fetch, and push one repo for an hour."""
        repo = (await json_object(c)).get("repo", "")
        if not isinstance(repo, str):
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text="repo is a string.",
                example='{"repo": "https://github.com/org/heldso"}',
            )
        w.json(cast(JsonValue, await repos.repo_token(self.yard, repo, actor=_actor(c))))

    async def logs_get(self, c: Context, w: Writer) -> None:
        """Container logs from the journal; they outlive the container.

        after is a journald cursor: only entries after it are returned, first
        `lines` of them, so a poller resumes where it left off. An Accept
        header that names application/json answers
        {"entries": [...], "cursor": ...} instead of text lines.
        """
        query = c.req.query
        lines = _lines(str(query.get("lines") or ""))
        since = _log_time(str(query.get("since") or ""))
        until = _log_time(str(query.get("until") or ""))
        after = str(query.get("after") or "")
        match = _container_match(str(query.get("container") or ""))
        log_filter = LogFilter.parse(query.getlist("hide"), query.getlist("only"))
        if query.get("follow") == "1":
            await _stream_journal(self.yard.config, c, w, [match], lines, log_filter)
            return
        entries = await journal.read_entries(
            self.yard.config, [match], lines=lines, since=since, until=until, after=after
        )
        shown = (
            entries
            if log_filter is None
            else [
                entry
                for entry, keep in zip(
                    entries, log_filter.select([journal.message_text(e) for e in entries]), strict=True
                )
                if keep
            ]
        )
        if "application/json" in (c.req.headers.get("accept") or ""):
            # The cursor is the last raw entry read, hidden ones included, so a filtered poller still advances.
            rows = [
                {
                    "cursor": str(entry.get("__CURSOR") or ""),
                    "container": str(entry.get("CONTAINER_NAME") or ""),
                    "time": journal.timestamp(entry),
                    "message": journal.message_text(entry),
                }
                for entry in shown
            ]
            w.json(
                cast(JsonValue, {"entries": rows, "cursor": str(entries[-1].get("__CURSOR") or "") if entries else ""})
            )
            return
        w.text("".join(line + "\n" for entry in shown if (line := journal.format_line(entry))))

    async def events_get(self, c: Context, w: Writer) -> None:
        """One merged timeline of Podman and Yard events."""
        query = c.req.query
        lines = _lines(str(query.get("lines") or ""))
        since = _log_time(str(query.get("since") or "")) or "-1d"
        until = _log_time(str(query.get("until") or ""))
        want = (
            str(query.get("app") or ""),
            str(query.get("container") or ""),
            str(query.get("source") or ""),
            str(query.get("event") or ""),
        )
        if query.get("follow") == "1":
            await _stream_events(self.yard.config, c, w, lines, since, want)
            return
        entries = await journal.read_entries(
            self.yard.config,
            _EVENT_MATCHES,
            lines=50_000,
            since=since,
            until=until,
        )
        rows = [row for entry in entries if (row := _event_row(entry)) and _event_keeps(row, *want)]
        w.json(cast(JsonValue, rows[-lines:]))


@dataclass(frozen=True, slots=True)
class AppsPage:
    """Apps routes."""

    yard: Yard

    def register(self, app: StarioApp, middleware: Sequence[Middleware] = ()) -> None:
        app.add(APPS, self.apps_list, middleware=middleware)
        app.add(APP_CREATE, self.apps_create, middleware=middleware)
        app.add(APP, self.app_page, middleware=middleware)
        app.add(APP_PATCH, self.app_patch, middleware=middleware)
        app.add(APP_DELETE, self.app_delete, middleware=middleware)
        app.add(APP_DEPLOY, self.app_deploy, middleware=middleware)
        app.add(APP_DEPLOY_GET, self.app_deploy_get, middleware=middleware)
        app.add(APP_FILE, self.app_file, middleware=middleware)
        app.add(ENV, self.env_show, middleware=middleware)
        app.add(ENV_PATCH, self.env_patch, middleware=middleware)
        app.add(ENV_PUT, self.env_put, middleware=middleware)
        app.add(CADDY, self.caddy_show, middleware=middleware)
        app.add(CADDY_PUT, self.caddy_put, middleware=middleware)

    async def apps_list(self, _c: Context, w: Writer) -> None:
        listed = await status.list_containers(self.yard)
        w.json([app_json(app, status.running_plan(app, listed)) for app in self.yard.store.list_apps()])

    async def apps_create(self, c: Context, w: Writer) -> None:
        data = await json_object(c)
        fields = app_fields(data, {"name", "repo", "ref", "trigger", "runs"})
        name = data.get("name", "")
        repo = fields.pop("repo", "")
        if not isinstance(name, str) or not isinstance(repo, str):
            raise YardError(
                400,
                "bad_json",
                "bad json",
                help_text="name and repo are strings.",
            )
        if not name.strip():
            raise YardError(
                400,
                "bad_json",
                "name is required",
                context={"field": "name"},
                help_text="An app name uses lowercase letters, digits, and -",
                example="staging",
            )
        if not repo.strip() and not fields.get("runs", "").strip():
            raise YardError(
                400,
                "bad_json",
                "repo is required",
                context={"field": "repo"},
                help_text="The repo as a full https URL; for a repo-less app pass an image or a URL in runs.",
                example="https://github.com/org/heldso",
            )
        app = apps.create(self.yard, name, repo, actor=_actor(c), **cast(dict[str, Any], fields))
        w.json(cast(JsonValue, {"app": cast(JsonValue, app_json(app))}))

    async def app_page(self, c: Context, w: Writer) -> None:
        page = await status.app_report(self.yard, c.match.params["name"])
        w.json(cast(JsonValue, page_json(page)))

    async def app_patch(self, c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        fields = app_fields(await json_object(c), {"repo", "ref", "trigger", "runs"})
        if not fields:
            raise YardError(
                400,
                "bad_json",
                "nothing to change",
                help_text="Set one of repo, ref, trigger, or runs.",
            )
        app = apps.update(self.yard, name, actor=_actor(c), **cast(dict[str, Any], fields))
        w.json(cast(JsonValue, {"app": app_json(app, await status.get_plan(self.yard, app))}))

    async def app_delete(self, c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        removed = await apps.delete(self.yard, name, actor=_actor(c))
        body: dict[str, Any] = {"name": removed.name, "hosts": removed.hosts}
        if removed.caddy_error:
            body["caddy_error"] = removed.caddy_error
        w.json(cast(JsonValue, body))

    async def app_deploy(self, c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        self.yard.store.app(name)
        data = await json_object(c)
        for key in data:
            raise YardError(
                400,
                "unknown_field",
                f"unknown field {key}",
                help_text="A deploy request takes an empty body.",
            )
        job = apps.start_deploy(self.yard, name, cause="manual", actor=_actor(c))
        w.json(cast(JsonValue, {"deploy": _job_json(job)}), status=202)

    async def app_deploy_get(self, c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        self.yard.store.app(name)
        job = self.yard.deploys.get(name)
        if job is None or (job.status != "running" and c.req.query.get("follow") != "1"):
            # No deploy runs: the deploy container is the record. A skipped deploy has none.
            record = await status.last_deploy(self.yard, name)
            if record is not None:
                body = asdict(record)
                if record.status == "ok":
                    body["port"] = await apps.exposed_port(self.yard, name)
                w.json(cast(JsonValue, {"deploy": body}))
                return
            if job is None:
                raise YardError(404, "not_found", f"{name} has no deploy yet", context={"app": name})
        if c.req.query.get("follow") != "1":
            body = _job_json(job)
            body["lines"] = job.lines
            w.json(cast(JsonValue, {"deploy": body}))
            return
        w.headers.set("content-type", "application/x-ndjson")
        w.write_headers(200)
        seen = 0
        while not c.disconnected:
            start = max(0, len(job.lines) - (job.pushed - seen))
            for line in job.lines[start:]:
                w.write(json.dumps({"line": line}).encode() + b"\n")
            seen = job.pushed
            if job.status != "running" and seen == job.pushed:
                break
            await w.drain()
            if seen == job.pushed:
                try:
                    await asyncio.wait_for(job.changed.wait(), 15)
                except TimeoutError:
                    pass
        body = _job_json(job)
        if job.status == "ok":
            body["port"] = await apps.exposed_port(self.yard, name)
        w.write(json.dumps({"deploy": body}).encode() + b"\n")
        await w.drain()
        w.end()

    async def app_file(self, c: Context, w: Writer) -> None:
        """The compose or Containerfile an app runs, as text/plain."""
        app = self.yard.store.app(c.match.params["name"])
        w.text(apps.app_file(self.yard.config, app, await status.get_plan(self.yard, app), c.match.params["which"]))

    async def env_show(self, c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        self.yard.store.app(name)
        w.json(cast(JsonValue, self.yard.store.read_env(name)))

    async def env_patch(self, c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        self.yard.store.app(name)
        body = await json_object(c)
        patch: dict[str, str | None] = {}
        for key, value in body.items():
            if not isinstance(key, str) or (value is not None and not isinstance(value, str)):
                raise YardError(
                    400,
                    "bad_json",
                    "bad json",
                    help_text="env is an object of strings or null.",
                    example='{"KEY": null}',
                )
            patch[key] = value
        keys = self.yard.store.patch_env(name, patch)
        _env_set_event(
            self.yard.config,
            c,
            name,
            sorted(key for key, value in patch.items() if value is not None),
            sorted(key for key, value in patch.items() if value is None),
        )
        w.json({"keys": keys})

    async def env_put(self, c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        self.yard.store.app(name)
        body = await json_object(c)
        env: dict[str, str] = {}
        for key, value in body.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise YardError(
                    400,
                    "bad_json",
                    "bad json",
                    help_text="env is an object of strings.",
                    example='{"KEY": "value"}',
                )
            env[key] = value
        removed = sorted(set(self.yard.store.read_env(name)) - set(env))
        keys = self.yard.store.write_env(name, env)
        _env_set_event(self.yard.config, c, name, sorted(env), removed)
        w.json({"keys": keys})

    async def caddy_show(self, c: Context, w: Writer) -> None:
        """The site file as text/plain; empty when the app has none."""
        name = c.match.params["name"]
        self.yard.store.app(name)
        w.text(sites.get_site(self.yard, name))

    async def caddy_put(self, c: Context, w: Writer) -> None:
        """The whole site file as the body. Caddy checks it on load; empty text removes it."""
        name = c.match.params["name"]
        self.yard.store.app(name)
        raw = await c.req.body()
        try:
            text = raw.decode()
        except UnicodeDecodeError as exc:
            raise YardError(400, "bad_caddyfile", "a site file is UTF-8 text") from exc
        hosts = await sites.set_site(self.yard, name, text, actor=_actor(c))
        w.json(cast(JsonValue, {"hosts": hosts}))


@dataclass(frozen=True, slots=True)
class AccessPage:
    """Access routes."""

    yard: Yard

    def register(self, app: StarioApp, middleware: Sequence[Middleware] = ()) -> None:
        app.add(TOKENS, self.tokens_list, middleware=middleware)
        app.add(TOKEN_CREATE, self.tokens_create, middleware=middleware)
        app.add(TOKEN_DELETE, self.tokens_delete, middleware=middleware)

    async def tokens_list(self, _c: Context, w: Writer) -> None:
        w.json([asdict(token) for token in self.yard.store.list_tokens()])

    async def tokens_create(self, c: Context, w: Writer) -> None:
        data = await json_object(c)
        name = data.get("name", "")
        if not isinstance(name, str) or not name:
            raise YardError(400, "bad_json", "bad json", help_text="name is a string.")
        plain = self.yard.store.create_token(name)
        journal.send(self.yard.config, "token.create", f"token {name}", name=name, actor=_actor(c))
        w.json({"name": name, "token": plain})

    async def tokens_delete(self, c: Context, w: Writer) -> None:
        name = c.match.params["name"]
        actor = _actor(c)
        self.yard.store.delete_token(name, actor=actor)
        journal.send(self.yard.config, "token.delete", f"token {name}", name=name, actor=actor)
        w.json({"name": name})


def register(app: StarioApp, yard: Yard) -> None:
    auth = (bearer(yard.store),)
    # YardError is the API's failure shape on every route. Uncaught exceptions
    # fall through to the framework's own 500.
    app.use("/", catch_errors(YardError, respond=_error_body))
    app.add(HEALTH, health)
    origin = next(forge for forge in yard.forges.values() if isinstance(forge, Origin))
    github = next(forge for forge in yard.forges.values() if isinstance(forge, GitHub))
    HooksPage(yard, origin, github).register(app)
    HostPage(yard).register(app, middleware=auth)
    AppsPage(yard).register(app, middleware=auth)
    AccessPage(yard).register(app, middleware=auth)
