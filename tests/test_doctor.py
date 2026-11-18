"""`marcellus doctor`: every check's pass / warn / fail with fakes (no network, no
broker, no real Frigate), the hard time limit, check independence, exit codes,
the pinned `--json` shape, secrets staying out of the output, and one run of
the whole command against a temp config."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from marcellus import doctor
from marcellus.cli import app
from marcellus.config import Settings
from marcellus.doctor import (
    FAIL,
    PASS,
    SKIPPED,
    WARN,
    CheckResult,
    Context,
)

NGINX_400 = (
    "<html><head><title>400 The plain HTTP request was sent to HTTPS port</title></head>"
    "<body><center>The plain HTTP request was sent to HTTPS port</center></body></html>"
)
SECRET_PW = "hunter2-broker-password"
SECRET_KEY = "relay-key-4b1c9e"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """`load_settings` copies legacy env vars into os.environ for good, so other
    test modules can leave MARCELLUS_* behind; none of it may reach these tests."""
    for key in list(os.environ):
        if key.startswith(("MARCELLUS_", "FRIGATE_SIDECAR_")):
            monkeypatch.delenv(key)


# -- fakes -----------------------------------------------------------------------


def _resp(status: int, text: str = "", url: str = "http://x/") -> httpx.Response:
    return httpx.Response(status, text=text, request=httpx.Request("GET", url))


class FakeFrigate:
    """`frigate_get` over a URL-suffix table of Response or exception."""

    def __init__(self, table: dict[str, httpx.Response | Exception]) -> None:
        self.table = table
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, url: str, *, verify_tls: bool, timeout: float) -> httpx.Response:
        self.calls.append((url, verify_tls))
        for key, value in self.table.items():
            if url.endswith(key) or key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise httpx.ConnectError("connection refused")


def _self_signed() -> httpx.ConnectError:
    cause = Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    err = httpx.ConnectError(str(cause), request=httpx.Request("GET", "https://x:8971/"))
    err.__cause__ = cause
    return err


def make_frigate_db(path: Path, *, rows: list[tuple[str, float]] | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE event (id TEXT)")
    conn.execute("CREATE TABLE reviewsegment (id TEXT)")
    conn.execute("CREATE TABLE recordings (id TEXT, path TEXT, start_time REAL)")
    for i, (p, t) in enumerate(rows or []):
        conn.execute("INSERT INTO recordings VALUES (?, ?, ?)", (str(i), p, t))
    conn.commit()
    conn.close()


def make_settings(tmp_path: Path, **over: Any) -> Settings:
    cfg: dict[str, Any] = {
        "frigate": {
            "base_url": "http://frigate.test:5000",
            "proxy_base_url": "https://frigate.test:8971",
            "verify_tls": False,
            "db_path": str(tmp_path / "frigate.db"),
            "recordings_path": str(tmp_path / "media"),
        },
        "sidecar": {
            "db_path": str(tmp_path / "side.db"),
            "bind_host": "0.0.0.0",
            "bind_port": 5001,
        },
        "scrub": {"cache_dir": str(tmp_path / "scrub"), "enabled": True},
        "push": {"enabled": False},
    }
    for key, value in over.items():
        cfg.setdefault(key, {}).update(value)
    return Settings.model_validate(cfg)


def make_ctx(tmp_path: Path, **over: Any) -> Context:
    ctx_over = over.pop("ctx", {})
    settings = make_settings(tmp_path, **over)
    ctx = Context(
        settings=settings,
        timeout=1.0,
        mqtt_wait=0.3,
        frigate_get=FakeFrigate({}),
        plain_get=lambda url, timeout: (_ for _ in ()).throw(OSError("no route")),
        run=lambda cmd, timeout: None,
        which=lambda name: None,
        can_read=lambda p: True,
        current_user=lambda: "tester",
    )
    for key, value in ctx_over.items():
        setattr(ctx, key, value)
    return ctx


def one(result_fn: Any, ctx: Context) -> CheckResult:
    return result_fn(ctx)  # type: ignore[no-any-return]


# -- config ----------------------------------------------------------------------


def test_config_passes(tmp_path: Path) -> None:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text("frigate:\n  base_url: http://f:5000\n")
    settings, res, path = doctor.load_config(str(cfg))
    assert settings is not None and path == cfg
    assert res.status == PASS and str(cfg) in res.detail


def test_config_missing_explicit_file_fails(tmp_path: Path) -> None:
    settings, res, _ = doctor.load_config(str(tmp_path / "nope.yml"))
    assert settings is None and res.status == FAIL
    assert "does not exist" in res.detail and res.fix


def test_config_honours_marcellus_config_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "env.yml"
    cfg.write_text("log_level: DEBUG\n")
    monkeypatch.setenv("MARCELLUS_CONFIG", str(cfg))
    settings, res, path = doctor.load_config(None)
    assert path == cfg and settings is not None and res.status == PASS


def test_config_validation_error_names_key_not_value(tmp_path: Path) -> None:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text(f"sidecar:\n  bind_port: not-a-number-{SECRET_PW}\n")
    settings, res, _ = doctor.load_config(str(cfg))
    assert settings is None and res.status == FAIL
    assert "sidecar.bind_port" in res.detail
    assert SECRET_PW not in res.detail + res.fix


def test_config_yaml_error_has_line_and_no_content(tmp_path: Path) -> None:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text(f"push:\n  mqtt_password: {SECRET_PW}\n bad: [unclosed\n")
    _, res, _ = doctor.load_config(str(cfg))
    assert res.status == FAIL and "line" in res.detail
    assert SECRET_PW not in res.detail + res.fix


def test_config_unknown_key_warns(tmp_path: Path) -> None:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text("frigate:\n  bsae_url: http://x\n")
    settings, res, _ = doctor.load_config(str(cfg))
    assert settings is not None and res.status == WARN
    assert "frigate.bsae_url" in res.detail


def test_config_no_file_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARCELLUS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("marcellus.config.DEFAULT_CONFIG_PATHS", ("./nothing.yml",))
    settings, res, path = doctor.load_config(None)
    assert settings is not None and path is None and res.status == WARN


# -- frigate-api / signin / urls --------------------------------------------------


def test_frigate_api_pass_shows_version(tmp_path: Path) -> None:
    fake = FakeFrigate({"/api/version": _resp(200, "0.18.2")})
    res = doctor.check_frigate_api(make_ctx(tmp_path, ctx={"frigate_get": fake}))
    assert res.status == PASS and "0.18.2" in res.detail


def test_frigate_api_unreachable_fails_with_fix(tmp_path: Path) -> None:
    res = doctor.check_frigate_api(make_ctx(tmp_path))
    assert res.status == FAIL and "frigate.base_url" in res.fix


def test_frigate_api_passes_verify_tls_setting(tmp_path: Path) -> None:
    fake = FakeFrigate({"/api/version": _resp(200, "0.18.2")})
    doctor.check_frigate_api(make_ctx(tmp_path, ctx={"frigate_get": fake}))
    assert fake.calls and fake.calls[0][1] is False


def test_frigate_api_non_200_fails(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, ctx={"frigate_get": FakeFrigate({"/api/version": _resp(404)})})
    assert doctor.check_frigate_api(ctx).status == FAIL


def test_signin_untrusted_certificate_reuses_tls_diagnosis(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        frigate={"verify_tls": True},
        ctx={"frigate_get": FakeFrigate({"8971": _self_signed()})},
    )
    res = doctor.check_frigate_signin(ctx)
    assert res.status == FAIL and "tls untrusted" in res.detail
    assert "frigate.verify_tls: false" in res.fix and "\n" not in res.fix


def test_signin_http_to_https_port_reuses_diagnosis(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        frigate={"proxy_base_url": "http://frigate.test:8971"},
        ctx={"frigate_get": FakeFrigate({"8971": _resp(400, NGINX_400)})},
    )
    res = doctor.check_frigate_signin(ctx)
    assert res.status == FAIL and "https required" in res.detail
    assert "https://" in res.fix


def test_signin_on_passes_and_says_so(tmp_path: Path) -> None:
    fake = FakeFrigate(
        {"8971/api/version": _resp(401), "5000/api/config": _resp(200, '{"auth":{"enabled":true}}')}
    )
    res = doctor.check_frigate_signin(make_ctx(tmp_path, ctx={"frigate_get": fake}))
    assert res.status == PASS and "sign-in is ON" in res.detail


def test_signin_off_passes_and_says_so(tmp_path: Path) -> None:
    fake = FakeFrigate(
        {
            "8971/api/version": _resp(200, "0.18.2"),
            "5000/api/config": _resp(200, '{"auth":{"enabled":false}}'),
        }
    )
    res = doctor.check_frigate_signin(make_ctx(tmp_path, ctx={"frigate_get": fake}))
    assert res.status == PASS and "OFF" in res.detail


def test_signin_origin_open_but_frigate_signin_on_warns(tmp_path: Path) -> None:
    fake = FakeFrigate(
        {
            "8971/api/version": _resp(200, "0.18.2"),
            "5000/api/config": _resp(200, '{"auth":{"enabled":true}}'),
        }
    )
    res = doctor.check_frigate_signin(make_ctx(tmp_path, ctx={"frigate_get": fake}))
    assert res.status == WARN and "8971" in res.fix


def test_signin_unreachable_fails(tmp_path: Path) -> None:
    res = doctor.check_frigate_signin(make_ctx(tmp_path))
    assert res.status == FAIL and "frigate.proxy_base_url" in res.fix


def test_urls_differ(tmp_path: Path) -> None:
    assert doctor.check_urls_differ(make_ctx(tmp_path)).status == PASS
    same = make_ctx(
        tmp_path,
        frigate={"base_url": "http://f:5000", "proxy_base_url": "http://f:5000/"},
    )
    res = doctor.check_urls_differ(same)
    assert res.status == WARN and "8971" in res.fix


# -- frigate-db ------------------------------------------------------------------


def test_db_passes(tmp_path: Path) -> None:
    make_frigate_db(tmp_path / "frigate.db")
    res = doctor.check_frigate_db(make_ctx(tmp_path))
    assert res.status == PASS


def test_db_missing_fails(tmp_path: Path) -> None:
    res = doctor.check_frigate_db(make_ctx(tmp_path))
    assert res.status == FAIL and "frigate.db_path" in res.fix


def test_db_wrong_tables_fails(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "frigate.db")
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    res = doctor.check_frigate_db(make_ctx(tmp_path))
    assert res.status == FAIL and "event" in res.detail


def test_db_not_sqlite_fails(tmp_path: Path) -> None:
    (tmp_path / "frigate.db").write_text("this is not a database" * 50)
    assert doctor.check_frigate_db(make_ctx(tmp_path)).status == FAIL


def test_db_unreadable_names_group_and_exact_usermod(tmp_path: Path) -> None:
    import grp

    db_file = tmp_path / "frigate.db"
    make_frigate_db(db_file)
    group = grp.getgrgid(db_file.stat().st_gid).gr_name
    ctx = make_ctx(tmp_path, ctx={"can_read": lambda p: False, "current_user": lambda: "svc"})
    res = doctor.check_frigate_db(ctx)
    assert res.status == FAIL
    assert f"sudo usermod -aG {group} svc" in res.fix
    assert "owner" in res.detail and "group" in res.detail


def test_db_service_user_differs_is_reported(tmp_path: Path) -> None:
    db_file = tmp_path / "frigate.db"
    make_frigate_db(db_file)
    os.chmod(db_file, 0o600)
    ctx = make_ctx(
        tmp_path,
        ctx={
            "which": lambda n: "/bin/systemctl" if n == "systemctl" else None,
            "run": lambda cmd, t: (0, "nobody\n"),
            "current_user": lambda: "tester",
        },
    )
    res = doctor.check_frigate_db(ctx)
    assert res.status == FAIL
    assert "service user nobody" in res.detail and "running as tester" in res.detail
    assert "usermod -aG" in res.fix and "nobody" in res.fix
    assert "chmod g+r" in res.fix  # mode 600: group membership alone would not help


def test_db_service_user_differs_but_can_read_passes_with_note(tmp_path: Path) -> None:
    db_file = tmp_path / "frigate.db"
    make_frigate_db(db_file)
    os.chmod(db_file, 0o644)
    ctx = make_ctx(
        tmp_path,
        ctx={
            "which": lambda n: "/bin/systemctl" if n == "systemctl" else None,
            "run": lambda cmd, t: (0, "nobody\n"),
        },
    )
    res = doctor.check_frigate_db(ctx)
    assert res.status == PASS and "service runs as nobody" in res.detail


# -- recordings ------------------------------------------------------------------


def _recording(tmp_path: Path, age_s: float = 30.0, *, create: bool = True) -> None:
    media = tmp_path / "media"
    seg = media / "recordings" / "2026-11-18" / "10" / "cam"
    if create:
        seg.mkdir(parents=True)
        (seg / "00.00.mp4").write_bytes(b"x")
    make_frigate_db(
        tmp_path / "frigate.db",
        rows=[("/media/frigate/recordings/2026-11-18/10/cam/00.00.mp4", time.time() - age_s)],
    )


def test_recordings_match_frigate(tmp_path: Path) -> None:
    _recording(tmp_path)
    res = doctor.check_recordings(make_ctx(tmp_path))
    assert res.status == PASS and "matches" in res.detail


def test_recordings_dir_missing_fails(tmp_path: Path) -> None:
    res = doctor.check_recordings(make_ctx(tmp_path))
    assert res.status == FAIL and "frigate.recordings_path" in res.fix


def test_recordings_mapping_miss_fails_with_explanation(tmp_path: Path) -> None:
    (tmp_path / "media").mkdir()
    _recording(tmp_path, create=False)
    res = doctor.check_recordings(make_ctx(tmp_path))
    assert res.status == FAIL and "REPLACES" in res.fix


def test_recordings_media_path_mismatch_warns(tmp_path: Path) -> None:
    _recording(tmp_path)
    res = doctor.check_recordings(make_ctx(tmp_path, frigate={"media_path": "/srv/other"}))
    assert res.status == WARN and "media_path" in res.fix


def test_recordings_stale_warns(tmp_path: Path) -> None:
    _recording(tmp_path, age_s=7200)
    res = doctor.check_recordings(make_ctx(tmp_path))
    assert res.status == WARN and "old" in res.detail


def test_recordings_brand_new_frigate_warns_not_fails(tmp_path: Path) -> None:
    (tmp_path / "media").mkdir()
    make_frigate_db(tmp_path / "frigate.db")
    res = doctor.check_recordings(make_ctx(tmp_path))
    assert res.status == WARN and "no recordings yet" in res.detail


def test_recordings_without_db_uses_files_on_disk(tmp_path: Path) -> None:
    seg = tmp_path / "media" / "recordings" / "2026-11-18" / "10" / "cam"
    seg.mkdir(parents=True)
    (seg / "00.00.mp4").write_bytes(b"x")
    res = doctor.check_recordings(make_ctx(tmp_path))
    assert res.status == PASS


def test_recordings_unreadable_fails(tmp_path: Path) -> None:
    (tmp_path / "media").mkdir()
    res = doctor.check_recordings(make_ctx(tmp_path, ctx={"can_read": lambda p: False}))
    assert res.status == FAIL


# -- scrub-cache / ffmpeg ----------------------------------------------------------


def test_scrub_cache_skipped_when_off(tmp_path: Path) -> None:
    res = doctor.check_scrub_cache(make_ctx(tmp_path, scrub={"enabled": False}))
    assert res.status == SKIPPED


def test_scrub_cache_pass_and_will_be_created(tmp_path: Path) -> None:
    res = doctor.check_scrub_cache(make_ctx(tmp_path, scrub={"min_free_bytes": 1}))
    assert res.status == PASS and "free" in res.detail


def test_scrub_cache_low_space_fails(tmp_path: Path) -> None:
    res = doctor.check_scrub_cache(make_ctx(tmp_path, scrub={"min_free_bytes": 1 << 60}))
    assert res.status == FAIL and "min_free_bytes" in res.fix


def test_scrub_cache_not_writable_fails(tmp_path: Path) -> None:
    (tmp_path / "scrub").mkdir()
    os.chmod(tmp_path / "scrub", 0o500)
    try:
        if os.access(tmp_path / "scrub", os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        res = doctor.check_scrub_cache(make_ctx(tmp_path))
        assert res.status == FAIL
    finally:
        os.chmod(tmp_path / "scrub", 0o700)


def test_scrub_cache_same_filesystem_as_recordings_warns(tmp_path: Path) -> None:
    (tmp_path / "media").mkdir()
    res = doctor.check_scrub_cache(make_ctx(tmp_path, scrub={"min_free_bytes": 1}))
    assert res.status == WARN and "different filesystem" in res.fix


def test_ffmpeg_missing_fails_when_scrub_on_warns_when_off(tmp_path: Path) -> None:
    assert doctor.check_ffmpeg(make_ctx(tmp_path)).status == FAIL
    assert doctor.check_ffmpeg(make_ctx(tmp_path, scrub={"enabled": False})).status == WARN


def test_ffmpeg_runs(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        ctx={
            "which": lambda n: f"/usr/bin/{n}",
            "run": lambda cmd, t: (0, "ffmpeg version 6.1.1 Copyright\n"),
        },
    )
    res = doctor.check_ffmpeg(ctx)
    assert res.status == PASS and "6.1.1" in res.detail


def test_ffmpeg_will_not_run_fails(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path, ctx={"which": lambda n: f"/usr/bin/{n}", "run": lambda cmd, t: (127, "")}
    )
    assert doctor.check_ffmpeg(ctx).status == FAIL


# -- mqtt ------------------------------------------------------------------------


class FakeReason:
    def __init__(self, failure: bool, text: str = "") -> None:
        self.is_failure = failure
        self.text = text

    def __str__(self) -> str:
        return self.text


class FakeMsg:
    def __init__(self, topic: str) -> None:
        self.topic = topic


class FakeMqtt:
    """A paho stand-in: no sockets. `behaviour` picks the broker's answer."""

    instances: list[FakeMqtt] = []

    def __init__(self, client_id: str, behaviour: str = "ok") -> None:
        self.client_id = client_id
        self.behaviour = behaviour
        self.on_connect: Any = None
        self.on_message: Any = None
        self.subscribed: list[str] = []
        self.creds: tuple[str, str | None] | None = None
        self.stopped = False
        FakeMqtt.instances.append(self)

    def username_pw_set(self, user: str, pw: str | None) -> None:
        self.creds = (user, pw)

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        if self.behaviour == "unreachable":
            raise ConnectionRefusedError(f"refused by {host}:{port} using {SECRET_PW}")

    def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    def loop_start(self) -> None:
        if self.behaviour == "hang":
            return
        rc = FakeReason(self.behaviour == "auth", "Not authorized")
        threading.Thread(target=self._deliver, args=(rc,), daemon=True).start()

    def _deliver(self, rc: FakeReason) -> None:
        self.on_connect(self, None, {}, rc, None)
        if self.behaviour == "ok":
            self.on_message(self, None, FakeMsg("frigate/available"))

    def loop_stop(self) -> None:
        self.stopped = True

    def disconnect(self) -> None:
        pass


def _mqtt_ctx(tmp_path: Path, behaviour: str) -> Context:
    FakeMqtt.instances.clear()
    return make_ctx(
        tmp_path,
        push={
            "enabled": True,
            "mqtt_host": "broker.test",
            "mqtt_username": "frigate",
            "mqtt_password": SECRET_PW,
        },
        ctx={"mqtt_client_factory": lambda cid: FakeMqtt(cid, behaviour)},
    )


def test_mqtt_skipped_when_push_off(tmp_path: Path) -> None:
    res = doctor.check_mqtt(make_ctx(tmp_path))
    assert res.status == SKIPPED and "push is off" in res.detail


def test_mqtt_pass_and_does_not_reuse_the_sidecars_client_id(tmp_path: Path) -> None:
    res = doctor.check_mqtt(_mqtt_ctx(tmp_path, "ok"))
    assert res.status == PASS and "frigate/available" in res.detail
    client = FakeMqtt.instances[0]
    assert client.client_id == "marcellus-push-doctor"
    assert set(client.subscribed) == {"frigate/available", "frigate/reviews", "frigate/events"}
    assert client.creds == ("frigate", SECRET_PW) and client.stopped


def test_mqtt_connected_but_silent_warns(tmp_path: Path) -> None:
    res = doctor.check_mqtt(_mqtt_ctx(tmp_path, "silent"))
    assert res.status == WARN and "mqtt:" in res.fix


def test_mqtt_login_refused_fails(tmp_path: Path) -> None:
    res = doctor.check_mqtt(_mqtt_ctx(tmp_path, "auth"))
    assert res.status == FAIL and "mqtt_password" in res.fix


def test_mqtt_unreachable_fails_and_hides_password(tmp_path: Path) -> None:
    ctx = _mqtt_ctx(tmp_path, "unreachable")
    results = doctor.run_checks(ctx, (("mqtt", doctor.check_mqtt),))
    assert results[0].status == FAIL and "push.mqtt_host" in results[0].fix
    assert SECRET_PW not in results[0].detail + results[0].fix


def test_mqtt_no_connack_fails(tmp_path: Path) -> None:
    ctx = _mqtt_ctx(tmp_path, "hang")
    ctx.timeout = 0.2
    assert doctor.check_mqtt(ctx).status == FAIL


# -- push relay ----------------------------------------------------------------------


def _relay_ctx(tmp_path: Path, plain_get: Any, **push: Any) -> Context:
    return make_ctx(
        tmp_path,
        push={"enabled": True, "relay_base_url": "https://relay.test", **push},
        ctx={"plain_get": plain_get},
    )


def test_relay_skipped_when_off_or_mock(tmp_path: Path) -> None:
    assert doctor.check_push_relay(make_ctx(tmp_path)).status == SKIPPED
    assert (
        doctor.check_push_relay(_relay_ctx(tmp_path, None, transport="mock")).status == SKIPPED
    )


def test_relay_answers_passes_without_leaking_key(tmp_path: Path) -> None:
    seen: list[str] = []

    def get(url: str, timeout: float) -> tuple[int, bytes]:
        seen.append(url)
        return 404, b""

    ctx = _relay_ctx(tmp_path, get, relay_key=SECRET_KEY)
    res = doctor.check_push_relay(ctx)
    assert res.status == PASS and SECRET_KEY not in res.detail
    assert seen == ["https://relay.test/"]  # a plain GET: nothing is pushed


def test_relay_5xx_and_unreachable_fail(tmp_path: Path) -> None:
    assert doctor.check_push_relay(_relay_ctx(tmp_path, lambda u, t: (503, b""))).status == FAIL
    def dead(url: str, timeout: float) -> tuple[int, bytes]:
        raise OSError("dns failure")

    res = doctor.check_push_relay(_relay_ctx(tmp_path, dead))
    assert res.status == FAIL and "push.relay_base_url" in res.fix


# -- service -------------------------------------------------------------------------


def _service_ctx(
    tmp_path: Path, *, state: str | None, health: tuple[int, dict[str, Any]] | None
) -> Context:
    def run(cmd: list[str], timeout: float) -> tuple[int, str] | None:
        if cmd[:2] == ["systemctl", "is-active"] and state is not None:
            return (0 if state == "active" else 3), state + "\n"
        return None

    def plain(url: str, timeout: float) -> tuple[int, bytes]:
        if health is None:
            raise OSError("connection refused")
        assert url == "http://127.0.0.1:5001/healthz"
        return health[0], json.dumps(health[1]).encode()

    return make_ctx(
        tmp_path,
        ctx={
            "run": run,
            "plain_get": plain,
            "which": lambda n: "/bin/systemctl" if n == "systemctl" else None,
        },
    )


def test_service_running_and_healthy(tmp_path: Path) -> None:
    res = doctor.check_service(_service_ctx(tmp_path, state="active", health=(200, {})))
    assert res.status == PASS and "systemd unit marcellus is active" in res.detail


def test_service_stopped_fails_with_start_line(tmp_path: Path) -> None:
    res = doctor.check_service(_service_ctx(tmp_path, state="inactive", health=None))
    assert res.status == FAIL and "systemctl start marcellus" in res.fix


def test_service_active_but_no_answer_fails(tmp_path: Path) -> None:
    res = doctor.check_service(_service_ctx(tmp_path, state="active", health=None))
    assert res.status == FAIL and "bind_port" in res.fix


def test_service_degraded_names_checks(tmp_path: Path) -> None:
    body = {"status": "degraded", "checks": {"mqtt": "disconnected", "db": "ok"}}
    res = doctor.check_service(_service_ctx(tmp_path, state="active", health=(503, body)))
    assert res.status == FAIL and "mqtt" in res.detail and "db" not in res.detail.split(":")[-1]


def test_service_nothing_detectable_is_skipped(tmp_path: Path) -> None:
    res = doctor.check_service(_service_ctx(tmp_path, state=None, health=None))
    assert res.status == SKIPPED


def test_service_by_hand_but_answering_passes(tmp_path: Path) -> None:
    res = doctor.check_service(_service_ctx(tmp_path, state=None, health=(200, {})))
    assert res.status == PASS and "outside systemd" in res.detail


def test_service_docker_container(tmp_path: Path) -> None:
    def run(cmd: list[str], timeout: float) -> tuple[int, str] | None:
        return (0, "running\n") if cmd[0] == "docker" else None

    ctx = make_ctx(
        tmp_path,
        ctx={
            "run": run,
            "which": lambda n: "/usr/bin/docker" if n == "docker" else None,
            "plain_get": lambda u, t: (200, b"{}"),
        },
    )
    res = doctor.check_service(ctx)
    assert res.status == PASS and "container marcellus is running" in res.detail


# -- runner: limits, independence, scrubbing --------------------------------------------


def test_hard_time_limit_is_honoured(tmp_path: Path) -> None:
    gate = threading.Event()

    def hang(ctx: Context) -> CheckResult:
        gate.wait(30)
        return CheckResult("hang", PASS, "late")

    ctx = make_ctx(tmp_path)
    ctx.timeout = 0.2
    started = time.monotonic()
    try:
        results = doctor.run_checks(ctx, (("hang", hang),))
    finally:
        gate.set()
    assert time.monotonic() - started < 3.0
    assert results[0].status == FAIL and "no answer within" in results[0].detail


def test_checks_are_independent_and_ordered(tmp_path: Path) -> None:
    def boom(ctx: Context) -> CheckResult:
        raise RuntimeError(f"exploded with {SECRET_PW}")

    def fine(ctx: Context) -> CheckResult:
        return CheckResult("b", PASS, "ok")

    ctx = make_ctx(tmp_path, push={"mqtt_password": SECRET_PW})
    results = doctor.run_checks(ctx, (("a", boom), ("b", fine), ("c", boom)))
    assert [r.id for r in results] == ["a", "b", "c"]
    assert [r.status for r in results] == [FAIL, PASS, FAIL]
    assert SECRET_PW not in json.dumps([r.as_dict() for r in results])


def test_checks_run_in_the_specified_order_and_cover_the_list() -> None:
    assert [cid for cid, _ in doctor.CHECKS] == [
        "frigate-api",
        "frigate-signin",
        "frigate-urls-differ",
        "frigate-db",
        "recordings",
        "scrub-cache",
        "ffmpeg",
        "mqtt",
        "push-relay",
        "service",
    ]


def test_url_credentials_are_scrubbed(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, frigate={"base_url": f"http://admin:{SECRET_PW}@f.test:5000"})
    res = doctor.run_checks(ctx, (("frigate-api", doctor.check_frigate_api),))[0]
    assert SECRET_PW not in res.detail + res.fix
    assert "admin" not in res.detail


def test_exit_code_and_summary() -> None:
    ok = [
        CheckResult("a", PASS, ""),
        CheckResult("b", WARN, "w", "f"),
        CheckResult("c", SKIPPED, ""),
    ]
    assert doctor.exit_code(ok) == 0
    assert doctor.exit_code([*ok, CheckResult("d", FAIL, "x", "y")]) == 1
    assert doctor.summarize(ok) == {PASS: 1, WARN: 1, FAIL: 0, SKIPPED: 1}


# -- the command, end to end --------------------------------------------------------


runner = CliRunner()


def _write_config(tmp_path: Path, **extra: str) -> Path:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text(
        f"""
frigate:
  base_url: http://frigate.test:5000
  proxy_base_url: https://frigate.test:8971
  verify_tls: false
  db_path: {tmp_path / "frigate.db"}
  recordings_path: {tmp_path / "media"}
sidecar:
  db_path: {tmp_path / "side.db"}
scrub:
  enabled: true
  cache_dir: {tmp_path / "scrub"}
  min_free_bytes: 1
push:
  enabled: true
  mqtt_host: broker.test
  mqtt_password: {SECRET_PW}
  relay_key: {SECRET_KEY}
  relay_base_url: https://relay.test
{extra.get("tail", "")}
"""
    )
    return cfg


@pytest.fixture
def faked_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _recording(tmp_path)
    frigate = FakeFrigate(
        {
            "8971/api/version": _resp(401),
            "5000/api/version": _resp(200, "0.18.2"),
            "5000/api/config": _resp(200, '{"auth":{"enabled":true}}'),
        }
    )

    def factory(settings: Settings, path: Path | None) -> Context:
        return Context(
            settings=settings,
            config_path=path,
            frigate_get=frigate,
            plain_get=lambda url, t: (
                (200, b'{"status":"ok","checks":{}}') if url.endswith("/healthz") else (404, b"")
            ),
            run=lambda cmd, t: (0, "ffmpeg version 7.0\n" if cmd[0] == "ffmpeg" else "active\n"),
            which=lambda n: f"/usr/bin/{n}",
            mqtt_client_factory=lambda cid: FakeMqtt(cid, "ok"),
            current_user=lambda: "tester",
        )

    monkeypatch.setattr(doctor, "_default_context", factory)
    return _write_config(tmp_path)


def test_command_end_to_end_all_good_exits_zero(faked_world: Path) -> None:
    result = runner.invoke(app, ["doctor", "--config", str(faked_world), "--no-color"])
    out = result.output
    assert result.exit_code == 0, out
    for cid, _ in doctor.CHECKS:
        assert cid in out
    ids = [line.split()[1] for line in out.splitlines() if line.startswith(" ") and line[1] != " "]
    assert ids == ["config", *(cid for cid, _ in doctor.CHECKS)]
    assert "passed" in out.splitlines()[-1]
    assert SECRET_PW not in out and SECRET_KEY not in out
    assert "\x1b[" not in out


def test_command_honours_marcellus_config_env(
    faked_world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARCELLUS_CONFIG", str(faked_world))
    result = runner.invoke(app, ["doctor", "--no-color"])
    assert result.exit_code == 0 and str(faked_world) in result.output


def test_command_failure_exits_one_and_prints_fix(
    tmp_path: Path, faked_world: Path
) -> None:
    (tmp_path / "frigate.db").unlink()
    result = runner.invoke(app, ["doctor", "--config", str(faked_world), "--no-color"])
    assert result.exit_code == 1
    assert "fix:" in result.output and "frigate.db_path" in result.output
    assert "failed" in result.output.splitlines()[-1]


def test_command_json_shape_is_pinned(faked_world: Path) -> None:
    result = runner.invoke(app, ["doctor", "--config", str(faked_world), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert [row["id"] for row in data] == ["config"] + [cid for cid, _ in doctor.CHECKS]
    for row in data:
        assert set(row) == {"id", "status", "detail", "fix"}
        assert row["status"] in {"pass", "warn", "fail", "skipped"}
        assert all(isinstance(v, str) for v in row.values())
    assert all(row["fix"] == "" for row in data if row["status"] == "pass")
    assert SECRET_PW not in result.output and SECRET_KEY not in result.output


def test_command_with_bad_config_skips_the_rest(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yml"
    cfg.write_text("sidecar:\n  bind_port: nope\n")
    result = runner.invoke(app, ["doctor", "--config", str(cfg), "--json"])
    data = json.loads(result.output)
    assert result.exit_code == 1 and data[0]["status"] == "fail"
    assert {row["status"] for row in data[1:]} == {"skipped"}


def test_command_push_off_skips_mqtt_and_relay(
    tmp_path: Path, faked_world: Path
) -> None:
    text = faked_world.read_text()
    off = text.replace("enabled: true\n  mqtt_host", "enabled: false\n  mqtt_host")
    faked_world.write_text(off)
    result = runner.invoke(app, ["doctor", "--config", str(faked_world), "--json"])
    by_id = {row["id"]: row for row in json.loads(result.output)}
    assert by_id["mqtt"]["status"] == "skipped" and "push is off" in by_id["mqtt"]["detail"]
    assert by_id["push-relay"]["status"] == "skipped"


def test_command_options_exist() -> None:
    # Rich colours the help where a terminal is detected (CI on 3.12 does), which
    # splits "--json" with escape codes: ask for plain, wide output.
    result = runner.invoke(
        app, ["doctor", "--help"], env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for flag in ("--json", "--timeout", "--mqtt-wait", "--no-color", "--config"):
        assert flag in plain
    assert result.exit_code == 0
