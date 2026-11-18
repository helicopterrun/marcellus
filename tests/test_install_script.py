"""install.sh: legacy detection, release pinning, and Docker host-path handling."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"


def _detect(tmp_path: Path, *, unit=False, opt=False, etc=False, new=False) -> int:
    paths = {
        "LEGACY_UNIT": tmp_path / "frigate-sidecar.service",
        "LEGACY_DIR": tmp_path / "opt-frigate-sidecar",
        "LEGACY_CONF": tmp_path / "etc-frigate-sidecar",
        "NEW_UNIT": tmp_path / "marcellus.service",
    }
    if unit:
        paths["LEGACY_UNIT"].write_text("")
    if opt:
        paths["LEGACY_DIR"].mkdir()
    if etc:
        paths["LEGACY_CONF"].mkdir()
    if new:
        paths["NEW_UNIT"].write_text("")
    env = {**os.environ, **{k: str(v) for k, v in paths.items()}}
    env["MARCELLUS_INSTALL_SOURCE_ONLY"] = "1"
    return subprocess.run(
        ["bash", "-c", f"source {SCRIPT} && legacy_install_found"], env=env, check=False
    ).returncode


@pytest.mark.parametrize("kw", [{"unit": True}, {"opt": True}, {"etc": True}])
def test_legacy_found(tmp_path: Path, kw: dict) -> None:
    assert _detect(tmp_path, **kw) == 0


def test_clean_host(tmp_path: Path) -> None:
    assert _detect(tmp_path) != 0


def test_already_migrated_is_not_blocked(tmp_path: Path) -> None:
    # Old directories left behind after a migration must not block upgrades.
    assert _detect(tmp_path, unit=True, opt=True, new=True) != 0


def test_syntax_and_refusal_message(tmp_path: Path) -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0
    env = {
        **os.environ,
        "LEGACY_DIR": str(tmp_path),
        "NEW_UNIT": str(tmp_path / "none.service"),
        "MARCELLUS_INSTALL_SOURCE_ONLY": "1",
    }
    out = subprocess.run(
        ["bash", "-c", f"source {SCRIPT} && legacy_install_message"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "systemctl stop frigate-sidecar" in out.stdout
    assert "ALLOW_LEGACY_COEXIST=1" in out.stdout


# ---- helpers -----------------------------------------------------------------

ROOT = SCRIPT.parent
KEYS = ("FRIGATE_CONFIG", "FRIGATE_DB_DIR", "FRIGATE_RECORDINGS")


def _sh(
    snippet: str, *, env: dict[str, str] | None = None, stdin: str = ""
) -> subprocess.CompletedProcess:
    """Run a snippet with install.sh's functions loaded (source-only mode)."""
    base = {k: v for k, v in os.environ.items() if k not in KEYS and k != "MARCELLUS_VERSION"}
    base["MARCELLUS_INSTALL_SOURCE_ONLY"] = "1"
    base["FRIGATE_SEARCH_ROOTS"] = "/nonexistent-root"
    base.update(env or {})
    return subprocess.run(
        ["bash", "-c", f"source {SCRIPT}\n{snippet}"],
        env=base,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _frigate_tree(tmp_path: Path, *, recordings: bool = True) -> dict[str, str]:
    cfg = tmp_path / "frigate" / "config.yml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("mqtt: {}\n")
    db = tmp_path / "frigate" / "database"
    db.mkdir()
    (db / "frigate.db").write_text("")
    rec = tmp_path / "frigate" / "media" / "recordings"
    rec.mkdir(parents=True)
    if recordings:
        (rec / "2026-01-01").mkdir()
    return {"FRIGATE_CONFIG": str(cfg), "FRIGATE_DB_DIR": str(db), "FRIGATE_RECORDINGS": str(rec)}


# ---- version pin -------------------------------------------------------------


def _script_version() -> str:
    m = re.search(r'MARCELLUS_VERSION="\$\{MARCELLUS_VERSION:-([^}]+)\}"', SCRIPT.read_text())
    assert m, "install.sh has no MARCELLUS_VERSION default"
    return m.group(1)


def _package_version() -> str:
    init = (ROOT / "src" / "marcellus" / "__init__.py").read_text()
    m = re.search(r'__version__ = "([^"]+)"', init)
    assert m
    return m.group(1)


def test_script_version_matches_package_version() -> None:
    # Release prep must bump install.sh's MARCELLUS_VERSION with __version__.
    assert _script_version() == _package_version()


def test_pins_in_docs_and_compose_match_version() -> None:
    version = _package_version()
    train = version.rsplit(".", 1)[0]
    assert f"marcellus:{train}}}" in (ROOT / "docker-compose.yml").read_text()
    assert f":{train}`" in (ROOT / "SECURITY.md").read_text()
    for page in (
        ROOT / "README.md",
        ROOT / "src" / "marcellus" / "guide_content" / "deployment.md",
    ):
        text = page.read_text()
        urls = re.findall(
            r"marcellus/(v[0-9][^/\s]*|main)/(?:install\.sh|docker-compose\.yml|\.env\.example)",
            text,
        )
        assert urls, f"{page.name} has no pinned install command"
        assert set(urls) == {f"v{version}"}, f"{page.name}: {set(urls)}"


def test_urls_for_a_release() -> None:
    out = _sh(
        "MARCELLUS_VERSION=0.5.0; raw_base; echo; wheel_name; echo; wheel_url; echo; "
        "image_ref; echo; release_ref"
    )
    raw, wheel, url, image, ref = out.stdout.splitlines()
    assert raw == "https://raw.githubusercontent.com/helicopterrun/marcellus/v0.5.0"
    assert wheel == "marcellus-0.5.0-py3-none-any.whl"
    assert url == (
        "https://github.com/helicopterrun/marcellus/releases/download/"
        "v0.5.0/marcellus-0.5.0-py3-none-any.whl"
    )
    assert image == "ghcr.io/helicopterrun/marcellus:0.5"
    assert ref == "v0.5.0"


def test_urls_accept_v_prefix_and_main() -> None:
    assert _sh("MARCELLUS_VERSION=v0.6.2; wheel_name; echo; image_ref").stdout.split() == [
        "marcellus-0.6.2-py3-none-any.whl",
        "ghcr.io/helicopterrun/marcellus:0.6",
    ]
    main = _sh("MARCELLUS_VERSION=main; raw_base; echo; image_ref").stdout.split()
    assert main == [
        "https://raw.githubusercontent.com/helicopterrun/marcellus/main",
        "ghcr.io/helicopterrun/marcellus:latest",
    ]


@pytest.mark.parametrize(
    ("version", "ok"),
    [
        ("0.5.0", True),
        ("v0.5.1", True),
        ("main", True),
        ("0.5", False),
        ("", False),
        ("0.5.0;rm", False),
        ("../x", False),
    ],
)
def test_version_validation(version: str, ok: bool) -> None:
    got = _sh(f"MARCELLUS_VERSION='{version}'; version_valid").returncode == 0
    assert got is ok


def test_no_unpinned_sources_left() -> None:
    text = SCRIPT.read_text()
    assert "/main/" not in text.replace("raw.githubusercontent.com/helicopterrun/marcellus/v", "")
    # the only git+https is the explicit MARCELLUS_VERSION=main branch
    assert text.count("git+https") == 1


# ---- path validation ---------------------------------------------------------


def test_valid_tree_has_no_problems(tmp_path: Path) -> None:
    tree = _frigate_tree(tmp_path)
    for key, val in tree.items():
        assert _sh(f'path_problem {key} "{val}"').returncode == 1, key


@pytest.mark.parametrize(
    ("key", "mutate", "needle"),
    [
        ("FRIGATE_CONFIG", "missing", "does not exist"),
        ("FRIGATE_CONFIG", "swap:FRIGATE_DB_DIR", "not a file"),
        ("FRIGATE_DB_DIR", "swap:FRIGATE_CONFIG", "not a directory"),
        ("FRIGATE_DB_DIR", "swap:FRIGATE_RECORDINGS", "no frigate.db"),
        ("FRIGATE_RECORDINGS", "swap:FRIGATE_CONFIG", "not a directory"),
        ("FRIGATE_RECORDINGS", "relative", "absolute"),
        ("FRIGATE_RECORDINGS", "space", ".env"),
        ("FRIGATE_RECORDINGS", "empty", "not set"),
    ],
)
def test_path_problems(tmp_path: Path, key: str, mutate: str, needle: str) -> None:
    tree = _frigate_tree(tmp_path)
    if mutate == "missing":
        val = str(tmp_path / "nope")
    elif mutate.startswith("swap:"):
        val = tree[mutate.split(":")[1]]
    elif mutate == "relative":
        val = "recordings"
    elif mutate == "space":
        val = str(tmp_path / "a b")
    else:
        val = ""
    out = _sh(f'path_problem {key} "{val}"')
    assert out.returncode == 0
    assert needle in out.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
def test_unreadable_is_a_problem(tmp_path: Path) -> None:
    tree = _frigate_tree(tmp_path)
    cfg = Path(tree["FRIGATE_CONFIG"])
    cfg.chmod(0)
    try:
        assert "not readable" in _sh(f'path_problem FRIGATE_CONFIG "{cfg}"').stdout
    finally:
        cfg.chmod(0o600)


def test_recordings_empty(tmp_path: Path) -> None:
    empty = tmp_path / "e"
    empty.mkdir()
    assert _sh(f'recordings_empty "{empty}"').returncode == 0
    (empty / "x").mkdir()
    assert _sh(f'recordings_empty "{empty}"').returncode != 0


# ---- detection ---------------------------------------------------------------


def test_candidates_from_frigate_container_mounts() -> None:
    mounts = "/config=/srv/f/config\n/media/frigate=/srv/f/media\n/etc/localtime=/etc/localtime"
    got = {
        k: _sh(f"candidate_paths {k}", env={"FRIGATE_MOUNTS": mounts}).stdout.split() for k in KEYS
    }
    assert got["FRIGATE_CONFIG"][:2] == ["/srv/f/config/config.yml", "/srv/f/config/config.yaml"]
    assert got["FRIGATE_DB_DIR"][:2] == ["/srv/f/config", "/srv/f/media"]
    assert got["FRIGATE_RECORDINGS"][0] == "/srv/f/media/recordings"


def test_best_candidate_skips_invalid_and_uses_common_roots(tmp_path: Path) -> None:
    root = tmp_path / "frigate"
    _frigate_tree(tmp_path)  # frigate/config.yml, database/, media/recordings
    env = {"FRIGATE_SEARCH_ROOTS": f"{tmp_path}/other {root}"}
    assert _sh("best_candidate FRIGATE_CONFIG", env=env).stdout == str(root / "config.yml")
    assert _sh("best_candidate FRIGATE_DB_DIR", env=env).stdout == str(root / "database")
    assert _sh("best_candidate FRIGATE_RECORDINGS", env=env).stdout == str(
        root / "media" / "recordings"
    )
    assert (
        _sh(
            "best_candidate FRIGATE_CONFIG", env={"FRIGATE_SEARCH_ROOTS": f"{tmp_path}/other"}
        ).returncode
        != 0
    )


# ---- .env writing and resolution --------------------------------------------

ENV_EXAMPLE = (
    "# comment\n#FRIGATE_CONFIG=/opt/frigate/config.yml\n#FRIGATE_DB_DIR=/x\n#SIDECAR_DATA=./data\n"
)


def test_set_env_value_replaces_commented_active_and_appends(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(ENV_EXAMPLE + "FRIGATE_RECORDINGS=/old\nOTHER=1\n")
    _sh(
        f'set_env_value "{f}" FRIGATE_CONFIG /a/config.yml; '
        f'set_env_value "{f}" FRIGATE_RECORDINGS /new; '
        f'set_env_value "{f}" NEWKEY /z'
    )
    lines = f.read_text().splitlines()
    assert "FRIGATE_CONFIG=/a/config.yml" in lines
    assert "#FRIGATE_CONFIG=/opt/frigate/config.yml" not in lines
    assert "FRIGATE_RECORDINGS=/new" in lines and "FRIGATE_RECORDINGS=/old" not in lines
    assert "#FRIGATE_DB_DIR=/x" in lines and "OTHER=1" in lines
    assert lines[-1] == "NEWKEY=/z"


def _resolve(
    tmp_path: Path, env_text: str, env: dict[str, str], *, interactive: int = 0, stdin: str = ""
):
    f = tmp_path / ".env"
    f.write_text(env_text)
    return f, _sh(
        f'FRIGATE_MOUNTS=""; resolve_paths "{f}" {interactive} && rc=0 || rc=$?; '
        f'for k in {" ".join(KEYS)}; do v=RESOLVED_$k; echo "$k=${{!v}}"; done; '
        f"[ $rc -eq 0 ] || paths_stop_message; exit $rc",
        env=env,
        stdin=stdin,
    )


def test_env_overrides_resolve_and_write(tmp_path: Path) -> None:
    tree = _frigate_tree(tmp_path)
    f, out = _resolve(tmp_path, ENV_EXAMPLE, tree)
    assert out.returncode == 0, out.stdout + out.stderr
    for k, v in tree.items():
        assert f"{k}={v}" in out.stdout
    _sh(f'FRIGATE_MOUNTS=""; resolve_paths "{f}" 0; write_env_values "{f}"', env=tree)
    text = f.read_text()
    for k, v in tree.items():
        assert f"\n{k}={v}\n" in "\n" + text + "\n"


def test_rerun_keeps_existing_valid_values(tmp_path: Path) -> None:
    tree = _frigate_tree(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "frigate.db").write_text("")
    # existing .env holds valid values; detection must not replace them
    text = "".join(f"{k}={v}\n" for k, v in tree.items())
    f, out = _resolve(tmp_path, text, {"FRIGATE_SEARCH_ROOTS": str(other)})
    assert out.returncode == 0
    _sh(
        f'FRIGATE_MOUNTS=""; resolve_paths "{f}" 0; write_env_values "{f}"',
        env={"FRIGATE_SEARCH_ROOTS": str(other)},
    )
    assert f.read_text() == text


def test_invalid_existing_value_stops_non_interactive_and_is_not_overwritten(
    tmp_path: Path,
) -> None:
    tree = _frigate_tree(tmp_path)
    text = f"FRIGATE_CONFIG={tmp_path}/gone.yml\nFRIGATE_DB_DIR={tree['FRIGATE_DB_DIR']}\n"
    f, out = _resolve(tmp_path, text, {"FRIGATE_RECORDINGS": tree["FRIGATE_RECORDINGS"]})
    assert out.returncode != 0
    assert "gone.yml" in out.stdout and "does not exist" in out.stdout
    assert f.read_text() == text


def test_non_tty_stop_message_lists_paths_and_how_to_pass_them(tmp_path: Path) -> None:
    f, out = _resolve(tmp_path, ENV_EXAMPLE, {})
    assert out.returncode != 0
    for k in KEYS:
        assert k in out.stdout
    assert "<none found>" in out.stdout
    assert "FRIGATE_CONFIG=/opt/frigate/config.yml" in out.stdout
    assert "nothing was started" in out.stdout
    assert f.read_text() == ENV_EXAMPLE


def test_detected_values_are_accepted_when_all_found(tmp_path: Path) -> None:
    _frigate_tree(tmp_path)
    f, out = _resolve(tmp_path, ENV_EXAMPLE, {"FRIGATE_SEARCH_ROOTS": str(tmp_path / "frigate")})
    assert out.returncode == 0, out.stdout
    assert f"FRIGATE_CONFIG={tmp_path}/frigate/config.yml" in out.stdout


def test_interactive_prompts_until_valid(tmp_path: Path) -> None:
    tree = _frigate_tree(tmp_path)
    answers = f"{tmp_path}/typo\n{tree['FRIGATE_CONFIG']}\n\n{tree['FRIGATE_RECORDINGS']}\n"
    f, out = _resolve(
        tmp_path,
        ENV_EXAMPLE,
        {"FRIGATE_DB_DIR": tree["FRIGATE_DB_DIR"]},
        interactive=1,
        stdin=answers,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "does not exist" in out.stderr  # the typo was rejected, then re-asked
    assert f"FRIGATE_CONFIG={tree['FRIGATE_CONFIG']}" in out.stdout
    assert f"FRIGATE_RECORDINGS={tree['FRIGATE_RECORDINGS']}" in out.stdout


# ---- the closing checklist (fsc doctor) ----------------------------------------

_STUB = """#!/usr/bin/env bash
# Stand-in for `fsc` (and, via docker, for the container's fsc). Behaviour comes
# from STUB_MODE: ok | attention2 | attention1 | nodoctor | fails_silently
args=("$@")
for i in "${!args[@]}"; do [ "${args[$i]}" = fsc ] && args=("${args[@]:$((i+1))}") && break; done
case " ${args[*]} " in
  *" --help "*)
    echo "Usage: fsc [OPTIONS] COMMAND"
    [ "$STUB_MODE" = nodoctor ] || echo "  doctor   Check the install"
    echo "  serve    Run the HTTP server"
    exit 0 ;;
esac
echo "ARGS: ${args[*]}" >> "$STUB_LOG"
echo "PW_SEEN: ${MARCELLUS_PUSH__MQTT_PASSWORD:+yes}" >> "$STUB_LOG"
case "$STUB_MODE" in
  ok)
    printf ' \\xe2\\x9c\\x93 config  loaded\\n\\nEverything needed is working.\\n'; exit 0 ;;
  attention2)
    printf ' \\xe2\\x9c\\x93 config  ok\\n \\xe2\\x9c\\x97 mqtt  refused\\n ! recordings  empty\\n'
    exit 1 ;;
  attention1)
    printf ' ! recordings  empty\\n'; exit 0 ;;
  fails_silently)
    echo "Error: no such container"; exit 1 ;;
esac
"""


def _doctor_env(tmp_path: Path, mode: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # `docker` stub: `docker compose exec -T marcellus fsc ...` -> the same stub
    stub = tmp_path / "stub.sh"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    (bindir / "docker").symlink_to(stub)
    install = tmp_path / "opt-marcellus"
    (install / "venv" / "bin").mkdir(parents=True)
    (install / "venv" / "bin" / "fsc").symlink_to(stub)
    return {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "INSTALL_DIR": str(install),
        "STUB_MODE": mode,
        "STUB_LOG": str(tmp_path / "log"),
        "PUSH_ENV_FILE": str(tmp_path / "push.env"),
    }


def _finish(tmp_path: Path, kind: str, mode: str) -> subprocess.CompletedProcess:
    env = _doctor_env(tmp_path, mode)
    out = _sh(f"finish_with_doctor {kind}; echo RC=$?", env=env)
    # the closing step never fails the install
    assert out.stdout.rstrip().endswith("RC=0"), out.stdout + out.stderr
    return out


@pytest.mark.parametrize("kind", ["docker", "systemd"])
def test_doctor_all_good_closes_with_everything_working(tmp_path: Path, kind: str) -> None:
    out = _finish(tmp_path, kind, "ok")
    assert "Everything needed is working" in out.stdout  # the checklist itself is shown
    assert "everything is working" in out.stdout
    assert "need attention" not in out.stdout


def test_doctor_failures_are_counted_and_the_exact_docker_command_is_given(
    tmp_path: Path,
) -> None:
    out = _finish(tmp_path, "docker", "attention2")
    install = tmp_path / "opt-marcellus"
    assert "mqtt  refused" in out.stdout
    assert (
        f"2 checks need attention: fix them and run `cd {install} && "
        "docker compose exec marcellus fsc doctor` again"
    ) in out.stdout
    log = (tmp_path / "log").read_text()
    assert "doctor --no-color" in log  # asked for plain output


def test_doctor_systemd_command_names_the_venv_and_config(tmp_path: Path) -> None:
    out = _finish(tmp_path, "systemd", "attention1")
    install = tmp_path / "opt-marcellus"
    assert (
        f"1 check needs attention: fix it and run `sudo {install}/venv/bin/fsc doctor "
        "--config /etc/marcellus/sidecar.yml` again"
    ) in out.stdout


def test_doctor_systemd_passes_the_stored_password_through_the_environment(
    tmp_path: Path,
) -> None:
    (tmp_path / "push.env").write_text("OTHER=1\nMARCELLUS_PUSH__MQTT_PASSWORD='s3 cret'\n")
    out = _finish(tmp_path, "systemd", "ok")
    assert "PW_SEEN: yes" in (tmp_path / "log").read_text()
    assert "s3 cret" not in out.stdout + out.stderr  # never printed
    assert "bash -c" not in out.stdout  # nothing to re-run when all is well


def test_doctor_systemd_rerun_command_loads_the_push_env_file(tmp_path: Path) -> None:
    (tmp_path / "push.env").write_text("MARCELLUS_PUSH__MQTT_PASSWORD=x\n")
    out = _finish(tmp_path, "systemd", "attention1")
    assert f"set -a; . {tmp_path}/push.env;" in out.stdout


@pytest.mark.parametrize("kind", ["docker", "systemd"])
def test_doctor_absent_in_an_older_release_is_skipped_with_one_line(
    tmp_path: Path, kind: str
) -> None:
    out = _finish(tmp_path, kind, "nodoctor")
    assert "skipping the checklist: this release has no 'fsc doctor'" in out.stdout
    assert not (tmp_path / "log").exists()  # doctor was never run
    assert "need attention" not in out.stdout and "everything is working" not in out.stdout


def test_doctor_that_cannot_run_does_not_claim_success(tmp_path: Path) -> None:
    out = _finish(tmp_path, "docker", "fails_silently")
    assert "everything is working" not in out.stdout
    assert "the checklist did not finish" in out.stdout
    assert "docker compose exec marcellus fsc doctor" in out.stdout


def test_install_script_ends_both_paths_with_the_checklist() -> None:
    text = SCRIPT.read_text()
    assert "finish_with_doctor docker" in text and "finish_with_doctor systemd" in text
    assert text.index("finish_with_doctor docker") < text.index("Docker not found")
    assert text.rindex("finish_with_doctor systemd") > text.index("Docker not found")
