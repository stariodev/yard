"""Shared values: the error shape, name and env checks, time text, atomic writes. Stdlib only."""

import ast
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

JsonScalar = str | int | float | bool | None


class YardError(Exception):
    """status and code identify the failure. message is the short reason."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        exit_code: int | None = None,
        *,
        context: dict[str, JsonScalar] | None = None,
        help_text: str | None = None,
        example: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.context = dict(context) if context else {}
        self.help_text = help_text
        self.example = example

    def shown_context(self) -> dict[str, JsonScalar]:
        found = dict(self.context)
        if self.exit_code is not None:
            found.setdefault("exit_code", self.exit_code)
        return found

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        shown = self.shown_context()
        if shown:
            body["context"] = shown
        if self.help_text:
            body["help"] = self.help_text
        if self.example:
            body["example"] = self.example
        return body

    def __str__(self) -> str:
        parts = [self.message]
        shown = self.shown_context()
        if shown:
            ctx = ", ".join(f"{key}={value!r}" for key, value in shown.items())
            parts.append(f"  Context: {ctx}")
        if self.help_text:
            parts.append(f"  Help: {self.help_text}")
        if self.example:
            parts.append(f"  Example:\n{self.example}")
        return "\n".join(parts)


def explain(text: str) -> str:
    """Turn an API error body into the same text as str(YardError)."""

    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return text
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        return text
    raw_context = error.get("context")
    context: dict[str, JsonScalar] | None = None
    exit_code = None
    if isinstance(raw_context, dict):
        context = {}
        for key, value in raw_context.items():
            if not isinstance(key, str):
                continue
            if value is not None and not isinstance(value, str | int | float | bool):
                continue
            if key == "exit_code" and isinstance(value, int):
                exit_code = value
                continue
            context[key] = value
    help_text = error.get("help")
    example = error.get("example")
    return str(
        YardError(
            0,
            str(error.get("code") or ""),
            error["message"],
            exit_code,
            context=context,
            help_text=help_text if isinstance(help_text, str) else None,
            example=example if isinstance(example, str) else None,
        )
    )


_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_REPO_PART = re.compile(r"[A-Za-z0-9._-]+")
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9@%_+=:,./-]+")

# The git forges Yard knows: Cursor Origin and GitHub. A repo is https://<host>/<owner>/<name>.
FORGE_HOSTS = ("origin.cursor.com", "github.com")


def check_name(name: str) -> None:
    if name.startswith("yard-"):
        raise YardError(
            400,
            "bad_name",
            "name must be a short lowercase word",
            context={"name": name},
            help_text="yard-* is reserved for this host.",
            example="heldso",
        )
    if not _NAME.fullmatch(name):
        raise YardError(
            400,
            "bad_name",
            "name must be a short lowercase word",
            context={"name": name},
            help_text="Start with a letter. Then use letters, digits, and hyphens, up to 63 characters.",
            example="heldso",
        )


def check_app_name(name: str) -> None:
    check_name(name)
    if name == "caddy":
        raise YardError(
            400,
            "bad_name",
            "name must be a short lowercase word",
            context={"name": name},
            help_text="caddy is reserved for the proxy.",
            example="heldso",
        )


def check_ref(ref: str) -> None:
    if not ref or ".." in ref or ref.startswith("/") or any(ch in ref for ch in " \t\\"):
        raise YardError(
            400,
            "bad_ref",
            "ref must be a branch name",
            context={"ref": ref},
            help_text="Use a branch name. Do not start with a slash, and do not use spaces or ..",
            example="main",
        )


def check_repo(repo: str) -> str:
    """A forge repo as a full https URL. Returns the normalized spelling."""
    text = repo.strip()
    if text.endswith("/"):
        text = text[:-1]
    if text.endswith(".git"):
        text = text[:-4]
    parts = urlsplit(text)
    pieces = parts.path.split("/")
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or host not in FORGE_HOSTS
        or parts.query
        or parts.fragment
        or len(pieces) != 3
        or not pieces[1]
        or not pieces[2]
        or not _REPO_PART.fullmatch(pieces[1])
        or not _REPO_PART.fullmatch(pieces[2])
    ):
        raise YardError(
            400,
            "bad_repo",
            "repo must be the full https URL",
            context={"repo": repo},
            help_text="Use https://<forge>/<owner>/<name>.",
            example="https://github.com/org/heldso",
        )
    # The host is canonical; the owner and name keep their spelling.
    return f"https://{host}/{pieces[1]}/{pieces[2]}"


def clone_url(repo: str) -> str:
    """The git URL for a repo URL."""
    return repo + ".git"


def check_trigger(text: str) -> str:
    """The app trigger: push (the default) or manual."""
    text = text.strip().lower()
    if text in {"", "push"}:
        return "push"
    if text == "manual":
        return "manual"
    raise _bad_trigger(text)


def _bad_trigger(text: str) -> NoReturn:
    raise YardError(
        400,
        "bad_trigger",
        "trigger must be push or manual",
        context={"trigger": text},
        help_text="push deploys when the repo moves. manual deploys only on yard app deploy.",
        example="push",
    )


def app_container(name: str) -> str:
    """Compose names the one app container `<project>-app-1`."""
    return f"{name}-app-1"


def deploy_container(name: str) -> str:
    """The apply container of the last deploy. Its log is the deploy log."""
    return f"deploy-{name}"


def default_site(hosts: list[str], dial: str, window: str = "10s") -> str:
    """The default Caddy site: every host to one container. Caddy holds requests for `window` during a swap."""
    return (
        f"{', '.join(hosts)} {{\n"
        f"\treverse_proxy {dial} {{\n"
        f"\t\tlb_try_duration {window}\n"
        "\t\tlb_try_interval 250ms\n"
        "\t}\n"
        "}\n"
    )


def check_domain(domain: str) -> None:
    if not domain:
        return
    if ".." in domain or not _DOMAIN.fullmatch(domain):
        raise YardError(
            400,
            "bad_domain",
            "domain must be a hostname",
            context={"domain": domain},
            help_text="Use a lowercase hostname.",
            example="heldso.example.com",
        )


def check_build_path(text: str) -> str:
    """A repo-relative build path: a compose file or a Containerfile."""
    value = text.strip()
    if (
        not value
        or len(value) > 200
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or ".." in value.split("/")
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
    ):
        raise YardError(
            400,
            "bad_path",
            "path must be a file inside the repo",
            context={"path": text},
            help_text="A relative path like Containerfile or ops/compose.yml. No leading slash, no .., no spaces.",
            example="ops/Containerfile",
        )
    return value


# The Containerfiles Yard writes. `runs yard:<name>` names one of these.
TEMPLATES = ("python-uv", "python-pip", "go", "node-npm", "http-nu")

_TEMPLATE = re.compile(r"yard:[a-z0-9-]+$")


def parse_runs(text: str) -> tuple[str, str]:
    """Classify a `runs` value. Returns (kind, normalized).

    Kinds: auto (unset), template (yard:<name>), url (https), image, path.
    """
    value = text.strip()
    if value in {"", "auto"}:
        return "auto", ""
    if value.startswith("yard:"):
        if not _TEMPLATE.fullmatch(value):
            raise _bad_runs(value, f"template names are lowercase: {', '.join(f'yard:{t}' for t in TEMPLATES)}")
        return "template", value
    if value.startswith("https://"):
        if len(value) > 500:
            raise _bad_runs(value, "the URL is over 500 characters")
        return "url", value
    if value.startswith("http://"):
        raise _bad_runs(value, "files are fetched over https")
    if "://" in value:
        raise _bad_runs(value, "files are fetched over https")
    if ":" in value or "@" in value:
        return "image", check_image(value)
    return "path", check_build_path(value)


def _bad_runs(value: str, message: str) -> YardError:
    return YardError(
        400,
        "bad_runs",
        f"runs {value}: {message}",
        context={"runs": value},
        help_text="auto, a path in the repo, yard:<template>, an image name:tag, or an https URL.",
    )


def name_ok(name: str) -> bool:
    if not name or len(name) > 128:
        return False
    for index, char in enumerate(name):
        if char == "_" or "A" <= char <= "Z" or "a" <= char <= "z":
            continue
        if index and "0" <= char <= "9":
            continue
        return False
    return True


def check_env(env: Mapping[str, str | None]) -> None:
    for key in env:
        if not name_ok(key):
            raise YardError(
                400,
                "bad_key",
                f"bad env key {key}",
                context={"key": key},
                help_text="A key uses letters, digits, and _",
                example="API_TOKEN",
            )


def image_sha(image: str) -> str:
    """The image tag, with a template suffix dropped: localhost/x:sha12-tpl8 -> sha12."""
    if ":" not in image:
        return ""
    head, _, tail = image.rpartition(":")
    if not head or not tail:
        return ""
    base, dash, suffix = tail.rpartition("-")
    if dash and head.startswith("localhost/") and re.fullmatch(r"[0-9a-f]{8}", suffix):
        return base
    return tail


def short_sha(sha: str) -> str:
    """The image tag. Twelve chars, the docker convention."""
    return sha[:12]


def parse_env(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        trimmed = trimmed.removeprefix("export ")
        key, sep, value = trimmed.partition("=")
        key = key.strip()
        if not sep or not name_ok(key):
            raise _env_error(f"line {number} must be NAME=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError) as exc:
                raise _env_error(f"line {number} has a broken quoted value") from exc
            if not isinstance(parsed, str):
                raise _env_error(f"line {number} has a broken quoted value")
            value = parsed
        if "\r" in value or "\n" in value:
            raise _env_error(f"line {number}: a value stays on one line")
        if key in found:
            raise _env_error(f"line {number} repeats {key}")
        found[key] = value
    return found


def format_env(env: dict[str, str]) -> str:
    """KEY=value lines that parse_env reads back. A value outside the plain set is a JSON string."""
    lines: list[str] = []
    for key in sorted(env):
        value = env[key]
        # Compose and parse_env both read a double-quoted value with backslash escapes.
        shown = value if value and _PLAIN_VALUE.fullmatch(value) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}={shown}\n")
    return "".join(lines)


def format_bytes(size: int) -> str:
    value = float(max(size, 0))
    units = ["B", "kB", "MB", "GB", "TB", "PB"]
    unit = units[0]
    for name in units:
        unit = name
        if value < 1000 or name == units[-1]:
            break
        value /= 1000
    if unit == "B":
        return f"{int(value)}B"
    return f"{value:.4g}{unit}"


def _env_error(message: str) -> YardError:
    return YardError(
        400,
        "bad_key",
        message,
        help_text="A line is NAME=value. A # line is a comment.",
        example="DOMAIN=heldso.example.com",
    )


def now() -> str:
    """The API stamp: 2026-10-05T12:00:00Z, with fractions only when needed."""
    stamp = datetime.now(UTC)
    if stamp.microsecond == 0:
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    frac = f"{stamp.microsecond:06d}".rstrip("0")
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + frac + "Z"


def parse(text: str) -> datetime | None:
    """Our stamp or a podman stamp. Naive is UTC. Unparseable is None."""
    if not text:
        return None
    head = text.split("|", 1)[0]
    if head.endswith("Z"):
        head = head[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(head)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def stamp(moment: datetime) -> str:
    """A datetime as the API stamp."""
    moment = moment.astimezone(UTC)
    base = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond == 0:
        return base + "Z"
    return base + "." + f"{moment.microsecond:06d}".rstrip("0") + "Z"


def ago(when: str, now: datetime) -> str:
    """A stamp as an age: now, 30s, 5m, 2h, 3d. Empty for a missing or old stamp."""
    moment = parse(when)
    if moment is None or moment.year < 2000:
        return ""
    return age((now - moment).total_seconds())


def age(seconds: float) -> str:
    """A span since a moment: now, 30s, 5m, 2h, 3d."""
    count = int(seconds)
    if count < 5:
        return "now"
    if count < 60:
        return f"{count}s"
    minutes, _seconds = divmod(count, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, _minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h"
    days, _hours = divmod(hours, 24)
    return f"{days}d"


def duration(delta: timedelta) -> str:
    """A span: 350ms, 4.5s, 2m0s, 1h2m3s."""
    ms = round(delta.total_seconds() * 1000)
    if ms < 1000:
        return f"{ms}ms"
    seconds, frac = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes}m{seconds}s"
    if minutes:
        return f"{minutes}m{seconds}s"
    if frac:
        return f"{seconds}.{frac:03d}".rstrip("0") + "s"
    return f"{seconds}s"


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """A tmp file in the same directory, chmod, then a replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(path)


def ensure_file(path: Path, mode: int = 0o600) -> None:
    """An empty private file, or just the mode on an existing one."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        path.chmod(mode)
    else:
        atomic_write(path, "", mode)


_IMAGE_REPO = re.compile(r"[a-z0-9._-]+")
_IMAGE_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_IMAGE_DIGEST = re.compile(r"[0-9a-f]{64}")


def check_image(text: str) -> str:
    """An image ref: [registry/]repo[:tag][@sha256:<64 hex>]. A tag or a digest is required."""
    ref = text.strip()
    bad = YardError(
        400,
        "bad_image",
        "an image is [registry/]name[:tag][@sha256:<64 hex>]",
        context={"image": text},
        help_text="Name a tag: postgres:17 follows every 17.x release.",
        example="postgres:17",
    )
    name, sep, digest = ref.partition("@")
    if sep and not _IMAGE_DIGEST.fullmatch(digest.removeprefix("sha256:")):
        raise bad
    if len(name) > 255 or "://" in name:
        raise bad
    last = name.rsplit("/", 1)[-1]
    repo, colon, tag = last.rpartition(":")
    if colon and not _IMAGE_TAG.fullmatch(tag):
        raise bad
    base = name[: -len(last)] + (repo if colon else last)
    if not all(_IMAGE_REPO.fullmatch(part) for part in base.split("/")):
        raise bad
    if not colon and not sep:
        raise bad
    return ref
