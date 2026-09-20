from __future__ import annotations

from pathlib import Path

import pytest

from marcellus.config import Settings, load_settings


def test_defaults_when_no_file_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(__import__("os").environ):
        if k.startswith("MARCELLUS_") or k.startswith("FRIGATE_SIDECAR_"):
            monkeypatch.delenv(k, raising=False)
    s = load_settings(config_path="/nonexistent/path.yml")
    assert s.sidecar.bind_port == 5001
    assert s.frigate.base_url.startswith("http://")


def test_yaml_overrides_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text(
        """
frigate:
  base_url: http://example.test:5000
sidecar:
  bind_port: 9999
log_level: DEBUG
"""
    )
    s = load_settings(config_path=cfg)
    assert s.frigate.base_url == "http://example.test:5000"
    assert s.sidecar.bind_port == 9999
    assert s.log_level == "DEBUG"


def test_env_overrides_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text("sidecar:\n  bind_port: 9999\n")
    monkeypatch.setenv("MARCELLUS_SIDECAR__BIND_PORT", "1234")
    s = load_settings(config_path=cfg)
    assert s.sidecar.bind_port == 1234


def test_settings_constructible_directly() -> None:
    s = Settings()
    assert s.sidecar.bind_port == 5001


def test_unknown_top_level_key_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`extra="ignore"` keeps a stale/renamed top-level key from failing
    startup, but it must still be logged so it isn't silently dropped."""
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text("nonexistent_top_level_key: 1\n")
    with caplog.at_level("WARNING", logger="marcellus.config"):
        load_settings(config_path=cfg)
    assert any(
        "nonexistent_top_level_key" in r.message for r in caplog.records
    )


def test_unknown_nested_key_warns_with_dotted_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo'd/renamed key nested under a section is walked recursively and
    reported with its dotted path (e.g. `push.nonexistent_nested_key`), not
    just flagged at the top level."""
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text(
        """
push:
  enabled: true
  nonexistent_nested_key: 5
"""
    )
    with caplog.at_level("WARNING", logger="marcellus.config"):
        s = load_settings(config_path=cfg)
    assert s.push.enabled is True  # extra="ignore" -- still loads fine
    assert any(
        "push.nonexistent_nested_key" in r.message for r in caplog.records
    )


def test_legacy_env_prefix_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-rename FRIGATE_SIDECAR_* var still takes effect when no
    MARCELLUS_* equivalent is set."""
    monkeypatch.delenv("MARCELLUS_SIDECAR__BIND_PORT", raising=False)
    monkeypatch.setenv("FRIGATE_SIDECAR_SIDECAR__BIND_PORT", "4321")
    s = load_settings(config_path="/nonexistent/path.yml")
    assert s.sidecar.bind_port == 4321


def test_new_env_prefix_wins_over_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both prefixes set the same key, MARCELLUS_* wins."""
    monkeypatch.setenv("FRIGATE_SIDECAR_SIDECAR__BIND_PORT", "4321")
    monkeypatch.setenv("MARCELLUS_SIDECAR__BIND_PORT", "1234")
    s = load_settings(config_path="/nonexistent/path.yml")
    assert s.sidecar.bind_port == 1234


def test_legacy_env_prefix_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("MARCELLUS_SIDECAR__BIND_PORT", raising=False)
    monkeypatch.delenv("MARCELLUS_LOG_LEVEL", raising=False)
    monkeypatch.setenv("FRIGATE_SIDECAR_SIDECAR__BIND_PORT", "4321")
    monkeypatch.setenv("FRIGATE_SIDECAR_LOG_LEVEL", "DEBUG")
    with caplog.at_level("WARNING", logger="marcellus.config"):
        load_settings(config_path="/nonexistent/path.yml")
    deprecation_warnings = [
        r.message for r in caplog.records if "deprecated" in r.message
    ]
    assert len(deprecation_warnings) == 1
    assert "FRIGATE_SIDECAR_SIDECAR__BIND_PORT" in deprecation_warnings[0]
    assert "FRIGATE_SIDECAR_LOG_LEVEL" in deprecation_warnings[0]


def test_known_keys_produce_no_warnings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text(
        """
frigate:
  base_url: http://example.test:5000
sidecar:
  bind_port: 9999
push:
  enabled: true
log_level: DEBUG
"""
    )
    with caplog.at_level("WARNING", logger="marcellus.config"):
        load_settings(config_path=cfg)
    unknown_key_warnings = [
        r.message for r in caplog.records if "unknown key" in r.message
    ]
    assert unknown_key_warnings == []
