"""Shared values: the error shape, name and env checks, atomic writes."""

import json
import re
import stat
import subprocess
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.shared import (
    FORGE_HOSTS,
    YardError,
    age,
    atomic_write,
    check_app_name,
    check_domain,
    check_name,
    check_ref,
    check_repo,
    check_trigger,
    default_site,
    ensure_file,
    explain,
    parse_env,
)


def test_error_text_matches_stario() -> None:
    exc = YardError(
        400,
        "bad_name",
        "name must be a short lowercase word",
        help_text="Start with a letter.",
    )
    assert str(exc) == "name must be a short lowercase word\n  Help: Start with a letter."
    body = {"error": exc.payload()}
    assert body["error"] == {
        "code": "bad_name",
        "message": "name must be a short lowercase word",
        "help": "Start with a letter.",
    }
    assert explain(json.dumps(body)) == str(exc)


def test_plain_text_stays_plain() -> None:
    assert explain("podman is down") == "podman is down"


def _names(*reject: str):
    banned = set(reject)

    def ok(name: str) -> bool:
        if name in banned:
            return False
        try:
            check_name(name)
        except YardError:
            return False
        return True

    return st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True).filter(ok)


def _hostnames():
    label = st.from_regex(r"[a-z][a-z0-9]{0,6}", fullmatch=True)
    return st.lists(label, min_size=1, max_size=3).map(".".join)


def _env_keys():
    return st.from_regex(r"[A-Z][A-Z0-9_]{1,12}", fullmatch=True).filter(lambda key: not key.startswith("PROJECT_"))


def _env_values():
    return st.from_regex(r"[A-Za-z0-9./:@_-]{0,16}", fullmatch=True)


def _bad_names():
    return st.one_of(
        st.just(""),
        st.just("yard-sampler"),
        st.just("Heldso"),
        st.from_regex(r"[A-Z][A-Za-z0-9]{0,6}", fullmatch=True),
        st.from_regex(r"yard-[a-z]{1,6}", fullmatch=True),
        st.from_regex(r"[0-9][a-z0-9]{0,6}", fullmatch=True),
    )


@example("heldso")
@example("yard")
@example("a")
@example("a-")
@given(_names())
def test_repo_names_pass(name: str) -> None:
    check_name(name)


@example("heldso")
@example("yard")
@given(_names("caddy"))
def test_app_names_pass(name: str) -> None:
    check_app_name(name)


@example("")
@example("yard-sampler")
@example("Heldso")
@given(_bad_names())
def test_repo_names_fail(name: str) -> None:
    with pytest.raises(YardError) as caught:
        check_name(name)
    assert caught.value.code == "bad_name"


def test_caddy_is_a_repo_name_and_not_an_app_name() -> None:
    check_name("caddy")
    with pytest.raises(YardError) as caught:
        check_app_name("caddy")
    assert caught.value.code == "bad_name"


_REF = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,16}", fullmatch=True).filter(
    lambda ref: ".." not in ref and not ref.startswith("/")
)


@example("main")
@example("feature/login")
@given(_REF)
def test_refs_pass(ref: str) -> None:
    check_ref(ref)


@example("")
@example("../x")
@example("/main")
@example("a b")
@example("a\tb")
@example("a\\b")
@given(
    st.one_of(
        st.just(""),
        st.just("../x"),
        st.just("/main"),
        st.just("a b"),
        st.from_regex(r"/[a-z]{1,8}", fullmatch=True),
        st.from_regex(r"[a-z]{0,4}\.\.[a-z]{0,4}", fullmatch=True),
        st.from_regex(r"[a-z]{0,4} [a-z]{0,4}", fullmatch=True),
    )
)
def test_refs_fail(ref: str) -> None:
    with pytest.raises(YardError) as caught:
        check_ref(ref)
    assert caught.value.code == "bad_ref"


_REPO_SIDE = st.from_regex(r"[A-Za-z0-9._-]+", fullmatch=True).filter(lambda text: text not in {".", ".."})


@example("https://origin.cursor.com/org/heldso")
@example("https://github.com/Bobowski/yard")
@given(
    st.builds(
        lambda host, a, b: f"https://{host}/{a}/{b}",
        st.sampled_from(FORGE_HOSTS),
        _REPO_SIDE,
        _REPO_SIDE,
    )
)
def test_forge_repos_pass(repo: str) -> None:
    assert check_repo(repo) == repo


@given(
    st.builds(
        lambda host, a, b, tail: f"https://{host}/{a}/{b}{tail}",
        st.sampled_from(FORGE_HOSTS),
        _REPO_SIDE,
        _REPO_SIDE,
        st.sampled_from(["/", ".git", ".git/"]),
    )
)
def test_forge_repo_trailers_normalize(host_and_repo: str) -> None:
    text = host_and_repo
    expected = text[:-1] if text.endswith("/") else text
    if expected.endswith(".git"):
        expected = expected[:-4]
    assert check_repo(text) == expected


@example("")
@example("noslash")
@example("org/heldso")
@example("gitlab.com/org/heldso")
@example("https://github.com/a/b/c")
@example("https://github.com/a b/c")
@given(
    st.sampled_from(
        [
            "",
            "noslash",
            "org/heldso",
            "gitlab.com/org/heldso",
            "https://github.com/a/b/c",
            "https://github.com//heldso",
            "https://github.com/org/",
            "https://github.com/a b/c",
            "a b",
        ]
    )
)
def test_forge_repos_fail(repo: str) -> None:
    with pytest.raises(YardError) as caught:
        check_repo(repo)
    assert caught.value.code == "bad_repo"


@example("")
@example("heldso.example.com")
@given(st.one_of(st.just(""), _hostnames()))
def test_domains_pass(domain: str) -> None:
    check_domain(domain)


@example("..")
@example("heldso..com")
@example("-bad")
@example("Bad.com")
@given(
    st.one_of(
        st.just(".."),
        st.just("heldso..com"),
        st.from_regex(r"[A-Z][A-Za-z]{1,6}", fullmatch=True),
        st.from_regex(r"-[a-z]{1,6}", fullmatch=True),
        _hostnames().map(lambda host: host + ".."),
    )
)
def test_domains_fail(domain: str) -> None:
    with pytest.raises(YardError) as caught:
        check_domain(domain)
    assert caught.value.code == "bad_domain"


def test_trigger_push() -> None:
    for text in ("push", "PUSH", ""):
        assert check_trigger(text) == "push"


def test_trigger_manual() -> None:
    for text in ("manual", "MANUAL"):
        assert check_trigger(text) == "manual"


@example("every")
@example("every 5m")
@example("weekly")
@example("5m")
@example("every tomorrow")
@given(st.sampled_from(["nightly", "weekly", "every 5m", "cron 5m", "on commit", "off", "service", "on push"]))
def test_trigger_rejects(text: str) -> None:
    with pytest.raises(YardError) as caught:
        check_trigger(text)
    assert caught.value.code == "bad_trigger"


@example({"API_URL": "https://x", "SECRET_KEY": "abc"})
@given(st.dictionaries(_env_keys(), _env_values(), max_size=5))
def test_env_lines_round_trip(env: dict[str, str]) -> None:
    text = "".join(f"{key}={env[key]}\n" for key in sorted(env))
    assert parse_env(text) == env


@example("ab c")
@given(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9 ]{0,12}", fullmatch=True))
def test_quoted_env_values(value: str) -> None:
    assert parse_env(f'SECRET="{value}"\n') == {"SECRET": value}


def test_atomic_write_replaces_the_file(tmp_path: Path):
    path = tmp_path / "dir" / "state.json"
    atomic_write(path, "one\n")
    assert path.read_text() == "one\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    atomic_write(path, "two\n")
    assert path.read_text() == "two\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [item.name for item in path.parent.iterdir()] == ["state.json"]


def test_ensure_file_keeps_an_existing_file(tmp_path: Path):
    path = tmp_path / "env"
    ensure_file(path)
    assert path.read_text() == ""
    path.write_text("A=1\n")
    ensure_file(path)
    assert path.read_text() == "A=1\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_age():
    assert age(0.5) == "now"
    assert age(30) == "30s"
    assert age(300) == "5m"
    assert age(3 * 3600) == "3h"
    assert age(15 * 86400) == "15d"


def test_check_image(tmp_path):
    from app.shared import check_image

    assert check_image("postgres:17") == "postgres:17"
    assert check_image("ghcr.io/you/api:1.4") == "ghcr.io/you/api:1.4"
    assert check_image("postgres@sha256:" + "a" * 64) == "postgres@sha256:" + "a" * 64
    assert check_image("gcr.io/distroless/static-debian12:latest") == "gcr.io/distroless/static-debian12:latest"
    with pytest.raises(YardError, match="Name a tag"):
        check_image("postgres")
    with pytest.raises(YardError):
        check_image("Postgres:17")
    with pytest.raises(YardError):
        check_image("https://registry/postgres:17")
    with pytest.raises(YardError):
        check_image("postgres:17@sha256:short")


def test_check_runs_and_check_compose_url():
    import pytest as _p

    from app.shared import YardError, check_compose_url, check_runs

    assert check_runs("") == ""
    assert check_runs("auto") == ""
    assert check_runs("yard:python-uv") == "yard:python-uv"
    assert check_runs("ops/stack.yml") == "ops/stack.yml"
    for bad in ("https://example.com/x.yml", "postgres:17", "yard:Bad", "yard:", "/abs"):
        with _p.raises(YardError):
            check_runs(bad)
    assert check_compose_url("https://example.com/x.yml") == "https://example.com/x.yml"
    for bad in ("http://example.com/x", "ftp://x", "https://" + "x" * 500):
        with _p.raises(YardError):
            check_compose_url(bad)


def test_image_sha_drops_the_template_suffix():
    from app.shared import image_sha

    assert image_sha("localhost/x:abcdef123456-3f9a1c2e") == "abcdef123456"
    assert image_sha("localhost/x:abcdef123456") == "abcdef123456"
    assert image_sha("postgres:17") == "17"
    assert image_sha("foo:1.2-deadbeef") == "1.2-deadbeef"


def test_install_sh_writes_the_default_site():
    """install.sh cannot import Python, so its printf template must print what default_site writes."""
    script = (Path(__file__).parent.parent / "install.sh").read_text()
    found = re.search(r"site=\"\$\(printf '([^']*)' \"\$domain\"\)\"", script)
    assert found, "install.sh no longer writes the site with printf"
    out = subprocess.run(["printf", found.group(1), "yard.example.com"], capture_output=True, text=True, check=True)
    assert out.stdout == default_site(["yard.example.com"], "yard-app-1:8000")
