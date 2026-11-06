"""The app records, the deploy, the checks, and prune. The yard record is ensured at boot."""

import hashlib
import logging
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from app.server import compose, detect, git, journal, podman, sites, sources, status
from app.server.detect import STAMP, Plan
from app.server.forges import Forge
from app.server.status import Record
from app.server.store import App, Config, Deployed
from app.server.yard import Job, Tip, Yard
from app.shared import YardError, app_container, atomic_write, clone_url, deploy_container, ensure_file, now

log = logging.getLogger("yard")


def ensure_host(yard: Yard) -> None:
    """The yard app record. Its compose runs the app and Caddy."""
    ensure_file(yard.config.env("yard"))
    try:
        yard.store.app("yard")
    except YardError as exc:
        if exc.status != 404:
            raise
        yard.store.create_app("yard", trigger="manual")


def create(yard: Yard, name: str, *, actor: str = "", **source: str) -> App:
    """A new app. `source` holds repo, ref, runs, image, compose_url, and trigger."""
    app = yard.store.create_app(name, **source)
    ensure_file(yard.config.env(app.name))
    _journal_app(yard, "app.create", app, actor)
    return app


def update(yard: Yard, name: str, *, actor: str = "", **changes: str | None) -> App:
    app = yard.store.update_app(name, **changes)
    _journal_app(yard, "app.update", app, actor)
    return app


def _journal_app(yard: Yard, event: str, app: App, actor: str) -> None:
    journal.send(
        yard.config,
        event,
        f"app {app.name} {event.split('.')[1]}d",
        app=app.name,
        repo=app.repo,
        ref=app.ref,
        runs=app.runs,
        image=app.image,
        compose_url=app.compose_url,
        trigger=app.trigger,
        actor=actor,
    )


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

    project = compose.saved_project(yard.config, app, await status.get_plan(yard, app))
    if project is not None:
        try:
            await compose.run(yard.config, name, "purge", project)
        except Exception as exc:
            log.warning("purge %s: %s", name, exc)

    yard.store.delete_app(name)
    directory = yard.config.app_dir(name)
    if directory.exists():
        shutil.rmtree(directory)
    journal.send(yard.config, "app.delete", f"app {name} deleted", app=name, actor=actor)
    return removed


# --- Deploy -------------------------------------------------------------------------------------
#
# A deploy runs one container, deploy-<name>. Yard prepares the plan (sync, detect, pull, fetch),
# then starts the container detached, follows its log, and reads its exit code. The container's
# labels and log are the only record. A running deploy container is the deploy lock.


def start_deploy(yard: Yard, name: str, *, cause: str = "manual", actor: str = "") -> Job:
    """The running job for the app, or a new deploy spawned in the background."""
    running = yard.deploys.get(name)
    if running is not None and running.status == "running":
        return running
    job = _new_job(yard, name, cause, actor)
    yard.spawn(_run(yard, job))
    return job


async def deploy(yard: Yard, name: str, *, cause: str = "manual", actor: str = "") -> Record:
    """Deploy what the source says now, and return the record of the deploy container."""
    return await _run(yard, _new_job(yard, name, cause, actor))


def _new_job(yard: Yard, name: str, cause: str, actor: str) -> Job:
    """A new job. It replaces the app's job only when that one has ended; `_run` takes the slot
    once it holds the lock, so a follower of the running deploy keeps its job."""
    job = Job(id=secrets.token_hex(4), app=name, cause=cause, actor=actor, started=now())
    running = yard.deploys.get(name)
    if running is None or running.status != "running":
        yard.deploys[name] = job
    return job


async def _run(yard: Yard, job: Job) -> Record:
    try:
        async with yard.lock(job.app):
            yard.deploys[job.app] = job
            record = await _deploy(yard, job)
    except Exception as exc:
        job.push(f"!! {exc}")
        record = _memory_record(job, "failed", str(exc))
    job.end(asdict(record))
    return record


async def _deploy(yard: Yard, job: Job) -> Record:
    config, name = yard.config, job.app
    app = yard.store.app(name)
    if config.skip_deploy:
        job.push("YARD_SKIP_DEPLOY is set. Deploy did not run.")
        return _memory_record(job, "skipped")
    listed = await status.list_containers(yard)
    running = listed.get(deploy_container(name))
    if running is not None and running.running:
        job.push(f"== {deploy_container(name)} is running. This deploy follows it.")
        await compose.follow(config, name, job.push)
        await compose.wait(config, name)
        # The deploy it followed may run an older revision: check again.
        want(yard, [name], job.cause, job.actor)
        return await _read_record(yard, job)
    before = status.running_plan(app, listed) or Deployed()
    target = Deployed()
    try:
        project = await _prepare(yard, app, job, before, target)
    except YardError as exc:
        # The apply never runs. The deploy container still records this deploy: its log is the lines above.
        job.push(f"!! {exc}")
        try:
            await compose.run(config, name, "fail", None, labels=_labels(job, target, before, str(exc)), log=job.lines)
        except YardError as failed:
            log.warning("deploy record %s: %s", name, failed.message)
            return _memory_record(job, "failed", str(exc))
        return await _read_record(yard, job)
    labels = _labels(job, target, before)
    await compose.run(config, name, "up", project, labels=labels, log=job.lines, on_line=job.push)
    return await _read_record(yard, job)


async def _prepare(yard: Yard, app: App, job: Job, before: Deployed, target: Deployed) -> compose.Project:
    """Resolve the source into `target` and build the project. `target` keeps what is known when this fails,
    so the check loop does not try the same revision again."""
    config = yard.config
    if app.repo:
        job.push(f"== sync {app.repo} {app.ref}")
        forge = yard.forge(app.repo)
        forge.require_connected()
        target.revision = await git.sync(
            config.clone(app.name), clone_url(app.repo), app.ref, await forge.token(app.repo)
        )
        target.runs = app.runs
        target.template = STAMP if app.runs.startswith("yard:") else ""
        plan = _plan(yard, app, before)
        target.runs, target.kind, target.reason, target.template = plan.answer, plan.kind, plan.reason, plan.template
        job.push(f"== runs {plan.answer} ({plan.kind}" + (f"; {plan.reason}" if plan.reason else "") + ")")
    elif app.image:
        job.push(f"== pull {app.image}")
        target.revision, target.kind = await sources.digest(app.image), "image"
    elif app.compose_url:
        job.push(f"== fetch {app.compose_url}")
        body = await sources.fetch_file(app.compose_url)
        target.revision, target.kind = "sha256:" + hashlib.sha256(body).hexdigest(), "compose"
        _save_compose_copy(config, app, body, target.revision, before.revision)
    else:
        raise YardError(
            400,
            "no_source",
            f"app {app.name} has no source",
            help_text="Set one: yard app set --repo URL, --image NAME:TAG, or --compose-url URL",
        )
    target.image = compose.plan_image(app, target.revision, target.template)
    try:
        project = compose.project(config, app, target)
    except OSError as exc:
        raise YardError(502, "compose", str(exc)) from exc
    # Only a repo app builds, and a sha whose image is still local skips the build.
    if not app.repo or await _image_exists(yard, target.image):
        project.env["SKIP_BUILD"] = "1"
    # A failed `up --wait` puts the previous project back up.
    previous_copy = config.compose_copy(app.name, before.revision) if app.compose_url and before.revision else None
    if previous_copy is not None and previous_copy.is_file():
        project.env["PREVIOUS_COMPOSE_FILE"] = str(previous_copy)
    elif not app.compose_url and before.image and before.image != target.image:
        project.env["PREVIOUS_IMAGE"] = before.image
    return project


def _save_compose_copy(config: Config, app: App, body: bytes, revision: str, before: str) -> None:
    """Check a fetched compose file and keep it. The copy that runs now stays too: a revert needs it."""
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
    copy = config.compose_copy(app.name, revision)
    atomic_write(copy, text)
    keep = {copy, config.compose_copy(app.name, before)} if before else {copy}
    for path in copy.parent.glob("*.yml"):
        if path not in keep:
            path.unlink()


def _labels(job: Job, target: Deployed, before: Deployed, error: str = "") -> dict[str, str]:
    """The deploy container's labels: the whole record, set when the container is created.

    `status.running_plan` reads the new plan on exit 0 and the `before.` plan otherwise.
    """
    labels = {"cause": job.cause, "actor": job.actor, "error": (error.splitlines() or [""])[0]}
    for item in fields(Deployed):
        labels[item.name] = getattr(target, item.name)
        labels[f"before.{item.name}"] = getattr(before, item.name)
    return {key: value for key, value in labels.items() if value}


async def _read_record(yard: Yard, job: Job) -> Record:
    found = status.deploy_record(await status.list_containers(yard), job.app)
    return found or _memory_record(job, "failed", f"lost {deploy_container(job.app)} while it ran")


def _memory_record(job: Job, outcome: str, error: str = "") -> Record:
    """The record of a deploy that left no deploy container."""
    return Record(job.app, "", "", outcome, job.cause, job.actor, "", error, job.started, now())


async def exposed_port(yard: Yard, name: str) -> int:
    """The one tcp port the running app container exposes; 0 when it is not exactly one."""
    try:
        return podman.parse_port(await yard.podman.run("inspect", app_container(name), "--format", "json"))
    except Exception:
        return 0


def _plan(yard: Yard, app: App, previous: Deployed) -> Plan:
    """What the app runs at this commit: the record's runs, or the auto resolve."""
    plan = detect.plan_for(yard.config.clone(app.name), app.name, app.runs)
    # The auto guard: a silent change of answer asks for a pin instead. All templates count as one answer.
    if not app.runs and previous.runs and _answer(previous.runs) != _answer(plan.answer):
        raise YardError(
            502,
            "detect",
            f"auto found {plan.answer} ({plan.kind}); the last deploy ran {previous.runs} ({previous.kind})",
            help_text=f"Pin one: yard app set --runs {plan.answer}   or   yard app set --runs {previous.runs}",
        )
    if plan.generated:
        atomic_write(yard.config.app_dir(app.name) / "Containerfile", plan.generated)
    return plan


def _answer(runs: str) -> str:
    return "template" if runs.startswith("yard:") else runs


def app_file(config: Config, app: App, deployed: Deployed | None, which: str) -> str:
    """The file a `yard app cat` prints: the compose files or the Containerfile of the running project."""
    if which not in ("compose", "containerfile"):
        raise YardError(404, "no_file", f"no file {which}", help_text="The files are compose and containerfile.")
    if deployed is None:
        raise YardError(
            404,
            "no_file",
            f"{app.name} has no {which} yet",
            help_text=f"The app answers after the first deploy: yard app deploy --app {app.name}",
        )
    project = compose.project(config, app, deployed)
    if which == "compose":
        paths = [Path(path) for path in project.files]
    else:
        if "DOCKERFILE" not in project.env:
            raise YardError(
                404,
                "no_file",
                f"{app.name} builds no image from a Containerfile that Yard knows",
                help_text=f"yard app cat compose --app {app.name}",
            )
        paths = [Path(project.env["CHECKOUT"]) / project.env["DOCKERFILE"]]
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise YardError(
            404,
            "no_file",
            f"{missing[0].name} of {app.name} is missing",
            help_text=f"Deploy again: yard app deploy --app {app.name}",
        )
    if len(paths) == 1:
        return paths[0].read_text()
    return "\n".join(f"# {path.name}\n{path.read_text()}" for path in paths)


async def _image_exists(yard: Yard, image: str) -> bool:
    """True when podman already holds the tag. A podman failure means build."""
    try:
        await yard.podman.run("image", "exists", image)
    except YardError:
        return False
    return True


# --- Checks -------------------------------------------------------------------------------------
#
# One worker checks the apps. The timer asks for every app; a webhook asks for the apps of one repo.
# Each app has at most one check at a time, and a request during that check runs once after it.


def want(yard: Yard, names: list[str], cause: str = "check", actor: str = "check") -> None:
    """Ask the worker to check these apps."""
    for name in names:
        yard.wanted[name] = (cause, actor)
    yard.wake.set()


async def loop(yard: Yard) -> None:
    """The check worker."""
    due = time.monotonic() + yard.config.check_seconds
    while await yard.pause(max(0.0, due - time.monotonic()), yard.wake):
        yard.wake.clear()
        if time.monotonic() >= due:
            for app in yard.store.list_apps():
                yard.wanted.setdefault(app.name, ("check", "check"))
            due = time.monotonic() + yard.config.check_seconds
        for name in [name for name in yard.wanted if name not in yard.checking]:
            cause, actor = yard.wanted.pop(name)
            yard.checking[name] = yard.spawn(_check_one(yard, name, cause, actor))


async def _check_one(yard: Yard, name: str, cause: str, actor: str) -> None:
    try:
        await check(yard, name, cause=cause, actor=actor)
    except Exception as exc:
        log.warning("%s check: %s", name, exc)
    finally:
        yard.checking.pop(name, None)
        if name in yard.wanted:
            yard.wake.set()


async def check(yard: Yard, name: str, *, cause: str = "check", actor: str = "check") -> None:
    """Read the source revision. A push app whose revision moved deploys once."""
    try:
        app = yard.store.app(name)
    except YardError:
        return
    tip = yard.tips.setdefault(name, Tip())
    try:
        tip.revision, tip.error = await _revision(yard, app), ""
    except Exception as exc:
        tip.error = exc.message if isinstance(exc, YardError) else str(exc)
        log.warning("%s source: %s", name, tip.error)
        return
    if app.trigger != "push":
        return
    listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    moved = _moved(app, status.running_plan(app, listed), tip.revision)
    # The last try is on the deploy container too, so a restart does not retry a failed plan.
    if not moved or not _moved(app, status.tried_plan(listed, name), tip.revision):
        return
    journal.send(yard.config, "check.moved", f"{name} {moved} moved", app=name, tip=tip.revision)
    await deploy(yard, name, cause=cause, actor=actor)


async def _revision(yard: Yard, app: App) -> str:
    """The newest revision of the source: a commit, an image digest, or the sha256 of a compose file."""
    if app.repo:
        forge = yard.forge(app.repo)
        forge.require_connected()
        token = await forge.token(app.repo)
        tip = await git.remote_tip(yard.config.clone(app.name), clone_url(app.repo), app.ref, token)
        forge.last_check = now()
        return tip
    if app.image:
        return await sources.digest(app.image)
    if app.compose_url:
        return "sha256:" + hashlib.sha256(await sources.fetch_file(app.compose_url)).hexdigest()
    raise YardError(400, "no_source", f"app {app.name} has no source")


def _moved(app: App, plan: Deployed | None, revision: str) -> str:
    """What moved since the plan: commit, image, file, runs, or template. "" when nothing did."""
    if plan is None or plan.revision != revision:
        return "commit" if app.repo else "image" if app.image else "file"
    if app.runs and plan.runs != app.runs:
        return "runs"
    if status.template_moved(plan):
        return "template"
    return ""


def check_repo(yard: Yard, repo: str, actor: str) -> None:
    """The webhook path: ask for a check of the push apps on one repo."""
    names = [app.name for app in yard.store.list_apps() if app.repo.lower() == repo.lower() and app.trigger == "push"]
    want(yard, names, "push", actor)


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


async def prune_loop(yard: Yard) -> None:
    """The daily prune. The first run waits ten minutes so the boot deploys settle."""
    if not await yard.pause(600):
        return
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
        if not await yard.pause(24 * 3600):
            return


# The newest images of each app that a prune keeps beyond the running one, for a rollback by hand.
KEEP_IMAGES = 3


@dataclass(slots=True)
class Image:
    tag: str
    bytes: int
    kept: str = ""


@dataclass(slots=True)
class Report:
    images: list[Image] = field(default_factory=list)
    # The bytes the image store shrank by. A dry run frees nothing.
    freed: int = 0
    days: int = 0
    dry: bool = False
    # The untagged build images older than `days`, removed. A dry run finds the ones that are untagged now;
    # Podman's prune also takes the parents that it leaves untagged, so the real count can be higher.
    untagged: int = 0
    # Why `podman image prune` stopped part way; "" when it finished.
    untagged_error: str = ""


async def prune(yard: Yard, days: int, dry: bool, *, actor: str = "") -> Report:
    """Remove the app images no container uses, then the untagged build images older than `days`.

    Each app keeps its running image and its newest KEEP_IMAGES. One app that deploys many times a
    day keeps only those: every build copies its venv into a layer of its own. An untagged image
    newer than `days` stays: it is the build cache of the next deploy.
    """
    listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    keep_images = {item.image for item in listed.values()}
    cutoff = time.time() - days * 86400
    until = f"until={days * 24}h"
    result = Report(days=days, dry=dry)
    before = 0 if dry else await _image_bytes(yard)

    newest: dict[str, int] = {}
    tagged = podman.parse_images(await yard.podman.run("images", "--format", "json"))
    tagged.sort(key=lambda image: image.created.timestamp() if image.created else 0, reverse=True)
    for image in tagged:
        if not image.name.startswith("localhost/") or image.name == "localhost/yard-apply":
            continue
        repo = image.name.rpartition(":")[0]
        rank = newest[repo] = newest.get(repo, -1) + 1
        old = not image.created or image.created.timestamp() < cutoff
        if image.name in keep_images or (rank < KEEP_IMAGES and not old):
            continue
        kept = ""
        if not dry:
            try:
                await yard.podman.run("rmi", image.name)
            except YardError as exc:
                # Podman refuses an image a container uses. The refusal is a kept reason.
                kept = exc.message.removeprefix(f"podman rmi {image.name}: ").splitlines()[-1].strip()
        result.images.append(Image(image.name, image.size, kept))

    if dry:
        found = await yard.podman.run("images", "-q", "--filter", "dangling=true", "--filter", until)
        result.untagged = len(found.split())
        return result
    # Podman's own prune: dangling images older than `days`. --external also takes the ones a build
    # container left by a stopped build still names; a refusal stops it part way, and the report says so.
    try:
        removed = await yard.podman.run("image", "prune", "-f", "--external", "--filter", until, timeout=600)
        result.untagged = len(removed.split())
    except YardError as exc:
        # The message is "podman <args>: <podman's text>"; the report keeps Podman's last line.
        result.untagged_error = (exc.message.partition(": ")[2] or exc.message).splitlines()[-1].strip()
        log.warning("prune: %s", exc.message)
    result.freed = max(0, before - await _image_bytes(yard))
    count = sum(1 for image in result.images if not image.kept)
    journal.send(
        yard.config,
        "prune",
        f"pruned {count} images and {result.untagged} untagged",
        count=str(count),
        untagged=str(result.untagged),
        untagged_error=result.untagged_error,
        freed=str(result.freed),
        actor=actor,
    )
    return result


async def _image_bytes(yard: Yard) -> int:
    """The size of the image store; 0 when Podman does not say."""
    try:
        found = podman.parse_system_df(await yard.podman.run("system", "df", "--format", "json"))
    except YardError:
        return 0
    return found[0] if found else 0
