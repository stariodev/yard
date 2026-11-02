"""The git checkout in the current directory, and the app names Yard derives from it."""

import argparse
import re
import subprocess
from urllib.parse import urlsplit

from app.cli import client
from app.shared import FORGE_HOSTS


def git_out(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if proc.returncode:
        return ""
    return proc.stdout.strip()


def checkout() -> tuple[str, str, str]:
    """(top, https repo URL, branch) of this directory; each is "" when git does not know it."""
    top = git_out("rev-parse", "--show-toplevel")
    if not top:
        return "", "", ""
    return top, remote_repo(git_out("remote", "get-url", "origin")), git_out("branch", "--show-current")


def remote_repo(remote: str) -> str:
    """The https repo URL when the remote is on Origin or GitHub. Three spellings count."""
    remote = remote.strip()
    short = re.fullmatch(r"git@([^:/]+):(.+)", remote)
    if short:
        host, path = short.group(1), short.group(2)
    else:
        parts = urlsplit(remote)
        host, path = parts.hostname or "", parts.path.lstrip("/")
    if host not in FORGE_HOSTS:
        return ""
    if path.endswith(".git"):
        path = path[:-4]
    found = path.split("/")
    if len(found) != 2 or not all(found):
        return ""
    return f"https://{host}/{found[0]}/{found[1]}"


def source_name(kind: str, runs: str) -> str:
    """The app name for a repo-less app: the image's last repo segment, or the URL file's stem."""
    if kind == "image":
        return runs.split("@", 1)[0].rsplit(":", 1)[0].rsplit("/", 1)[-1]
    tail = runs.rsplit("/", 1)[-1]
    return tail.removesuffix(".yml").removesuffix(".yaml").removesuffix(".compose")


def slug(text: str) -> str:
    """An app-name slug from loose text: lowercase, digits, and hyphens."""
    name = re.sub(r"[^a-z0-9-]+", "-", text.lower())
    return re.sub(r"-+", "-", name).strip("-")[:63].strip("-")


def default_name(repo: str, ref: str) -> str:
    """repo name for main and master; otherwise <repo>-<branch> as an app name."""
    name = repo.split("/")[-1]
    if ref not in {"main", "master"}:
        name = f"{name}-{ref}"
    return slug(name)


def app_name(client_: client.Client, args: argparse.Namespace) -> str:
    """The app for this checkout. --app overrides. Every error mentions --app."""
    if args.app.strip():
        return str(args.app).strip()
    top, repo, branch = checkout()
    if not top:
        client.die(
            "not a git checkout here; pass --app NAME, or cd into the app's checkout. yard app ls lists the apps."
        )
    if not repo:
        client.die(
            "the origin remote is not on Origin or GitHub; pass --app NAME, or cd into the app's checkout."
            " yard app ls lists the apps."
        )
    if not branch:
        client.die("detached HEAD; pass --app NAME. yard app ls lists the apps.")
    rows = client.object_of(client.api(client_, "GET", "/api/v1/apps", None))
    found = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("repo") or "").lower() == repo.lower()
        and str(row.get("ref") or "") == branch
    ]
    if not found:
        client.die(
            f"no app follows {repo} {branch}.\n"
            "Create one here: yard app new   (or pass --app NAME; yard app ls lists the apps)"
        )
    if len(found) > 1:
        client.die(f"more than one app follows {repo} {branch}; pass --app NAME. yard app ls lists the apps.")
    return str(found[0]["name"])
