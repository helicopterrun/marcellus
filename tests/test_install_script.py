"""install.sh: the legacy (frigate-sidecar) bare-metal detection."""

from __future__ import annotations

import os
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
        env=env, capture_output=True, text=True, check=False,
    )
    assert "systemctl stop frigate-sidecar" in out.stdout
    assert "ALLOW_LEGACY_COEXIST=1" in out.stdout
