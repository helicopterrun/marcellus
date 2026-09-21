"""Runtime tuning overrides + effective-config machinery for `/settings`'
Tuning panel and `GET/PUT /v1/tuning` (`routes/tuning.py`).

`KNOBS` is a registry covering every field of every `Settings` section (plus
root `log_level`) with the metadata the UI needs to render a control for it:
whether it's user-editable at all (`editable`), whether a change reaches the
running process without a restart (`live`), its display kind, and any
range/choice constraints. Everything else -- reading/writing the override
file, validating a PUT body, applying overrides in place, and reporting
"effective value + where it came from" -- lives here too, with no FastAPI
import, so `config.load_settings` can use it for the CLI/watchdog/
face_capture-timer processes as well as the web app.

No import of `marcellus.config` at call time is a problem here (this module
imports it freely); the cycle risk runs the other way -- `config.py` imports
this module lazily, inside `load_settings`, to avoid it.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from marcellus.config import (
    EncountersSection,
    FaceCaptureSection,
    FaceEnrichSection,
    FrigateSection,
    ProxySection,
    PushSection,
    ScrubSection,
    Settings,
    SidecarSection,
    WatchdogSection,
)

logger = logging.getLogger(__name__)

KnobKind = Literal[
    "int",
    "float",
    "bool",
    "str",
    "enum",
    "list_str",
    "path",
    "url",
    "secret",
    "dict_int",
    "json",
    "pair_list",
]


@dataclass(frozen=True)
class Knob:
    section: str  # top-level Settings field, e.g. "push"; "" for root fields (log_level)
    field: str
    kind: KnobKind
    live: bool = False  # True = in-place mutation reaches the reader immediately
    editable: bool = True  # False = display-only in the UI (wiring/secrets)
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def key(self) -> str:
        return f"{self.section}.{self.field}" if self.section else self.field


_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "frigate": FrigateSection,
    "sidecar": SidecarSection,
    "face_capture": FaceCaptureSection,
    "face_enrich": FaceEnrichSection,
    "watchdog": WatchdogSection,
    "scrub": ScrubSection,
    "proxy": ProxySection,
    "encounters": EncountersSection,
    "push": PushSection,
}

#: Pure-model defaults (no env/YAML involvement -- these are plain
#: `BaseModel`s, not `BaseSettings`, so constructing them touches nothing).
_DEFAULT_SECTIONS: dict[str, Any] = {name: model() for name, model in _SECTION_MODELS.items()}
_DEFAULT_ROOT: dict[str, Any] = {"log_level": "INFO"}

#: Sections that are "separate processes" (watchdog timer, face-capture
#: timer): every field there is user-editable but restart-required, even
#: ones that would otherwise look like wiring (hosts/paths/commands) --
#: the value lands in the override file and the next run of that process
#: (which calls `load_settings` itself) picks it up.
_RESTART_PROCESS_SECTIONS = {"face_capture", "watchdog"}

#: Sections that are pure wiring end to end (auth origins, DB/media paths) --
#: every field is display-only.
_ALL_WIRING_SECTIONS = {"frigate"}

#: Field-name patterns that mark a field as wiring/secret (`editable=False`)
#: outside of `_RESTART_PROCESS_SECTIONS`/`_ALL_WIRING_SECTIONS`.
_WIRING_SUFFIXES = ("_url", "_path", "_dir", "_host", "_port")
_WIRING_EXPLICIT_FIELDS = {
    "mqtt_host",
    "mqtt_port",
    "mqtt_username",
    "mqtt_password",
    "mqtt_client_id",
    "mqtt_topic_reviews",
    "mqtt_topic_available",
    "mqtt_topic_events",
    "relay_key",
    "server_id",
    "pass_request_headers",
}
_SECRET_FIELDS = {"mqtt_password", "relay_key"}

#: `section.field` -> live=True (see settings-spec.md Part A2's classification).
_LIVE_KEYS = {
    "encounters.backfill_lookback_s",
    "encounters.reconcile_interval_s",
    "encounters.gap_s",
    "encounters.max_duration_s",
    "encounters.recent_cameras",
    "encounters.min_copresence_s",
    "encounters.adjacency",
    "encounters.not_adjacent",
    "encounters.transition_min_samples",
    "encounters.transition_max_sample_s",
    "encounters.transition_learn_interval_s",
    "encounters.transition_learn_window_days",
    "encounters.transition_default_s",
    "encounters.transition_overrides",
    "encounters.transition_slack",
    "encounters.use_learned_gaps",
    "push.delivery_urgent_resound_s",
    "push.delivery_urgent_resound_enabled",
    "push.delivery_urgent_resound_max",
    "push.delivery_backfill_staleness_s",
    "push.offline_silence_s",
    "sidecar.login_rate_limit_attempts",
    "sidecar.login_rate_limit_window_s",
    "sidecar.auth_cache_ttl_s",
    "sidecar.remember_ttl_s",
    "scrub.retention_days",
    "scrub.min_free_bytes",
    "scrub.ffmpeg_concurrency",
    "scrub.backfill_segments_per_cycle",
    "scrub.backfill_time_budget_s",
    "log_level",
    "face_enrich.interval_s",
    "encounters.timeline_max_window_s",
}
#: `face_enrich.*` is live=True except `interval_s` (already above) and
#: `model_dir` (still wiring -- see `_field_editable`/`_field_kind`).
_LIVE_SECTION_EXCEPT = {"face_enrich": {"interval_s", "model_dir", "enabled"}}
# `face_enrich.enabled` is intentionally left restart-required: whether the
# background loop task exists at all is decided once, at server startup.

_ENUM_CHOICES: dict[str, tuple[str, ...]] = {
    "scrub.format": ("jpeg", "webp"),
    "push.transport": ("mock", "relay"),
    "push.dwell_source": ("events", "reviews"),
    "log_level": ("DEBUG", "INFO", "WARNING", "ERROR"),
}

#: Mirrors the matching pydantic `Field(ge=..., le=...)` constraints.
_RANGE_OVERRIDES: dict[str, tuple[float | None, float | None]] = {
    "push.mqtt_queue_max": (100, 100000),
    "push.relay_retry_attempts": (1, 10),
    "push.relay_breaker_failures": (1, 50),
    "push.relay_breaker_open_s": (1, 600),
}

_HELP_OVERRIDES: dict[str, str] = {
    "log_level": "Root/uvicorn logger level.",
    "encounters.gap_s": "Max gap (s) before a new atom starts a new encounter, by label family.",
    "encounters.adjacency": (
        "Extra camera-pair edges added to the zone-derived adjacency graph, "
        "one 'camera_a, camera_b' pair per line."
    ),
    "encounters.not_adjacent": (
        "Camera-pair edges removed from the zone-derived adjacency graph, "
        "one 'camera_a, camera_b' pair per line."
    ),
    "encounters.transition_default_s": (
        "Fallback {p10, p50, p90} seconds written for a camera-pair/family edge "
        "with too few samples."
    ),
    "encounters.transition_overrides": (
        "Manual transition overrides, keyed 'camA>camB' or 'camA>camB:family', "
        "each an object of {p10, p50, p90}."
    ),
    "encounters.use_learned_gaps": (
        "Live: when on, the linker's 'adjacent' reason uses learned camera-pair "
        "transition times (p90 x transition_slack) instead of the flat gap_s "
        "allowance, for pairs with enough learned samples."
    ),
    "encounters.transition_slack": (
        "Live: multiplier applied to a learned p90 to get the allowed gap when "
        "use_learned_gaps is on."
    ),
    "scrub.format": "Sprite-sheet cell image format.",
    "push.transport": "Push transport: mock (log only) or relay (real APNs via the relay).",
    "push.dwell_source": "Where a situation's loiter check gets its clock: events or reviews.",
}


def _unwrap_optional(annotation: Any) -> Any:
    if get_origin(annotation) in (Union, types.UnionType):
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _infer_kind(annotation: Any) -> KnobKind:
    annotation = _unwrap_optional(annotation)
    origin = get_origin(annotation)
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is str:
        return "str"
    if annotation is Path:
        return "path"
    if origin is list:
        return "list_str"
    if origin is dict:
        return "dict_int"
    return "str"


def _field_editable(section: str, name: str) -> bool:
    if section in _RESTART_PROCESS_SECTIONS:
        return True
    if section in _ALL_WIRING_SECTIONS:
        return False
    if name in _SECRET_FIELDS or name in _WIRING_EXPLICIT_FIELDS:
        return False
    return not name.endswith(_WIRING_SUFFIXES)


def _field_live(section: str, name: str, editable: bool) -> bool:
    if not editable:
        return False
    key = f"{section}.{name}" if section else name
    if key in _LIVE_KEYS:
        return True
    exceptions = _LIVE_SECTION_EXCEPT.get(section)
    return exceptions is not None and name not in exceptions


def _field_kind(section: str, name: str, annotation: Any) -> KnobKind:
    key = f"{section}.{name}" if section else name
    if key in _ENUM_CHOICES:
        return "enum"
    if name in _SECRET_FIELDS:
        return "secret"
    if name.endswith("_url"):
        return "url"
    if name.endswith(("_path", "_dir")) or annotation is Path:
        return "path"
    if key == "encounters.gap_s":
        return "dict_int"
    if key in ("encounters.adjacency", "encounters.not_adjacent"):
        return "pair_list"
    if key in ("push.delivery_zone_place_map", "push.delivery_la_families"):
        return "json"
    if key == "encounters.transition_overrides":
        return "json"
    return _infer_kind(annotation)


def _build_knobs() -> tuple[Knob, ...]:
    knobs: list[Knob] = []
    for section, model in _SECTION_MODELS.items():
        for name, model_field in model.model_fields.items():
            editable = _field_editable(section, name)
            live = _field_live(section, name, editable)
            kind = _field_kind(section, name, model_field.annotation)
            key = f"{section}.{name}"
            lo, hi = _RANGE_OVERRIDES.get(key, (None, None))
            if lo is None and hi is None and kind in ("int", "float"):
                lo = 0
            knobs.append(
                Knob(
                    section=section,
                    field=name,
                    kind=kind,
                    live=live,
                    editable=editable,
                    min=lo,
                    max=hi,
                    choices=_ENUM_CHOICES.get(key, ()),
                    help=_HELP_OVERRIDES.get(key, (model_field.description or "")),
                )
            )
    # Root field.
    knobs.append(
        Knob(
            section="",
            field="log_level",
            kind="enum",
            live="log_level" in _LIVE_KEYS,
            editable=True,
            choices=_ENUM_CHOICES["log_level"],
            help=_HELP_OVERRIDES["log_level"],
        )
    )
    return tuple(knobs)


KNOBS: tuple[Knob, ...] = _build_knobs()
KNOBS_BY_KEY: dict[str, Knob] = {k.key: k for k in KNOBS}


# --------------------------------------------------------------------------
# Two snapshots, both taken by `config.load_settings`:
#  * base    -- yaml/env/defaults BEFORE the override file is applied. What a
#               removed override reverts to (`routes/tuning.py` PUT).
#  * startup -- AFTER the file was applied: the values this process actually
#               booted with. `pending_restart` compares the live object to
#               this, so an override that was already in the file at boot is
#               not "pending", and removing a non-live override is.
# --------------------------------------------------------------------------

_BASE_SNAPSHOT: dict[str, Any] | None = None
_STARTUP_SNAPSHOT: dict[str, Any] | None = None


def snapshot_base(settings: Settings) -> None:
    """Record `settings` before any override is applied (revert target)."""
    global _BASE_SNAPSHOT
    _BASE_SNAPSHOT = settings.model_dump()


def snapshot_startup(settings: Settings) -> None:
    """Record the values the process is starting with (after the override
    file was applied). If `snapshot_base` was not called first, this also
    serves as the base -- the two only differ when a file was applied."""
    global _BASE_SNAPSHOT, _STARTUP_SNAPSHOT
    _STARTUP_SNAPSHOT = settings.model_dump()
    if _BASE_SNAPSHOT is None:
        _BASE_SNAPSHOT = _STARTUP_SNAPSHOT


def get_startup_snapshot() -> dict[str, Any] | None:
    """The revert target for a removed override (the base snapshot)."""
    return _BASE_SNAPSHOT


def reset_for_tests() -> None:
    """Test isolation hook, same purpose as `push.policy_settings.reset_for_tests`."""
    global _BASE_SNAPSHOT, _STARTUP_SNAPSHOT
    _BASE_SNAPSHOT = None
    _STARTUP_SNAPSHOT = None


def snapshot_value(snapshot: dict[str, Any], knob: Knob) -> Any:
    if not knob.section:
        return snapshot.get(knob.field)
    return snapshot.get(knob.section, {}).get(knob.field)


def get_field(settings: Settings, knob: Knob) -> Any:
    target = getattr(settings, knob.section) if knob.section else settings
    return getattr(target, knob.field)


def set_field(settings: Settings, knob: Knob, value: Any) -> None:
    target = getattr(settings, knob.section) if knob.section else settings
    if knob.kind == "path" and value is not None and not isinstance(value, Path):
        value = Path(value)
    setattr(target, knob.field, value)
    if not knob.section and knob.field == "log_level" and isinstance(value, str):
        apply_log_level(value)


def apply_log_level(level: str) -> None:
    """Set the root logger (and uvicorn's) level -- the A4 refactor that
    makes `log_level` a live knob."""
    lvl = getattr(logging, level.upper(), None)
    if not isinstance(lvl, int):
        return
    logging.getLogger().setLevel(lvl)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(lvl)


# --------------------------------------------------------------------------
# Override file
# --------------------------------------------------------------------------


def overrides_path(settings: Settings) -> Path:
    """Resolve `sidecar.tuning_path`, relative to CWD like `push_settings_path`."""
    return Path(settings.sidecar.tuning_path)


def read_rev(path: str | Path) -> int:
    try:
        with Path(path).open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 1
    rev = data.get("_rev") if isinstance(data, dict) else None
    return rev if isinstance(rev, int) and rev > 0 else 1


def read_overrides(path: str | Path) -> dict[str, Any]:
    """Flat `{"section.field": value}`. Missing file -> `{}`; corrupt file
    -> log a warning and return `{}` (never raises)."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("tuning: %s is corrupt, ignoring overrides", p)
        return {}
    if not isinstance(data, dict):
        logger.warning("tuning: %s does not contain a JSON object, ignoring overrides", p)
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def write_overrides(path: str | Path, overrides: dict[str, Any]) -> int:
    """Atomic write (tmp + `os.replace`); sorted keys, indent 2. Returns the
    new revision."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_rev = (read_rev(p) if p.exists() else 0) + 1
    payload = {"_rev": new_rev, **overrides}
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, p)
    return new_rev


# --------------------------------------------------------------------------
# Env-lock detection (shared by `config.load_settings` and `effective`)
# --------------------------------------------------------------------------


def _env_var_names(knob: Knob) -> tuple[str, ...]:
    field_upper = knob.field.upper()
    if knob.section:
        section_upper = knob.section.upper()
        return (
            f"MARCELLUS_{section_upper}__{field_upper}",
            f"FRIGATE_SIDECAR_{section_upper}__{field_upper}",
        )
    return (f"MARCELLUS_{field_upper}", f"FRIGATE_SIDECAR_{field_upper}")


def is_env_locked(key: str, environ: Any = None) -> bool:
    """True when an env var pins `key` (a `"section.field"` string), env
    always winning over the override file."""
    environ = environ if environ is not None else os.environ
    knob = KNOBS_BY_KEY.get(key)
    if knob is None:
        return False
    return any(name in environ for name in _env_var_names(knob))


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _range_errors(knob: Knob, value: float) -> list[str]:
    errs = []
    if knob.min is not None and value < knob.min:
        errs.append(f"{knob.key}: must be >= {knob.min}")
    if knob.max is not None and value > knob.max:
        errs.append(f"{knob.key}: must be <= {knob.max}")
    return errs


def _type_errors(knob: Knob, value: Any) -> list[str]:
    k = knob.kind
    if k == "bool":
        if not isinstance(value, bool):
            return [f"{knob.key}: expected a boolean"]
        return []
    if k == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{knob.key}: expected an integer"]
        return _range_errors(knob, value)
    if k == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{knob.key}: expected a number"]
        return _range_errors(knob, value)
    if k in ("str", "path", "url", "secret"):
        if not isinstance(value, str):
            return [f"{knob.key}: expected a string"]
        return []
    if k == "enum":
        if value not in knob.choices:
            return [f"{knob.key}: must be one of {knob.choices}, got {value!r}"]
        return []
    if k == "list_str":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return [f"{knob.key}: expected a list of strings"]
        return []
    if k == "dict_int":
        if not isinstance(value, dict) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value.values()
        ):
            return [f"{knob.key}: expected an object of numbers"]
        return []
    if k == "json":
        if not isinstance(value, dict):
            return [f"{knob.key}: expected an object"]
        return []
    if k == "pair_list":
        if not isinstance(value, list) or not all(
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(x, str) and x for x in pair)
            for pair in value
        ):
            return [f"{knob.key}: expected a list of [camera_a, camera_b] pairs"]
        if any(pair[0] == pair[1] for pair in value):
            return [f"{knob.key}: a camera pair can't name the same camera twice"]
        return []
    return []  # pragma: no cover -- exhaustive over KnobKind


def normalise_pairs(value: list[list[str]]) -> list[list[str]]:
    """Sort each `[camera_a, camera_b]` pair and dedupe, for a `pair_list`
    knob (`encounters.adjacency`/`not_adjacent`) -- pair order shouldn't
    matter to storage or to `build_adjacency`, which treats them as an
    undirected edge."""
    seen: list[list[str]] = []
    for pair in value:
        sorted_pair = sorted(pair)
        if sorted_pair not in seen:
            seen.append(sorted_pair)
    return seen


def _default_settings_dump() -> dict[str, Any]:
    dump: dict[str, Any] = {name: model.model_dump() for name, model in _DEFAULT_SECTIONS.items()}
    dump.update(_DEFAULT_ROOT)
    return dump


def validate(overrides: dict[str, Any], settings: Settings | None = None) -> list[str]:
    """Unknown key, non-editable key, wrong type, out of range, bad enum --
    then a pydantic cross-field pass (scrub's derived-interval validator and
    friends) against `settings.model_dump()` (or, with no `settings` given,
    the pure defaults) with `overrides` applied on top."""
    errors: list[str] = []
    for key, value in overrides.items():
        knob = KNOBS_BY_KEY.get(key)
        if knob is None:
            errors.append(f"unknown key: {key!r}")
            continue
        if not knob.editable:
            errors.append(f"{key} is not editable")
            continue
        errors.extend(_type_errors(knob, value))
    if errors:
        return errors

    base = settings.model_dump() if settings is not None else _default_settings_dump()
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        knob = KNOBS_BY_KEY[key]
        section_dict = merged if not knob.section else merged.setdefault(knob.section, {})
        if knob.kind == "dict_int" and isinstance(value, dict):
            section_dict[knob.field] = {**section_dict.get(knob.field, {}), **value}
        else:
            section_dict[knob.field] = value
    try:
        Settings.model_validate(merged)
    except ValidationError as exc:
        for err in exc.errors():
            errors.append(err.get("msg", str(err)))
    return errors


# --------------------------------------------------------------------------
# Apply / effective / pending-restart
# --------------------------------------------------------------------------


def apply_overrides(settings: Settings, overrides: dict[str, Any]) -> None:
    """In-place `setattr` for every key, live or not -- non-live ones still
    land so the value is visible, and are picked up by the next restart or
    (for `face_capture`/`watchdog`) the next timer run."""
    for key, value in overrides.items():
        knob = KNOBS_BY_KEY.get(key)
        if knob is None:
            continue
        if knob.kind == "dict_int" and isinstance(value, dict):
            current = dict(get_field(settings, knob) or {})
            current.update(value)
            set_field(settings, knob, current)
        else:
            set_field(settings, knob, value)


def _mask(knob: Knob, value: Any) -> Any:
    if knob.kind == "secret":
        return "••••" if value else ""
    if knob.kind == "path" and isinstance(value, Path):
        return str(value)
    return value


def _default_value(knob: Knob) -> Any:
    if not knob.section:
        return _DEFAULT_ROOT.get(knob.field)
    return getattr(_DEFAULT_SECTIONS[knob.section], knob.field)


def effective(
    settings: Settings, overrides: dict[str, Any], environ: Any = None
) -> list[dict[str, Any]]:
    """One row per `Knob`: current value, default, and where the current
    value came from (`default`/`yaml`/`env`/`override`)."""
    environ = environ if environ is not None else os.environ
    rows: list[dict[str, Any]] = []
    for knob in KNOBS:
        locked = is_env_locked(knob.key, environ)
        current = get_field(settings, knob)
        default = _default_value(knob)
        override_value = overrides.get(knob.key)
        if knob.key in overrides and override_value is not None:
            source = "override"
        elif locked:
            source = "env"
        elif current != default:
            source = "yaml"
        else:
            source = "default"
        rows.append(
            {
                "key": knob.key,
                "section": knob.section,
                "field": knob.field,
                "kind": knob.kind,
                "value": _mask(knob, current),
                "default": _mask(knob, default),
                "source": source,
                "live": knob.live,
                "editable": knob.editable,
                "min": knob.min,
                "max": knob.max,
                "choices": list(knob.choices),
                "help": knob.help,
                "locked": locked,
            }
        )
    return rows


def pending_restart(settings: Settings, overrides: dict[str, Any] | None = None) -> list[str]:
    """Every `live=False` knob whose in-memory value differs from what the
    process booted with -- the "restart required" list. Covers both an added
    override and a removed one (the field reverted, but the running code
    still holds the old value). `overrides` is accepted for call-site
    symmetry with `effective()` and not needed."""
    snapshot = _STARTUP_SNAPSHOT
    if snapshot is None:
        return []
    pending: list[str] = []
    for knob in KNOBS:
        if knob.live:
            continue
        current = get_field(settings, knob)
        startup_value = snapshot_value(snapshot, knob)
        if knob.kind == "path":
            current = str(current) if current is not None else current
            startup_value = str(startup_value) if startup_value is not None else startup_value
        if current != startup_value:
            pending.append(knob.key)
    return sorted(pending)
