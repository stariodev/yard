"""The file store: apps, tokens, the admin token, and the deployed file."""

from pathlib import Path

import pytest

from app.server.store import Store, ensure_admin
from app.shared import YardError


def test_store_rows(tmp_path):
    store = Store.open(tmp_path)
    app = store.create_app("heldso", "https://origin.cursor.com/org/app")
    assert app.ref == "main"
    assert app.trigger == "push"
    app = store.update_app("heldso", ref="release", trigger="manual")
    assert app.ref == "release"
    assert app.trigger == "manual"
    text = (tmp_path / "apps" / "heldso" / "app").read_text()
    assert "repo https://origin.cursor.com/org/app\n" in text
    assert "trigger manual\n" in text
    app = store.update_app("heldso", ref="")
    assert app.ref == "main"
    assert [app.name for app in store.list_apps() if app.repo == "https://origin.cursor.com/org/app"] == ["heldso"]

    plain = store.create_token("ada")
    assert store.token_ok(plain) is not None
    assert store.token_ok("nope") is None
    store.delete_token("ada")
    assert store.token_ok(plain) is None


def test_the_record_has_four_keys(tmp_path):
    store = Store.open(tmp_path)
    store.create_app("heldso", "https://origin.cursor.com/org/app")
    fields = {}
    for line in (tmp_path / "apps" / "heldso" / "app").read_text().splitlines():
        key, _, value = line.partition(" ")
        fields[key] = value
    assert sorted(fields) == ["created", "ref", "repo", "trigger"]


def test_a_hand_edit_reloads(tmp_path):
    store = Store.open(tmp_path)
    store.create_app("heldso", "https://origin.cursor.com/org/app", trigger="manual")
    path = tmp_path / "apps" / "heldso" / "app"
    path.write_text(path.read_text().replace("trigger manual", "trigger push"))
    assert store.app("heldso").trigger == "push"


def test_bootstrap_repo_may_be_empty(tmp_path):
    store = Store.open(tmp_path)
    app = store.create_app("yard")
    assert app.repo == ""
    with pytest.raises(YardError) as caught:
        store.update_app("yard", repo="noslash")
    assert caught.value.code == "bad_repo"


def test_ensure_admin_writes_the_token_once(tmp_path):
    store = Store.open(tmp_path)
    plain = ensure_admin(store)
    assert plain
    assert (tmp_path / "tokens" / "admin.token").read_text().strip() == plain
    assert (tmp_path / "tokens" / "admin").is_file()
    assert ensure_admin(store) == ""


def test_a_deleted_admin_stays_deleted(tmp_path):
    """Another token removed admin. The next boot makes no new admin, and the plain copy is gone."""
    store = Store.open(tmp_path)
    plain = ensure_admin(store)
    store.create_token("ci")
    store.delete_token("admin", actor="ci")
    assert not (tmp_path / "tokens" / "admin.token").exists()
    assert ensure_admin(store) == ""
    assert store.token_ok(plain) is None
    assert [token.name for token in store.list_tokens()] == ["ci"]


def test_a_repo_less_app_reads_back_no_ref(tmp_path):
    store = Store.open(tmp_path)
    store.create_app("web", image="docker.io/library/nginx:1.27")
    assert store.app("web").ref == ""
    assert store.app("web").trigger == "push"


def test_trigger_is_push_or_manual(tmp_path):
    store = Store.open(tmp_path)
    with pytest.raises(YardError, match="trigger must be"):
        store.create_app("nightly", "https://origin.cursor.com/org/app", ref="main", trigger="1h")
    app = store.create_app("nightly", "https://origin.cursor.com/org/app", ref="main", trigger="manual")
    assert app.trigger == "manual"
    app = store.update_app("nightly", trigger="")
    assert app.trigger == "push"


def test_env_files_round_trip(tmp_path):
    store = Store.open(tmp_path)
    store.create_app("heldso", "https://origin.cursor.com/org/app")
    assert store.write_env("heldso", {"B": "2", "A": "1"}) == ["A", "B"]
    assert store.read_env("heldso") == {"A": "1", "B": "2"}
    assert store.patch_env("heldso", {"B": None, "C": "3"}) == ["A", "C"]
    assert store.read_env("heldso") == {"A": "1", "C": "3"}
    path = tmp_path / "apps" / "heldso" / "env"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_runs_round_trip(tmp_path: Path):
    store = Store.open(tmp_path)
    app = store.create_app("heldso", "https://github.com/org/heldso", runs="ops/stack.yml")
    assert app.runs == "ops/stack.yml"
    assert store.app("heldso").runs == "ops/stack.yml"
    app = store.update_app("heldso", runs="yard:python-uv")
    assert app.runs == "yard:python-uv"
    assert "runs yard:python-uv" in (tmp_path / "apps" / "heldso" / "app").read_text()
    app = store.update_app("heldso", runs="")
    assert app.runs == ""


def test_runs_kinds(tmp_path: Path):
    store = Store.open(tmp_path)
    store.create_app("heldso", "https://github.com/org/heldso")
    for bad in ("/abs", "a/../b", "a b", "a\\b"):
        with pytest.raises(YardError):
            store.update_app("heldso", runs=bad)
    with pytest.raises(YardError, match="unknown template"):
        store.update_app("heldso", runs="yard:rust")
    app = store.update_app("heldso", runs="yard:http-nu")
    assert app.runs == "yard:http-nu"
    with pytest.raises(YardError, match="use --image postgres:17"):
        store.update_app("heldso", runs="postgres:17")


def test_an_app_has_one_source(tmp_path: Path):
    store = Store.open(tmp_path)
    with pytest.raises(YardError, match="one source"):
        store.create_app("db", "https://github.com/org/db", image="postgres:17")
    store.create_app("db", image="postgres:17")
    with pytest.raises(YardError, match="no repo"):
        store.update_app("db", runs="ops/stack.yml")
    with pytest.raises(YardError, match="one source"):
        store.update_app("db", image="postgres:18", compose_url="https://example.com/x.yml")
    # A new source replaces the old one.
    app = store.update_app("db", compose_url="https://example.com/x.yml")
    assert (app.image, app.compose_url) == ("", "https://example.com/x.yml")
    app = store.update_app("db", repo="https://github.com/org/db", runs="ops/stack.yml")
    assert (app.repo, app.ref, app.runs, app.compose_url) == ("https://github.com/org/db", "main", "ops/stack.yml", "")
    with pytest.raises(YardError, match="cannot be empty"):
        store.update_app("db", repo="")
    # A repo app that moves to an image drops its ref and runs.
    app = store.update_app("db", image="postgres:17")
    assert (app.repo, app.ref, app.runs, app.image) == ("", "", "", "postgres:17")


def test_repo_less_app_has_no_ref(tmp_path: Path):
    store = Store.open(tmp_path)
    app = store.create_app("db", image="postgres:17")
    assert app.ref == ""
    assert "ref" not in (tmp_path / "apps" / "db" / "app").read_text()
    with pytest.raises(YardError, match="has no repo"):
        store.update_app("db", ref="main")
    # Setting a repo fills ref with main.
    app = store.update_app("db", repo="https://github.com/org/db")
    assert app.ref == "main"
