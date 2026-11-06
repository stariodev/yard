"""The shared spine of the detectors: the checkout handle and the plan."""

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TEMPLATES = _HERE / "templates"
BASES: dict[str, str] = json.loads((_HERE / "bases.json").read_text())
DOWNLOADS: dict[str, dict] = json.loads((_HERE / "downloads.json").read_text())


def _stamp() -> str:
    """The first 8 hex of a sha256 over every file of the detect package.

    A change to a detector, a template, or a pin moves the stamp, and each app on a
    yard:<template> deploys once.
    """
    digest = hashlib.sha256()
    for path in sorted(_HERE.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json", ".Containerfile"}:
            digest.update(path.relative_to(_HERE).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:8]


STAMP = _stamp()


class Repo:
    """A checkout path with small cached helpers."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._pyproject: dict | None = None

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def text(self, rel: str) -> str:
        path = self.root / rel
        return path.read_text() if path.is_file() else ""

    @property
    def pyproject(self) -> dict:
        if self._pyproject is None:
            try:
                self._pyproject = tomllib.loads(self.text("pyproject.toml"))
            except ValueError:
                self._pyproject = {}
        return self._pyproject


@dataclass(slots=True)
class Plan:
    """One answer to "what does the app run"."""

    kind: str  # "compose" or "containerfile"
    answer: str  # a repo-relative path, or "yard:<template>" for a written Containerfile
    reason: str  # human text: "found compose.yml", "stario: pyproject.toml declares stario"
    generated: str = ""  # the rendered Containerfile when answer is "yard:<name>"
    template: str = ""  # STAMP when answer is "yard:<name>"


def pin(ref: str) -> str:
    """image:tag@sha256:<index digest>, from the pinned base table."""
    return f"{ref}@{BASES[ref]}"


def _header(answer: str, reason: str, name: str) -> str:
    return (
        f"# Written by Yard for {answer} ({reason})."
        f" To change it, copy it into your repo: yard app cat containerfile --app {name or '<name>'} > Containerfile\n"
    )


def render(answer: str, reason: str, name: str, template: str = "", **values: str) -> str:
    text = (_TEMPLATES / f"{template or answer}.Containerfile").read_text()
    values["name"] = name or "<name>"
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return _header(answer, reason, name) + text


def plan_template(answer: str, reason: str, name: str, template: str = "", **values: str) -> Plan:
    """A containerfile plan: the rendered text and the template stamp."""
    return Plan("containerfile", answer, reason, render(answer, reason, name, template, **values), STAMP)


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"
