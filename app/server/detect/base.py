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
    template: str = ""  # first 8 hex of the rendered text's sha256


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
    """A containerfile plan: the rendered text and its 8-hex template stamp."""
    generated = render(answer, reason, name, template, **values)
    return Plan("containerfile", answer, reason, generated, template_hash(generated))


def template_hash(text: str) -> str:
    """The template stamp: first 8 hex of the written file's sha256."""
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"
