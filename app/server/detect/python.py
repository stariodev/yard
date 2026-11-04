"""The Python detectors: uv or pip and one of the known entrypoints."""

import re

from app.server.detect.base import Plan, Repo, pin, plan_template
from app.shared import YardError

LANGUAGE = "python"
MARKERS = ("pyproject.toml", "requirements.txt")

VERSIONS = ("3.12", "3.13", "3.14")
_DEFAULT = "3.14"

_REQUIRES_PY = re.compile(r">=\s*3\.(\d+)")
_DOT_VERSION = re.compile(r"^3\.(\d+)")


def detect(repo: Repo, name: str) -> Plan | None:
    if repo.exists("uv.lock"):
        return plan(repo, name, reason="python: uv.lock")
    if repo.exists("requirements.txt"):
        return plan(repo, name, reason="python: requirements.txt")
    if repo.exists("pyproject.toml"):
        raise YardError(
            502,
            "detect",
            "found pyproject.toml but no uv.lock or requirements.txt",
            help_text="Run uv lock and commit uv.lock, add requirements.txt, or add a Containerfile.",
        )
    return None


def plan(repo: Repo, name: str, *, reason: str) -> Plan:
    """Render the one Python Containerfile: uv or pip, the entrypoint, PYTHONUNBUFFERED + PORT."""
    if repo.exists("app/main.py"):
        cmd = '["python", "-m", "app.main"]'
    elif repo.exists("main.py"):
        cmd = '["python", "main.py"]'
    elif repo.exists("app.py"):
        cmd = '["python", "app.py"]'
    else:
        scripts = repo.pyproject.get("project", {}).get("scripts", {})
        if repo.exists("uv.lock") and repo.pyproject.get("build-system") and len(scripts) == 1:
            cmd = f'["{next(iter(scripts))}"]'
        else:
            raise YardError(
                502,
                "detect",
                f"found a {reason.split(':', 1)[0].strip().capitalize()} app but no app/main.py, main.py, or app.py",
                help_text="Add one of those files, or add a Containerfile.",
            )
    # One stage: the venv layer stays cached until the lock changes, so a code-only deploy
    # writes only the source layer. uv is bind-mounted and never lands in the image.
    if repo.exists("uv.lock"):
        answer = "yard:python-uv"
        # uv installs the project when tool.uv.package says so, else when there is a build-system.
        packaged = repo.pyproject.get("tool", {}).get("uv", {}).get("package", bool(repo.pyproject.get("build-system")))
        uv = f"--mount=type=bind,from={pin('ghcr.io/astral-sh/uv:0.13.5')},source=/uv,target=/bin/uv"
        install = {
            "env": " \\\n    UV_COMPILE_BYTECODE=1 \\\n    UV_LINK_MODE=copy \\\n    UV_PYTHON_DOWNLOADS=0",
            "deps": (
                f"RUN {uv} \\\n"
                "    --mount=type=cache,target=/root/.cache/uv \\\n"
                "    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \\\n"
                "    --mount=type=bind,source=uv.lock,target=uv.lock \\\n"
                "    uv sync --locked --no-install-project --no-dev"
            ),
            # Only a packaged project installs itself; this step runs after every code change.
            "project": (
                f"\nRUN {uv} \\\n    --mount=type=cache,target=/root/.cache/uv \\\n    uv sync --locked --no-dev\n"
                if packaged
                else ""
            ),
        }
    else:
        answer = "yard:python-pip"
        install = {
            "env": "",
            "deps": (
                "RUN --mount=type=cache,target=/root/.cache/pip \\\n"
                "    --mount=type=bind,source=requirements.txt,target=requirements.txt \\\n"
                "    python -m venv /workspace/.venv && \\\n"
                "    /workspace/.venv/bin/pip install -r requirements.txt"
            ),
            "project": "",
        }
    return plan_template(
        answer, reason, name, template="python", python=pin(f"python:{_version(repo)}-slim-trixie"), cmd=cmd, **install
    )


def _version(repo: Repo) -> str:
    """An exact .python-version must be in the set; >=3.N takes the smallest cover; default 3.14."""
    pinned = _DOT_VERSION.match(repo.text(".python-version").strip())
    if pinned:
        minor = f"3.{pinned.group(1)}"
        if minor not in VERSIONS:
            raise _version_error(minor)
        return minor
    requires = str(repo.pyproject.get("project", {}).get("requires-python") or "")
    found = _REQUIRES_PY.search(requires)
    if found:
        ask = int(found.group(1))
        fit = next((v for v in VERSIONS if int(v.split(".")[1]) >= ask), "")
        if not fit:
            raise _version_error(f"3.{ask}")
        return fit
    return _DEFAULT


def _version_error(ask: str) -> YardError:
    return YardError(
        502,
        "detect",
        f"Yard's python templates cover {', '.join(VERSIONS)}; this repo asks {ask}",
        help_text="Add a Containerfile.",
    )
