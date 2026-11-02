"""What an app runs when its record does not say: a repo compose, a repo Containerfile, or a written one.

A detector is one module in this package for one language or framework. It exposes:

- LANGUAGE: the language it is an opinionated kind of (several detectors may share one).
- MARKERS: repo files that prove the language is present (first match names the language).
- detect(repo, name) -> Plan | None: None means "not mine"; YardError means "mine but broken".

A detector imports Repo, Plan, pin, and plan_template from detect.base.
A generated Containerfile lives in templates/ next to this file and renders through render.
Every FROM and COPY --from in a template is pinned to an index digest in bases.json.
A detector must refuse (YardError or None) rather than guess an install or entrypoint.

A PR adding a detector adds the module, its template, a bases.json pin, an entry in
DETECTORS (specific before generic), and tests under tests/server/.
"""

import re
from pathlib import Path

from app.server import git
from app.server.compose import COMPOSE_NAMES, DOCKERFILES
from app.server.detect import go, http_nu, node, python, stario
from app.server.detect.base import Plan, Repo, _join
from app.shared import YardError

DETECTORS = (stario, python, go, node, http_nu)

__all__ = ["DETECTORS", "Plan", "Repo", "classify", "language", "plan_for", "resolve"]

_SERVICES = re.compile(r"^services:\s*(#.*)?$", re.M)
_INSTRUCTION = re.compile(r"^(FROM|ARG)\s", re.I)
BUILD_LINE = re.compile(r"^\s+build:", re.M)


def classify(text: str) -> str:
    """A file's kind by its content: compose names services, a Containerfile starts FROM or ARG."""
    compose = bool(_SERVICES.search(text))
    containerfile = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        containerfile = bool(_INSTRUCTION.match(stripped))
        break
    if compose and not containerfile:
        return "compose"
    if containerfile and not compose:
        return "containerfile"
    found = []
    if compose:
        found.append("a services: block")
    if containerfile:
        found.append("a FROM or ARG instruction")
    raise YardError(
        502,
        "detect",
        f"the file has {', '.join(found) or 'no services: block and no FROM'}",
        help_text="A compose file starts services:; a Containerfile starts FROM.",
    )


def _languages(repo: Repo) -> dict[str, list[str]]:
    """The languages present in the checkout, language name -> marker files."""
    found: dict[str, list[str]] = {}
    for mod in DETECTORS:
        marker = next((m for m in mod.MARKERS if repo.exists(m)), "")
        if marker:
            found.setdefault(mod.LANGUAGE, []).append(marker)
    return found


def resolve(checkout: Path, name: str = "") -> Plan:
    """What the checked-out repo runs. A written Containerfile comes back in generated."""
    for candidate in COMPOSE_NAMES:
        if (checkout / candidate).is_file():
            return Plan("compose", candidate, f"found {candidate}")
    for candidate in DOCKERFILES:
        if (checkout / candidate).is_file():
            return Plan("containerfile", candidate, f"found {candidate}")
    return language(checkout, name)


def plan_for(checkout: Path, name: str, runs: str) -> Plan:
    """The deploy plan for a runs value on a repo app: yard:<t> or auto. Paths classify by content."""
    if runs.startswith("yard:"):
        plan = language(checkout, name)
        if plan.answer != runs:
            raise YardError(
                502,
                "detect",
                f"{runs} does not fit this repo: Yard detects {plan.answer} ({plan.reason})",
                help_text=f"Run yard app set --runs {plan.answer} to accept it, or add a Containerfile.",
            )
        return plan
    if runs:
        path = (checkout / runs).resolve()
        root = checkout.resolve()
        if root not in path.parents and path != root:
            raise YardError(502, "detect", f"{runs} is outside the checkout")
        if not path.is_file():
            raise YardError(
                502,
                "detect",
                f"{runs} is not a file in the checkout",
                help_text="Add the file and push, or run yard app set --runs auto.",
            )
        return Plan(classify(path.read_text()), runs, "")
    return resolve(checkout, name)


def language(checkout: Path, name: str = "") -> Plan:
    """Which bundled Containerfile the repo's language gets. Repo compose and Containerfile files are not read."""
    repo = Repo(checkout)
    found = _languages(repo)
    if len(found) > 1:
        markers = [markers[0] for _lang, markers in found.items()]
        raise YardError(
            502,
            "detect",
            f"found {_join(markers)}; Yard will not guess",
            help_text="Add a Containerfile, or run yard app set --runs PATH.",
        )
    if not found:
        sha = git.head(checkout)[:12] or "the checkout"
        raise YardError(
            502,
            "detect",
            f"no compose file, Containerfile, or known language (Python, Go, Node, Nushell) in {sha}",
            help_text="Add a Containerfile: stario.dev/yard/what-runs#when-yard-refuses",
        )
    lang = next(iter(found))
    for mod in DETECTORS:
        if mod.LANGUAGE != lang:
            continue
        plan = mod.detect(repo, name)
        if plan is not None:
            return plan
    raise YardError(502, "detect", f"found {found[lang][0]} but no detector claims it")
