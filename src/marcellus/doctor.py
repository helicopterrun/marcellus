"""`marcellus doctor` -- a read-only checklist of everything an install needs.

Each check is a plain function `check_x(ctx) -> CheckResult`. `run_checks()`
runs them side by side on daemon threads, each under a hard time limit, and
returns the results in a fixed order, so one check that fails, hangs or raises
never changes what the others report.

Nothing here writes anything (databases open read-only, probes are GETs, the
MQTT client only subscribes), and no secret is printed: every result passes
through `Context.scrub()`, which blanks the configured passwords and keys and
the credentials inside URLs.

What is shared rather than copied:

- the TLS / wrong-scheme diagnosis and its plain-language fix text come from
  `frigate_tls` (`classify_error`, `classify_response`, `explain`), the same
  words `/healthz` (`frigate_diagnosis`) and `fsc init` use;
- "sign-in is off" is `auth.config_says_signin_off`, the exact test behind
  `frigate_allows_anonymous`;
- "the two Frigate URLs are identical" is `config.origins_identical`, the test
  behind the startup warning;
- the Frigate database opens through `db.open_frigate_ro`, and recording paths
  map through `scrub.mapping.map_recording_path`, as the generator does;
- the MQTT client is built the way `push.mqtt` builds its own (paho v2 API) and
  subscribes to the same configured topics.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml
from pydantic import ValidationError

from marcellus import db, frigate_tls
from marcellus.auth import config_says_signin_off
from marcellus.config import Settings, discover_config_path, load_settings, origins_identical
from marcellus.frigate_discovery import media_root_of
from marcellus.scrub.mapping import map_recording_path

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIPPED = "skipped"
STATUSES = (PASS, WARN, FAIL, SKIPPED)

#: Frigate tables the sidecar reads.
EXPECTED_FRIGATE_TABLES = ("event", "recordings", "reviewsegment")

#: A newest recording older than this means Frigate is not recording right now.
RECORDING_STALE_S = 3600.0

#: Name of the systemd unit and of the container the install paths create.
SERVICE_NAME = "marcellus"

_SECRET_MASK = "***"


@dataclass(frozen=True)
class CheckResult:
    """One line of the checklist. `fix` is one line, empty for a pass."""

    id: str
    status: str
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "status": self.status, "detail": self.detail, "fix": self.fix}


# -- seams (real implementations; tests swap them) -------------------------------


def _frigate_get(url: str, *, verify_tls: bool, timeout: float) -> httpx.Response:
    return frigate_tls.get(url, verify_tls=verify_tls, timeout=timeout)


def _plain_get(url: str, timeout: float) -> tuple[int, bytes]:
    """GET for the push relay and the sidecar's own `/healthz` (never Frigate).

    Any HTTP answer, error statuses included, is returned as `(status, body)`;
    only a failure to get an answer raises (`OSError`).
    """
    req = urllib.request.Request(url, headers={"User-Agent": "marcellus-doctor"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status), bytes(resp.read(65536))
    except urllib.error.HTTPError as exc:
        return int(exc.code), bytes(exc.read(65536))
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from exc


def _run_command(cmd: list[str], timeout: float) -> tuple[int, str] | None:
    """`(returncode, stdout)`, or None when the command cannot be run at all."""
    try:
        done = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode, done.stdout


def _paho_client(client_id: str) -> Any:
    import paho.mqtt.client as mqtt_client

    return mqtt_client.Client(
        client_id=client_id,
        callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]  # paho re-exports it without __all__
    )


def _can_read(path: Path) -> bool:
    return os.access(path, os.R_OK)


def _current_user() -> str:
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


@dataclass
class Context:
    settings: Settings
    timeout: float = 5.0
    mqtt_wait: float = 20.0
    config_path: Path | None = None
    frigate_get: Callable[..., httpx.Response] = _frigate_get
    plain_get: Callable[[str, float], tuple[int, bytes]] = _plain_get
    run: Callable[[list[str], float], tuple[int, str] | None] = _run_command
    which: Callable[[str], str | None] = shutil.which
    mqtt_client_factory: Callable[[str], Any] = _paho_client
    can_read: Callable[[Path], bool] = _can_read
    current_user: Callable[[], str] = _current_user
    extra_secrets: tuple[str, ...] = field(default_factory=tuple)

    def secrets(self) -> list[str]:
        push = self.settings.push
        values = [push.mqtt_password or "", push.relay_key, *self.extra_secrets]
        for url in (
            self.settings.frigate.base_url,
            self.settings.frigate.proxy_base_url,
            push.relay_base_url,
        ):
            with contextlib.suppress(ValueError):
                values.append(urlsplit(url).password or "")
        return [v for v in values if len(v) >= 3]

    def scrub(self, text: str) -> str:
        for secret in sorted(self.secrets(), key=len, reverse=True):
            text = text.replace(secret, _SECRET_MASK)
        return text


class _Budget:
    """One check's time allowance, shared across its sequential requests."""

    def __init__(self, total: float) -> None:
        self._end = time.monotonic() + total

    def left(self) -> float:
        return max(0.2, self._end - time.monotonic())


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _clean_url(url: str) -> str:
    """`url` without credentials."""
    try:
        parts = urlsplit(url)
        if parts.username is None and parts.password is None:
            return url
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return url


def _short_error(exc: BaseException) -> str:
    text = _one_line(str(exc)) or type(exc).__name__
    return text if len(text) <= 160 else text[:157] + "..."


# -- config ----------------------------------------------------------------------


def load_config(explicit: str | None = None) -> tuple[Settings | None, CheckResult, Path | None]:
    """Load settings exactly as `serve` does, and report on it.

    Returns `(settings or None, the `config` result, the file path)`.
    """
    path = discover_config_path(explicit)
    problems: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            problems.append(record.getMessage())

    log = logging.getLogger("marcellus.config")
    handler = _Collect(level=logging.WARNING)
    saved = (log.propagate, log.level)
    log.addHandler(handler)
    log.propagate = False
    log.setLevel(logging.WARNING)
    try:
        if path is not None and not path.exists():
            return (
                None,
                CheckResult(
                    "config",
                    FAIL,
                    f"{path} does not exist (serve would silently run on defaults)",
                    "create it with `fsc init -o <path>`, or point MARCELLUS_CONFIG at the "
                    "real file",
                ),
                path,
            )
        try:
            settings = load_settings(explicit)
        except ValidationError as exc:
            errors = exc.errors()
            key = ".".join(str(p) for p in errors[0]["loc"]) if errors else ""
            msg = _one_line(str(errors[0]["msg"])) if errors else "invalid value"
            where = f"{key}: " if key else ""
            more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            return (
                None,
                CheckResult(
                    "config",
                    FAIL,
                    f"{path or 'settings'}: {where}{msg}{more}",
                    f"fix `{key}` in {path or 'the config'} (see config/sidecar.example.yml)"
                    if key
                    else f"fix the value named above in {path or 'the config'}",
                ),
                path,
            )
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = f" at line {mark.line + 1}" if mark is not None else ""
            # `problem`, not str(exc): the latter quotes the offending line, which
            # may hold a password.
            why = _one_line(str(getattr(exc, "problem", None) or "syntax error"))
            return (
                None,
                CheckResult(
                    "config",
                    FAIL,
                    f"{path} is not valid YAML{line}: {why}",
                    f"correct the YAML syntax in {path}",
                ),
                path,
            )
        except Exception as exc:  # noqa: BLE001 -- any load failure is the answer
            return (
                None,
                CheckResult(
                    "config",
                    FAIL,
                    f"could not load settings: {_short_error(exc)}",
                    f"fix {path or 'the config or MARCELLUS_* environment'} and rerun",
                ),
                path,
            )
    finally:
        log.removeHandler(handler)
        log.propagate, log.level = saved

    unknown = sorted(
        {m.group(1) for p in problems if (m := re.search(r"unknown key '([^']+)'", p))}
    )
    if unknown:
        return (
            settings,
            CheckResult(
                "config",
                WARN,
                f"{path} loaded, but ignored unknown key(s): {', '.join(unknown)}",
                "check the spelling against config/sidecar.example.yml (unknown keys are ignored)",
            ),
            path,
        )
    if path is None:
        return (
            settings,
            CheckResult(
                "config",
                WARN,
                "no config file found; using built-in defaults and MARCELLUS_* variables",
                "write one with `fsc init`, or set MARCELLUS_CONFIG to its path",
            ),
            path,
        )
    return settings, CheckResult("config", PASS, f"{path} loaded"), path


# -- Frigate URLs ----------------------------------------------------------------


@dataclass
class _Answer:
    """What one GET to a Frigate origin came to."""

    response: httpx.Response | None
    diagnosis: str | None
    error: str = ""


def _ask_frigate(ctx: Context, url: str, budget: _Budget) -> _Answer:
    try:
        resp = ctx.frigate_get(
            url, verify_tls=ctx.settings.frigate.verify_tls, timeout=budget.left()
        )
    except (httpx.HTTPError, OSError) as exc:
        return _Answer(None, frigate_tls.classify_error(exc, url), _short_error(exc))
    diagnosis = frigate_tls.classify_response(
        resp.status_code, resp.text, url, resp.headers.get("location")
    )
    return _Answer(resp, diagnosis)


def _diagnosis_fix(diagnosis: str, url: str, setting: str) -> str:
    return _one_line(frigate_tls.explain(diagnosis, _clean_url(url), setting=setting))


def _unreachable_fix(setting: str, error: str) -> str:
    low = error.lower()
    if "wrong_version_number" in low or "record layer" in low or "unknown protocol" in low:
        return (
            f"that port speaks plain HTTP, but `{setting}` uses https://: change it to http://"
        )
    return (
        f"check `{setting}`, that Frigate is running, and that this machine can reach that "
        "host and port"
    )


def check_frigate_api(ctx: Context) -> CheckResult:
    base = ctx.settings.frigate.base_url
    url = base.rstrip("/") + "/api/version"
    ans = _ask_frigate(ctx, url, _Budget(ctx.timeout))
    shown = _clean_url(base)
    if ans.diagnosis is not None:
        return CheckResult(
            "frigate-api",
            FAIL,
            f"{shown}: {ans.diagnosis.replace('_', ' ')}",
            _diagnosis_fix(ans.diagnosis, base, "frigate.base_url"),
        )
    if ans.response is None:
        return CheckResult(
            "frigate-api",
            FAIL,
            f"{shown} did not answer ({ans.error})",
            _unreachable_fix("frigate.base_url", ans.error),
        )
    if ans.response.status_code != 200:
        return CheckResult(
            "frigate-api",
            FAIL,
            f"{shown} answered HTTP {ans.response.status_code}",
            "frigate.base_url should be Frigate's unauthenticated API port (usually :5000)",
        )
    version = _one_line(ans.response.text)[:40]
    return CheckResult("frigate-api", PASS, f"Frigate {version} at {shown}")


def check_frigate_signin(ctx: Context) -> CheckResult:
    s = ctx.settings.frigate
    budget = _Budget(ctx.timeout)
    url = s.proxy_base_url.rstrip("/") + "/api/version"
    shown = _clean_url(s.proxy_base_url)
    ans = _ask_frigate(ctx, url, budget)
    if ans.diagnosis is not None:
        return CheckResult(
            "frigate-signin",
            FAIL,
            f"{shown}: {ans.diagnosis.replace('_', ' ')}",
            _diagnosis_fix(ans.diagnosis, s.proxy_base_url, "frigate.proxy_base_url"),
        )
    if ans.response is None:
        return CheckResult(
            "frigate-signin",
            FAIL,
            f"{shown} did not answer ({ans.error})",
            _unreachable_fix("frigate.proxy_base_url", ans.error),
        )
    status = ans.response.status_code
    if status >= 500:
        return CheckResult(
            "frigate-signin",
            FAIL,
            f"{shown} answered HTTP {status}",
            "check Frigate's own logs; `frigate.proxy_base_url` should be its signed-in port "
            "(usually :8971)",
        )

    # What Frigate itself says about sign-in, read as `frigate_allows_anonymous` does.
    signin_off: bool | None = None
    config_url = s.base_url.rstrip("/") + "/api/config"
    cfg = _ask_frigate(ctx, config_url, budget)
    if cfg.response is not None and cfg.response.status_code == 200:
        try:
            signin_off = config_says_signin_off(cfg.response.json())
        except ValueError:
            signin_off = None

    open_here = 200 <= status < 300
    if signin_off is True and open_here:
        return CheckResult(
            "frigate-signin",
            PASS,
            f"{shown} answers; Frigate sign-in is OFF (auth.enabled: false), "
            "so the sidecar admits callers without a session",
        )
    if signin_off is True:
        return CheckResult(
            "frigate-signin",
            PASS,
            f"{shown} answers (HTTP {status}); Frigate config says sign-in is off, but this "
            "origin still asks for credentials",
        )
    if open_here and signin_off is False:
        return CheckResult(
            "frigate-signin",
            WARN,
            f"{shown} serves requests with no sign-in although Frigate has sign-in ON: it is "
            "probably Frigate's unauthenticated port",
            "point `frigate.proxy_base_url` at Frigate's signed-in port (usually :8971)",
        )
    known = (
        "Frigate sign-in is ON"
        if signin_off is False
        else "Frigate's sign-in setting could not be read from frigate.base_url"
    )
    return CheckResult("frigate-signin", PASS, f"{shown} answers (HTTP {status}); {known}")


def check_urls_differ(ctx: Context) -> CheckResult:
    s = ctx.settings.frigate
    if origins_identical(s.base_url, s.proxy_base_url):
        return CheckResult(
            "frigate-urls-differ",
            WARN,
            f"frigate.base_url and frigate.proxy_base_url are both {_clean_url(s.base_url)}",
            "set `frigate.proxy_base_url` to Frigate's signed-in port (usually :8971); "
            "identical URLs turn the sidecar's session check into a no-op",
        )
    return CheckResult("frigate-urls-differ", PASS, "the two Frigate URLs are different origins")


# -- Frigate database and recordings ---------------------------------------------


def _service_user(ctx: Context) -> str | None:
    """The user the systemd unit runs as, when there is one."""
    if ctx.which("systemctl") is None:
        return None
    out = ctx.run(["systemctl", "show", SERVICE_NAME, "-p", "User", "--value"], 3.0)
    if out is None or out[0] != 0:
        return None
    name = out[1].strip()
    return name or None


def _names(st: os.stat_result) -> tuple[str, str]:
    import grp
    import pwd

    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return owner, group


def _mode_readable_by(st: os.stat_result, user: str) -> bool | None:
    """Can `user` read a file with these mode bits? None when the user is unknown.

    Reads the file's own owner/group/other bits only; the directories leading
    to it are not examined.
    """
    import grp
    import pwd

    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return None
    gids = {pw.pw_gid} | {g.gr_gid for g in grp.getgrall() if user in g.gr_mem}
    if pw.pw_uid == 0:
        return True
    if st.st_uid == pw.pw_uid:
        return bool(st.st_mode & 0o400)
    if st.st_gid in gids:
        return bool(st.st_mode & 0o040)
    return bool(st.st_mode & 0o004)


def _db_permission_fix(path: Path, st: os.stat_result, user: str) -> str:
    _owner, group = _names(st)
    fix = f"sudo usermod -aG {group} {user}  (then restart the service so it picks up the group)"
    if not st.st_mode & 0o040:
        fix += f"; the file's group also needs read: sudo chmod g+r {path}"
    return fix


def check_frigate_db(ctx: Context) -> CheckResult:
    path = Path(ctx.settings.frigate.db_path)
    if not path.exists():
        return CheckResult(
            "frigate-db",
            FAIL,
            f"{path} does not exist",
            "set `frigate.db_path` to Frigate's frigate.db as this machine sees it "
            "(mount or copy the path in; Docker needs a bind mount)",
        )
    if not path.is_file():
        return CheckResult(
            "frigate-db",
            FAIL,
            f"{path} is not a file",
            "set `frigate.db_path` to the frigate.db file itself, not its directory",
        )
    st = path.stat()
    owner, group = _names(st)
    me = ctx.current_user()
    svc = _service_user(ctx)
    note = ""
    if svc is not None and svc != me:
        note = f"; running as {me} but the service runs as {svc}"
        svc_ok = _mode_readable_by(st, svc)
        if svc_ok is False:
            return CheckResult(
                "frigate-db",
                FAIL,
                f"{path} (owner {owner}, group {group}) is not readable by service user "
                f"{svc}{note}",
                _db_permission_fix(path, st, svc),
            )
    if not ctx.can_read(path):
        return CheckResult(
            "frigate-db",
            FAIL,
            f"{path} (owner {owner}, group {group}) is not readable by {me}"
            + (f"; the service runs as {svc}" if svc and svc != me else ""),
            _db_permission_fix(path, st, svc or me),
        )
    try:
        conn = db.open_frigate_ro(path)
    except (sqlite3.Error, OSError) as exc:
        return CheckResult(
            "frigate-db",
            FAIL,
            f"{path} will not open read-only: {_short_error(exc)}",
            "check that the file is an intact Frigate database and its directory is readable "
            "(WAL mode needs the -wal/-shm files beside it)",
        )
    try:
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    except sqlite3.Error as exc:
        return CheckResult(
            "frigate-db",
            FAIL,
            f"{path} cannot be read: {_short_error(exc)}",
            "make sure `frigate.db_path` is Frigate's frigate.db and not a different or "
            "damaged SQLite file",
        )
    finally:
        conn.close()
    missing = [t for t in EXPECTED_FRIGATE_TABLES if t not in tables]
    if missing:
        return CheckResult(
            "frigate-db",
            FAIL,
            f"{path} opens but lacks table(s): {', '.join(missing)}",
            "`frigate.db_path` must be Frigate's own frigate.db (not the sidecar's database, "
            "and not an empty file)",
        )
    return CheckResult("frigate-db", PASS, f"{path} opens read-only; Frigate tables present{note}")


def _newest_recording_row(path: Path) -> tuple[str, float] | None:
    conn = db.open_frigate_ro(path)
    try:
        row = conn.execute(
            "SELECT path, start_time FROM recordings ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row[0]:
        return None
    return str(row[0]), float(row[1] or 0.0)


def _newest_file_age(root: Path, now: float, budget: _Budget) -> float | None:
    """Seconds since the newest file near the top of `root`, or None when there
    are none. Bounded: four directory levels, newest names first."""

    def newest(d: Path, depth: int) -> float | None:
        if budget.left() <= 0.25:
            return None
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name, reverse=True)
        except OSError:
            return None
        best: float | None = None
        for entry in entries:
            try:
                if entry.is_file():
                    best = max(best or 0.0, entry.stat().st_mtime)
                elif depth > 0 and entry.is_dir():
                    found = newest(Path(entry.path), depth - 1)
                    if found is not None:
                        best = max(best or 0.0, found)
                        break
            except OSError:
                continue
        return best

    mtime = newest(root, 4)
    return None if mtime is None else max(0.0, now - mtime)


def check_recordings(ctx: Context) -> CheckResult:
    frigate = ctx.settings.frigate
    root = Path(frigate.recordings_path)
    budget = _Budget(ctx.timeout)
    if not root.is_dir():
        return CheckResult(
            "recordings",
            FAIL,
            f"{root} does not exist",
            "set `frigate.recordings_path` to where Frigate's media directory (the one that "
            "holds recordings/) is mounted on this machine",
        )
    if not ctx.can_read(root):
        return CheckResult(
            "recordings",
            FAIL,
            f"{root} is not readable by {ctx.current_user()}",
            f"give the service user read access to {root} (group membership or `chmod`)",
        )
    now = time.time()

    newest: tuple[str, float] | None = None
    db_path = Path(frigate.db_path)
    if db_path.is_file():
        with contextlib.suppress(sqlite3.Error, OSError):
            newest = _newest_recording_row(db_path)
    if newest is not None:
        raw, started = newest
        media = str(frigate.media_path).rstrip("/")
        if not (raw == media or raw.startswith(media + "/")):
            right = media_root_of(raw) or "the directory Frigate records under"
            return CheckResult(
                "recordings",
                WARN,
                f"Frigate stores recordings under {raw.rsplit('/', 1)[0]}..., which is not "
                f"inside frigate.media_path ({media})",
                f"set `frigate.media_path` to {right} (the part of {raw} before recordings/)",
            )
        mapped = map_recording_path(raw, frigate.media_path, root)
        age = max(0.0, now - started)
        if not mapped.exists():
            return CheckResult(
                "recordings",
                FAIL,
                f"Frigate's newest recording is {raw}, which maps to {mapped}, and nothing "
                "is there",
                "frigate.recordings_path REPLACES frigate.media_path (it is not the recordings/ "
                "folder inside it): point it at the host copy of Frigate's media directory",
            )
        if age > RECORDING_STALE_S:
            return CheckResult(
                "recordings",
                WARN,
                f"{root} matches Frigate's layout, but its newest recording is "
                f"{_age_text(age)} old",
                "check that Frigate is recording (cameras on, `record: enabled: true`)",
            )
        return CheckResult(
            "recordings",
            PASS,
            f"{root} matches Frigate's own recording paths; newest segment {_age_text(age)} old",
        )

    # No Frigate database rows to compare with: fall back to what is on disk.
    age_s = _newest_file_age(root, now, budget)
    if age_s is None:
        return CheckResult(
            "recordings",
            WARN,
            f"{root} exists but holds no recordings yet",
            "expected on a brand-new Frigate: wait for it to record, then rerun doctor",
        )
    if age_s > RECORDING_STALE_S:
        return CheckResult(
            "recordings",
            WARN,
            f"{root} exists; its newest file is {_age_text(age_s)} old",
            "check that Frigate is recording, and that this is the directory it writes to",
        )
    return CheckResult(
        "recordings", PASS, f"{root} exists; newest file {_age_text(age_s)} old"
    )


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.0f} days"


def _size_text(n: float) -> str:
    return f"{n / (1024 ** 3):.1f} GiB"


# -- scrub cache, ffmpeg ---------------------------------------------------------


def check_scrub_cache(ctx: Context) -> CheckResult:
    scrub = ctx.settings.scrub
    if not scrub.enabled:
        return CheckResult("scrub-cache", SKIPPED, "scrub is off (scrub.enabled: false)")
    cache = Path(scrub.cache_dir)
    probe = cache
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        return CheckResult(
            "scrub-cache",
            FAIL,
            f"{cache} has no existing parent directory",
            "set `scrub.cache_dir` to a directory on a disk with room to spare",
        )
    if not os.access(probe, os.W_OK | os.X_OK):
        return CheckResult(
            "scrub-cache",
            FAIL,
            f"{probe} is not writable by {ctx.current_user()}",
            f"make {cache} writable by the service user (`chown`), or change `scrub.cache_dir`",
        )
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        return CheckResult(
            "scrub-cache",
            WARN,
            f"could not read free space on {probe}: {_short_error(exc)}",
            "check the filesystem under `scrub.cache_dir`",
        )
    note = "" if cache.exists() else " (will be created on first use)"
    if free < scrub.min_free_bytes:
        return CheckResult(
            "scrub-cache",
            FAIL,
            f"{cache}: {_size_text(free)} free, below scrub.min_free_bytes "
            f"({_size_text(scrub.min_free_bytes)}), so generation is paused",
            "free up space, move `scrub.cache_dir` to a bigger disk, or lower "
            "`scrub.min_free_bytes`",
        )
    rec = Path(ctx.settings.frigate.recordings_path)
    try:
        if rec.exists() and os.stat(probe).st_dev == os.stat(rec).st_dev:
            return CheckResult(
                "scrub-cache",
                WARN,
                f"{cache} is on the same filesystem as the recordings ({_size_text(free)} free)",
                "put `scrub.cache_dir` on a different filesystem: the cache must not compete "
                "with Frigate for the recordings disk",
            )
    except OSError:
        pass
    return CheckResult("scrub-cache", PASS, f"{cache} writable{note}; {_size_text(free)} free")


def check_ffmpeg(ctx: Context) -> CheckResult:
    needed = ctx.settings.scrub.enabled
    missing = [b for b in ("ffmpeg", "ffprobe") if ctx.which(b) is None]
    if missing:
        return CheckResult(
            "ffmpeg",
            FAIL if needed else WARN,
            f"{' and '.join(missing)} not found on PATH"
            + ("" if needed else " (only needed for scrub and wildlife)"),
            "install ffmpeg (`sudo apt install ffmpeg`); the Docker image already includes it",
        )
    out = ctx.run(["ffmpeg", "-version"], max(1.0, ctx.timeout - 0.5))
    if out is None or out[0] != 0:
        return CheckResult(
            "ffmpeg",
            FAIL if needed else WARN,
            "ffmpeg is on PATH but will not run",
            "reinstall ffmpeg, and check that the service user may execute it",
        )
    first = out[1].splitlines()[0] if out[1].strip() else "ffmpeg"
    m = re.search(r"version\s+(\S+)", first)
    return CheckResult("ffmpeg", PASS, f"ffmpeg {m.group(1) if m else 'present'} runs")


# -- push ------------------------------------------------------------------------


def _reason_failed(rc: Any) -> bool:
    flag = getattr(rc, "is_failure", None)
    if flag is not None:
        return bool(flag)
    return rc not in (0, None)


def check_mqtt(ctx: Context) -> CheckResult:
    push = ctx.settings.push
    if not push.enabled:
        return CheckResult("mqtt", SKIPPED, "push is off (push.enabled: false)")
    where = f"{push.mqtt_host}:{push.mqtt_port}"
    topics = [push.mqtt_topic_available, push.mqtt_topic_reviews, push.mqtt_topic_events]
    connected = threading.Event()
    message = threading.Event()
    state: dict[str, str] = {}

    # A separate client id: sharing the running sidecar's would make the broker
    # drop the sidecar's own connection.
    client = ctx.mqtt_client_factory(f"{push.mqtt_client_id}-doctor")
    if push.mqtt_username:
        client.username_pw_set(push.mqtt_username, push.mqtt_password)

    def on_connect(c: Any, _u: Any, _f: Any, rc: Any, _props: Any = None) -> None:
        if _reason_failed(rc):
            state["refused"] = str(rc)
        else:
            for topic in topics:
                c.subscribe(topic)
        connected.set()

    def on_message(_c: Any, _u: Any, msg: Any) -> None:
        state.setdefault("topic", str(getattr(msg, "topic", "")))
        message.set()

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        try:
            client.connect(push.mqtt_host, push.mqtt_port, 30)
            client.loop_start()
        except OSError as exc:
            return CheckResult(
                "mqtt",
                FAIL,
                f"cannot connect to {where}: {_short_error(exc)}",
                "check `push.mqtt_host` and `push.mqtt_port`, and that the broker is running "
                "and reachable from here",
            )
        if not connected.wait(ctx.timeout):
            return CheckResult(
                "mqtt",
                FAIL,
                f"{where} accepted the connection but never answered within {ctx.timeout:g}s",
                "check that `push.mqtt_port` is an MQTT port (usually 1883) and not another "
                "service's",
            )
        if "refused" in state:
            return CheckResult(
                "mqtt",
                FAIL,
                f"{where} refused the login ({state['refused']})",
                "check `push.mqtt_username` and `push.mqtt_password` against the broker's users",
            )
        if message.wait(ctx.mqtt_wait):
            return CheckResult(
                "mqtt", PASS, f"connected to {where}; Frigate is publishing ({state['topic']})"
            )
        return CheckResult(
            "mqtt",
            WARN,
            f"connected to {where}, but nothing on {', '.join(topics)} in {ctx.mqtt_wait:g}s",
            "check Frigate's own `mqtt:` section (enabled: true, this broker) and that its "
            "topic_prefix matches `push.mqtt_topic_*`",
        )
    finally:
        with contextlib.suppress(Exception):
            client.loop_stop()
        with contextlib.suppress(Exception):
            client.disconnect()


def check_push_relay(ctx: Context) -> CheckResult:
    push = ctx.settings.push
    if not push.enabled:
        return CheckResult("push-relay", SKIPPED, "push is off (push.enabled: false)")
    if push.transport == "mock":
        return CheckResult("push-relay", SKIPPED, "push.transport is mock; nothing is sent")
    url = push.relay_base_url
    shown = _clean_url(url)
    try:
        status, _body = ctx.plain_get(url.rstrip("/") + "/", ctx.timeout)
    except OSError as exc:
        return CheckResult(
            "push-relay",
            FAIL,
            f"{shown} did not answer ({_short_error(exc)})",
            "check this machine's internet access (DNS, firewall) and `push.relay_base_url`",
        )
    if status >= 500:
        return CheckResult(
            "push-relay",
            FAIL,
            f"{shown} answered HTTP {status}",
            "the relay is having trouble: retry later, or check `push.relay_base_url`",
        )
    cred = (
        "relay_key set"
        if push.relay_key
        else "no relay_key; phones supply their own relay credential"
    )
    return CheckResult("push-relay", PASS, f"{shown} answers (HTTP {status}); {cred}")


# -- service ---------------------------------------------------------------------


def _probe_host(bind_host: str) -> str:
    if bind_host in ("0.0.0.0", ""):  # noqa: S104
        return "127.0.0.1"
    if bind_host in ("::", "[::]"):
        return "[::1]"
    return f"[{bind_host}]" if ":" in bind_host and not bind_host.startswith("[") else bind_host


def _service_manager_state(ctx: Context) -> tuple[str, str] | None:
    """`(what it is, state)` for the systemd unit or the Docker container."""
    if ctx.which("systemctl") is not None:
        out = ctx.run(["systemctl", "is-active", SERVICE_NAME], 3.0)
        if out is not None:
            state = out[1].strip() or "unknown"
            if state != "unknown":
                return f"systemd unit {SERVICE_NAME}", state
    if ctx.which("docker") is not None:
        out = ctx.run(
            ["docker", "ps", "-a", "--filter", f"name=^{SERVICE_NAME}$", "--format", "{{.State}}"],
            4.0,
        )
        if out is not None and out[0] == 0 and out[1].strip():
            return f"container {SERVICE_NAME}", out[1].strip().splitlines()[0]
    return None


def check_service(ctx: Context) -> CheckResult:
    sidecar = ctx.settings.sidecar
    budget = _Budget(ctx.timeout)
    host = _probe_host(sidecar.bind_host)
    health_url = f"http://{host}:{sidecar.bind_port}/healthz"
    where = f"{host.strip('[]')}:{sidecar.bind_port}"
    managed = _service_manager_state(ctx)

    health: tuple[int, dict[str, Any]] | None = None
    health_error = ""
    try:
        status, body = ctx.plain_get(health_url, budget.left())
        try:
            parsed = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            parsed = {}
        health = (status, parsed if isinstance(parsed, dict) else {})
    except OSError as exc:
        health_error = _short_error(exc)

    if managed is not None:
        what, state = managed
        if state not in ("active", "running"):
            start = (
                f"sudo systemctl start {SERVICE_NAME}  (then `journalctl -u {SERVICE_NAME} -n 50`)"
                if what.startswith("systemd")
                else f"docker start {SERVICE_NAME}  (then `docker logs --tail 50 {SERVICE_NAME}`)"
            )
            return CheckResult("service", FAIL, f"{what} is {state}", start)
        label = f"{what} is {state}"
    else:
        label = "no systemd unit or Docker container found"

    if health is None:
        if managed is None:
            return CheckResult(
                "service",
                SKIPPED,
                f"{label}, and nothing answers /healthz on {where} (not started yet?)",
            )
        return CheckResult(
            "service",
            FAIL,
            f"{label}, but /healthz on {where} does not answer ({health_error})",
            "check `sidecar.bind_host` / `sidecar.bind_port`, and the service log for a "
            "startup error",
        )
    status, body_json = health
    if status == 200:
        suffix = "" if managed is not None else " (running outside systemd/Docker)"
        return CheckResult("service", PASS, f"{label}; /healthz on {where} is ok{suffix}")
    failing = body_json.get("checks", {})
    bad = sorted(
        k
        for k, v in failing.items()
        if isinstance(v, str) and v not in ("ok", "connected", "disabled", "starting")
    )
    named = f": {', '.join(bad)}" if bad else ""
    return CheckResult(
        "service",
        FAIL,
        f"{label}; /healthz on {where} says degraded (HTTP {status}){named}",
        f"open http://{where}/healthz for the full report, then see the Troubleshooting page",
    )


# -- running everything ----------------------------------------------------------


CheckFn = Callable[[Context], CheckResult]

#: (id, function). The order here is the order of the report.
CHECKS: tuple[tuple[str, CheckFn], ...] = (
    ("frigate-api", check_frigate_api),
    ("frigate-signin", check_frigate_signin),
    ("frigate-urls-differ", check_urls_differ),
    ("frigate-db", check_frigate_db),
    ("recordings", check_recordings),
    ("scrub-cache", check_scrub_cache),
    ("ffmpeg", check_ffmpeg),
    ("mqtt", check_mqtt),
    ("push-relay", check_push_relay),
    ("service", check_service),
)

#: Seconds a check may run beyond its own budget before it is given up on, so
#: its own timeout message wins the race against the hard limit.
_GRACE_S = 0.75


def _limit_for(check_id: str, ctx: Context) -> float:
    base = ctx.timeout + _GRACE_S
    if check_id == "mqtt":
        return base + ctx.mqtt_wait
    if check_id == "service":
        return base + 8.0  # the manager lookups run before the request
    return base


def run_checks(
    ctx: Context, checks: tuple[tuple[str, CheckFn], ...] = CHECKS
) -> list[CheckResult]:
    """Run `checks` side by side, each under its own hard limit; results in order.

    A check that raises becomes a failed result; one that outlives its limit
    is abandoned (its daemon thread cannot hold the process open) and reported
    as failed. Either way the others are unaffected.
    """
    slots: dict[str, list[CheckResult]] = {cid: [] for cid, _ in checks}
    started = time.monotonic()
    threads: dict[str, threading.Thread] = {}

    def make(cid: str, fn: CheckFn) -> Callable[[], None]:
        def run() -> None:
            try:
                slots[cid].append(fn(ctx))
            except Exception as exc:  # noqa: BLE001 -- a broken check is a finding
                slots[cid].append(
                    CheckResult(
                        cid,
                        FAIL,
                        f"the check itself crashed ({type(exc).__name__}: {_short_error(exc)})",
                        "this is a bug in doctor: please report it with `fsc version`",
                    )
                )

        return run

    for cid, fn in checks:
        thread = threading.Thread(target=make(cid, fn), name=f"doctor-{cid}", daemon=True)
        threads[cid] = thread
        thread.start()

    results: list[CheckResult] = []
    for cid, _fn in checks:
        limit = _limit_for(cid, ctx)
        remaining = max(0.0, started + limit - time.monotonic())
        threads[cid].join(remaining)
        if slots[cid]:
            res = slots[cid][0]
        else:
            res = CheckResult(
                cid,
                FAIL,
                f"no answer within {limit - _GRACE_S:g}s",
                "the host or service did not respond in time: check the network path, or "
                "rerun with a larger --timeout",
            )
        results.append(
            CheckResult(
                res.id,
                res.status,
                _one_line(ctx.scrub(res.detail)),
                _one_line(ctx.scrub(res.fix)),
            )
        )
    return results


def summarize(results: list[CheckResult]) -> dict[str, int]:
    return {s: sum(1 for r in results if r.status == s) for s in STATUSES}


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0


def skipped_for_config(reason: str) -> list[CheckResult]:
    """Every check after `config`, skipped because settings did not load."""
    return [
        CheckResult(cid, SKIPPED, f"not run: {reason}") for cid, _fn in CHECKS
    ]


# -- command-line front end --------------------------------------------------------


def _default_context(settings: Settings, path: Path | None) -> Context:
    return Context(settings=settings, config_path=path)


_MARKS = {
    PASS: ("✓", "green"),
    WARN: ("!", "yellow"),
    FAIL: ("✗", "red"),
    SKIPPED: ("-", "bright_black"),
}


def run_cli(
    config: str | None,
    *,
    output_json: bool,
    timeout: float,
    mqtt_wait: float,
    color: bool | None,
) -> int:
    """Run doctor and print the report. Returns the process exit code."""
    import typer

    settings, config_result, path = load_config(config)
    if settings is None:
        results = [config_result, *skipped_for_config("the config did not load")]
    else:
        ctx = _default_context(settings, path)
        ctx.timeout = timeout
        ctx.mqtt_wait = mqtt_wait
        scrubbed = CheckResult(
            config_result.id,
            config_result.status,
            _one_line(ctx.scrub(config_result.detail)),
            _one_line(ctx.scrub(config_result.fix)),
        )
        results = [scrubbed, *run_checks(ctx)]

    if output_json:
        typer.echo(json.dumps([r.as_dict() for r in results], indent=2))
        return exit_code(results)

    width = max(len(r.id) for r in results)
    for r in results:
        mark, colour = _MARKS[r.status]
        typer.secho(f" {mark} ", fg=colour, nl=False, color=color)
        typer.secho(f"{r.id:<{width}}", bold=True, nl=False, color=color)
        typer.echo(f"  {r.detail}")
        if r.fix and r.status != PASS:
            typer.secho(f"   {'':<{width}}  fix: {r.fix}", fg=colour, color=color)
    counts = summarize(results)
    parts = [f"{counts[PASS]} passed"]
    if counts[WARN]:
        parts.append(f"{counts[WARN]} warning{'s' if counts[WARN] != 1 else ''}")
    if counts[FAIL]:
        parts.append(f"{counts[FAIL]} failed")
    if counts[SKIPPED]:
        parts.append(f"{counts[SKIPPED]} skipped")
    code = exit_code(results)
    typer.echo("")
    typer.secho(
        ("Everything needed is working. " if code == 0 else "Fix the failures above. ")
        + ", ".join(parts)
        + ".",
        bold=True,
        fg="green" if code == 0 and not counts[WARN] else ("yellow" if code == 0 else "red"),
        color=color,
    )
    return code
