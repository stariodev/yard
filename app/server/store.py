"""Process settings and Yard's files. Config is the environment; Store is the records on disk."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from app.shared import (
    YardError,
    atomic_write,
    check_app_name,
    check_compose_url,
    check_env,
    check_image,
    check_name,
    check_ref,
    check_repo,
    check_runs,
    check_trigger,
    ensure_file,
    format_env,
    now,
    parse_env,
)


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    podman_sock: str
    podman_bin: str
    skip_deploy: bool
    boot: bool
    remote: bool
    check_seconds: float = 300.0
    prune_days: int = 7
    journal_dir: Path = Path("/var/log/journal")
    journalctl_bin: str = "journalctl"
    journal_socket: Path = Path("/run/systemd/journal/socket")
    # The main Caddyfile on Caddy's disk. Caddy resolves its imports there too.
    caddyfile: Path = Path("/etc/caddy/Caddyfile")

    @property
    def token_file(self) -> Path:
        return self.root / "tokens" / "admin.token"

    def app_dir(self, name: str) -> Path:
        return self.root / "apps" / name

    def data(self, name: str) -> Path:
        return self.app_dir(name) / "data"

    def env(self, name: str) -> Path:
        return self.app_dir(name) / "env"

    def compose_copy(self, name: str, revision: str) -> Path:
        """The fetched compose file of a URL app, named by 12 hex of its sha256."""
        return self.app_dir(name) / "compose" / f"{revision[7:19]}.yml"

    @property
    def podman_socket(self) -> str:
        """The host socket the apply container mounts."""
        return self.podman_sock or "/run/podman/podman.sock"

    def clone(self, name: str) -> Path:
        """The plain git clone for the app. A deploy resets it to the ref tip."""
        return self.app_dir(name) / "repo"

    @property
    def caddy_socket(self) -> Path:
        """The Caddy admin unix socket. The caddy container writes it to run/."""
        return self.app_dir("yard") / "caddy" / "run" / "admin.sock"

    def caddy_data(self) -> Path:
        return self.app_dir("yard") / "caddy" / "data"

    def caddy_sites(self) -> Path:
        """The site files. They sit in Caddy's /config mount, and the main Caddyfile imports *.caddy."""
        return self.app_dir("yard") / "caddy" / "config" / "sites"

    def site(self, name: str) -> Path:
        return self.caddy_sites() / f"{name}.caddy"

    @classmethod
    def from_env(cls) -> Config:
        root = Path(os.environ.get("YARD_ROOT", "yard-data")).resolve()
        boot = os.environ.get("YARD_BOOT", "1") != "0"
        podman_sock = os.environ.get("YARD_PODMAN_SOCK", "")
        remote = bool(podman_sock or os.environ.get("CONTAINER_HOST"))
        return cls(
            root=root,
            podman_sock=podman_sock,
            podman_bin=os.environ.get("YARD_PODMAN_BIN", "podman"),
            skip_deploy=os.environ.get("YARD_SKIP_DEPLOY", "") == "1",
            boot=boot,
            remote=remote,
            check_seconds=float(os.environ.get("YARD_CHECK_SECONDS", "300")),
            prune_days=int(os.environ.get("YARD_PRUNE_DAYS", "7")),
            journal_dir=Path(os.environ.get("YARD_JOURNAL_DIR", "/var/log/journal")),
            journalctl_bin=os.environ.get("YARD_JOURNALCTL_BIN", "journalctl"),
            journal_socket=Path(os.environ.get("YARD_JOURNAL_SOCKET", "/run/systemd/journal/socket")),
        )


def token_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


@dataclass(slots=True)
class App:
    """One app. Its source is one of repo (with ref and runs), image, or compose_url."""

    name: str
    trigger: str
    created: str
    repo: str = ""
    ref: str = ""
    # What a repo app runs: "" (auto), yard:<template>, or a path in the repo.
    runs: str = ""
    image: str = ""
    compose_url: str = ""


@dataclass(slots=True)
class Deployed:
    """What a deploy runs. The deploy container's labels hold it.

    kind is containerfile, compose, or image. runs is the repo answer: a path or yard:<template>.
    template is detect.STAMP for a yard:<template> answer.
    """

    revision: str = ""
    runs: str = ""
    kind: str = ""
    reason: str = ""
    template: str = ""
    image: str = ""


@dataclass(slots=True)
class Token:
    name: str
    created: str


class Store:
    """Apps and tokens on disk. Each call reads the files."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def open(cls, root: Path) -> Store:
        (root / "apps").mkdir(parents=True, exist_ok=True)
        tokens = root / "tokens"
        tokens.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cls(root)

    def _record(self, name: str) -> Path:
        return self.root / "apps" / name / "app"

    def list_apps(self) -> list[App]:
        base = self.root / "apps"
        if not base.is_dir():
            return []
        found: list[App] = []
        for path in base.iterdir():
            file = path / "app"
            if path.is_dir() and file.is_file():
                found.append(_read_app(path.name, file.read_text()))
        return sorted(found, key=lambda app: app.name)

    def app(self, name: str) -> App:
        file = self._record(name)
        if not file.is_file():
            raise YardError(
                404,
                "not_found",
                f"app {name} does not exist",
                help_text="Run yard app ls to see the apps on this host.",
            )
        return _read_app(name, file.read_text())

    def create_app(
        self,
        name: str,
        repo: str = "",
        *,
        ref: str = "",
        runs: str = "",
        image: str = "",
        compose_url: str = "",
        trigger: str = "",
    ) -> App:
        check_app_name(name)
        if self._record(name).is_file():
            raise YardError(409, "exists", "already exists", help_text="Pick another name.")
        app = App(name, check_trigger(trigger), now())
        _set_source(app, repo=repo, ref=ref, runs=runs, image=image, compose_url=compose_url)
        self._write(app)
        return app

    def update_app(
        self,
        name: str,
        *,
        repo: str | None = None,
        ref: str | None = None,
        runs: str | None = None,
        image: str | None = None,
        compose_url: str | None = None,
        trigger: str | None = None,
    ) -> App:
        """Change the fields that are not None. A new source replaces the old one."""
        app = self.app(name)
        if trigger is not None:
            app.trigger = check_trigger(trigger)
        if "" in (repo, image, compose_url):
            raise YardError(
                400,
                "bad_source",
                "a source cannot be empty",
                help_text="To change the source, pass the new one: --repo, --image, or --compose-url.",
            )
        given = {key: value for key, value in (("repo", repo), ("image", image), ("compose_url", compose_url)) if value}
        if len(given) > 1:
            raise _two_sources()
        source = {"repo": app.repo, "image": app.image, "compose_url": app.compose_url}
        if given:
            source = dict.fromkeys(source, "") | given
        keep = not given or "repo" in given and check_repo(given["repo"]) == app.repo
        _set_source(
            app,
            **source,
            ref=ref if ref is not None else (app.ref if keep else ""),
            runs=runs if runs is not None else (app.runs if keep else ""),
        )
        self._write(app)
        return app

    def delete_app(self, name: str) -> None:
        self.app(name)
        self._record(name).unlink()

    def create_token(self, name: str) -> str:
        check_name(name)
        path = self.root / "tokens" / name
        if path.exists():
            raise YardError(409, "exists", "already exists", help_text="Pick another name.")
        plain = secrets.token_hex(24)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(path, f"{now()}\n{token_hash(plain)}\n")
        return plain

    def read_env(self, name: str) -> dict[str, str]:
        path = self._env(name)
        if not path.is_file():
            return {}
        return parse_env(path.read_text())

    def write_env(self, name: str, env: dict[str, str]) -> list[str]:
        check_env(env)
        self._write_env(name, env)
        return sorted(env)

    def patch_env(self, name: str, patch: dict[str, str | None]) -> list[str]:
        check_env(patch)
        env = self.read_env(name)
        for key, value in patch.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        self._write_env(name, env)
        return sorted(env)

    def list_tokens(self) -> list[Token]:
        base = self.root / "tokens"
        if not base.is_dir():
            return []
        found: list[Token] = []
        for path in sorted(base.iterdir(), key=lambda item: item.name):
            token = self._token(path)
            if token is not None:
                found.append(token)
        return found

    def delete_token(self, name: str, actor: str = "") -> None:
        if name == actor:
            raise YardError(
                403,
                "forbidden",
                "you cannot delete your own token",
                help_text="Ask another token to remove it.",
            )
        path = self.root / "tokens" / name
        if not path.is_file():
            raise YardError(
                404,
                "not_found",
                f"unknown token {name}",
                help_text="Run yard token ls.",
            )
        path.unlink()
        if name == "admin":
            # The first-boot copy of the plain token goes with it.
            (self.root / "tokens" / "admin.token").unlink(missing_ok=True)

    def token_ok(self, plain: str) -> Token | None:
        """The token whose stored hash matches. Each file is read once."""
        if not plain:
            return None
        digest = token_hash(plain)
        base = self.root / "tokens"
        if not base.is_dir():
            return None
        for path in base.iterdir():
            lines = self._token_lines(path)
            if len(lines) >= 2 and hmac.compare_digest(lines[1], digest):
                return Token(path.name, lines[0])
        return None

    def _write(self, app: App) -> None:
        atomic_write(self._record(app.name), _format(app))

    def _env(self, name: str) -> Path:
        return self._record(name).with_name("env")

    def _write_env(self, name: str, env: dict[str, str]) -> None:
        path = self._env(name)
        ensure_file(path)
        atomic_write(path, format_env(env))

    def _token(self, path: Path) -> Token | None:
        lines = self._token_lines(path)
        return Token(path.name, lines[0]) if len(lines) >= 2 else None

    def _token_lines(self, path: Path) -> list[str]:
        """The created stamp and the hash of a token file; [] for anything else in tokens/."""
        if not path.is_file() or path.suffix == ".token" or not _name_ok(path.name):
            return []
        return path.read_text().splitlines()


def _name_ok(name: str) -> bool:
    try:
        check_name(name)
    except YardError:
        return False
    return True


def _format(app: App) -> str:
    fields = (
        ("repo", app.repo),
        ("ref", app.ref),
        ("runs", app.runs),
        ("image", app.image),
        ("compose_url", app.compose_url),
        ("trigger", app.trigger),
        ("created", app.created),
    )
    return "".join(f"{key} {value}\n" for key, value in fields if value)


def _read_app(name: str, text: str) -> App:
    fields = dict.fromkeys(("repo", "ref", "runs", "image", "compose_url", "trigger", "created"), "")
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in fields:
            fields[key] = value.strip()
    return App(name, **{**fields, "trigger": fields["trigger"] or "push"})


def _set_source(app: App, *, repo: str, ref: str, runs: str, image: str, compose_url: str) -> None:
    """Check and set the source. One of repo, image, and compose_url; ref and runs belong to a repo."""
    if sum(1 for value in (repo, image, compose_url) if value) > 1:
        raise _two_sources()
    if (ref or runs) and not repo:
        raise YardError(
            400,
            "bad_source",
            f"ref and runs belong to a repo, and {app.name} has no repo",
            help_text="Give the app a repo: yard app set --repo https://<forge>/<owner>/<name>",
        )
    app.repo = check_repo(repo) if repo else ""
    app.ref = ref or "main" if repo else ""
    if app.ref:
        check_ref(app.ref)
    app.runs = check_runs(runs) if repo else ""
    app.image = check_image(image) if image else ""
    app.compose_url = check_compose_url(compose_url) if compose_url else ""


def _two_sources() -> YardError:
    return YardError(
        400,
        "bad_source",
        "an app has one source: repo, image, or compose_url",
        help_text="Pass one of --repo, --image, or --compose-url.",
    )


def ensure_admin(store: Store) -> str:
    """The first token, on a host with none. A deleted admin stays deleted: another token removed it."""
    if store.list_tokens():
        return ""
    plain = store.create_token("admin")
    atomic_write(store.root / "tokens" / "admin.token", plain + "\n")
    return plain
