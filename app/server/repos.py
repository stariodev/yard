"""Repos on a forge: `yard repo new` and an app that makes apps land here."""

from app.server import journal
from app.server.forges import Forge
from app.server.yard import Yard
from app.shared import YardError, check_app_name, check_repo


async def create_repo(yard: Yard, name: str, owner: str = "", forge_name: str = "", *, actor: str) -> dict:
    """A private repo. The one connected forge and its one installation are the defaults."""
    check_app_name(name)
    forge = _connected_forge(yard, forge_name)
    if not owner:
        accounts = [str(entry.get("account") or "") for entry in forge.installations]
        if len(accounts) != 1 or not accounts[0]:
            raise YardError(
                400,
                "owner_required",
                "name the owner account",
                help_text=f"The installations: {', '.join(accounts) or 'none'}.",
            )
        owner = accounts[0]
    repo = await forge.create_repo(owner, name)
    journal.send(yard.config, "repo.create", f"repo {repo}", forge=forge.name, repo=repo, actor=actor)
    return {"repo": repo}


async def repo_token(yard: Yard, repo: str, *, actor: str) -> dict:
    """A token that can clone, fetch, and push one repo for an hour. The journal names the repo, never the token."""
    repo = check_repo(repo)
    forge = yard.forge(repo)
    forge.require_connected()
    token, expires_at = await forge.repo_token(repo)
    journal.send(yard.config, "repo.token", f"token for {repo}", forge=forge.name, repo=repo, actor=actor)
    # Plain git takes it as the password for the user x-access-token; no gh is needed.
    return {"username": "x-access-token", "token": token, "expires_at": expires_at}


def _connected_forge(yard: Yard, name: str) -> Forge:
    if name:
        forge = yard.forge_named(name)
        forge.require_connected()
        return forge
    connected = [forge for forge in yard.forges.values() if forge.connected()]
    if not connected:
        raise YardError(502, "forge", "no forge is connected", help_text="Run yard forge setup github.")
    if len(connected) > 1:
        names = " or ".join(forge.name for forge in connected)
        raise YardError(400, "forge_required", "name the forge", help_text=f"Pass forge: {names}.")
    return connected[0]
