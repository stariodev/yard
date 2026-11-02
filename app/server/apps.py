"""The app records, the deploy, the checks, and prune. The yard record is ensured at boot."""

import asyncio
import hashlib
import logging
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from app.server import compose, detect, git, journal, podman, sites, sources, status
from app.server.detect import Plan
from app.server.forges import Forge
from app.server.store import App, Config, Deployed, read_deployed, write_deployed
from app.server.yard import Job, Tip, Yard
from app.shared import YardError, app_container, atomic_write, clone_url, deploy_container, ensure_file, now, parse_runs

log = logging.getLogger("yard")


def ensure_host(yard: Yard) -> None:
    """The yard app record. Its compose runs the app and Caddy."""
    config = yard.config
    ensure_file(config.env("yard"))
    try:
        yard.store.app("yard")
    except YardError as exc:
        if exc.code != "not_found":
            raise
        yard.store.create_app("yard", "", "", "manual")
    # A yard self-deploy ends "pending": the running process is the truth, so the
    # boot sha is the deployed revision. Without this the check loop would
    # redeploy Yard on every restart.
    if config.sha:
        deployed = read_deployed(config, "yard")
        if deployed is None:
            deployed = _yard_deployed(config)
        if deployed.revision != config.sha:
            deployed.revision = config.sha
            write_deployed(config, "yard", deployed)


def _yard_deployed(config: Config) -> Deployed:
    """The deployed row for a first boot: resolved from the clone when there is one."""
    clone = config.clone("yard")
    try:
        plan = detect.resolve(clone, "yard")
    except Exception:
        return Deployed()
    return Deployed(runs=plan.answer, kind=plan.kind, reason=plan.reason)


def create(
    yard: Yard,
    name: str,
    repo: str,
    ref: str = "",
    trigger: str = "",
    runs: str = "",
    *,
    actor: str = "",
) -> App:
    app = yard.store.create_app(name, repo, ref, trigger, runs)
    ensure_file(yard.config.env(app.name))
    journal.send(
        yard.config,
        "app.create",
        f"app {app.name} created",
        app=app.name,
        repo=app.repo,
        ref=app.ref,
        trigger=app.trigger,
        runs=app.runs,
        actor=actor,
    )
    return app


def update(
    yard: Yard,
    name: str,
    *,
    repo: str | None = None,
    ref: str | None = None,
    trigger: str | None = None,
    runs: str | None = None,
    actor: str = "",
) -> App:
    app = yard.store.update_app(name, repo=repo, ref=ref, trigger=trigger, runs=runs)
    journal.send(
        yard.config,
        "app.update",
        f"app {app.name} updated",
        app=app.name,
        repo=app.repo,
        ref=app.ref,
        trigger=app.trigger,
        runs=app.runs,
        actor=actor,
    )
    return app


@dataclass(slots=True)
class Removed:
    """What a delete did. caddy_error is set when Caddy refused the load without the site file."""

    name: str
    hosts: list[str] = field(default_factory=list)
    caddy_error: str = ""


async def delete(yard: Yard, name: str, *, actor: str = "") -> Removed:
    if name == "yard":
        raise YardError(
            400,
            "forbidden",
            "the yard app runs Yard itself",
            help_text="To stop Yard, run: sudo -u yard podman stop yard-app-1 on the host.",
        )
    app = yard.store.app(name)
    # The site goes first, so Caddy never dials a container that is gone.
    removed = Removed(name, hosts=await sites.list_hosts(yard, name))
    removed.caddy_error = await sites.remove_site(yard, name, actor=actor)

    project = compose.saved_project(yard.config, app)
    if project is not None:
        try:
            await compose.apply(yard.config, name, "purge", project)
        except Exception as exc:
            log.warning("purge %s: %s", name, exc)

    yard.store.delete_app(name)
    directory = yard.config.app_dir(name)
    if directory.exists():
        shutil.rmtree(directory)
    journal.send(yard.config, "app.delete", f"app {name} deleted", app=name, actor=actor)
    return removed


@dataclass(slots=True)
class Outcome:
    status: str
    image: str = ""
    error: str = ""
    previous: str = ""
    port: int = 0


def start_deploy(yard: Yard, name: str, *, cause: str = "manual", actor: str = "") -> Job:
    """The running job for the app, or a new deploy spawned in the background."""
    running = yard.deploys.get(name)
    if running is not None and running.status == "running":
        return running
    job = _new_job(yard, name, cause, actor)
    yard.spawn(_finish(yard, job))
    return job


def _new_job(yard: Yard, name: str, cause: str, actor: str) -> Job:
    job = Job(
        id=secrets.token_hex(4),
        app=name,
        cause=cause,
        actor=actor,
        started=now(),
    )
    yard.deploys[name] = job
    return job


async def deploy(yard: Yard, name: str, *, cause: str = "manual", actor: str = "") -> Outcome:
    """One deploy path: resolve what the source says, then run compose up.

    A repo deploys its ref tip; an image pins a digest; a URL fetches a compose
    file. The deployed file is written only after an ok deploy.
    """
    return await _finish(yard, _new_job(yard, name, cause, actor))


async def _finish(yard: Yard, job: Job) -> Outcome:
    try:
        outcome = await _deploy_body(yard, job)
    except Exception as exc:
        outcome = Outcome("failed", error=str(exc))
        job.push(f"!! {exc}")
    job.status = outcome.status
    job.ended = now()
    job.outcome = asdict(outcome)
    job.changed.set()
    return outcome


async def _deploy_body(yard: Yard, job: Job) -> Outcome:
    name = job.app
    async with yard.lock(name):
        try:
            app = yard.store.app(name)
        except YardError as exc:
            job.push(f"!! {exc}")
            return Outcome("failed", error=str(exc))
        if yard.config.skip_deploy:
            job.push("YARD_SKIP_DEPLOY is set. Deploy did not run.")
            return Outcome("skipped")
        if app.name == "yard" and await _self_deploy_running(yard):
            # `podman run --replace` would kill that apply mid-build. The next Yard process checks the tip again.
            job.push("== a yard deploy is still running. This deploy did not run.")
            return Outcome("skipped", error="a yard deploy is still running")
        ready = Ready(revision="")
        try:
            if app.repo:
                job.push(f"== sync {app.repo} {app.ref}")
                ready = await _ready_repo(yard, app, job, ready)
            else:
                ready = await _ready_source(yard, app, job, ready)
        except YardError as exc:
            # The apply never ran. A failed deploy container still records this deploy.
            job.push(f"!! {exc}")
            outcome = Outcome("failed", ready.image, str(exc))
            await compose.record_failure(yard.config, app.name, _labels(job, ready, outcome), job.lines)
            _deploy_end(yard, app.name, ready.revision, outcome)
            return outcome
        return await _apply(yard, app, job, ready)


@dataclass(slots=True)
class Ready:
    """What a deploy applies. Each source fills it; `_apply` runs it the same way."""

    revision: str
    image: str = ""
    project: compose.Project | None = None
    deployed: Deployed | None = None
    # A URL compose app reverts to its previous file, not to an image.
    revert_image: bool = True


async def _ready_repo(yard: Yard, app: App, job: Job, ready: Ready) -> Ready:
    """Sync the clone to the ref tip and resolve the plan."""
    forge = yard.forge(app.repo)
    if not forge.connected():
        raise YardError(502, "forge", f"{forge.name} is not connected. Run yard forge setup {forge.name}.")
    config = yard.config
    checkout = config.clone(app.name)
    token = await forge.token(app.repo)
    ready.revision = await git.sync(checkout, clone_url(app.repo), app.ref, token)
    # Stamp the tried pair before the plan can fail, or the check loop retries every round.
    _set_tried(yard, app.name, ready.revision, _template_at(checkout, app))
    plan = _plan(yard, app, ready.revision)
    _set_tried(yard, app.name, ready.revision, plan.template)
    ready.image = compose.image_tag(app.name, ready.revision, plan.template)
    dockerfile = ""
    if plan.kind == "containerfile":
        dockerfile = str(config.app_dir(app.name) / "Containerfile") if plan.answer.startswith("yard:") else plan.answer
    try:
        project = compose.project(
            config, app, plan.kind, plan.answer, ready.revision, ready.image, checkout, dockerfile
        )
    except OSError as exc:
        raise YardError(502, "compose", str(exc)) from exc
    job.push(f"== runs {plan.answer} ({plan.kind}" + (f"; {plan.reason}" if plan.reason else "") + ")")
    # A deploy of a sha whose image is still local skips the build.
    if await _image_exists(yard, ready.image):
        project.env["SKIP_BUILD"] = "1"
    ready.project = project
    ready.deployed = Deployed(
        revision=ready.revision, runs=plan.answer, kind=plan.kind, reason=plan.reason, template=plan.template
    )
    return ready


async def _ready_source(yard: Yard, app: App, job: Job, ready: Ready) -> Ready:
    """A repo-less app: pin an image by digest, or fetch a compose file over https."""
    config = yard.config
    kind, _value = parse_runs(app.runs)
    if kind not in ("image", "url"):
        job.push("Set the repo: yard app set --repo https://<host>/<owner>/<name>")
        raise YardError(400, "no_repo", f"app {app.name} has no repo")
    job.push(f"== pull {app.runs}" if kind == "image" else f"== fetch {app.runs}")
    previous_copy = None
    if kind == "image":
        ready.revision = await sources.digest(app.runs)
        _set_tried(yard, app.name, ready.revision, "")
        ready.image = f"{app.runs.split('@', 1)[0]}@{ready.revision}"
    else:
        body = await sources.fetch_file(app.runs)
        ready.revision = "sha256:" + hashlib.sha256(body).hexdigest()
        _set_tried(yard, app.name, ready.revision, "")
        text = body.decode("utf-8", "replace")
        if detect.classify(text) != "compose":
            raise YardError(502, "detect", "a Containerfile needs a repo to build; a URL can name a compose file")
        if detect.BUILD_LINE.search(text):
            raise YardError(
                502,
                "detect",
                "a compose file from a URL cannot build: there is no checkout",
                help_text="Build the image elsewhere, or give the app a repo.",
            )
        kind = "compose"
        ready.image = f"compose:{ready.revision[7:19]}"
        ready.revert_image = False
        previous = read_deployed(config, app.name)
        if previous is not None and previous.revision:
            previous_copy = config.compose_copy(app.name, previous.revision)
        copy = config.compose_copy(app.name, ready.revision)
        copy.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(copy, text)
    project = compose.project(config, app, kind, app.runs, ready.revision, ready.image, config.app_dir(app.name))
    project.env["SKIP_BUILD"] = "1"
    if previous_copy is not None and previous_copy.is_file():
        project.env["PREVIOUS_COMPOSE_FILE"] = str(previous_copy)
    ready.project = project
    ready.deployed = Deployed(revision=ready.revision, runs=app.runs, kind=kind)
    return ready


async def _apply(yard: Yard, app: App, job: Job, ready: Ready) -> Outcome:
    """One apply container runs compose up. That container is the record of this deploy."""
    config = yard.config
    assert ready.project is not None and ready.deployed is not None
    project = ready.project
    journal.send(
        config,
        "deploy.start",
        f"deploy {app.name} {ready.revision[:12]}",
        app=app.name,
        sha=ready.revision,
        image=ready.image,
        runs=ready.deployed.runs,
        kind=ready.deployed.kind,
        cause=job.cause,
        actor=job.actor,
    )
    # A failed `up --wait` reverts to the running image.
    previous = await _running_image(yard, app.name)
    if ready.revert_image and previous and previous != ready.image:
        project.env["PREVIOUS_IMAGE"] = previous
    labels = _labels(job, ready, Outcome("running", ready.image, previous=previous))
    # Upgrading yard replaces this process, so that apply run is detached.
    detach = app.name == "yard"
    code, _text = await compose.apply(config, app.name, "up", project, labels=labels, detach=detach, on_line=job.push)
    if detach:
        return Outcome("pending", ready.image, previous=previous)
    if code == 3:
        outcome = Outcome("reverted", ready.image, f"unhealthy; reverted to {previous}", previous)
    elif code:
        outcome = Outcome("failed", ready.image, f"apply exited {code}", previous)
    else:
        outcome = Outcome("ok", ready.image, previous=previous)
    _deploy_end(yard, app.name, ready.revision, outcome)
    if outcome.status == "ok":
        write_deployed(config, app.name, ready.deployed)
        if ready.deployed.kind == "compose" and not app.repo:
            _prune_copies(config.compose_copy(app.name, ready.revision))
        outcome.port = await exposed_port(yard, app.name)
    return outcome


def _labels(job: Job, ready: Ready, outcome: Outcome) -> dict[str, str]:
    """The deploy container's labels: what `status.deploy_record` and `settle` read back."""
    plan = ready.deployed or Deployed()
    labels = {
        "sha": ready.revision,
        "image": ready.image,
        "runs": plan.runs,
        "kind": plan.kind,
        "reason": plan.reason,
        "template": plan.template,
        "cause": job.cause,
        "actor": job.actor,
        "previous": outcome.previous,
        "error": (outcome.error.splitlines() or [""])[0],
    }
    return {key: value for key, value in labels.items() if value}


def _prune_copies(keep: Path) -> None:
    """Keep the current URL copy and the newest one before it, for the revert."""
    others = sorted(
        (p for p in keep.parent.glob("*.yml") if p != keep),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    for path in others[1:]:
        path.unlink()


async def exposed_port(yard: Yard, name: str) -> int:
    """The one tcp port the running app container exposes; 0 when it is not exactly one."""
    try:
        return podman.parse_port(await yard.podman.run("inspect", app_container(name), "--format", "json"))
    except Exception:
        return 0


def _deploy_end(yard: Yard, name: str, sha: str, outcome: Outcome) -> None:
    journal.send(
        yard.config,
        "deploy.end",
        f"deploy {name} {outcome.status}",
        priority=3 if outcome.status in {"failed", "reverted"} else 6,
        app=name,
        sha=sha,
        image=outcome.image,
        status=outcome.status,
        previous=outcome.previous,
        container=deploy_container(name),
        error=(outcome.error.splitlines() or [""])[0],
    )


async def settle(yard: Yard) -> None:
    """Finish the deploys that a replaced Yard process left. Runs once at boot.

    A yard self-deploy swaps yard-app-1 while another app's apply container keeps
    running. The old process dies before it writes `deployed` and `deploy.end`;
    the deploy container still holds the record.
    """
    try:
        listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    except YardError as exc:
        log.warning("settle: %s", exc)
        return
    names = [app.name for app in yard.store.list_apps() if deploy_container(app.name) in listed]
    await asyncio.gather(*(_settle(yard, name, listed) for name in names))


async def _settle(yard: Yard, name: str, listed: dict[str, podman.Container]) -> None:
    """A running record lost its process: wait for it, then send its deploy.end.
    An ok record with a new sha lost its `deployed` write: write it from the labels."""
    async with yard.lock(name):
        record = status.deploy_record(listed, name)
        waited = record is not None and record["status"] == "running"
        if waited:
            try:
                await yard.podman.run("wait", deploy_container(name), timeout=None)
                listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
            except YardError as exc:
                log.warning("settle %s: %s", name, exc)
                return
            record = status.deploy_record(listed, name)
        if record is None or record["status"] == "running":
            return
        deployed = read_deployed(yard.config, name)
        # ensure_host owns the yard row: the running process is its revision.
        stale = (
            name != "yard"
            and record["status"] == "ok"
            and bool(record["sha"])
            and (deployed is None or deployed.revision != record["sha"])
        )
        if stale:
            write_deployed(yard.config, name, _record_deployed(listed[deploy_container(name)].labels, deployed))
        if waited or stale:
            outcome = Outcome(record["status"], record["image"], record["error"], record["previous"])
            _deploy_end(yard, name, record["sha"], outcome)


def _record_deployed(labels: dict[str, str], old: Deployed | None) -> Deployed:
    """The deployed row from the record's labels. A 0.11.0 record has no plan labels: keep the old plan."""
    if labels.get("kind"):
        return Deployed(
            revision=labels["sha"],
            runs=labels.get("runs", ""),
            kind=labels["kind"],
            reason=labels.get("reason", ""),
            template=labels.get("template", ""),
        )
    return replace(old or Deployed(), revision=labels["sha"])


async def _self_deploy_running(yard: Yard) -> bool:
    """True while the detached apply container of a yard self-deploy runs."""
    try:
        listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    except YardError:
        return False
    item = listed.get(deploy_container("yard"))
    return item is not None and item.running


def _set_tried(yard: Yard, name: str, revision: str, template: str) -> Tip:
    tip = yard.tips.setdefault(name, Tip())
    tip.tried = (revision, template)
    return tip


def _plan(yard: Yard, app: App, sha: str) -> Plan:
    """What the app runs at this commit: the record's runs, or the auto resolve."""
    clone = yard.config.clone(app.name)
    plan = detect.plan_for(clone, app.name, app.runs)
    if not app.runs:
        # The auto guard: a silent change of kind asks for a pin instead.
        previous = read_deployed(yard.config, app.name)
        if previous is not None and previous.runs and _key(previous.runs) != _key(plan.answer):
            raise YardError(
                502,
                "detect",
                f"auto found {plan.answer} ({plan.kind}); the last deploy ran {previous.runs} ({previous.kind})",
                help_text=f"Pin one: yard app set --runs {plan.answer}   or   yard app set --runs {previous.runs}",
            )
    if plan.generated:
        atomic_write(yard.config.app_dir(app.name) / "Containerfile", plan.generated)
    return plan


def _key(answer: str) -> str:
    """The auto-guard key: all templates are one answer; a path change is a new answer."""
    return "template" if answer.startswith("yard:") else answer


def app_file(config: Config, app: App, which: str) -> str:
    """The file a `yard app cat` prints, from the deployed answer."""
    deploy = "The app answers after the first deploy: yard app deploy --app " + app.name
    deployed = read_deployed(config, app.name)
    if which == "compose":
        if deployed is None:
            raise YardError(404, "no_file", f"{app.name} has no compose file yet", help_text=deploy)
        if deployed.kind == "compose":
            if deployed.runs.startswith("https://"):
                path = config.compose_copy(app.name, deployed.revision)
                if path.is_file():
                    return path.read_text()
                raise YardError(404, "no_file", f"{app.name}'s compose copy is missing", help_text=deploy)
            return _clone_file(config.clone(app.name), deployed.runs, app.name)
        return _yard_files(deployed.kind)
    if which == "containerfile":
        if deployed is None:
            raise YardError(404, "no_file", f"{app.name} has no Containerfile yet", help_text=deploy)
        if deployed.kind == "containerfile":
            if deployed.runs.startswith("yard:"):
                path = config.app_dir(app.name) / "Containerfile"
                if path.is_file():
                    return path.read_text()
                raise YardError(
                    404,
                    "no_file",
                    f"{app.name}'s Containerfile is missing",
                    help_text="Deploy again: yard app deploy --app " + app.name,
                )
            return _clone_file(config.clone(app.name), deployed.runs, app.name)
        if deployed.kind == "image":
            raise YardError(
                404,
                "no_file",
                f"{app.name} runs an image; there is no Containerfile",
                help_text="yard app cat compose",
            )
        raise YardError(
            404,
            "no_file",
            f"{app.name} runs its own compose file; that file names its build",
            help_text="yard app cat compose",
        )
    raise YardError(404, "no_file", f"no file {which}", help_text="The files are compose and containerfile.")


def _yard_files(kind: str) -> str:
    """Yard's own compose files, each preceded by its name."""
    names = ["app.compose.yml"] + (["build.compose.yml"] if kind == "containerfile" else [])
    parts = []
    for name in names:
        path = Path(compose.APPLY_DIR) / name
        if not path.is_file():
            raise YardError(502, "no_file", f"Yard's {name} is missing on this host")
        parts.append(f"# {name}\n{path.read_text()}")
    return "\n".join(parts)


def _clone_file(clone: Path, rel: str, name: str) -> str:
    root = clone.resolve()
    path = (root / rel).resolve()
    if path.parent != root and root not in path.parents:
        raise YardError(404, "no_file", f"{rel} is outside the clone")
    if not path.is_file():
        raise YardError(
            404,
            "no_file",
            f"{rel} is not in {name}'s clone",
            help_text=f"The clone is the deployed commit: yard app deploy --app {name}",
        )
    return path.read_text()


async def _image_exists(yard: Yard, image: str) -> bool:
    """True when podman already holds the tag. A podman failure means build."""
    try:
        await yard.podman.run("image", "exists", image)
    except YardError:
        return False
    return True


async def _running_image(yard: Yard, name: str) -> str:
    """The image behind the running app container; empty when there is none."""
    try:
        listed = podman.parse_ps(await yard.podman.run("ps", "--format", "json"))
    except YardError:
        return ""
    item = listed.get(app_container(name))
    return item.image if item and item.running else ""


_DEBOUNCE = 10.0


async def check_all(yard: Yard) -> None:
    """Poll every push app: repo tips once per forge, image and URL apps per app."""
    for forge in yard.forges.values():
        await _check_forge(yard, forge)
    fetched: dict[str, bytes] = {}
    for app in yard.store.list_apps():
        if not app.repo:
            await _check(yard, app, fetched=fetched, cause="check", actor="check")


async def _check(
    yard: Yard,
    app: App,
    *,
    token: str = "",
    fetched: dict[str, bytes] | None = None,
    cause: str,
    actor: str,
) -> None:
    """The newest (revision, template) for one app. A moved pair deploys once."""
    try:
        if app.repo:
            clone = yard.config.clone(app.name)
            tip = await git.remote_tip(clone, clone_url(app.repo), app.ref, token)
            template = _template_at(clone, app)
            current = (tip, template)
        elif parse_runs(app.runs)[0] == "image":
            current = (await sources.digest(app.runs), "")
        else:
            assert fetched is not None
            body = fetched.get(app.runs)
            if body is None:
                body = await sources.fetch_file(app.runs)
                fetched[app.runs] = body
            current = ("sha256:" + hashlib.sha256(body).hexdigest(), "")
    except Exception as exc:
        tip = yard.tips.setdefault(app.name, Tip())
        tip.error = exc.message if isinstance(exc, YardError) else str(exc)
        log.warning("%s source: %s", app.name, tip.error)
        return
    tip = yard.tips.setdefault(app.name, Tip())
    tip.error = ""
    tip.revision, tip.template = current
    if app.trigger != "push":
        return
    deployed = read_deployed(yard.config, app.name) or Deployed()
    old = (deployed.revision, deployed.template)
    if current == old or current == tip.tried:
        return
    moved = _moved(app, deployed, current)
    journal.send(
        yard.config,
        "check.moved",
        f"{app.name} {moved} moved",
        app=app.name,
        head=deployed.revision,
        tip=current[0],
        template=current[1],
    )
    await deploy(yard, app.name, cause=cause, actor=actor)


def _template_at(clone: Path, app: App) -> str:
    """The template stamp a deploy of the clone would write; "" for non-template answers."""
    if app.runs and not app.runs.startswith("yard:"):
        return ""
    try:
        return detect.plan_for(clone, app.name, app.runs).template
    except YardError:
        return ""


def _moved(app: App, deployed: Deployed, current: tuple[str, str]) -> str:
    """What the check saw move: commit, template, image, or file."""
    parts = []
    if current[0] != deployed.revision:
        if not app.repo:
            parts.append("file" if parse_runs(app.runs)[0] == "url" else "image")
        else:
            parts.append("commit")
    if current[1] != deployed.template:
        parts.append("template")
    return " and ".join(parts) or "revision"


async def _check_forge(yard: Yard, forge: Forge) -> None:
    if not forge.connected():
        return
    prefix = f"https://{forge.host}/"
    for app in yard.store.list_apps():
        if app.trigger != "push" or not app.repo.lower().startswith(prefix):
            continue
        try:
            token = await forge.token(app.repo)
        except YardError as exc:
            log.warning("%s token: %s", forge.name, exc)
            continue
        await _check(yard, app, token=token, cause="check", actor="check")
    forge.last_check = now()


async def check_repo(yard: Yard, repo: str) -> None:
    """The webhook path: check the apps on one repo. The first push runs now."""
    key = repo.lower()
    if _debounce(yard, key, lambda: _check_repo(yard, repo)):
        await _check_repo(yard, repo)


async def check_installation(yard: Yard, forge: Forge) -> None:
    """An installation event refreshes the saved installation. The push debounce applies."""
    if _debounce(yard, f"installation:{forge.host}", lambda: refresh_installations(yard, forge)):
        await refresh_installations(yard, forge)


def _debounce(yard: Yard, key: str, run: Callable[[], Awaitable[object]]) -> bool:
    """Leading plus trailing. True runs now; false queues one run at the end of the window."""
    if time.monotonic() - yard.checked.get(key, -_DEBOUNCE) < _DEBOUNCE:
        if key not in yard.queued:
            yard.queued[key] = run
            yard.spawn(_trailing(yard, key))
        return False
    yard.checked[key] = time.monotonic()
    return True


async def _trailing(yard: Yard, key: str) -> None:
    await asyncio.sleep(max(0.0, _DEBOUNCE - (time.monotonic() - yard.checked[key])))
    run = yard.queued.pop(key)
    yard.checked[key] = time.monotonic()
    await run()


async def refresh_installations(yard: Yard, forge: Forge) -> str:
    """Ask the forge for the app's installations and save them. Returns the error text, "" when it answered."""
    try:
        installations = await forge.find_installations()
    except YardError as exc:
        log.warning("%s installation: %s", forge.name, exc)
        return str(exc)
    if installations:
        journal.send(
            yard.config,
            "forge.installation",
            f"{forge.name} installation",
            forge=forge.name,
            accounts=",".join(str(entry.get("account") or "") for entry in installations),
            installations=",".join(str(entry.get("id") or "") for entry in installations),
        )
    return ""


async def _check_repo(yard: Yard, repo: str) -> None:
    try:
        forge = yard.forge(repo)
    except YardError:
        return
    if not forge.connected():
        return
    try:
        token = await forge.token(repo)
    except YardError as exc:
        log.warning("%s token: %s", forge.name, exc)
        return
    for app in yard.store.list_apps():
        if app.repo.lower() == repo.lower() and app.trigger == "push":
            await _check(yard, app, token=token, cause="push", actor=f"webhook:{forge.name}")


async def loop(yard: Yard) -> None:
    """One loop that asks the forges for the watched refs on a timer."""
    try:
        while True:
            await asyncio.sleep(yard.config.check_seconds)
            try:
                await check_all(yard)
            except Exception as exc:
                log.warning("check: %s", exc)
    except asyncio.CancelledError:
        return


async def prune_loop(yard: Yard) -> None:
    """The daily prune. The first run waits ten minutes so the boot deploys settle."""
    try:
        await asyncio.sleep(600)
        while True:
            try:
                found = await prune(yard, yard.config.prune_days, False, actor="auto")
            except Exception as exc:
                log.warning("prune: %s", exc)
                yard.last_prune_error = str(exc)
            else:
                yard.last_prune = now()
                yard.last_prune_freed = found.freed
                yard.last_prune_error = ""
            await asyncio.sleep(24 * 3600)
    except asyncio.CancelledError:
        return


@dataclass(slots=True)
class Image:
    tag: str
    bytes: int
    kept: str = ""


@dataclass(slots=True)
class Report:
    images: list[Image] = field(default_factory=list)
    freed: int = 0
    days: int = 0
    dry: bool = False


async def prune(yard: Yard, days: int, dry: bool, *, actor: str = "") -> Report:
    """Remove localhost images no container uses."""
    listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    keep_images = {item.image for item in listed.values() if item.image.startswith("localhost/")}

    cutoff = time.time() - days * 86400
    result = Report(days=days, dry=dry)
    raw = await yard.podman.run("images", "--format", "json")
    for image in podman.parse_images(raw):
        if not image.name.startswith("localhost/") or image.name == "localhost/yard-apply":
            continue
        if image.name in keep_images or (image.created and image.created.timestamp() >= cutoff):
            continue
        kept = ""
        if not dry:
            try:
                await yard.podman.run("rmi", image.name)
            except Exception as exc:
                # Podman refuses an image a container uses. The refusal is a kept reason.
                kept = str(exc).removeprefix(f"podman rmi {image.name}: ").splitlines()[-1].strip()
        if not kept:
            result.freed += image.size
        result.images.append(Image(image.name, image.size, kept))
    # podman image prune does not run: the build cache is worth keeping.
    if not dry:
        count = sum(1 for image in result.images if not image.kept)
        journal.send(
            yard.config,
            "prune",
            f"pruned {count} images",
            count=str(count),
            freed=str(result.freed),
            actor=actor,
        )
    return result
