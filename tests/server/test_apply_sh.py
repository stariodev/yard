"""ops/apply/apply.sh against a fake docker-compose on PATH."""

import os
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "apply" / "apply.sh"


def _bins(tmp_path: Path) -> Path:
    """Fake docker-compose, podman-remote, and curl. They echo calls to the log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker-compose").write_text(
        "#!/bin/sh\n"
        'echo "dc:$*" >> "$CALL_LOG"\n'
        'echo "env IMAGE=$IMAGE SHA=$SHA" >> "$CALL_LOG"\n'
        'if [ "$1" = "config" ]; then\n'
        '  printf "%s\\n" "$DC_CONFIG"\n'
        "  exit 0\n"
        "fi\n"
        'echo "dc:$*"\n'
        'if [ "$1" = "up" ] && [ -n "${FAIL_IMAGE:-}" ] && [ "$IMAGE" = "$FAIL_IMAGE" ]; then\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "${FAKE_FAIL:-}" ] && [ ! -f "$FAIL_SPENT" ]; then\n'
        '  touch "$FAIL_SPENT"\n'
        "  exit 1\n"
        "fi\n"
        'exit "${DC_CODE:-0}"\n'
    )
    (bin_dir / "docker-compose").chmod(0o755)
    (bin_dir / "podman-remote").write_text('#!/bin/sh\necho "podman:$*" >> "$CALL_LOG"\nexit 0\n')
    (bin_dir / "podman-remote").chmod(0o755)
    (bin_dir / "wget").write_text('#!/bin/sh\necho "wget:$*" >> "$CALL_LOG"\nexit "${WGET_CODE:-0}"\n')
    (bin_dir / "wget").chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, *argv: str, **extra: str) -> tuple[int, str, str, str]:
    bin_dir = _bins(tmp_path)
    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "FAIL_SPENT": str(tmp_path / "spent"),
            "COMPOSE_PROJECT_NAME": "heldso",
            "COMPOSE_FILE": "a.yml:b.yml",
            "PROJECT_NAME": "heldso",
            "SHA": "abc",
            "IMAGE": "localhost/heldso:abc",
            "DC_CONFIG": "services:\n  app:\n    networks:\n      yard:\n",
        }
    )
    env.update(extra)
    proc = subprocess.run(
        ["sh", str(SCRIPT), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, log.read_text() if log.exists() else "", proc.stderr


def test_up_builds_starts_and_reports_ready(tmp_path: Path):
    code, out, log, _err = _run(tmp_path, "up")
    assert code == 0
    assert re.search(r"^built \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", out, re.MULTILINE)
    assert "dc:build" in log
    assert "dc:up -d --wait --wait-timeout 120 --remove-orphans --no-build" in log


def test_up_probes_the_url_once_the_container_runs(tmp_path: Path):
    code, out, log, _err = _run(tmp_path, "up", PROBE_URL="http://yard:8000/health")
    assert code == 0
    assert "== probe http://yard:8000/health" in out
    assert log.count("wget:") == 1


def test_up_reverts_when_the_probe_gets_no_answer(tmp_path: Path):
    code, out, log, _err = _run(
        tmp_path,
        "up",
        PROBE_URL="http://yard:8000/health",
        PROBE_SECONDS="2",
        WGET_CODE="1",
        PREVIOUS_IMAGE="localhost/heldso:old",
    )
    assert code == 3
    assert "== unhealthy: no answer at http://yard:8000/health" in out
    assert "== reverted localhost/heldso:old" in out


def test_up_without_a_probe_url_asks_nothing(tmp_path: Path):
    code, _out, log, _err = _run(tmp_path, "up")
    assert code == 0
    assert "wget:" not in log


def test_up_reuses_the_image_when_skip_build_is_set(tmp_path: Path):
    code, out, log, _err = _run(tmp_path, "up", SKIP_BUILD="1")
    assert code == 0
    assert "== reuse localhost/heldso:abc" in out
    assert "== build" not in out
    assert "dc:up -d --wait --wait-timeout 120 --remove-orphans --no-build" in log


def test_up_reverts_to_the_previous_image(tmp_path: Path):
    code, out, log, _err = _run(
        tmp_path, "up", FAIL_IMAGE="localhost/heldso:abc", PREVIOUS_IMAGE="localhost/heldso:old"
    )
    assert code == 3
    assert "== revert localhost/heldso:old" in out
    assert "== reverted localhost/heldso:old" in out
    assert "dc:up -d --no-build\nenv IMAGE=localhost/heldso:old" in log


def test_up_fails_when_the_revert_fails(tmp_path: Path):
    code, out, log, _err = _run(
        tmp_path,
        "up",
        FAKE_FAIL="up",
        PREVIOUS_IMAGE="localhost/heldso:old",
        FAIL_IMAGE="localhost/heldso:old",
    )
    assert code == 1
    assert "== revert localhost/heldso:old" in out
    assert "== revert failed" in out


def test_up_fails_without_a_previous_image(tmp_path: Path):
    code, out, _, _err = _run(tmp_path, "up", FAIL_IMAGE="localhost/heldso:abc")
    assert code == 1
    assert "== unhealthy: up failed" in out
    assert "== revert" not in out


def test_unknown_command_exits(tmp_path: Path):
    code, _out, _log, err = _run(tmp_path, "job")
    assert code == 2
    assert "usage:" in err


def test_purge(tmp_path: Path):
    code, _, log, _err = _run(tmp_path, "purge")
    assert code == 0
    assert "dc:down -v" in log


def test_up_rejects_an_app_off_the_yard_network(tmp_path: Path):
    code, _out, _log, err = _run(tmp_path, "up", DC_CONFIG="services:\n  web:\n    image: x\n")
    assert code == 1
    assert "not on network yard" in err


def test_warnings_come_from_the_compose_file(tmp_path: Path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  app:\n    networks:\n      - yard\n")
    code, out, _, _err = _run(tmp_path, "up", COMPOSE_FILE=str(compose))
    assert code == 0
    assert "${APP_ENV}" in out
    assert "${DATA_DIR}" in out


def test_yard_files_skip_the_check_and_build_with_podman_remote(tmp_path: Path):
    code, out, log, _err = _run(
        tmp_path,
        "up",
        YARD_FILES="1",
        COMPOSE_FILE="/usr/local/share/yard/apply/app.compose.yml:/usr/local/share/yard/apply/build.compose.yml",
        CHECKOUT="/srv/yard/apps/heldso/repo",
        DOCKERFILE="Containerfile",
        DC_CONFIG="anything",
    )
    assert code == 0
    assert "podman:build --format docker --pull=missing -t localhost/heldso:abc" in log
    assert "-f /srv/yard/apps/heldso/repo/Containerfile" in log
    assert "dc:build" not in log


def test_absolute_dockerfile_used_as_is(tmp_path: Path):
    code, _out, log, _err = _run(
        tmp_path,
        "up",
        YARD_FILES="1",
        CHECKOUT="/srv/yard/apps/heldso/repo",
        DOCKERFILE="/srv/yard/apps/heldso/Containerfile",
    )
    assert code == 0
    assert (
        "podman:build --format docker --pull=missing -t localhost/heldso:abc -f /srv/yard/apps/heldso/Containerfile"
        in log
    )


def test_revert_uses_the_previous_compose_file(tmp_path: Path):
    code, out, log, _err = _run(
        tmp_path,
        "up",
        FAKE_FAIL="up",
        PREVIOUS_COMPOSE_FILE="/srv/yard/apps/heldso/compose/abc123def456.yml",
    )
    assert code == 3
    assert "== revert /srv/yard/apps/heldso/compose/abc123def456.yml" in out
    assert "== reverted" in out
    assert "dc:up -d --no-build" in log
