"""Git forges: Cursor Origin and GitHub Apps. One app key, one installation per account, one token per installation.

state.json is the truth. The files are read on every access, so a setup run while
the server is up takes effect without a restart. Responses never echo a key, a
secret, or a token back out.
"""

import asyncio
import base64
import hashlib
import hmac
import json  # stdlib: JWT signing bytes and the sorted state file need a stable byte format.
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from stario import json as stario_json

from app.shared import YardError, atomic_write
from app.shared import parse as _parse_stamp

_STATE = "state.json"
_KEY = "key.pem"


class Forge:
    """One forge app for this host. Subclasses name the API and sign the JWT."""

    name = "forge"
    host = ""
    api = ""
    # The repo_permission help text for a create; subclasses set it.
    create_help = ""

    def __init__(self, dir: Path, api: str = "") -> None:
        self.dir = dir
        self.api = (api or self.api).rstrip("/")
        self.last_delivery = ""
        self.last_check = ""
        self._tokens: dict[str, tuple[str, float]] = {}

    def _state(self) -> dict:
        try:
            data = json.loads((self.dir / _STATE).read_text())
        except OSError, json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def app_id(self) -> str:
        return str(self._state().get("app_id") or "")

    @property
    def installations(self) -> list[dict]:
        """Every installation of the app: [{"id", "account", "selection"}].

        A legacy state with installation_id at the top level reads as one
        installation; the next save writes the list and drops the old keys.
        """
        state = self._state()
        found = state.get("installations")
        if isinstance(found, list):
            return [entry for entry in found if isinstance(entry, dict)]
        if state.get("installation_id"):
            return [
                {
                    "id": str(state.get("installation_id") or ""),
                    "account": str(state.get("account") or ""),
                    "selection": str(state.get("selection") or ""),
                }
            ]
        return []

    def save(self, drop: Iterable[str] = (), **fields: str | list) -> None:
        """Merge fields into state.json. The write is atomic."""
        state = {**self._state(), **{key: value for key, value in fields.items() if value or isinstance(value, list)}}
        for key in drop:
            state.pop(key, None)
        atomic_write(self.dir / _STATE, json.dumps(state, indent=2, sort_keys=True) + "\n")

    def connected(self) -> bool:
        return (self.dir / _KEY).is_file() and bool(self.app_id) and bool(self.installations)

    def installation_for(self, repo: str) -> dict | None:
        """The installation whose account owns the repo's owner, case-insensitively.

        An entry with no account is a legacy install from before accounts were
        saved; it owns every repo so nothing regresses.
        """
        owner = urlsplit(repo).path.strip("/").split("/", 1)[0]
        for installation in self.installations:
            account = str(installation.get("account") or "")
            if not account or account.lower() == owner.lower():
                return installation
        return None

    def selection(self, installation: dict) -> str:
        """all or selected. Subclasses read their own field."""
        return ""

    def account_type(self, installation: dict) -> str:
        """User or Organization on GitHub. Subclasses read their own field."""
        return ""

    def settings_url(self, installation: dict) -> str:
        """The page where the owner changes the repo selection; "" when the forge has none."""
        return ""

    def summary(self) -> dict:
        """What the API reports. Never a key, a secret, or a token."""
        return {
            "name": self.name,
            "host": self.host,
            "connected": self.connected(),
            "app_id": self.app_id,
            "installations": [
                {
                    "id": str(entry.get("id") or ""),
                    "account": str(entry.get("account") or ""),
                    "selection": str(entry.get("selection") or ""),
                    "settings_url": self.settings_url(entry),
                }
                for entry in self.installations
            ],
            "last_delivery": self.last_delivery,
            "last_check": self.last_check,
        }

    def app_jwt(self) -> str:
        """A signed JWT the forge checks against the app key. Subclasses implement it."""
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        return {}

    async def _call(self, method: str, path: str, bearer: str, body: dict | None = None) -> object:
        return await asyncio.to_thread(self._request, method, path, bearer, body)

    def _request(self, method: str, path: str, bearer: str, body: dict | None) -> object:
        data = None if body is None else stario_json.dumps(body).encode()
        request = urllib.request.Request(
            self.api + path,
            data=data,
            method=method,
            headers={
                "authorization": f"Bearer {bearer}",
                "content-type": "application/json",
                **self._headers(),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()[:400]
            raise YardError(
                502,
                self.name,
                f"{self.name} {method} {path}: {exc.code} {detail}".rstrip(),
                context={"status": exc.code},
            ) from exc
        except urllib.error.URLError as exc:
            raise YardError(502, self.name, f"{self.name} {method} {path}: {exc.reason}") from exc
        try:
            return stario_json.loads(text) if text.strip() else {}
        except ValueError:
            return {}

    async def get_app(self) -> dict:
        body = await self._call("GET", "/app", self.app_jwt())
        return body if isinstance(body, dict) else {}

    async def find_installations(self) -> list[dict]:
        """Every installation of the app, saved to state.json. Returns the saved entries."""
        body = await self._call("GET", "/app/installations?per_page=100", self.app_jwt())
        found = body.get("installations") if isinstance(body, dict) else body
        installations = [
            {
                "id": str(item.get("id") or ""),
                "account": _account(item),
                "selection": self.selection(item),
                "type": self.account_type(item),
            }
            for item in (found if isinstance(found, list) else [])
            if isinstance(item, dict)
        ]
        self.save(installations=installations, drop=("installation_id", "account", "selection"))
        return installations

    async def token(self, repo: str = "") -> str:
        """An installation access token for the repo's owner, or the first installation.

        Cached per installation until a minute before it expires. A repo no
        installation owns gets "": git asks for credentials only when the
        server demands them, so a public repo still clones.
        """
        installation = self.installation_for(repo) if repo else next(iter(self.installations), None)
        if installation is None:
            if repo:
                return ""
            raise YardError(
                502,
                self.name,
                f"{self.name} has no installation",
                help_text=f"Run yard forge setup {self.name}.",
            )
        installation = str(installation.get("id") or "")
        cached = self._tokens.get(installation)
        if cached and cached[1] - 60 > time.time():
            return cached[0]
        body = await self._call("POST", f"/app/installations/{installation}/access_tokens", self.app_jwt(), {})
        data = body if isinstance(body, dict) else {}
        token = str(data.get("token") or "")
        if not token:
            raise YardError(502, self.name, f"{self.name} returned no token")
        moment = _parse_stamp(str(data.get("expiresAt") or data.get("expires_at") or ""))
        self._tokens[installation] = (token, moment.timestamp() if moment else 0.0)
        return token

    def _installation(self, owner: str) -> dict:
        installation = self.installation_for(f"https://{self.host}/{owner}/x")
        if installation is None:
            raise YardError(
                404,
                "no_installation",
                f"the Yard app is not installed on {owner}",
                help_text=f"Install the Yard app on {owner}. yard forge setup {self.name} prints the link.",
            )
        return installation

    async def create_repo(self, owner: str, name: str) -> str:
        """A new private repo under owner. Returns its https URL.

        The installation must cover all repositories: a "selected" one cannot see
        the repo it makes, so the first push would wait on a grant by hand.
        """
        await self.find_installations()
        installation = self._installation(owner)
        if installation.get("selection") != "all":
            raise YardError(
                403,
                "repo_permission",
                f"the Yard {self.name} app on {owner} covers selected repos only",
                help_text=(
                    f"Install the app on {owner} for all repositories."
                    " A separate account for generated apps keeps your other repos out."
                ),
            )
        try:
            full = await self._make_repo(installation, owner, name)
        except YardError as exc:
            status = exc.context.get("status")
            if status in (409, 422):
                raise YardError(
                    409,
                    "repo_exists",
                    f"{owner}/{name} exists",
                    help_text=(
                        "Pick another name, or use the repo:"
                        f" git remote add origin https://{self.host}/{owner}/{name}.git"
                    ),
                ) from exc
            if status in (403, 404):
                raise YardError(
                    403,
                    "repo_permission",
                    f"the Yard {self.name} app cannot create repos",
                    help_text=self.create_help,
                ) from exc
            raise
        return f"https://{self.host}/{full}"

    async def _make_repo(self, installation: dict, owner: str, name: str) -> str:
        """POST the repo; return its owner/name. Subclasses implement it."""
        raise NotImplementedError

    async def repo_token(self, repo: str) -> tuple[str, str]:
        """(token, expires_at) for git on this one repo: clone, fetch, and push. Only GitHub makes one."""
        raise YardError(
            501,
            "token_unsupported",
            f"{self.name} cannot make a repo token",
            help_text="Use your own git credentials.",
        )


class Origin(Forge):
    """api.cursor.com. Yard makes the Ed25519 key; EdDSA signs the JWT."""

    name = "origin"
    host = "origin.cursor.com"
    api = "https://api.cursor.com/v1/origin"
    create_help = (
        "Approve the namespace:repositories:create scope for the Yard Origin app:"
        " run yard forge setup origin, or open the install link with include_granted_scopes=true."
    )

    def _key(self) -> Ed25519PrivateKey:
        path = self.dir / _KEY
        if not path.is_file():
            private = Ed25519PrivateKey.generate()
            atomic_write(
                path,
                private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
            )
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise YardError(
                502,
                "origin",
                "key.pem is not an Ed25519 key",
                help_text="Delete the bad key.pem and run yard forge setup origin.",
            )
        return key

    def summary(self) -> dict:
        summary = super().summary()
        # The public key is safe to show; first read creates the keypair.
        summary["public_key"] = self.public_key()
        return summary

    def public_key(self) -> str:
        """The PEM public key. Creates the keypair on first call."""
        return (
            self._key()
            .public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )

    def _jwt(self, app_id: str) -> str:
        now = int(time.time())
        header = _b64(json.dumps({"alg": "EdDSA", "kid": app_id, "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64(
            json.dumps(
                {"iss": app_id, "aud": "origin-apps", "iat": now, "exp": now + 600},
                separators=(",", ":"),
            ).encode()
        )
        sig = _b64(self._key().sign(f"{header}.{claims}".encode()))
        return f"{header}.{claims}.{sig}"

    def app_jwt(self) -> str:
        return self._jwt(self.app_id)

    def selection(self, installation: dict) -> str:
        return str(installation.get("repoSelectionMode") or installation.get("repo_selection_mode") or "")

    async def connect(self, app_id: str) -> dict:
        """Save the app id only when GET /app answers for it and the existing key."""
        info = await self.get_app_for(app_id)
        self.save(app_id=app_id, drop=("installations", "installation_id", "account", "selection"))
        self._tokens = {}
        return info

    async def get_app_for(self, app_id: str) -> dict:
        body = await self._call("GET", "/app", self._jwt(app_id))
        return body if isinstance(body, dict) else {}

    async def _make_repo(self, installation: dict, owner: str, name: str) -> str:
        body = await self._call(
            "POST",
            f"/repos/{owner}",
            await self.token(f"https://{self.host}/{owner}/{name}"),
            {"name": name, "visibility": "private"},
        )
        data = body if isinstance(body, dict) else {}
        return str(data.get("fullName") or f"{owner}/{name}")


class GitHub(Forge):
    """api.github.com. The user supplies the RSA key and the webhook secret."""

    name = "github"
    host = "github.com"
    api = "https://api.github.com"
    create_help = (
        "In the app's GitHub settings, set Repository creation to Read and write,"
        " then accept the new permission on the installation."
    )

    def _headers(self) -> dict[str, str]:
        # GitHub refuses a request with no User-Agent.
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "yard",
        }

    def _key(self) -> rsa.RSAPrivateKey:
        try:
            key = serialization.load_pem_private_key((self.dir / _KEY).read_bytes(), password=None)
        except (OSError, ValueError) as exc:
            raise YardError(
                502,
                "github",
                "key.pem is missing or does not parse",
                help_text="Run yard forge setup github.",
            ) from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise YardError(
                502,
                "github",
                "key.pem is not an RSA key",
                help_text="Run yard forge setup github.",
            )
        return key

    def _jwt(self, app_id: str, key: rsa.RSAPrivateKey) -> str:
        now = int(time.time())
        header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64(
            json.dumps(
                {"iat": now - 60, "exp": now + 540, "iss": int(app_id)},
                separators=(",", ":"),
            ).encode()
        )
        sig = _b64(key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()))
        return f"{header}.{claims}.{sig}"

    def app_jwt(self) -> str:
        app_id = self.app_id
        if not app_id.isdigit():
            raise YardError(
                502,
                "github",
                "state.json has a bad app id",
                help_text="Run yard forge setup github.",
            )
        return self._jwt(app_id, self._key())

    def selection(self, installation: dict) -> str:
        return str(installation.get("repository_selection") or "")

    def account_type(self, installation: dict) -> str:
        account = installation.get("account")
        return str(account.get("type") or "") if isinstance(account, dict) else ""

    def settings_url(self, installation: dict) -> str:
        ident, account = str(installation.get("id") or ""), str(installation.get("account") or "")
        if not ident:
            return ""
        if installation.get("type") == "Organization" and account:
            return f"https://github.com/organizations/{account}/settings/installations/{ident}"
        return f"https://github.com/settings/installations/{ident}"

    def summary(self) -> dict:
        summary = super().summary()
        # The slug names the public app page; it is not a secret.
        summary["slug"] = str(self._state().get("slug") or "")
        return summary

    async def connect(self, app_id: str, private_key: str, webhook_secret: str) -> dict:
        """Verify the new key against GET /app before anything is saved."""
        if not app_id.isdigit():
            raise YardError(400, "bad_app", "the app id is digits", help_text="The App ID on the app page.")
        if len(private_key.encode()) > 16 * 1024:
            raise YardError(
                400, "bad_key", "the private key is too large", help_text="Paste the key the app page gives."
            )
        try:
            key = serialization.load_pem_private_key(private_key.encode(), password=None)
        except ValueError as exc:
            raise YardError(
                400, "bad_key", "the private key does not parse", help_text="Paste the whole PEM, BEGIN to END."
            ) from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise YardError(
                400, "bad_key", "the private key is not an RSA key", help_text="The app page gives an RSA key."
            )
        if len(webhook_secret) < 32:
            raise YardError(
                400,
                "bad_secret",
                "the webhook secret is too short",
                help_text="At least 32 characters, from the app page.",
            )
        body = await self._call("GET", "/app", self._jwt(app_id, key))
        info = body if isinstance(body, dict) else {}
        atomic_write(self.dir / _KEY, private_key if private_key.endswith("\n") else private_key + "\n")
        self.save(
            app_id=app_id,
            slug=str(info.get("slug") or ""),
            webhook_secret=webhook_secret,
            drop=("installations", "installation_id", "account", "selection"),
        )
        self._tokens = {}
        return info

    def verify(self, body: bytes, header: str) -> bool:
        """X-Hub-Signature-256 over the raw body. No secret means no signature is good."""
        secret = str(self._state().get("webhook_secret") or "")
        if not secret:
            return False
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={digest}", header)

    async def _make_repo(self, installation: dict, owner: str, name: str) -> str:
        """POST /user/repos or /orgs/<owner>/repos; return the new owner/name."""
        path = "/user/repos" if installation.get("type") == "User" else f"/orgs/{owner}/repos"
        body = await self._call(
            "POST",
            path,
            await self.token(f"https://{self.host}/{owner}/{name}"),
            {"name": name, "private": True, "auto_init": False},
        )
        data = body if isinstance(body, dict) else {}
        return str(data.get("full_name") or f"{owner}/{name}")

    async def repo_token(self, repo: str) -> tuple[str, str]:
        """An installation token narrowed to contents write on one repo. Never cached."""
        owner, _, name = urlsplit(repo).path.strip("/").removesuffix(".git").partition("/")
        installation = self._installation(owner)
        try:
            body = await self._call(
                "POST",
                f"/app/installations/{installation.get('id')}/access_tokens",
                self.app_jwt(),
                {"repositories": [name], "permissions": {"contents": "write"}},
            )
        except YardError as exc:
            # GitHub answers 422 for a repo the installation cannot see or a permission it lacks.
            if exc.context.get("status") not in (403, 404, 422):
                raise
            raise YardError(
                403,
                "token_permission",
                f"the Yard github app cannot make a token for {owner}/{name}",
                help_text=(
                    "In the app's GitHub settings, set Contents to Read and write,"
                    f" accept the new permission, and install the app on {owner} for all repositories."
                ),
            ) from exc
        data = body if isinstance(body, dict) else {}
        token = str(data.get("token") or "")
        if not token:
            raise YardError(502, self.name, f"{self.name} returned no token")
        return token, str(data.get("expires_at") or "")


def _account(installation: dict) -> str:
    """GitHub answers account.login; Origin answers target.slug."""
    account = installation.get("account")
    if isinstance(account, dict) and account.get("login"):
        return str(account["login"])
    target = installation.get("target")
    if isinstance(target, dict):
        return str(target.get("slug") or "")
    return ""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
