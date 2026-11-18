"""Find Frigate's own configuration and read the facts `fsc init` needs from it.

Read-only: nothing here writes to Frigate's config or files, and nothing here
ever reads a password. Frigate's config can hold a literal password, an
environment placeholder (`{FRIGATE_MQTT_PASSWORD}`) or a secret mounted from a
file; the only thing recorded about any of them is that a login is needed
(`MqttFacts.needs_login`). The password value is dropped at parse time, so it
cannot reach a summary, a log line or an exception message.

Two sources, merged field by field:

- the running Frigate's `/api/config` (the config Frigate actually loaded, with
  environment placeholders already resolved and defaults filled in), preferred
  for every value it returns;
- Frigate's `config.yml` on disk, used for what the API leaves out, and as the
  only source when Frigate is not answering. A file is only believed to be THIS
  Frigate's when its camera list matches the API's.

Paths Frigate reports (`database.path`, the media directory) are paths inside
Frigate's container: they are returned as such and never used on this host
without a check that they exist here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from marcellus import frigate_tls

#: Frigate's own defaults, as the container sees them.
FRIGATE_DEFAULT_MEDIA = "/media/frigate"
FRIGATE_DEFAULT_DB = "/config/frigate.db"
FRIGATE_DEFAULT_TOPIC_PREFIX = "frigate"
FRIGATE_DEFAULT_MQTT_PORT = 1883

#: Where a Frigate checkout or install usually keeps its config (the same roots
#: `install.sh` searches), best first. Tests replace this.
SEARCH_ROOTS: tuple[str, ...] = (
    "/opt/frigate",
    "/srv/frigate",
    "/mnt/frigate",
    "/media/frigate",
    "/var/lib/frigate",
    "/etc/frigate",
    "/config",
)

_CONFIG_NAMES = ("config.yml", "config.yaml", "config/config.yml", "config/config.yaml")

#: `{FRIGATE_MQTT_PASSWORD}`-style placeholders (Frigate resolves them itself).
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class MqttFacts:
    """Frigate's `mqtt:` section, minus the password value."""

    enabled: bool = False
    host: str | None = None
    port: int | None = None
    user: str | None = None
    #: True when a user or a password is configured (the broker wants a login).
    needs_login: bool = False
    topic_prefix: str | None = None
    client_id: str | None = None
    #: Frigate talks TLS to the broker (Marcellus can only speak plain MQTT).
    tls: bool = False
    #: Fields whose value was an unresolved placeholder (host, user, ...).
    unresolved: list[str] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return self.topic_prefix or FRIGATE_DEFAULT_TOPIC_PREFIX


@dataclass
class FrigateFacts:
    version: str | None = None
    #: True when `/api/config` answered.
    api_ok: bool = False
    config_file: Path | None = None
    #: True: the file's cameras match the running Frigate; False: they do not;
    #: None: could not compare (Frigate not answering).
    file_verified: bool | None = None
    cameras: int | None = None
    auth_enabled: bool | None = None
    tls_enabled: bool | None = None
    mqtt: MqttFacts = field(default_factory=MqttFacts)
    #: Frigate's database path as Frigate sees it (a container path).
    db_path: str | None = None
    #: Frigate's media directory as Frigate sees it (a container path).
    media_path: str = FRIGATE_DEFAULT_MEDIA
    notes: list[str] = field(default_factory=list)

    @property
    def found_anything(self) -> bool:
        return self.api_ok or self.config_file is not None


# -- reading ------------------------------------------------------------------------


def read_config_file(path: Path) -> tuple[dict[str, Any] | None, str]:
    """`(parsed mapping, "")`, or `(None, why)` with a reason safe to print.

    Never puts the file's text in the reason: `yaml`'s own message quotes the
    offending line, which may hold a password.
    """
    try:
        text = path.read_text()
    except PermissionError:
        return None, "permission denied"
    except OSError as exc:
        return None, exc.strerror or "cannot be read"
    except UnicodeDecodeError:
        return None, "is not a text file"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f" (line {mark.line + 1})" if mark is not None else ""
        return None, f"is not valid YAML{line}"
    if data is None:
        return {}, ""
    if not isinstance(data, dict):
        return None, "is not a Frigate config (top level is not a mapping)"
    return data, ""


def fetch_api(
    base_url: str,
    *,
    verify_tls: bool = True,
    timeout: float = 3.0,
    get: Callable[..., Any] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """`(version, running config)` from Frigate's API; either may be None."""
    fetch = get or frigate_tls.get
    root = base_url.rstrip("/")
    version: str | None = None
    try:
        resp = fetch(root + "/api/version", verify_tls=verify_tls, timeout=timeout)
        if getattr(resp, "status_code", 200) < 400:
            version = str(resp.text).strip().strip('"') or None
    except Exception:  # noqa: BLE001 -- Frigate being down is the common case
        return None, None
    config: dict[str, Any] | None = None
    try:
        resp = fetch(root + "/api/config", verify_tls=verify_tls, timeout=timeout)
        if getattr(resp, "status_code", 200) < 400:
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("cameras"), dict):
                config = data
    except Exception:  # noqa: BLE001 -- not JSON, auth wall, ...: no API config
        config = None
    return version, config


def _camera_names(cfg: dict[str, Any] | None) -> set[str]:
    cams = (cfg or {}).get("cameras")
    return {str(k) for k in cams} if isinstance(cams, dict) else set()


def candidate_config_paths(
    hint: str | os.PathLike[str] | None = None,
    *,
    roots: Iterable[str] | None = None,
    environ: dict[str, str] | None = None,
) -> list[Path]:
    """Where Frigate's config.yml may be, best first. A directory is looked into."""
    env = os.environ if environ is None else environ
    out: list[Path] = []

    def add(p: str | os.PathLike[str]) -> None:
        path = Path(p).expanduser()
        if path.is_dir():
            for name in _CONFIG_NAMES[:2]:
                add(path / name)
            return
        if path not in out:
            out.append(path)

    if hint:
        add(hint)
    for key in ("FRIGATE_CONFIG", "CONFIG_FILE"):
        if env.get(key):
            add(env[key])
    for root in SEARCH_ROOTS if roots is None else roots:
        for name in _CONFIG_NAMES:
            add(Path(root) / name)
    home = env.get("HOME")
    if home:
        for name in _CONFIG_NAMES:
            add(Path(home) / "frigate" / name)
    return out


def find_config_file(
    api_cfg: dict[str, Any] | None,
    *,
    hint: str | os.PathLike[str] | None = None,
    roots: Iterable[str] | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[Path | None, dict[str, Any] | None, bool | None, list[str]]:
    """`(path, parsed, verified, notes)` for the config file that is Frigate's.

    With the API answering, a file counts only when its cameras overlap the
    running Frigate's (or both have none): another project's config.yml, or a
    stale copy, is skipped with a note. Without the API the first readable
    config wins, unverified. An explicit `hint` is used even when it does not
    match (the user said so), with `verified=False`.
    """
    notes: list[str] = []
    api_cams = _camera_names(api_cfg)
    first_unverified: tuple[Path, dict[str, Any]] | None = None
    mismatched: list[Path] = []
    hint_path = Path(hint).expanduser() if hint else None
    for path in candidate_config_paths(hint, roots=roots, environ=environ):
        if not path.exists():
            continue
        data, why = read_config_file(path)
        if data is None:
            notes.append(f"{path}: {why}")
            continue
        file_cams = _camera_names(data)
        if api_cfg is None:
            if "cameras" not in data and "mqtt" not in data and "detectors" not in data:
                continue  # some other project's config.yml
            if first_unverified is None:
                first_unverified = (path, data)
            continue
        if (file_cams & api_cams) or (not file_cams and not api_cams):
            return path, data, True, notes
        mismatched.append(path)
        if hint_path is not None and path == hint_path:
            first_unverified = (path, data)
    if api_cfg is None and first_unverified is not None:
        return first_unverified[0], first_unverified[1], None, notes
    for path in mismatched:
        notes.append(f"{path}: its cameras do not match the running Frigate, so it was skipped")
    if first_unverified is not None:
        return first_unverified[0], first_unverified[1], False, notes
    return None, None, None, notes


# -- merging the two sources --------------------------------------------------------


def _section(cfg: dict[str, Any] | None, name: str) -> dict[str, Any]:
    value = (cfg or {}).get(name)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    """A non-empty string, or None. Placeholders are handled by the caller."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _is_placeholder(value: str | None) -> bool:
    return bool(value and _PLACEHOLDER.search(value))


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _port(value: Any) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def mqtt_from(api_cfg: dict[str, Any] | None, file_cfg: dict[str, Any] | None) -> MqttFacts:
    """The `mqtt:` section, API values first, the file for what the API lacks."""
    api = _section(api_cfg, "mqtt")
    fil = _section(file_cfg, "mqtt")
    facts = MqttFacts()

    def pick(key: str) -> tuple[Any, bool]:
        """`(value, was_unresolved_placeholder)`: API first, then the file."""
        for src in (api, fil):
            raw = src.get(key)
            text = _text(raw)
            if text is None:
                continue
            if _is_placeholder(text):
                continue
            return raw, False
        return None, _is_placeholder(_text(fil.get(key)))

    enabled = _bool(api.get("enabled"))
    if enabled is None:
        enabled = _bool(fil.get("enabled"))
    # A mapping with no `enabled` key: Frigate treats the section as off unless
    # told otherwise only when it is absent; present means the user set it up.
    facts.enabled = bool(enabled) if enabled is not None else bool(fil)

    for key, attr in (("host", "host"), ("user", "user"), ("topic_prefix", "topic_prefix")):
        value, unresolved = pick(key)
        setattr(facts, attr, _text(value))
        if unresolved:
            facts.unresolved.append(key)
    value, unresolved = pick("client_id")
    facts.client_id = _text(value)
    value, unresolved = pick("port")
    facts.port = _port(value)
    if unresolved:
        facts.unresolved.append("port")
    # The password VALUE is never read: only whether one is configured.
    has_password = any(_text(src.get("password")) is not None for src in (api, fil))
    facts.needs_login = has_password or facts.user is not None or "user" in facts.unresolved
    facts.tls = any(
        _text(src.get(key)) is not None
        for src in (api, fil)
        for key in ("tls_ca_certs", "tls_client_cert", "tls_client_key")
    )
    return facts


def facts_from(
    version: str | None,
    api_cfg: dict[str, Any] | None,
    file_path: Path | None,
    file_cfg: dict[str, Any] | None,
    file_verified: bool | None,
    notes: Sequence[str] = (),
) -> FrigateFacts:
    facts = FrigateFacts(
        version=version,
        api_ok=api_cfg is not None,
        config_file=file_path,
        file_verified=file_verified,
        notes=list(notes),
    )
    cams = _camera_names(api_cfg) or _camera_names(file_cfg)
    facts.cameras = len(cams) if (api_cfg is not None or file_cfg is not None) else None
    for attr, name in (("auth_enabled", "auth"), ("tls_enabled", "tls")):
        value = _bool(_section(api_cfg, name).get("enabled"))
        if value is None:
            value = _bool(_section(file_cfg, name).get("enabled"))
        setattr(facts, attr, value)
    facts.mqtt = mqtt_from(api_cfg, file_cfg)
    for src in (api_cfg, file_cfg):
        path = _text(_section(src, "database").get("path"))
        if path and not _is_placeholder(path):
            facts.db_path = path
            break
    if facts.db_path is None and (api_cfg is not None or file_cfg is not None):
        facts.db_path = FRIGATE_DEFAULT_DB
    return facts


def discover(
    base_url: str,
    *,
    verify_tls: bool = True,
    config_hint: str | os.PathLike[str] | None = None,
    roots: Iterable[str] | None = None,
    environ: dict[str, str] | None = None,
    timeout: float = 3.0,
) -> FrigateFacts:
    """Ask the running Frigate and read its config file, then merge."""
    version, api_cfg = fetch_api(base_url, verify_tls=verify_tls, timeout=timeout)
    path, file_cfg, verified, notes = find_config_file(
        api_cfg, hint=config_hint, roots=roots, environ=environ
    )
    facts = facts_from(version, api_cfg, path, file_cfg, verified, notes)
    if path is not None and verified is False:
        facts.notes.append(
            f"{path} does not match the running Frigate's cameras; check it is the right file"
        )
    return facts


# -- host-side paths ----------------------------------------------------------------


def host_db_candidates(
    config_file: Path | None,
    container_db: str | None,
    *,
    environ: dict[str, str] | None = None,
) -> list[Path]:
    """Host paths where Frigate's database may be, derived from where its
    config.yml sits (Frigate's `/config` is that file's directory)."""
    env = os.environ if environ is None else environ
    name = Path(container_db or FRIGATE_DEFAULT_DB).name
    out: list[Path] = []

    def add(p: Path) -> None:
        if p not in out:
            out.append(p)

    if env.get("FRIGATE_DB_DIR"):
        add(Path(env["FRIGATE_DB_DIR"]) / name)
    if config_file is not None:
        base = config_file.parent
        if container_db and container_db.startswith("/config/"):
            add(base / container_db[len("/config/") :])
        add(base / name)
        add(base / "database" / name)
        add(base.parent / "database" / name)
    return out


def first_existing_file(paths: Iterable[Path]) -> Path | None:
    for p in paths:
        if p.is_file():
            return p
    return None


def media_root_of(recording_path: str) -> str | None:
    """Frigate's media directory, from one `recordings.path` row.

    Frigate stores `<media>/recordings/<date>/<hour>/<camera>/<mm.ss>.mp4`, so
    the media directory is everything before the first `/recordings/` segment.
    """
    marker = "/recordings/"
    idx = recording_path.find(marker)
    if idx <= 0:
        return None
    return recording_path[:idx]


@dataclass(frozen=True)
class RecordingsCheck:
    #: ok: looks right; warn: usable but unusual (say why); bad: not usable.
    status: str
    path: Path
    message: str


def check_recordings_root(path: Path) -> RecordingsCheck:
    """Does `path` look like the host copy of Frigate's media directory (the
    folder that holds `recordings/`)? A path that IS the recordings folder is
    corrected to its parent, because `frigate.recordings_path` replaces
    `frigate.media_path` and the `recordings/` segment comes from the database.
    """
    if not path.exists():
        return RecordingsCheck("bad", path, "does not exist")
    if not path.is_dir():
        return RecordingsCheck("bad", path, "is not a directory")
    if not os.access(path, os.R_OK | os.X_OK):
        return RecordingsCheck("bad", path, "is not readable")
    if (path / "recordings").is_dir():
        return RecordingsCheck("ok", path, "holds a recordings/ folder")
    try:
        entries = sorted(p.name for p in path.iterdir())
    except OSError:
        return RecordingsCheck("bad", path, "is not readable")
    looks_like_recordings = path.name == "recordings" or any(_DATE_DIR.match(e) for e in entries)
    if looks_like_recordings and path.parent != path:
        return RecordingsCheck(
            "ok",
            path.parent,
            f"{path} is Frigate's recordings folder itself; using the folder above it, "
            f"{path.parent} (the database names the recordings/ part)",
        )
    if not entries:
        return RecordingsCheck(
            "warn", path, "is empty (fine for a brand-new Frigate; otherwise check the path)"
        )
    return RecordingsCheck(
        "warn",
        path,
        "has no recordings/ folder inside it, so it may not be Frigate's media directory",
    )
