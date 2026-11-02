"""The Stario detector: a Python app whose pyproject declares stario."""

import re

from app.server.detect import python
from app.server.detect.base import Plan, Repo
from app.shared import YardError

LANGUAGE = "python"
MARKERS = ("pyproject.toml",)

_DEP_NAME = re.compile(r"[\[<>=!~;@ ]")


def detect(repo: Repo, name: str) -> Plan | None:
    if not repo.exists("pyproject.toml"):
        return None
    if "stario-create" not in repo.pyproject.get("tool", {}):
        deps = repo.pyproject.get("project", {}).get("dependencies", [])
        names = {_DEP_NAME.split(str(dep).strip().lower(), 1)[0] for dep in deps}
        if "stario" not in names:
            return None
    if not repo.exists("uv.lock"):
        raise YardError(
            502,
            "detect",
            "a Stario app needs uv.lock; run uv lock and commit it",
            help_text="uv lock && git add uv.lock && git commit && git push",
        )
    return python.plan(repo, name, reason="stario: pyproject.toml declares stario")
