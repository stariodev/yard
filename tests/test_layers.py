"""The import layers, checked with ast. api -> services -> yard/store/adapters -> shared."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# One external tool each, plus the store. They never know the Yard object or HTTP.
ADAPTERS = {"caddy", "compose", "detect", "forges", "git", "journal", "podman", "sources", "store"}

# Domain work. They call adapters and the store through the Yard object; never HTTP.
SERVICES = {"apps", "doctor", "logfilter", "repos", "sites", "status"}

# main is the composition root: the only file that may import the api.
COMPOSITION = {"main", "api"}

# The dependencies and runtime state; services import it, adapters never do.
STATE = {"yard"}


def _imports(source: str) -> set[str]:
    """Every dotted name a file imports: `from x import y` counts as x.y too."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _deps(package: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted((ROOT / "app" / package).rglob("*.py")):
        out[path.relative_to(ROOT).as_posix()] = _imports(path.read_text())
    single = ROOT / "app" / f"{package}.py"
    if single.is_file():
        out[single.relative_to(ROOT).as_posix()] = _imports(single.read_text())
    return out


def _module(file: str) -> str:
    """The layer name: the module under app/server, or its package (detect/python.py is detect)."""
    parts = Path(file).relative_to("app/server").parts
    return Path(parts[0]).stem


def _adapter_violations(deps: set[str]) -> set[str]:
    return {
        name
        for name in deps
        if name.startswith("app.server.api")
        or name.startswith("app.server.yard")
        or name.removeprefix("app.server.").split(".")[0] in SERVICES
    }


def test_shared_imports_only_the_stdlib():
    for file, deps in _deps("shared").items():
        bad = {name for name in deps if name == "app" or name.startswith("app.") or name in ("stario", "cryptography")}
        assert not bad, f"{file}: {bad}"


def test_cli_never_imports_the_server():
    for file, deps in _deps("cli").items():
        bad = {name for name in deps if name.startswith("app.server") or name in ("stario", "cryptography")}
        assert not bad, f"{file}: {bad}"


def test_server_never_imports_the_cli():
    for file, deps in _deps("server").items():
        bad = {name for name in deps if name.startswith("app.cli")}
        assert not bad, f"{file}: {bad}"


def test_every_server_module_has_one_layer():
    layers = [ADAPTERS, SERVICES, COMPOSITION, STATE]
    for file in _deps("server"):
        module = _module(file)
        if module == "__init__":
            continue
        count = sum(module in layer for layer in layers)
        assert count == 1, f"{file}: in {count} layers"


def test_adapters_never_import_services_yard_or_api():
    for file, deps in _deps("server").items():
        if _module(file) not in ADAPTERS:
            continue
        bad = _adapter_violations(deps)
        assert not bad, f"{file}: {bad}"


def test_only_main_imports_the_api():
    for file, deps in _deps("server").items():
        if _module(file) in COMPOSITION:
            continue
        bad = {name for name in deps if name.startswith("app.server.api")}
        assert not bad, f"{file}: {bad}"


def test_the_checker_flags_from_package_import_style():
    """`from app.server import api` must be as visible as `import app.server.api`."""
    source = "from app.server import api, compose\nfrom app.cli import main\n"
    deps = _imports(source)
    assert "app.server.api" in deps
    assert "app.server.compose" in deps
    assert _adapter_violations(deps)
    cli_bad = {name for name in deps if name.startswith("app.server") or name in ("stario", "cryptography")}
    assert cli_bad
