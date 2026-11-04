"""detect.resolve and plan_for: which file runs the app, and the Containerfile Yard writes."""

from pathlib import Path

import pytest

from app.server import detect
from app.server.detect.base import BASES
from app.shared import YardError

PYPROJECT = '[project]\nname = "heldso"\ndependencies = ["stario>=5"]\n'


def _write(checkout: Path, name: str, text: str = "x") -> None:
    path = checkout / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_repo_compose_wins(tmp_path: Path):
    _write(tmp_path, "compose.yml")
    _write(tmp_path, "Dockerfile")
    resolved = detect.resolve(tmp_path, "heldso")
    assert (resolved.kind, resolved.answer, resolved.reason) == ("compose", "compose.yml", "found compose.yml")


def test_repo_containerfile_beats_language(tmp_path: Path):
    _write(tmp_path, "ops/Containerfile")
    _write(tmp_path, "package.json", "{}")
    resolved = detect.resolve(tmp_path, "heldso")
    assert (resolved.kind, resolved.answer) == ("containerfile", "ops/Containerfile")


def test_stario(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", PYPROJECT)
    _write(tmp_path, "uv.lock", "lock")
    _write(tmp_path, "app/main.py")
    resolved = detect.resolve(tmp_path, "heldso")
    assert (resolved.kind, resolved.answer) == ("containerfile", "yard:python-uv")
    assert resolved.reason == "stario: pyproject.toml declares stario"
    assert resolved.generated.startswith(
        "# Written by Yard for yard:python-uv (stario: pyproject.toml declares stario)."
    )
    assert len(resolved.template) == 8
    assert "STARIO_HOST" not in resolved.generated


@pytest.mark.parametrize(
    "files",
    [{"serve.nu": "{|req| 'hi'}\n"}, {"pyproject.toml": '[project]\nname="x"\n', "uv.lock": "l", "main.py": ""}],
)
def test_every_from_is_pinned(tmp_path: Path, files: dict[str, str]):
    """Every FROM, COPY --from, and --mount from= an image ref in a rendered template carries @sha256:."""
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    for line in detect.resolve(tmp_path, "heldso").generated.splitlines():
        if line.startswith("FROM "):
            assert "@sha256:" in line, line
        for prefix in ("COPY --from=", "--mount=type=bind,from="):
            if prefix in line:
                ref = line.split(prefix, 1)[1].split(" ", 1)[0].split(",", 1)[0]
                if "/" in ref or ":" in ref:  # an image ref, not a stage name
                    assert "@sha256:" in ref, line


def test_every_base_is_used(tmp_path: Path):
    """Every bases.json entry appears in at least one rendered template."""
    used = set()
    renders = [
        {"go.mod": "module x\n\ngo 1.26\n", "main.go": "package main\n"},
        {"go.mod": "module x\n\ngo 1.27\n", "main.go": "package main\n"},
        {"package.json": '{"scripts":{"start":"x"},"engines":{"node":"22"}}'},
        {"package.json": '{"scripts":{"start":"x"}}'},
        {"pyproject.toml": '[project]\nrequires-python=">=3.12"\n', "uv.lock": "l", "app/main.py": ""},
        {"pyproject.toml": '[project]\nrequires-python=">=3.13"\n', "uv.lock": "l", "app/main.py": ""},
        {"pyproject.toml": '[project]\nrequires-python=">=3.14"\n', "uv.lock": "l", "app/main.py": ""},
        {"serve.nu": "{|req| 'hi'}\n"},
    ]
    for files in renders:
        for rel, text in files.items():
            _write(tmp_path, rel, text)
        plan = detect.resolve(tmp_path, "heldso")
        for line in plan.generated.splitlines():
            if "@sha256:" in line:
                for ref, digest in BASES.items():
                    if f"{ref}@{digest}" in line:
                        used.add(ref)
        for rel in files:
            (tmp_path / rel).unlink()
    assert used == set(BASES)


def test_stario_needs_uv_lock(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", PYPROJECT)
    with pytest.raises(YardError, match="uv.lock"):
        detect.resolve(tmp_path, "heldso")


def test_stario_no_entrypoint(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", PYPROJECT)
    _write(tmp_path, "uv.lock", "lock")
    with pytest.raises(YardError) as caught:
        detect.resolve(tmp_path, "heldso")
    assert caught.value.message == "found a Stario app but no app/main.py, main.py, or app.py"


def test_python_requirements(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "flask\n")
    _write(tmp_path, "app.py")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.answer == "yard:python-pip"
    assert resolved.reason == "python: requirements.txt"
    assert "pip install -r requirements.txt" in resolved.generated
    assert resolved.generated.count("\nFROM ") == 1
    assert 'CMD ["python", "app.py"]' in resolved.generated


def test_python_uv_and_entrypoint_order(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nrequires-python = ">=3.13"\n')
    _write(tmp_path, "uv.lock", "lock")
    _write(tmp_path, "app/main.py")
    _write(tmp_path, "main.py")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.answer == "yard:python-uv"
    assert resolved.reason == "python: uv.lock"
    assert "uv sync --locked --no-install-project --no-dev" in resolved.generated
    assert "uv sync --locked --no-dev" not in resolved.generated
    assert resolved.generated.count("\nFROM ") == 1
    assert 'CMD ["python", "-m", "app.main"]' in resolved.generated
    assert f"python:3.13-slim-trixie@{BASES['python:3.13-slim-trixie']}" in resolved.generated


def test_python_pyproject_without_lock_or_requirements(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\n')
    _write(tmp_path, "main.py")
    with pytest.raises(YardError, match="no uv.lock or requirements.txt"):
        detect.resolve(tmp_path, "heldso")


def test_python_no_entrypoint(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "flask\n")
    with pytest.raises(YardError) as caught:
        detect.resolve(tmp_path, "heldso")
    assert caught.value.message == "found a Python app but no app/main.py, main.py, or app.py"


def test_go_root_main(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/heldso\n\ngo 1.26\n")
    _write(tmp_path, "main.go", "package main\n")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.answer == "yard:go"
    assert f"golang:1.26@{BASES['golang:1.26']}" in resolved.generated
    assert "go build -trimpath -o /out/app ." in resolved.generated
    assert "gcr.io/distroless/static-debian12" in resolved.generated


def test_go_minimum_takes_the_smallest_cover(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/heldso\n\ngo 1.24\n")
    _write(tmp_path, "main.go", "package main\n")
    resolved = detect.resolve(tmp_path, "heldso")
    assert "golang:1.26@" in resolved.generated


def test_go_one_cmd(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/heldso\n")
    _write(tmp_path, "cmd/server/main.go", "package main\n")
    resolved = detect.resolve(tmp_path, "heldso")
    assert "go build -trimpath -o /out/app ./cmd/server" in resolved.generated


def test_go_two_cmds_fails(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/heldso\n")
    _write(tmp_path, "cmd/a/main.go", "package main\n")
    _write(tmp_path, "cmd/b/main.go", "package main\n")
    with pytest.raises(YardError, match="a, b"):
        detect.resolve(tmp_path, "heldso")


def test_go_too_new_fails(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/heldso\n\ngo 1.99\n")
    _write(tmp_path, "main.go", "package main\n")
    with pytest.raises(YardError, match="cover 1.26, 1.27; this repo asks 1.99"):
        detect.resolve(tmp_path, "heldso")


def test_node(tmp_path: Path):
    _write(tmp_path, "package.json", '{"scripts": {"start": "node server.js"}, "engines": {"node": ">=20"}}')
    _write(tmp_path, "package-lock.json", "{}")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.answer == "yard:node-npm"
    assert resolved.reason == "node: package.json start script"
    assert f"node:22-slim@{BASES['node:22-slim']}" in resolved.generated
    assert "RUN npm ci" in resolved.generated
    assert 'CMD ["npm", "start"]' in resolved.generated


def test_node_build_script(tmp_path: Path):
    _write(tmp_path, "package.json", '{"scripts": {"start": "x", "build": "tsc"}}')
    resolved = detect.resolve(tmp_path, "heldso")
    assert "RUN npm install" in resolved.generated
    assert "RUN npm run build" in resolved.generated
    assert "node:24-slim@" in resolved.generated


def test_node_no_start(tmp_path: Path):
    _write(tmp_path, "package.json", '{"scripts": {}}')
    with pytest.raises(YardError, match="no start script"):
        detect.resolve(tmp_path, "heldso")


def test_node_pnpm_fails(tmp_path: Path):
    _write(tmp_path, "package.json", '{"scripts": {"start": "x"}}')
    _write(tmp_path, "pnpm-lock.yaml", "lock")
    with pytest.raises(YardError, match="pnpm"):
        detect.resolve(tmp_path, "heldso")


def test_ambiguous_fails(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\n')
    _write(tmp_path, "package.json", "{}")
    with pytest.raises(YardError, match="pyproject.toml and package.json"):
        detect.resolve(tmp_path, "heldso")


def test_nothing_fails(tmp_path: Path):
    with pytest.raises(YardError) as caught:
        detect.resolve(tmp_path, "heldso")
    assert (
        caught.value.message
        == "no compose file, Containerfile, or known language (Python, Go, Node, Nushell) in the checkout"
    )
    assert caught.value.help_text == "Add a Containerfile: stario.dev/yard/what-runs#when-yard-refuses"


def test_python_version_sources(tmp_path: Path):
    _write(tmp_path, ".python-version", "3.12\n")
    _write(tmp_path, "requirements.txt", "x\n")
    _write(tmp_path, "main.py")
    assert "python:3.12-slim-trixie@" in detect.resolve(tmp_path, "n").generated


def test_python_exact_pin_outside_the_set_fails(tmp_path: Path):
    _write(tmp_path, ".python-version", "3.11\n")
    _write(tmp_path, "requirements.txt", "x\n")
    _write(tmp_path, "main.py")
    with pytest.raises(YardError, match="cover 3.12, 3.13, 3.14; this repo asks 3.11"):
        detect.resolve(tmp_path, "n")


def test_python_single_script_entrypoint(tmp_path: Path):
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname="x"\n[project.scripts]\nserve = "x:main"\n\n[build-system]\nrequires=[]\n',
    )
    _write(tmp_path, "uv.lock", "lock")
    generated = detect.resolve(tmp_path, "n").generated
    assert 'CMD ["serve"]' in generated
    # A packaged project installs itself after the source copy.
    assert generated.index("COPY . /workspace") < generated.index("uv sync --locked --no-dev")


@pytest.mark.parametrize(
    ("pyproject", "installs"),
    [
        ('[project]\nname="x"\n', False),
        ('[project]\nname="x"\n[build-system]\nrequires=[]\n', True),
        ('[project]\nname="x"\n[build-system]\nrequires=[]\n[tool.uv]\npackage=false\n', False),
        ('[project]\nname="x"\n[tool.uv]\npackage=true\n', True),
    ],
)
def test_python_uv_installs_a_packaged_project(tmp_path: Path, pyproject: str, installs: bool):
    _write(tmp_path, "pyproject.toml", pyproject)
    _write(tmp_path, "uv.lock", "lock")
    _write(tmp_path, "main.py")
    assert ("uv sync --locked --no-dev" in detect.resolve(tmp_path, "n").generated) is installs


def test_stario_dep_matching(tmp_path: Path):
    from app.server.detect import Repo, stario

    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nrequires-python=">=3.14"\ndependencies=["stario>=5"]\n',
    )
    _write(tmp_path, "uv.lock", "l")
    _write(tmp_path, "app/main.py")
    plan = stario.detect(Repo(tmp_path), "n")
    assert plan is not None and plan.answer == "yard:python-uv"
    _write(tmp_path, "pyproject.toml", '[project]\nrequires-python=">=3.14"\ndependencies=["Stario[x]==5"]\n')
    plan = stario.detect(Repo(tmp_path), "n")
    assert plan is not None and plan.answer == "yard:python-uv"
    _write(tmp_path, "pyproject.toml", '[project]\nrequires-python=">=3.14"\ndependencies=["stariox==1"]\n')
    assert stario.detect(Repo(tmp_path), "n") is None


def test_invalid_pyproject_is_empty(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", "not [valid")
    _write(tmp_path, "uv.lock", "l")
    _write(tmp_path, "main.py")
    resolved = detect.resolve(tmp_path, "n")
    assert resolved.answer == "yard:python-uv" and "python:3.14" in resolved.generated


def test_classify():
    assert detect.classify("services:\n  app:\n") == "compose"
    assert detect.classify("services: # hi\n  app:\n") == "compose"
    assert detect.classify("FROM scratch\n") == "containerfile"
    assert detect.classify("# hi\n\narg X\nFROM scratch\n") == "containerfile"
    assert detect.classify("services:\nFROM x\n") == "compose"  # services: wins; no first FROM
    with pytest.raises(YardError):
        detect.classify("nothing\n")


def test_plan_for_template(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", PYPROJECT)
    _write(tmp_path, "uv.lock", "lock")
    _write(tmp_path, "app/main.py")
    plan = detect.plan_for(tmp_path, "heldso", "yard:python-uv")
    assert plan.answer == "yard:python-uv" and plan.template
    with pytest.raises(YardError, match="yard:go does not fit this repo"):
        detect.plan_for(tmp_path, "heldso", "yard:go")


def test_plan_for_path_classifies(tmp_path: Path):
    _write(tmp_path, "ops/stack.yml", "services:\n  app:\n")
    plan = detect.plan_for(tmp_path, "heldso", "ops/stack.yml")
    assert (plan.kind, plan.answer) == ("compose", "ops/stack.yml")
    _write(tmp_path, "ops/Box", "FROM scratch\n")
    plan = detect.plan_for(tmp_path, "heldso", "ops/Box")
    assert plan.kind == "containerfile"
    with pytest.raises(YardError, match="not a file"):
        detect.plan_for(tmp_path, "heldso", "ops/missing.yml")


def test_http_nu(tmp_path: Path):
    _write(tmp_path, "serve.nu", "{|req| 'hi'}\n")
    resolved = detect.resolve(tmp_path, "xs-app")
    assert (resolved.kind, resolved.answer, resolved.reason) == (
        "containerfile",
        "yard:http-nu",
        "http-nu: serve.nu",
    )
    text = resolved.generated
    assert "v0.18.0" in text
    assert "0.0.0.0:8000" in text and "--store", "/data/store"
    assert "--datastar" in text
    assert '"http-nu", "--datastar", "--store", "/data/store", "0.0.0.0:8000", "./serve.nu"' in text
    assert "28814093dd40cebc32cd533dde6ac10a0be67698a25c47ffd16b47d32fd65954" in text
    assert "6714826225d66bfceb1cf029f6df3024f6f013c9e4c4e2bc62a7c660f9262f28" in text
    assert f"alpine:3.22@{BASES['alpine:3.22']}" in text
    assert "gcr.io/distroless/static-debian12@" in text
    assert resolved.template


def test_http_nu_needs_a_handler(tmp_path: Path):
    _write(tmp_path, "serve.nu", "print hello\n")
    with pytest.raises(YardError, match="no handler closure"):
        detect.resolve(tmp_path, "xs-app")


def test_http_nu_subdir_only_is_not_detected(tmp_path: Path):
    _write(tmp_path, "sub/serve.nu", "{|req| 'hi'}\n")
    with pytest.raises(
        YardError,
        match=r"no compose file, Containerfile, or known language \(Python, Go, Node, Nushell\)",
    ):
        detect.resolve(tmp_path, "xs-app")


def test_http_nu_plus_another_language_refuses(tmp_path: Path):
    _write(tmp_path, "serve.nu", "{|req| 'hi'}\n")
    _write(tmp_path, "package.json", "{}")
    with pytest.raises(YardError, match="will not guess"):
        detect.resolve(tmp_path, "xs-app")


def test_plan_for_http_nu(tmp_path: Path):
    _write(tmp_path, "serve.nu", "{|req| 'hi'}\n")
    plan = detect.plan_for(tmp_path, "xs-app", "yard:http-nu")
    assert plan.answer == "yard:http-nu" and plan.template
    _write(tmp_path, "pyproject.toml", PYPROJECT)
    (tmp_path / "uv.lock").write_text("l")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("")
    (tmp_path / "serve.nu").unlink()
    with pytest.raises(YardError, match="yard:http-nu does not fit this repo"):
        detect.plan_for(tmp_path, "xs-app", "yard:http-nu")
