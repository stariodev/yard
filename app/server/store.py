"""Process settings and Yard's files. Config is the environment; Store is the records on disk."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from app.shared import (
    TEMPLATES,
    YardError,
    atomic_write,
    check_app_name,
    check_env,
    check_name,
    check_ref,
    check_repo,
    check_trigger,
    ensure_file,
    format_env,
    now,
    parse_env,
    parse_runs,
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
    sha: str = ""
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
            sha=os.environ.get("SHA", ""),
            journal_dir=Path(os.environ.get("YARD_JOURNAL_DIR", "/var/log/journal")),
            journalctl_bin=os.environ.get("YARD_JOURNALCTL_BIN", "journalctl"),
            journal_socket=Path(os.environ.get("YARD_JOURNAL_SOCKET", "/run/systemd/journal/socket")),
        )


def token_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


@dataclass(slots=True)
class App:
    name: str
    repo: str
    ref: str
    trigger: str
    created: str
    # What the app runs: auto (""), yard:<template>, a repo path, an image, or an https URL.
    runs: str = ""


@dataclass(slots=True)
class Deployed:
    """What the last ok deploy ran, kept in apps/<name>/deployed."""

    revision: str = ""
    runs: str = ""
    kind: str = ""
    reason: str = ""
    template: str = ""


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
                "app does not exist",
                context={"name": name},
                help_text="Run yard app ls to see the apps on this host.",
            )
        return _read_app(name, file.read_text())

    def create_app(
        self,
        name: str,
        repo: str,
        ref: str = "",
        trigger: str = "",
        runs: str = "",
    ) -> App:
        check_app_name(name)
        if repo:
            repo = check_repo(repo)
        ref = ref or ("main" if repo else "")
        if ref:
            check_ref(ref)
        if ref and not repo:
            raise YardError(
                400,
                "bad_ref",
                f"ref is a branch of the repo, and {name} has no repo",
                context={"ref": ref, "name": name},
                help_text="Create an app with a repo to follow a branch.",
            )
        if self._record(name).is_file():
            raise YardError(409, "exists", "already exists", help_text="Pick another name.")
        app = App(name, repo, ref, check_trigger(trigger), now())
        if runs:
            app.runs = self._check_runs(app, runs)
        self._write(app)
        return app

    def update_app(
        self,
        name: str,
        *,
        repo: str | None = None,
        ref: str | None = None,
        trigger: str | None = None,
        runs: str | None = None,
    ) -> App:
        app = self.app(name)
        if repo is not None:
            app.repo = check_repo(repo)
        if ref is not None:
            if not app.repo:
                raise YardError(
                    400,
                    "bad_ref",
                    f"ref is a branch of the repo, and {name} has no repo",
                    context={"ref": ref, "name": name},
                    help_text="An app with no repo has no branch.",
                )
            ref = ref or "main"
            check_ref(ref)
            app.ref = ref
        if app.repo and not app.ref:
            app.ref = "main"
        if trigger is not None:
            app.trigger = check_trigger(trigger)
        if runs is not None:
            app.runs = self._check_runs(app, runs) if runs else ""
        else:
            # A repo change without a runs change must still satisfy the pair rule.
            self._check_pair(app, parse_runs(app.runs)[0], app.runs)
        self._write(app)
        return self.app(name)

    def delete_app(self, name: str) -> None:
        file = self._record(name)
        if not file.is_file():
            raise YardError(
                404,
                "not_found",
                "app does not exist",
                context={"name": name},
                help_text="Run yard app ls to see the apps on this host.",
            )
        file.unlink()

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
                context={"name": name},
                help_text="Ask another token to remove it.",
            )
        path = self.root / "tokens" / name
        if not path.is_file():
            raise YardError(
                404,
                "not_found",
                "unknown token",
                context={"name": name},
                help_text="Run yard token ls.",
            )
        path.unlink()

    def token_ok(self, plain: str) -> Token | None:
        if not plain:
            return None
        digest = token_hash(plain)
        base = self.root / "tokens"
        if not base.is_dir():
            return None
        for path in base.iterdir():
            token = self._token(path)
            if token is None:
                continue
            lines = path.read_text().splitlines()
            if len(lines) >= 2 and lines[1] == digest:
                return token
        return None

    def _check_runs(self, app: App, runs: str) -> str:
        """Normalize a runs value and check it against the repo."""
        kind, value = parse_runs(runs)
        if kind == "template" and value.removeprefix("yard:") not in TEMPLATES:
            raise YardError(
                400,
                "bad_runs",
                f"unknown template {value}",
                context={"runs": runs},
                help_text=f"The templates are {', '.join(f'yard:{t}' for t in TEMPLATES)}.",
            )
        self._check_pair(app, kind, value)
        return value

    def _check_pair(self, app: App, kind: str, value: str) -> None:
        """A repo app runs a file in it; a repo-less app pulls an image or fetches a URL."""
        if kind in ("path", "template") and not app.repo:
            raise YardError(
                400,
                "bad_runs",
                f"{value} is a path in a repo, and {app.name} has no repo",
                context={"runs": value, "name": app.name},
                help_text=(f"An image needs a tag, like {value}:1.0. A compose file can come from an https URL."),
            )
        if kind in ("image", "url") and app.repo:
            raise YardError(
                400,
                "bad_runs",
                "an app with a repo runs a file from it",
                context={"runs": value, "name": app.name},
                help_text="Use a path, auto, or yard:<template>. To run an image, create an app with no repo.",
            )

    def _write(self, app: App) -> None:
        text = f"repo {app.repo}\n"
        if app.ref:
            text += f"ref {app.ref}\n"
        text += f"trigger {app.trigger}\ncreated {app.created}\n"
        if app.runs:
            text += f"runs {app.runs}\n"
        atomic_write(self._record(app.name), text)

    def _env(self, name: str) -> Path:
        return self._record(name).with_name("env")

    def _write_env(self, name: str, env: dict[str, str]) -> None:
        path = self._env(name)
        ensure_file(path)
        atomic_write(path, format_env(env))

    def _token(self, path: Path) -> Token | None:
        if not path.is_file() or path.suffix == ".token" or not _name_ok(path.name):
            return None
        lines = path.read_text().splitlines()
        if len(lines) < 2:
            return None
        return Token(path.name, lines[0])


def _name_ok(name: str) -> bool:
    try:
        check_name(name)
    except YardError:
        return False
    return True


def _read_app(name: str, text: str) -> App:
    fields = {
        "repo": "",
        "ref": "main",
        "trigger": "push",
        "created": "",
        "runs": "",
    }
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in fields:
            fields[key] = value.strip()
    return App(
        name,
        fields["repo"],
        fields["ref"] or ("main" if fields["repo"] else ""),
        fields["trigger"] or "push",
        fields["created"],
        fields["runs"],
    )


def _deployed_file(config: Config, name: str) -> Path:
    return config.app_dir(name) / "deployed"


def read_deployed(config: Config, name: str) -> Deployed | None:
    """The deployed file, or None when the app never deployed ok."""
    path = _deployed_file(config, name)
    if not path.is_file():
        return None
    fields = {"revision": "", "runs": "", "kind": "", "reason": "", "template": ""}
    for line in path.read_text().splitlines():
        key, _, value = line.partition(" ")
        if key in fields:
            fields[key] = value.strip()
    return Deployed(**fields)


def write_deployed(config: Config, name: str, deployed: Deployed) -> None:
    text = "".join(
        f"{key} {value}\n"
        for key, value in (
            ("revision", deployed.revision),
            ("runs", deployed.runs),
            ("kind", deployed.kind),
            ("reason", deployed.reason),
            ("template", deployed.template),
        )
        if value
    )
    atomic_write(_deployed_file(config, name), text)


def ensure_admin(store: Store) -> str:
    if any(token.name == "admin" for token in store.list_tokens()):
        return ""
    plain = store.create_token("admin")
    atomic_write(store.root / "tokens" / "admin.token", plain + "\n")
    return plain
