"""`fsc init` finds Frigate's settings instead of asking for them.

Covers the discovery helpers (`frigate_discovery`), the env-file handling
(`init_push`) and the whole command through typer's CliRunner with scripted
stdin. Nothing touches the network, a real Frigate or a real MQTT broker; the
MQTT password used here is a made-up string whose absence from every output is
asserted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from marcellus import doctor, frigate_tls
from marcellus import frigate_discovery as fd
from marcellus import init_push as ip
from marcellus.cli import app
from marcellus.config import Settings
from marcellus.frigate_tls import SigninProbe

runner = CliRunner()
REAL_FETCH_API = fd.fetch_api  # the autouse fixture replaces fd.fetch_api
SECRET = "hunter2-Zq9"  # noqa: S105 -- a test value


# -- fixtures ---------------------------------------------------------------------------


def frigate_yaml(
    *,
    mqtt: str | None = None,
    cameras: tuple[str, ...] = ("porch", "garage"),
    extra: str = "",
) -> str:
    cams = "".join(f"  {c}:\n    ffmpeg:\n      inputs: []\n" for c in cameras)
    mqtt_block = (
        "" if mqtt is None else "mqtt:\n" + "".join(f"  {ln}\n" for ln in mqtt.splitlines())
    )
    return f"{mqtt_block}{extra}cameras:\n{cams}"


PLAIN_MQTT = f"enabled: true\nhost: mqtt.local\nport: 1883\nuser: frigate\npassword: {SECRET}\n"


def api_config(*, mqtt: dict[str, Any] | None = None, cameras=("porch", "garage"), **extra: Any):
    cfg: dict[str, Any] = {
        "cameras": {c: {} for c in cameras},
        "auth": {"enabled": True},
        "tls": {"enabled": True},
        "database": {"path": "/config/frigate.db"},
        "mqtt": mqtt if mqtt is not None else {"enabled": False},
    }
    cfg.update(extra)
    return cfg


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No network, no real search roots, no inherited Frigate/Marcellus env."""
    monkeypatch.setattr(fd, "SEARCH_ROOTS", ())
    monkeypatch.setattr(fd, "fetch_api", lambda *a, **k: (None, None))
    monkeypatch.setattr(
        frigate_tls,
        "probe_signin_port_bounded",
        lambda url, **k: SigninProbe(None, detail="stubbed"),
    )

    def no_frigate(url: str, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("test: no frigate here")

    monkeypatch.setattr(httpx, "get", no_frigate)
    for key in (
        "FRIGATE_CONFIG",
        "FRIGATE_DB_DIR",
        "FRIGATE_RECORDINGS",
        "CONFIG_FILE",
        ip.ENV_KEY,
        ip.PASSWORD_ENV_VAR,
        "MARCELLUS_CONFIG",
        "INSTALL_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ip, "running_in_container", lambda **_k: False)


class World:
    """A fake Frigate on disk, optionally answering over the API."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path / "frigate"
        self.root.mkdir()
        self.config = self.root / "config.yml"
        self.db_dir = self.root / "database"
        self.db_dir.mkdir()
        (self.db_dir / "frigate.db").write_bytes(b"")
        self.media = self.root / "media"
        (self.media / "recordings").mkdir(parents=True)
        self.tmp = tmp_path
        self.mp = monkeypatch
        monkeypatch.setattr(fd, "SEARCH_ROOTS", (str(self.root),))
        monkeypatch.setenv("FRIGATE_RECORDINGS", str(self.media))
        monkeypatch.setenv("FRIGATE_DB_DIR", str(self.db_dir))
        self.env_file = tmp_path / "push.env"

    def write(self, text: str) -> None:
        self.config.write_text(text)

    def api(self, cfg: dict[str, Any] | None, version: str = "0.18.2") -> None:
        self.mp.setattr(fd, "fetch_api", lambda *a, **k: (version, cfg))

    def init(self, *args: str, stdin: str = "") -> tuple[Any, dict[str, Any]]:
        out = self.tmp / "sidecar.yml"
        argv = [
            "init",
            "--output",
            str(out),
            "--frigate-url",
            "http://192.168.1.10:5000",
            "--proxy-url",
            "http://192.168.1.10:8971",
            "--sidecar-db",
            str(self.tmp / "marcellus.db"),
            "--bind-host",
            "127.0.0.1",
            "--bind-port",
            "5001",
            *args,
        ]
        result = runner.invoke(app, argv, input=stdin)
        cfg = yaml.safe_load(out.read_text()) if out.exists() else {}
        return result, cfg


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


@pytest.fixture
def mqtt_check(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replaces the doctor's MQTT check; records what it was given."""
    calls: list[dict[str, Any]] = []

    def fake(ctx: Any) -> doctor.CheckResult:
        calls.append(
            {
                "host": ctx.settings.push.mqtt_host,
                "user": ctx.settings.push.mqtt_username,
                "password": ctx.settings.push.mqtt_password,
                "secrets": ctx.secrets(),
            }
        )
        return doctor.CheckResult(
            "mqtt", doctor.PASS, f"connected to {ctx.settings.push.mqtt_host}:1883 ({SECRET})"
        )

    monkeypatch.setattr(doctor, "check_mqtt", fake)
    return calls


def everything(result: Any) -> str:
    return result.output + (getattr(result, "stderr", "") or "")


# -- discovery: the config file ---------------------------------------------------------


def parse(tmp_path: Path, text: str) -> fd.FrigateFacts:
    path = tmp_path / "config.yml"
    path.write_text(text)
    data, why = fd.read_config_file(path)
    assert data is not None, why
    return fd.facts_from(None, None, path, data, None)


def test_plain_mqtt_section(tmp_path: Path) -> None:
    facts = parse(tmp_path, frigate_yaml(mqtt=PLAIN_MQTT))
    m = facts.mqtt
    assert (m.enabled, m.host, m.port, m.user) == (True, "mqtt.local", 1883, "frigate")
    assert m.needs_login and not m.tls and m.prefix == "frigate"
    assert facts.cameras == 2
    assert SECRET not in repr(facts)


def test_env_substituted_password_is_detected_not_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRIGATE_MQTT_PASSWORD", SECRET)
    facts = parse(
        tmp_path,
        frigate_yaml(
            mqtt="host: '{FRIGATE_MQTT_HOST}'\nuser: frigate\npassword: '{FRIGATE_MQTT_PASSWORD}'\n"
        ),
    )
    assert facts.mqtt.needs_login
    assert facts.mqtt.host is None and "host" in facts.mqtt.unresolved
    assert SECRET not in repr(facts)


def test_mqtt_disabled(tmp_path: Path) -> None:
    facts = parse(tmp_path, frigate_yaml(mqtt="enabled: false\nhost: mqtt.local\n"))
    assert facts.mqtt.enabled is False


def test_mqtt_with_tls(tmp_path: Path) -> None:
    facts = parse(
        tmp_path,
        frigate_yaml(
            mqtt="enabled: true\nhost: mqtt.local\nport: 8883\ntls_ca_certs: /certs/ca.pem\n"
        ),
    )
    assert facts.mqtt.tls and facts.mqtt.port == 8883
    assert not facts.mqtt.needs_login


def test_no_mqtt_section(tmp_path: Path) -> None:
    facts = parse(tmp_path, frigate_yaml())
    assert facts.mqtt.enabled is False and facts.mqtt.host is None


def test_non_default_topic_prefix_and_client_id(tmp_path: Path) -> None:
    facts = parse(
        tmp_path,
        frigate_yaml(mqtt="host: m\ntopic_prefix: nvr\nclient_id: cam-box\n"),
    )
    assert facts.mqtt.prefix == "nvr" and facts.mqtt.client_id == "cam-box"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_unreadable_file_is_noted_and_skipped(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(frigate_yaml(mqtt=PLAIN_MQTT))
    path.chmod(0)
    try:
        found, data, _verified, notes = fd.find_config_file(None, hint=path, roots=())
    finally:
        path.chmod(0o644)
    assert found is None and data is None
    assert any("permission denied" in n for n in notes)


def test_invalid_yaml_never_quotes_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(f"mqtt:\n  password: {SECRET}\n  bad: [unclosed\n")
    data, why = fd.read_config_file(path)
    assert data is None
    assert "not valid YAML" in why and SECRET not in why


def test_candidates_search_common_roots_and_directories(tmp_path: Path) -> None:
    (tmp_path / "a" / "config").mkdir(parents=True)
    cfg = tmp_path / "a" / "config" / "config.yml"
    cfg.write_text(frigate_yaml())
    got = fd.candidate_config_paths(roots=[str(tmp_path / "a")], environ={})
    assert cfg in got
    # A directory given as the hint is looked into.
    assert fd.candidate_config_paths(tmp_path / "a" / "config", roots=(), environ={}) == [cfg][
        :1
    ] + [tmp_path / "a" / "config" / "config.yaml"]


# -- discovery: API versus file ---------------------------------------------------------


def test_file_matching_the_running_frigate_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text(frigate_yaml(mqtt=PLAIN_MQTT))
    path, _, verified, _ = fd.find_config_file(api_config(), roots=[str(tmp_path)])
    assert path == tmp_path / "config.yml" and verified is True


def test_file_of_another_frigate_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text(frigate_yaml(cameras=("other",)))
    path, _, _, notes = fd.find_config_file(api_config(), roots=[str(tmp_path)])
    assert path is None
    assert any("do not match the running Frigate" in n for n in notes)


def test_explicit_hint_is_used_even_when_it_does_not_match(tmp_path: Path) -> None:
    cfg = tmp_path / "mine.yml"
    cfg.write_text(frigate_yaml(cameras=("other",)))
    path, _, verified, _ = fd.find_config_file(api_config(), hint=cfg, roots=())
    assert path == cfg and verified is False


def test_api_values_win_and_the_file_fills_what_the_api_lacks(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(
        frigate_yaml(
            mqtt="host: '{FRIGATE_MQTT_HOST}'\nuser: from-file\nport: 1111\nclient_id: file-id\n"
            "password: '{FRIGATE_MQTT_PASSWORD}'\n"
        )
    )
    file_cfg, _ = fd.read_config_file(path)
    api = api_config(
        mqtt={
            "enabled": True,
            "host": "10.0.0.5",
            "port": 1883,
            "user": "from-api",
            "password": "*****",
        }
    )
    facts = fd.facts_from("0.18.2", api, path, file_cfg, True)
    m = facts.mqtt
    assert (m.host, m.port, m.user) == ("10.0.0.5", 1883, "from-api")  # API first
    assert m.client_id == "file-id"  # the API had none: the file fills in
    assert m.needs_login and not m.unresolved  # the API resolved the host placeholder
    assert facts.auth_enabled is True and facts.tls_enabled is True
    assert facts.db_path == "/config/frigate.db"


def test_api_alone_is_enough(tmp_path: Path) -> None:
    facts = fd.facts_from(
        "0.18.2",
        api_config(mqtt={"enabled": True, "host": "m", "port": 1883}),
        None, None, None,
    )
    assert facts.found_anything and facts.mqtt.host == "m" and facts.cameras == 2


def test_fetch_api_parses_version_and_config(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **_kw: object) -> httpx.Response:
        req = httpx.Request("GET", url)
        if url.endswith("/api/version"):
            return httpx.Response(200, text="0.18.2-abc", request=req)
        return httpx.Response(200, json=api_config(), request=req)

    monkeypatch.setattr(httpx, "get", fake_get)
    version, cfg = REAL_FETCH_API("http://x:5000")
    assert version == "0.18.2-abc" and cfg is not None and "porch" in cfg["cameras"]


def test_fetch_api_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert REAL_FETCH_API("http://x:5000") == (None, None)


def test_fetch_api_version_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **_kw: object) -> httpx.Response:
        req = httpx.Request("GET", url)
        if url.endswith("/api/version"):
            return httpx.Response(200, text="0.18.2", request=req)
        return httpx.Response(401, text="no", request=req)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert REAL_FETCH_API("http://x:5000") == ("0.18.2", None)


# -- discovery: host paths --------------------------------------------------------------


def test_media_root_comes_from_a_recording_path() -> None:
    assert (
        fd.media_root_of("/media/frigate/recordings/2026-01-01/10/cam/00.00.mp4")
        == "/media/frigate"
    )
    assert fd.media_root_of("/data/frigate/recordings/x.mp4") == "/data/frigate"
    assert fd.media_root_of("/elsewhere/x.mp4") is None


def test_recordings_root_checks(tmp_path: Path) -> None:
    media = tmp_path / "media"
    (media / "recordings" / "2026-01-01").mkdir(parents=True)
    assert fd.check_recordings_root(media).status == "ok"
    # Handing over the recordings folder itself is corrected to its parent.
    res = fd.check_recordings_root(media / "recordings")
    assert res.status == "ok" and res.path == media and "using the folder above" in res.message
    assert fd.check_recordings_root(tmp_path / "gone").status == "bad"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert fd.check_recordings_root(empty).status == "warn"
    other = tmp_path / "other"
    other.mkdir()
    (other / "notes.txt").write_text("x")
    assert fd.check_recordings_root(other).status == "warn"


def test_host_db_candidates_follow_the_config_location(tmp_path: Path) -> None:
    cfg = tmp_path / "frigate" / "config.yml"
    got = fd.host_db_candidates(cfg, "/config/frigate.db", environ={})
    assert got[0] == tmp_path / "frigate" / "frigate.db"
    assert tmp_path / "frigate" / "database" / "frigate.db" in got


# -- env file ---------------------------------------------------------------------------


def test_env_value_quoting() -> None:
    assert ip.format_env_value("abc-123_X") == "abc-123_X"
    assert ip.format_env_value("has space#and$") == "'has space#and$'"
    for bad in ("it's", "line\nbreak", ""):
        with pytest.raises(ip.EnvFileError) as exc:
            ip.format_env_value(bad)
        assert bad.strip() == "" or bad not in str(exc.value)


def test_write_secret_creates_private_file_and_keeps_other_lines(tmp_path: Path) -> None:
    path = tmp_path / "push.env"
    old = os.umask(0o022)
    try:
        ip.write_secret(path, SECRET, private=True)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == f"{ip.ENV_KEY}={SECRET}\n"
    path.write_text(f"A=1\n#{ip.ENV_KEY}=old\n{ip.ENV_KEY}=old\n{ip.ENV_KEY}=dup\nB=2\n")
    path.chmod(0o644)
    ip.write_secret(path, "new-pw", private=True)
    assert path.read_text() == f"A=1\n#{ip.ENV_KEY}=old\n{ip.ENV_KEY}=new-pw\nB=2\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # tightened
    assert ip.read_secret(path) == "new-pw"


def test_write_secret_leaves_an_existing_dotenv_mode_alone(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("FRIGATE_CONFIG=/x\n")
    path.chmod(0o640)
    ip.write_secret(path, "pw1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_target_detection(tmp_path: Path) -> None:
    explicit = ip.detect_target("/some/file.env", in_container=True)
    assert explicit.kind == "file" and explicit.path == Path("/some/file.env")
    boxed = ip.detect_target(None, install_dir="/opt/marcellus", in_container=True)
    assert boxed.kind == "docker" and boxed.path is None
    assert boxed.host_path == Path("/opt/marcellus/.env")
    (tmp_path / "docker-compose.yml").write_text("")
    host_docker = ip.detect_target(
        None,
        install_dir=str(tmp_path),
        in_container=False,
        exists=lambda p: p == str(tmp_path / "docker-compose.yml"),
    )
    assert host_docker.kind == "docker" and host_docker.path == tmp_path / ".env"
    bare = ip.detect_target(None, install_dir=str(tmp_path / "none"), in_container=False)
    assert bare.kind == "systemd" and bare.path == Path("/etc/marcellus-push.env")


def test_systemd_target_needs_root(tmp_path: Path) -> None:
    target = ip.EnvTarget("systemd", tmp_path / "push.env", tmp_path / "push.env")
    assert ip.can_store(target, is_root=True) and not ip.can_store(target, is_root=False)
    assert ip.can_store(ip.EnvTarget("file", tmp_path / "x.env"), is_root=False)
    assert not ip.can_store(ip.EnvTarget("docker", None, Path("/opt/marcellus/.env")))


def test_host_commands_hold_no_secret() -> None:
    cmds = ip.host_commands(
        ip.EnvTarget("systemd", None, Path("/etc/marcellus-push.env")), "RESTART"
    )
    text = "\n".join(cmds)
    assert "read -rsp" in text and ip.ENV_KEY in text and "0600" in text and "RESTART" in text


# -- the whole command ------------------------------------------------------------------


def test_non_interactive_uses_everything_found_and_says_what_it_assumed(world: World) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(
        api_config(mqtt={"enabled": True, "host": "mqtt.local", "port": 1883, "user": "frigate"})
    )
    result, cfg = world.init("--non-interactive")
    assert result.exit_code == 0, result.output
    assert (
        "Frigate 0.18.2 at http://192.168.1.10:5000, 2 cameras, sign-in on, TLS on" in result.output
    )
    assert "MQTT: broker at mqtt.local:1883 as user frigate" in result.output
    assert f"config file: {world.config} (matches the running Frigate)" in result.output
    assert "assumed" in result.output
    assert cfg["frigate"]["config_path"] == str(world.config)
    assert cfg["frigate"]["db_path"] == str(world.db_dir / "frigate.db")
    assert cfg["frigate"]["recordings_path"] == str(world.media)
    assert "push" not in cfg  # no --enable-push
    assert "push alerts: not set up" in result.output
    Settings.model_validate(cfg)


def test_interactive_confirm_accepts_the_found_values(world: World) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(
        api_config(mqtt={"enabled": True, "host": "mqtt.local", "port": 1883, "user": "frigate"})
    )
    # "Use these values?" -> Enter (yes); "Set up push alerts now?" -> Enter (no)
    result, cfg = world.init(stdin="\n\n")
    assert result.exit_code == 0, result.output
    assert "Use these values? [Y/n]" in result.output
    assert "Set up push alerts now? [y/N]" in result.output
    assert cfg["frigate"]["config_path"] == str(world.config)
    assert "push" not in cfg


def test_interactive_edit_walks_through_each_value(world: World) -> None:
    world.write(frigate_yaml())
    world.api(api_config())
    other = world.tmp / "elsewhere"
    (other / "recordings").mkdir(parents=True)
    # n -> edit: config path, db path, recordings, sidecar db, bind host, bind port
    answers = f"n\n\n\n{other}\n\n\n\nn\n"
    result, cfg = world.init(stdin=answers)
    assert result.exit_code == 0, result.output
    assert cfg["frigate"]["recordings_path"] == str(other)
    assert cfg["frigate"]["config_path"] == str(world.config)  # Enter kept the found value


def test_recordings_folder_given_instead_of_its_parent_is_corrected(world: World) -> None:
    world.write(frigate_yaml())
    result, cfg = world.init(
        "--non-interactive", "--recordings-path", str(world.media / "recordings")
    )
    assert result.exit_code == 0, result.output
    assert cfg["frigate"]["recordings_path"] == str(world.media)
    assert "using the folder above it" in result.output


def test_flags_beat_everything_found(world: World) -> None:
    world.write(frigate_yaml())
    result, cfg = world.init("--non-interactive", "--frigate-db", "/flag/frigate.db")
    assert cfg["frigate"]["db_path"] == "/flag/frigate.db"


def test_nothing_found_falls_back_to_asking_as_before(world: World, monkeypatch) -> None:
    monkeypatch.setattr(fd, "SEARCH_ROOTS", ())
    monkeypatch.delenv("FRIGATE_RECORDINGS")
    monkeypatch.delenv("FRIGATE_DB_DIR")
    gone = world.tmp / "nothing"
    # config, db, media (missing: "use it anyway" y), sidecar defaults, push no
    answers = f"{gone}/config.yml\n{gone}/frigate.db\n{gone}/media\ny\n\nn\n"
    result, cfg = world.init(stdin=answers)
    assert result.exit_code == 0, result.output
    assert "Found Frigate's settings" not in result.output
    assert cfg["frigate"]["config_path"] == f"{gone}/config.yml"


def test_init_never_writes_to_frigates_files(world: World, mqtt_check: list) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(
        api_config(mqtt={"enabled": True, "host": "mqtt.local", "port": 1883, "user": "frigate"})
    )

    def snapshot() -> dict[str, tuple[str, float]]:
        out = {}
        for p in sorted(world.root.rglob("*")):
            if p.is_file():
                out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime)
        return out

    before = snapshot()
    listing = sorted(str(p) for p in world.root.rglob("*"))
    result, _ = world.init(
        "--non-interactive",
        "--enable-push",
        "--env-file",
        str(world.env_file),
        "--mqtt-password-file",
        str(_pwfile(world)),
    )
    assert result.exit_code == 0, result.output
    assert snapshot() == before
    assert sorted(str(p) for p in world.root.rglob("*")) == listing


def _pwfile(world: World, value: str = SECRET) -> Path:
    path = world.tmp / "pw.txt"
    path.write_text(value + "\n")
    return path


# -- push ---------------------------------------------------------------------------------

BROKER_API = {
    "enabled": True,
    "host": "mqtt.local",
    "port": 1883,
    "user": "frigate",
    "password": "***",
}


def test_interactive_push_stores_password_only_in_the_env_file(
    world: World, mqtt_check: list, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    # confirm values, set up push, use the broker, hidden password
    result, cfg = world.init("--env-file", str(world.env_file), stdin=f"\ny\ny\n{SECRET}\n")
    assert result.exit_code == 0, result.output
    assert cfg["push"] == {
        "enabled": True,
        "mqtt_host": "mqtt.local",
        "mqtt_port": 1883,
        "mqtt_username": "frigate",
    }
    # the password is in the env file, with the right mode, and nowhere else
    assert world.env_file.read_text() == f"{ip.ENV_KEY}={SECRET}\n"
    assert stat.S_IMODE(world.env_file.stat().st_mode) == 0o600
    assert SECRET not in (world.tmp / "sidecar.yml").read_text()
    assert SECRET not in everything(result)
    assert SECRET not in caplog.text
    # the connection test got it, via the doctor's own check
    assert mqtt_check and mqtt_check[0]["password"] == SECRET and mqtt_check[0]["user"] == "frigate"
    assert "✓ MQTT: connected to mqtt.local:1883" in result.output
    assert "***" in result.output  # the doctor's scrubbing masked the echoed secret


def test_password_prompt_is_hidden(world: World, mqtt_check: list) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    result, _ = world.init("--env-file", str(world.env_file), stdin=f"\ny\ny\n{SECRET}\n")
    assert SECRET not in result.output  # not echoed back as typed


def test_push_refused_when_frigate_mqtt_is_off(world: World, mqtt_check: list) -> None:
    world.write(frigate_yaml(mqtt="enabled: false\nhost: mqtt.local\n"))
    world.api(api_config(mqtt={"enabled": False}))
    result, cfg = world.init(
        "--non-interactive", "--enable-push", "--env-file", str(world.env_file)
    )
    assert result.exit_code == 0
    assert "MQTT is off in Frigate" in result.output
    assert "push" not in cfg and not world.env_file.exists() and not mqtt_check


def test_non_interactive_push_needs_a_password_source(world: World, mqtt_check: list) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    result, cfg = world.init(
        "--non-interactive", "--enable-push", "--env-file", str(world.env_file)
    )
    assert result.exit_code == 0
    assert "no password was given" in result.output and "Skipping push" in result.output
    assert "push" not in cfg and not world.env_file.exists()


def test_non_interactive_password_file(world: World, mqtt_check: list) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    result, cfg = world.init(
        "--non-interactive",
        "--enable-push",
        "--env-file",
        str(world.env_file),
        "--mqtt-password-file",
        str(_pwfile(world)),
    )
    assert result.exit_code == 0, result.output
    assert cfg["push"]["enabled"] is True
    assert ip.read_secret(world.env_file) == SECRET
    assert (
        SECRET not in everything(result) and SECRET not in (world.tmp / "sidecar.yml").read_text()
    )


def test_non_interactive_password_from_environment(
    world: World, mqtt_check: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    monkeypatch.setenv(ip.PASSWORD_ENV_VAR, SECRET)
    result, _ = world.init("--non-interactive", "--enable-push", "--env-file", str(world.env_file))
    assert result.exit_code == 0, result.output
    assert ip.read_secret(world.env_file) == SECRET


def test_anonymous_broker_needs_no_password(world: World, mqtt_check: list) -> None:
    world.write(frigate_yaml(mqtt="enabled: true\nhost: mqtt.local\n"))
    world.api(api_config(mqtt={"enabled": True, "host": "mqtt.local", "port": 1883}))
    result, cfg = world.init(
        "--non-interactive", "--enable-push", "--env-file", str(world.env_file)
    )
    assert result.exit_code == 0, result.output
    assert cfg["push"] == {"enabled": True, "mqtt_host": "mqtt.local", "mqtt_port": 1883}
    assert not world.env_file.exists()
    assert mqtt_check and mqtt_check[0]["password"] is None


def test_non_default_topic_prefix_is_carried_over(world: World, mqtt_check: list) -> None:
    world.write(frigate_yaml(mqtt="host: m\ntopic_prefix: nvr\n"))
    world.api(api_config(mqtt={"enabled": True, "host": "m", "port": 1883, "topic_prefix": "nvr"}))
    result, cfg = world.init("--non-interactive", "--enable-push")
    assert cfg["push"]["mqtt_topic_reviews"] == "nvr/reviews"
    assert cfg["push"]["mqtt_topic_available"] == "nvr/available"


def test_tls_broker_is_warned_about(world: World, mqtt_check: list) -> None:
    world.write(frigate_yaml(mqtt="host: m\nport: 8883\ntls_ca_certs: /ca.pem\n"))
    result, _ = world.init("--non-interactive", "--enable-push")
    assert "Frigate talks TLS to its broker" in everything(result)


def test_not_root_prints_commands_instead_of_failing(
    world: World, mqtt_check: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    target = world.tmp / "etc" / "marcellus-push.env"
    monkeypatch.setattr(ip, "SYSTEMD_ENV_FILE", target)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    result, cfg = world.init(
        "--non-interactive", "--enable-push", "--mqtt-password-file", str(_pwfile(world))
    )
    assert result.exit_code == 0, result.output
    assert not target.exists()
    assert "was not stored (this is not root" in result.output
    assert "read -rsp" in result.output and str(target) in result.output
    assert SECRET not in everything(result)
    assert cfg["push"]["enabled"] is True
    # it still tested the connection with the password it was given
    assert mqtt_check and mqtt_check[0]["password"] == SECRET


def test_inside_the_container_prints_host_commands(
    world: World, mqtt_check: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    monkeypatch.setattr(ip, "running_in_container", lambda **_k: True)
    # interactive: it must not even ask for a password it cannot store
    result, cfg = world.init(stdin="\ny\ny\n")
    assert result.exit_code == 0, result.output
    assert "password was not stored" in result.output
    assert "/opt/marcellus/.env" in result.output and "docker compose up -d" in result.output
    assert "MQTT password (typing is hidden" not in result.output
    assert "not tested" in result.output and not mqtt_check
    assert cfg["push"]["mqtt_host"] == "mqtt.local"


def test_a_crash_in_the_connection_test_never_leaks_the_password(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(ctx: Any) -> None:
        raise RuntimeError(f"broker said no to {SECRET}")

    monkeypatch.setattr(doctor, "check_mqtt", boom)
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    result, _ = world.init(
        "--non-interactive",
        "--enable-push",
        "--env-file",
        str(world.env_file),
        "--mqtt-password-file",
        str(_pwfile(world)),
    )
    assert result.exit_code == 0
    assert "could not run (RuntimeError)" in everything(result)
    assert SECRET not in everything(result)


# -- re-running ---------------------------------------------------------------------------


def _existing(world: World) -> Path:
    path = world.tmp / "sidecar.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "frigate": {"base_url": "http://192.168.1.10:5000", "db_path": "/kept/frigate.db"},
                "scrub": {"enabled": True},
                "push": {"enabled": True, "mqtt_host": "old.local", "relay_key": "rk"},
            }
        )
    )
    return path


def test_rerun_keeps_other_settings_and_offers_existing_values(world: World) -> None:
    _existing(world)
    result, cfg = world.init("--force", "--non-interactive")
    assert result.exit_code == 0, result.output
    assert cfg["scrub"] == {"enabled": True}  # not ours: kept
    assert cfg["frigate"]["db_path"] == "/kept/frigate.db"  # existing beats a guess
    assert cfg["push"]["relay_key"] == "rk" and cfg["push"]["mqtt_host"] == "old.local"
    assert "push alerts: keeping the settings already in the config" in result.output


def test_rerun_refuses_without_force(world: World) -> None:
    path = _existing(world)
    before = path.read_text()
    result, _ = world.init("--non-interactive")
    assert result.exit_code == 1 and "--force" in result.output
    assert path.read_text() == before


def test_existing_env_password_is_not_overwritten_without_confirmation(
    world: World, mqtt_check: list
) -> None:
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    world.env_file.write_text(f"KEEP=1\n{ip.ENV_KEY}=old-password\n")
    # non-interactive with a new password: kept
    result, _ = world.init(
        "--non-interactive",
        "--enable-push",
        "--env-file",
        str(world.env_file),
        "--mqtt-password-file",
        str(_pwfile(world)),
    )
    assert ip.read_secret(world.env_file) == "old-password"
    assert "keeping it (not overwritten)" in result.output
    assert mqtt_check[-1]["password"] == "old-password"
    (world.tmp / "sidecar.yml").unlink()
    # interactive, answer "no" to replacing: kept
    result, _ = world.init("--env-file", str(world.env_file), stdin="\ny\ny\nn\n")
    assert "A password is already stored in" in result.output
    assert ip.read_secret(world.env_file) == "old-password"
    (world.tmp / "sidecar.yml").unlink()
    # interactive, answer "yes" and type a new one: replaced, other lines kept
    result, _ = world.init("--env-file", str(world.env_file), stdin=f"\ny\ny\ny\n{SECRET}\n")
    assert ip.read_secret(world.env_file) == SECRET
    assert world.env_file.read_text().startswith("KEEP=1\n")
    assert SECRET not in everything(result)


def test_password_already_in_the_process_environment_is_kept(
    world: World, mqtt_check: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ip.ENV_KEY, "from-compose")
    world.write(frigate_yaml(mqtt=PLAIN_MQTT))
    world.api(api_config(mqtt=BROKER_API))
    result, _ = world.init("--non-interactive", "--enable-push", "--env-file", str(world.env_file))
    assert result.exit_code == 0, result.output
    assert not world.env_file.exists()
    assert mqtt_check[0]["password"] == "from-compose"
    assert "from-compose" not in everything(result)


# -- help text ----------------------------------------------------------------------------


def test_init_help_lists_the_new_options() -> None:
    env = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    result = runner.invoke(app, ["init", "--help"], env=env)
    text = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for flag in ("--enable-push", "--mqtt-password-file", "--env-file", "--non-interactive"):
        assert flag in text
