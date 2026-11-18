"""The optional push step of `fsc init`: the broker settings, and the MQTT
password in the one place it may live.

The password never goes into `sidecar.yml`, never into a log line or message
and never back to the terminal. It is written only to an env file the service
reads at start-up as `MARCELLUS_PUSH__MQTT_PASSWORD`:

- systemd install: `/etc/marcellus-push.env` (root-owned, mode 0600; the unit
  reads it through `EnvironmentFile=-`);
- Docker install: the install directory's `.env` (compose passes it into the
  container). `init` runs INSIDE the container there and cannot see the host's
  `.env`, so unless `--env-file` points at a mounted copy it prints the exact
  host commands instead (the commands read the password silently, so they hold
  no secret either).

Everything that touches the filesystem or the process is a parameter, so tests
need no root and no container.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: The variable the service reads (`push.mqtt_password`, env-overridable).
ENV_KEY = "MARCELLUS_PUSH__MQTT_PASSWORD"
#: Where a non-interactive caller may pass the password (instead of a file).
PASSWORD_ENV_VAR = "MARCELLUS_INIT_MQTT_PASSWORD"  # noqa: S105 -- a variable NAME
SYSTEMD_ENV_FILE = Path("/etc/marcellus-push.env")
DEFAULT_INSTALL_DIR = "/opt/marcellus"

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./!^*~-]+$")


class EnvFileError(Exception):
    """A problem with an env file. Messages never contain the secret."""


# -- where the secret goes -----------------------------------------------------------


@dataclass(frozen=True)
class EnvTarget:
    """`kind` is "systemd", "docker" or "file" (explicit `--env-file`).

    `path` is None when the file is out of reach (init is inside the Docker
    container); `host_path` is where it is on the host, for the printed commands.
    """

    kind: str
    path: Path | None
    host_path: Path | None = None

    @property
    def label(self) -> str:
        return str(self.path or self.host_path)


def running_in_container(
    *, exists: Callable[[str], bool] = os.path.exists, read: Callable[[str], str] | None = None
) -> bool:
    """True inside a Docker/Podman container (the Docker install runs init there)."""
    if exists("/.dockerenv") or exists("/run/.containerenv"):
        return True

    def _read(path: str) -> str:
        try:
            with open(path, encoding="utf-8") as fh:  # noqa: PTH123
                return fh.read(65536)
        except OSError:
            return ""

    text = (read or _read)("/proc/1/cgroup")
    return any(word in text for word in ("docker", "containerd", "kubepods"))


def detect_target(
    env_file: str | None,
    *,
    install_dir: str | None = None,
    in_container: bool | None = None,
    exists: Callable[[str], bool] = os.path.exists,
) -> EnvTarget:
    """Which env file holds the password for THIS install, from how init runs.

    1. `--env-file`: that file, whatever kind of install.
    2. Inside the container: the Docker install, whose `.env` is on the host.
    3. On the host, with a compose deployment in the install directory and no
       systemd config: the Docker install's `.env`.
    4. Otherwise: the systemd install's `/etc/marcellus-push.env`.
    """
    install = Path(install_dir or os.environ.get("INSTALL_DIR") or DEFAULT_INSTALL_DIR)
    if env_file:
        return EnvTarget("file", Path(env_file))
    container = running_in_container() if in_container is None else in_container
    if container:
        return EnvTarget("docker", None, install / ".env")
    if exists(str(install / "docker-compose.yml")) and not exists("/etc/marcellus/sidecar.yml"):
        return EnvTarget("docker", install / ".env", install / ".env")
    return EnvTarget("systemd", SYSTEMD_ENV_FILE, SYSTEMD_ENV_FILE)


def can_store(target: EnvTarget, *, is_root: bool | None = None) -> bool:
    """Can init write the password to `target` itself?"""
    if target.path is None:
        return False
    root = (os.geteuid() == 0) if is_root is None else is_root
    if target.kind == "systemd" and not root:
        return False  # root-owned by design
    if target.path.exists():
        return os.access(target.path, os.W_OK)
    return os.access(target.path.parent, os.W_OK)


def host_commands(target: EnvTarget, service_restart: str) -> list[str]:
    """Exact commands that store the password without it appearing anywhere:
    `read -s` takes it from the keyboard, `printf` hands it straight to the file."""
    path = target.host_path or target.path or SYSTEMD_ENV_FILE
    if target.kind == "systemd":
        return [
            f"sudo install -m 0600 -o root -g root /dev/null {path}  # only if it is missing",
            "read -rsp 'MQTT password: ' p; echo",
            f"printf '{ENV_KEY}=%s\\n' \"$p\" | sudo tee -a {path} >/dev/null; unset p",
            service_restart,
        ]
    return [
        "read -rsp 'MQTT password: ' p; echo",
        f"printf '{ENV_KEY}=%s\\n' \"$p\" | sudo tee -a {path} >/dev/null; unset p",
        service_restart,
    ]


# -- the env file --------------------------------------------------------------------


def _line_key(line: str) -> str | None:
    if line.startswith(("#", " ")) or "=" not in line:
        return None
    return line.split("=", 1)[0].strip()


def has_secret(path: Path, key: str = ENV_KEY) -> bool:
    """Is `key` set to something non-empty in the env file? (Value never returned.)"""
    return read_secret(path, key) is not None


def read_secret(path: Path, key: str = ENV_KEY) -> str | None:
    """The stored value, for testing the connection only; None when unset."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    value: str | None = None
    for line in lines:
        if _line_key(line) == key:
            raw = line.split("=", 1)[1].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                raw = raw[1:-1]
            value = raw or None
    return value


def format_env_value(value: str) -> str:
    """`value` as one env-file token that systemd and Docker Compose both read
    back unchanged: bare when plain, single-quoted when it holds spaces, `#`,
    `$`, `"` or a backslash. Newlines and single quotes cannot be represented
    safely in both, so those are refused (without echoing the value)."""
    if not value:
        raise EnvFileError("the password is empty")
    if any(c in value for c in "\n\r\x00'"):
        raise EnvFileError(
            "the password contains a newline or a single quote, which an env file cannot hold "
            "safely; use one without, or add the line to the env file yourself"
        )
    return value if _SAFE_VALUE.match(value) else f"'{value}'"


def write_secret(path: Path, value: str, key: str = ENV_KEY, *, private: bool = False) -> None:
    """Set `key` in the env file, keeping every other line.

    A new file is created with mode 0600. `private=True` (the systemd file)
    also tightens an existing file to 0600. The file is rewritten in place, so
    a bind-mounted file keeps its identity.
    """
    token = format_env_value(value)
    new_line = f"{key}={token}"
    try:
        existing = path.read_text().splitlines() if path.exists() else []
    except OSError as exc:
        raise EnvFileError(f"cannot read {path}: {exc.strerror or 'error'}") from None
    out: list[str] = []
    replaced = False
    for line in existing:
        if _line_key(line) == key:
            if not replaced:
                out.append(new_line)
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(new_line)
    body = "\n".join(out) + "\n"
    try:
        if path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            if private or not existing:
                os.fchmod(fd, 0o600)
            os.write(fd, body.encode())
        finally:
            os.close(fd)
    except OSError as exc:
        raise EnvFileError(f"cannot write {path}: {exc.strerror or 'error'}") from None


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def read_password_file(path: str) -> str:
    """The first line of a password file (trailing newline removed)."""
    try:
        text = Path(path).read_text()
    except OSError as exc:
        why = exc.strerror or "error"
        raise EnvFileError(f"cannot read the password file {path}: {why}") from None
    value = text.split("\n", 1)[0].rstrip("\r")
    if not value:
        raise EnvFileError(f"the password file {path} is empty")
    return value


# -- the decision --------------------------------------------------------------------


@dataclass
class PushDecision:
    """What the push step settled; `config` is merged into `sidecar.yml`."""

    config: dict[str, object]
    target: EnvTarget
    #: Written to the env file after sidecar.yml is saved.
    password_to_store: str | None = field(default=None, repr=False)
    #: Used for the connection test only (stored, supplied or from the environment).
    password_for_test: str | None = field(default=None, repr=False)
    #: Lines to print once, e.g. host commands when the file is out of reach.
    follow_up: list[str] = field(default_factory=list)
