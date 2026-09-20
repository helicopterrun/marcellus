"""User-editable attention-ladder policy (Elsinore Phase 4).

Phase 2/3 shipped the routing table (`ladder_policy.TABLE`), the zone-place
heuristic (`delivery_wire.classify_place`), and the Live Activity family
toggles as hardcoded/config-file data. Phase 4 makes all three
user-editable from the app's settings screens, backed by one JSON document
(`config/push_settings.json`, not the sidecar's sqlite DB -- the app PUTs
the whole object on Save, and round-tripping a batch document through rows
and back is pure overhead this doesn't need) and one HTTP surface
(`routes/push.py`'s `/settings` endpoints).

This module owns the settings document's shape, defaults, validation,
persistence, and *application* -- `apply_settings` is the one place that
pushes a new routing table into `ladder_policy.set_table`, so "the sidecar
applies the new settings immediately" (design doc) is just "the next
`get_active()`/`ladder_policy.TABLE` read sees it", with no cache to
invalidate anywhere else.

`ladder_policy.TABLE`'s own literal default is deliberately left alone
(`ladder_policy.py`'s docstring on `set_table`) -- `DEFAULT_ROUTING_TABLE`
below is a separate, independent literal matching the product brief's
current recommended defaults, which differ from the routing engine's own
built-in baseline (`thing` no longer defaults to `quiet` at yard/doors/
private). The two are allowed to diverge: one is "what the evaluator ships
with if nothing ever touches it" (Phase 1's own tested baseline), the other
is "what a *freshly onboarded* settings file starts the user at".
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from marcellus.push import ladder_policy
from marcellus.push.live_activities import FAMILIES

#: Bumped only on a breaking shape change; the app pins against it the same
#: way the card payload contract does (`delivery.CONTRACT_VERSION`).
#: v2 (2026-08-20): package/bin/opening outcome rows exist; the LA family
#: booleans and alert_all_changes are derived, never authoritative.
SETTINGS_VERSION = 2

SUBJECTS = ("stranger", "known", "animal", "thing")
SUBJECTS_V2 = ("person", "vehicle", "animal", "thing")
#: V3 (2026-08-20, "one alerts stack"): the LA families that used to be
#: booleans become routed subjects with their own outcome rows. Only these
#: three moved -- person/person_restricted were already outcome-routed.
#: `routing_table_v2`/`outcomes` accept all seven; the extra rows are
#: optional in a PUT (an older app sends four and must keep working).
SUBJECTS_V3 = SUBJECTS_V2 + ("package", "bin", "opening")
SUBJECTS_V3_EXTRA = tuple(s for s in SUBJECTS_V3 if s not in SUBJECTS_V2)
PLACES = ("street", "yard", "doors", "private", "off_limits")
LEVELS = ladder_policy.LEVELS
RECOGNITION_MODES = ("off", "relax_one", "relax_to_quiet")

#: The merged outcome ladder (content design 2026-08-16): one dial per
#: subject x place answering "what happens?", folding the old level +
#: LA-family choice together. Ordinal, low -> high.
#:   off    -- suppressed entirely
#:   log    -- recorded, visible in the app, no delivery
#:   glance -- Live Activity only, never a banner or sound
#:   notify -- banner + Live Activity
#:   alarm  -- urgent: sound, re-sounds, time-sensitive
OUTCOMES = ("off", "log", "glance", "notify", "alarm")

#: Bijection with the routing levels the evaluator consumes ("off" has no
#: level -- it is enforced as suppression before evaluation).
LEVEL_TO_OUTCOME = {"log": "log", "quiet": "glance", "notify": "notify", "urgent": "alarm"}
OUTCOME_TO_LEVEL = {
    "off": "log",
    "log": "log",
    "glance": "quiet",
    "notify": "notify",
    "alarm": "urgent",
}

#: The routing table a freshly onboarded settings file starts the user at
#: (design doc §1, "match the v3 brief's §1.4 table exactly").
DEFAULT_ROUTING_TABLE: dict[str, dict[str, str]] = {
    "stranger": {
        "street": "log",
        "yard": "quiet",
        "doors": "notify",
        "private": "notify",
        "off_limits": "urgent",
    },
    "known": {
        "street": "log",
        "yard": "log",
        "doors": "quiet",
        "private": "quiet",
        "off_limits": "quiet",
    },
    "animal": {
        "street": "log",
        "yard": "quiet",
        "doors": "quiet",
        "private": "quiet",
        "off_limits": "quiet",
    },
    "thing": {
        "street": "log",
        "yard": "log",
        "doors": "log",
        "private": "log",
        "off_limits": "quiet",
    },
}

DEFAULT_ROUTING_TABLE_V2: dict[str, dict[str, str]] = {
    "person": {
        "street": "log",
        "yard": "quiet",
        "doors": "notify",
        "private": "notify",
        "off_limits": "urgent",
    },
    "vehicle": {
        "street": "log",
        "yard": "quiet",
        "doors": "quiet",
        "private": "quiet",
        "off_limits": "notify",
    },
    "animal": {
        "street": "log",
        "yard": "quiet",
        "doors": "quiet",
        "private": "quiet",
        "off_limits": "quiet",
    },
    "thing": {
        "street": "log",
        "yard": "log",
        "doors": "log",
        "private": "log",
        "off_limits": "quiet",
    },
}

#: Default outcome rows for the V3 subjects, chosen to reproduce what the
#: retired family booleans did when on: the family ran a silent LA wherever
#: it could fire (glance), with a heads-up in a restricted place (notify).
#: Cells where the family was noise (a package "on the street", bins deep
#: in a private area) idle at log. Authored in outcome vocabulary because
#: these subjects exist for the merged ladder; their `routing_table_v2`
#: rows are derived via `OUTCOME_TO_LEVEL` like every other lockstep row.
DEFAULT_EXTRA_OUTCOMES: dict[str, dict[str, str]] = {
    "package": {
        "street": "log",
        "yard": "glance",
        "doors": "glance",
        "private": "glance",
        "off_limits": "notify",
    },
    "bin": {
        "street": "glance",
        "yard": "glance",
        "doors": "glance",
        "private": "log",
        "off_limits": "notify",
    },
    "opening": {
        "street": "log",
        "yard": "glance",
        "doors": "glance",
        "private": "glance",
        "off_limits": "notify",
    },
}

DEFAULT_RECOGNITION: dict[str, str] = {
    "known_person": "relax_one",
    "known_vehicle": "relax_one",
}

#: Zone-name -> place-class guessing heuristic (design doc §5), checked in
#: this order -- most specific/alarming classes first, broadest/catch-all
#: last. Two collisions this order exists to resolve: "front_entry_person"
#: contains both a `yard` hint ("front") and a `doors` hint ("entry") -- the
#: doc's own example expects `doors` to win, so it's checked first. And
#: "sidewalk" (an explicit `street` pattern) contains "side" (a `private`
#: pattern) as a substring -- `street` is checked before `private` so the
#: doc's own literal example pattern wins over the coincidental substring.
_GUESS_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("doors", ("door", "gate", "entry", "entrance", "window")),
    ("off_limits", ("pool", "shed", "equipment", "restricted")),
    ("street", ("street", "road", "sidewalk", "curb", "highway")),
    ("private", ("back", "side", "rear", "alley", "fence")),
    ("yard", ("driveway", "porch", "walk", "path", "front", "parking")),
)

#: Name hints for "this zone/camera could be an opening" (`available_openings`
#: in the `GET` response) -- a superset of the `doors` place-class guess
#: patterns, since a garage is an opening the openings LA family cares about
#: even though it doesn't read as a "doors" *place class* on its own.
_OPENING_NAME_HINTS = ("door", "gate", "garage", "entry", "entrance")

#: 16-point compass rose for `camera_optics.faces`.
COMPASS_POINTS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


def _valid_time(val: str) -> bool:
    try:
        parts = val.split(":")
        if len(parts) != 2:
            return False
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, TypeError):
        return False


def _time_to_minutes(val: str) -> int:
    h, m = val.split(":")
    return int(h) * 60 + int(m)


def is_quiet_hours(settings: dict[str, Any], now_minutes: int) -> tuple[bool, str]:
    """Check if `now_minutes` (minutes since midnight, local) falls in the
    quiet-hours window. Returns `(active, mode)`. Wrap-around ranges
    (e.g. 22:00–07:00) are supported."""
    qh = settings.get("quiet_hours")
    if not isinstance(qh, dict):
        return False, ""
    try:
        start = _time_to_minutes(qh["start"])
        end = _time_to_minutes(qh["end"])
        mode = qh.get("mode", "cap_quiet")
    except (KeyError, ValueError, TypeError):
        return False, ""
    if start <= end:
        active = start <= now_minutes < end
    else:
        active = now_minutes >= start or now_minutes < end
    return active, mode if active else ""


def guess_zone_class(name: str, cameras: tuple[str, ...] = ()) -> str:
    """Best-effort place-class guess from a zone's name, falling back to its
    camera name(s) if the zone name itself doesn't hint at anything (e.g. a
    zone named after a street address whose camera is literally called
    "street"). Defaults to `"yard"` -- the safest middle ground: not silent
    like `street`, not alarming like `doors`."""
    for candidate in (name, *cameras):
        low = candidate.lower()
        for place, patterns in _GUESS_ORDER:
            if any(p in low for p in patterns):
                return place
    return "yard"


def _looks_like_opening(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in _OPENING_NAME_HINTS)


def default_settings() -> dict[str, Any]:
    return {
        "v": SETTINGS_VERSION,
        "routing_table": {subject: dict(row) for subject, row in DEFAULT_ROUTING_TABLE.items()},
        "routing_table_v2": {
            subject: dict(row) for subject, row in DEFAULT_ROUTING_TABLE_V2.items()
        }
        | {
            subject: {place: OUTCOME_TO_LEVEL[outcome] for place, outcome in row.items()}
            for subject, row in DEFAULT_EXTRA_OUTCOMES.items()
        },
        "recognition": dict(DEFAULT_RECOGNITION),
        "outcomes": {
            subject: {place: LEVEL_TO_OUTCOME[level] for place, level in row.items()}
            for subject, row in DEFAULT_ROUTING_TABLE_V2.items()
        }
        | {subject: dict(row) for subject, row in DEFAULT_EXTRA_OUTCOMES.items()},
        "zone_classes": {},
        # zone key -> human display name for notification copy ("back_critter"
        # -> "the back walkway"). Wins over Frigate's friendly_name. Edited on
        # the /zones page; empty string clears.
        "zone_names": {},
        "zone_overrides": {},
        "live_activities": {family: True for family in FAMILIES}
        | {
            "opening_picks": [],
            "delivery": "la_first",
            "alert_all_changes": False,
            "la_only": False,
        },
        "escalation_sound": "urgent",
        "mute_sounds": True,
        "quiet_hours": None,
        # camera -> [cameras that watch the same physical approach]. Dedup
        # merges same-kind stories across declared neighbors even when their
        # zone sets are disjoint (the stairway-tight/walkway split,
        # 2026-08-14). Symmetric at read time -- declaring one direction is
        # enough. Config-side only for now; the app never writes it.
        "camera_neighbors": {},
        # camera -> {"dx","dy"}: unit vector in normalized image space
        # (y down) meaning "toward home / the protected area", drawn on the
        # /cameras page. Movement dotted against it yields the LA heading
        # chip. Config/page-side only, sticky across app PUTs.
        "camera_headings": {},
        # camera -> {"x","y"} in 0..1 (+ optional "azimuth" degrees, 0 =
        # north/up clockwise, and "fov" degrees): position and view pie on
        # the /cameras layout map, up = north by convention. Feeds neighbor
        # suggestions (wedge overlap); never affects routing.
        "camera_layout": {},
        # {"x0","y0","x1","y1"} in 0..1 on the layout map: the drawn
        # "secure area" rectangle (home + protected ground). Declarative
        # for now — display and future direction semantics.
        "secure_area": None,
        # Real-world width of the layout map, in feet. Unlocks world-space
        # track projection (ground.world_position) and map trails.
        "map_scale_ft": None,
        # camera -> physical rig facts {hfov, mount_ft, tilt_deg, vfov?,
        # faces?, lens?, note?}. Seeded once from optics.DEPLOYMENT_SEED at
        # startup; edited on /cameras (onboarding + detail panel). Feeds
        # ground.camera_ground. Sticky across app PUTs.
        "camera_optics": {},
        # Let geometric fusion (cross-camera cluster within the
        # distance-scaled merge threshold, push/fusion.py) adopt an open
        # same-label card instead of minting a duplicate when zone dedup
        # found nothing. Default OFF: enable after grepping
        # "geometric_dedup: would_suppress" logs against real duplicates.
        "geometric_dedup": False,
        # None, or {ext, w, h, uploaded_at, calibration}: the uploaded
        # floorplan/site image behind the layout map. `calibration` remembers
        # the drawn reference line ({x0,y0,x1,y1,length_ft}) so it can be
        # redrawn/re-edited; map_scale_ft stays the operative scale value.
        "floorplan": None,
    }


def validate_settings(data: Any) -> list[str]:
    """Human-readable validation errors, empty if `data` is acceptable.
    Unknown *top-level* fields are ignored (forward compat, design doc §2);
    within the three known blocks, an unknown subject/place/family key is
    rejected outright -- those vocabularies are closed, so a typo there is
    much more likely a client bug than a field this sidecar hasn't learned
    about yet."""
    if not isinstance(data, dict):
        return ["settings must be an object"]
    errors: list[str] = []

    routing_table = data.get("routing_table")
    if routing_table is not None:
        if not isinstance(routing_table, dict):
            errors.append("routing_table must be an object")
        else:
            unknown_subjects = set(routing_table) - set(SUBJECTS)
            if unknown_subjects:
                errors.append(f"routing_table has unknown subject(s): {sorted(unknown_subjects)}")
            for subject in SUBJECTS:
                row = routing_table.get(subject)
                if not isinstance(row, dict):
                    errors.append(f"routing_table.{subject} must be an object")
                    continue
                unknown_places = set(row) - set(PLACES)
                if unknown_places:
                    errors.append(
                        f"routing_table.{subject} has unknown place(s): {sorted(unknown_places)}"
                    )
                for place in PLACES:
                    level = row.get(place)
                    if level not in LEVELS:
                        errors.append(
                            f"routing_table.{subject}.{place} must be one of {LEVELS},"
                            f" got {level!r}"
                        )

    routing_table_v2 = data.get("routing_table_v2")
    if routing_table_v2 is not None:
        if not isinstance(routing_table_v2, dict):
            errors.append("routing_table_v2 must be an object")
        else:
            unknown_subjects = set(routing_table_v2) - set(SUBJECTS_V3)
            if unknown_subjects:
                errors.append(
                    f"routing_table_v2 has unknown subject(s): {sorted(unknown_subjects)}"
                )
            for subject in SUBJECTS_V3:
                row = routing_table_v2.get(subject)
                # V3 rows are optional in a PUT: an older app sends only
                # the classic four and must not start failing validation.
                if row is None and subject in SUBJECTS_V3_EXTRA:
                    continue
                if not isinstance(row, dict):
                    errors.append(f"routing_table_v2.{subject} must be an object")
                    continue
                unknown_places = set(row) - set(PLACES)
                if unknown_places:
                    errors.append(
                        f"routing_table_v2.{subject} has unknown place(s): {sorted(unknown_places)}"
                    )
                for place in PLACES:
                    level = row.get(place)
                    if level not in LEVELS:
                        errors.append(
                            f"routing_table_v2.{subject}.{place} must be one of {LEVELS}, "
                            f"got {level!r}"
                        )

    recognition = data.get("recognition")
    if recognition is not None:
        if not isinstance(recognition, dict):
            errors.append("recognition must be an object")
        else:
            for key in ("known_person", "known_vehicle"):
                val = recognition.get(key)
                if val is not None and val not in RECOGNITION_MODES:
                    errors.append(
                        f"recognition.{key} must be one of {RECOGNITION_MODES}, got {val!r}"
                    )

    outcomes = data.get("outcomes")
    if outcomes is not None:
        if not isinstance(outcomes, dict):
            errors.append("outcomes must be an object")
        else:
            unknown = set(outcomes) - set(SUBJECTS_V3)
            if unknown:
                errors.append(f"outcomes has unknown subject(s): {sorted(unknown)}")
            for subject in SUBJECTS_V3:
                row = outcomes.get(subject)
                if row is None:
                    continue
                if not isinstance(row, dict):
                    errors.append(f"outcomes.{subject} must be an object")
                    continue
                for place, outcome in row.items():
                    if place not in PLACES:
                        errors.append(f"outcomes.{subject} has unknown place {place!r}")
                    elif outcome not in OUTCOMES:
                        errors.append(
                            f"outcomes.{subject}.{place} must be one of {OUTCOMES}, got {outcome!r}"
                        )

    zone_names = data.get("zone_names")
    if zone_names is not None:
        if not isinstance(zone_names, dict):
            errors.append("zone_names must be an object")
        else:
            for zone, name in zone_names.items():
                if not isinstance(name, str):
                    errors.append(f"zone_names.{zone} must be a string")

    zone_classes = data.get("zone_classes")
    if zone_classes is not None:
        if not isinstance(zone_classes, dict):
            errors.append("zone_classes must be an object")
        else:
            for zone, place in zone_classes.items():
                if place not in PLACES:
                    errors.append(f"zone_classes.{zone} must be one of {PLACES}, got {place!r}")

    zone_overrides = data.get("zone_overrides")
    if zone_overrides is not None:
        if not isinstance(zone_overrides, dict):
            errors.append("zone_overrides must be an object")
        else:
            # Zone names themselves are unrestricted (design doc §4 -- the
            # user might configure a zone before it appears in Frigate);
            # only the inner subject/level vocabulary is closed.
            valid_subjects = set(SUBJECTS) | set(SUBJECTS_V3)
            for zone, row in zone_overrides.items():
                if not isinstance(row, dict):
                    errors.append(f"zone_overrides.{zone} must be an object")
                    continue
                for subject, level in row.items():
                    if subject not in valid_subjects:
                        errors.append(
                            f"zone_overrides.{zone} has unknown subject {subject!r}, "
                            f"must be one of {sorted(valid_subjects)}"
                        )
                        continue
                    if level not in LEVELS:
                        errors.append(
                            f"zone_overrides.{zone}.{subject} must be one of {LEVELS}, "
                            f"got {level!r}"
                        )

    live_activities = data.get("live_activities")
    if live_activities is not None:
        if not isinstance(live_activities, dict):
            errors.append("live_activities must be an object")
        else:
            for family in FAMILIES:
                if family in live_activities and not isinstance(live_activities[family], bool):
                    errors.append(f"live_activities.{family} must be a boolean")
            picks = live_activities.get("opening_picks")
            if picks is not None and not (
                isinstance(picks, list) and all(isinstance(p, str) for p in picks)
            ):
                errors.append("live_activities.opening_picks must be a list of strings")
            aac = live_activities.get("alert_all_changes")
            if aac is not None and not isinstance(aac, bool):
                errors.append("live_activities.alert_all_changes must be a boolean")
            lao = live_activities.get("la_only")
            if lao is not None and not isinstance(lao, bool):
                errors.append("live_activities.la_only must be a boolean")
            delivery = live_activities.get("delivery")
            if delivery is not None and delivery not in ("la_first", "notifications"):
                errors.append("live_activities.delivery must be 'la_first' or 'notifications'")

    mute_sounds = data.get("mute_sounds")
    if mute_sounds is not None and not isinstance(mute_sounds, bool):
        errors.append("mute_sounds must be a boolean")

    geometric_dedup = data.get("geometric_dedup")
    if geometric_dedup is not None and not isinstance(geometric_dedup, bool):
        errors.append("geometric_dedup must be a boolean")

    quiet_hours = data.get("quiet_hours")
    if quiet_hours is not None:
        if not isinstance(quiet_hours, dict):
            errors.append("quiet_hours must be an object or null")
        else:
            for field in ("start", "end"):
                val = quiet_hours.get(field)
                if not isinstance(val, str) or not _valid_time(val):
                    errors.append(f"quiet_hours.{field} must be HH:MM (got {val!r})")
            mode = quiet_hours.get("mode")
            if mode not in ("cap_quiet", "mute_sounds"):
                errors.append(
                    f"quiet_hours.mode must be 'cap_quiet' or 'mute_sounds', got {mode!r}"
                )

    camera_neighbors = data.get("camera_neighbors")
    if camera_neighbors is not None:
        if not isinstance(camera_neighbors, dict):
            errors.append("camera_neighbors must be an object of camera -> [cameras]")
        else:
            for cam, neighbors in camera_neighbors.items():
                if not isinstance(neighbors, list) or not all(
                    isinstance(n, str) for n in neighbors
                ):
                    errors.append(f"camera_neighbors[{cam!r}] must be a list of camera names")

    camera_headings = data.get("camera_headings")
    if camera_headings is not None:
        if not isinstance(camera_headings, dict):
            errors.append("camera_headings must be an object of camera -> {dx, dy}")
        else:
            for cam, vec in camera_headings.items():
                ok = (
                    isinstance(vec, dict)
                    and isinstance(vec.get("dx"), (int, float))
                    and isinstance(vec.get("dy"), (int, float))
                    and math.isfinite(vec["dx"])
                    and math.isfinite(vec["dy"])
                    and (vec["dx"] or vec["dy"])
                )
                if not ok:
                    errors.append(f"camera_headings[{cam!r}] must be a non-zero {{dx, dy}} vector")

    camera_layout = data.get("camera_layout")
    if camera_layout is not None:
        if not isinstance(camera_layout, dict):
            errors.append("camera_layout must be an object of camera -> {x, y}")
        else:
            for cam, pos in camera_layout.items():
                ok = (
                    isinstance(pos, dict)
                    and isinstance(pos.get("x"), (int, float))
                    and isinstance(pos.get("y"), (int, float))
                    and 0.0 <= pos["x"] <= 1.0
                    and 0.0 <= pos["y"] <= 1.0
                )
                if ok and "azimuth" in pos:
                    ok = isinstance(pos["azimuth"], (int, float)) and math.isfinite(pos["azimuth"])
                if ok and "fov" in pos:
                    ok = isinstance(pos["fov"], (int, float)) and 10.0 <= pos["fov"] <= 360.0
                if not ok:
                    errors.append(
                        f"camera_layout[{cam!r}] must be {{x, y}} within 0..1 "
                        "(optional azimuth degrees, fov 10..360)"
                    )

    secure_area = data.get("secure_area")
    if secure_area is not None:
        ok = isinstance(secure_area, dict) and all(
            isinstance(secure_area.get(k), (int, float)) and 0.0 <= secure_area[k] <= 1.0
            for k in ("x0", "y0", "x1", "y1")
        )
        if not ok:
            errors.append("secure_area must be null or {x0, y0, x1, y1} within 0..1")

    map_scale_ft = data.get("map_scale_ft")
    if map_scale_ft is not None and not (
        isinstance(map_scale_ft, (int, float)) and 0 < map_scale_ft <= 100000
    ):
        errors.append("map_scale_ft must be null or a positive number of feet")

    camera_optics_doc = data.get("camera_optics")
    if camera_optics_doc is not None:
        if not isinstance(camera_optics_doc, dict):
            errors.append("camera_optics must be an object of camera -> rig facts")
        else:
            for cam, facts in camera_optics_doc.items():
                if not isinstance(facts, dict):
                    errors.append(f"camera_optics[{cam!r}] must be an object")
                    continue
                hfov = facts.get("hfov")
                if not (isinstance(hfov, (int, float)) and 10.0 < hfov <= 360.0):
                    errors.append(f"camera_optics[{cam!r}].hfov must be in (10, 360] degrees")
                mount = facts.get("mount_ft")
                if not (isinstance(mount, (int, float)) and 0.0 < mount <= 500.0):
                    errors.append(f"camera_optics[{cam!r}].mount_ft must be in (0, 500] feet")
                tilt = facts.get("tilt_deg")
                if not (isinstance(tilt, (int, float)) and -90.0 <= tilt <= 90.0):
                    errors.append(f"camera_optics[{cam!r}].tilt_deg must be in [-90, 90] degrees")
                vfov = facts.get("vfov")
                if vfov is not None and not (
                    isinstance(vfov, (int, float)) and 5.0 < vfov <= 180.0
                ):
                    errors.append(f"camera_optics[{cam!r}].vfov must be in (5, 180] degrees")
                faces = facts.get("faces")
                if faces is not None and faces not in COMPASS_POINTS:
                    errors.append(
                        f"camera_optics[{cam!r}].faces must be a 16-point compass "
                        f"direction, got {faces!r}"
                    )
                for text_key in ("lens", "note"):
                    val = facts.get(text_key)
                    if val is not None and not isinstance(val, str):
                        errors.append(f"camera_optics[{cam!r}].{text_key} must be a string")

    floorplan = data.get("floorplan")
    if floorplan is not None:
        if not isinstance(floorplan, dict):
            errors.append("floorplan must be null or an object")
        else:
            if not (isinstance(floorplan.get("ext"), str) and floorplan["ext"]):
                errors.append("floorplan.ext must be a non-empty string")
            for dim in ("w", "h"):
                v = floorplan.get(dim)
                if not (isinstance(v, int) and 0 < v <= 20000):
                    errors.append(f"floorplan.{dim} must be a positive pixel count")
            rot = floorplan.get("rotation_deg")
            if rot is not None and not (isinstance(rot, (int, float)) and -360 <= rot <= 360):
                errors.append("floorplan.rotation_deg must be a number in -360..360")
            cal = floorplan.get("calibration")
            if cal is not None:
                ok = (
                    isinstance(cal, dict)
                    and all(
                        isinstance(cal.get(k), (int, float)) and 0.0 <= cal[k] <= 1.0
                        for k in ("x0", "y0", "x1", "y1")
                    )
                    and isinstance(cal.get("length_ft"), (int, float))
                    and (0 < cal.get("length_ft", 0) <= 100000)
                )
                if not ok:
                    errors.append(
                        "floorplan.calibration must be null or {x0, y0, x1, y1} within "
                        "0..1 plus a positive length_ft"
                    )

    return errors


def _derived_family_booleans(outcomes: dict[str, dict[str, str]]) -> dict[str, Any]:
    """The retired `live_activities` family booleans, derived from the
    outcome ladder for GET responses: an older app's toggles then display
    what the ladder actually does (a fully-off row reads as family off),
    and `alert_all_changes` is permanently False -- per-cell outcomes
    replaced it (glance updates stay silent, notify+ pops)."""

    def alive(subject: str) -> bool:
        row = outcomes.get(subject, {})
        return any(cell != "off" for cell in row.values()) if row else True

    return {
        "package": alive("package"),
        "bins": alive("bin"),
        "openings": alive("opening"),
        "person": alive("person"),
        "person_restricted": outcomes.get("person", {}).get("off_limits", "alarm") != "off",
        "alert_all_changes": False,
    }


def normalize_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Fill in anything missing or invalid from a persisted-but-partial
    document with defaults, so a settings file written before a schema
    addition (a new LA family, say) survives forever without ever handing a
    reader a `KeyError`."""
    merged = default_settings()

    routing_table = data.get("routing_table")
    if isinstance(routing_table, dict):
        for subject in SUBJECTS:
            row = routing_table.get(subject)
            if isinstance(row, dict):
                for place in PLACES:
                    if row.get(place) in LEVELS:
                        merged["routing_table"][subject][place] = row[place]

    routing_table_v2 = data.get("routing_table_v2")
    if isinstance(routing_table_v2, dict):
        for subject in SUBJECTS_V3:
            row = routing_table_v2.get(subject)
            if isinstance(row, dict):
                for place in PLACES:
                    if row.get(place) in LEVELS:
                        merged["routing_table_v2"][subject][place] = row[place]

    recognition = data.get("recognition")
    if isinstance(recognition, dict):
        for key in ("known_person", "known_vehicle"):
            val = recognition.get(key)
            if val in RECOGNITION_MODES:
                merged["recognition"][key] = val

    outcomes = data.get("outcomes")
    stored_outcomes = (_active or {}).get("outcomes", {}) if isinstance(_active, dict) else {}
    if isinstance(outcomes, dict):
        # Outcomes are the authority: copy valid cells over the defaults,
        # then derive the legacy routing levels from them so the evaluator
        # (and an older app build reading routing_table_v2) stays in step.
        # A body that omits a V3 subject entirely (a four-subject app build)
        # keeps the *stored* row, not the default -- otherwise every PUT
        # from an older app would quietly reset the new rows.
        for subject in SUBJECTS_V3:
            row = outcomes.get(subject)
            if not isinstance(row, dict) and subject in SUBJECTS_V3_EXTRA:
                row = stored_outcomes.get(subject)
            if isinstance(row, dict):
                for place in PLACES:
                    if row.get(place) in OUTCOMES:
                        merged["outcomes"][subject][place] = row[place]
        for subject in SUBJECTS_V3:
            for place in PLACES:
                merged["routing_table_v2"][subject][place] = OUTCOME_TO_LEVEL[
                    merged["outcomes"][subject][place]
                ]
    else:
        # Legacy body (an older app build): derive outcomes from its levels.
        # "off" survives a legacy round trip: the legacy shape renders an
        # off cell as "log", so a stored off + incoming log stays off.
        for subject in SUBJECTS_V2:
            for place in PLACES:
                level = merged["routing_table_v2"][subject][place]
                derived = LEVEL_TO_OUTCOME[level]
                was_off = (
                    isinstance(stored_outcomes.get(subject), dict)
                    and stored_outcomes[subject].get(place) == "off"
                )
                merged["outcomes"][subject][place] = (
                    "off" if derived == "log" and was_off else derived
                )
        # Legacy bodies predate the V3 subjects entirely: preserve the
        # stored rows (or fall back to defaults) and keep v2 in lockstep.
        for subject in SUBJECTS_V3_EXTRA:
            row = stored_outcomes.get(subject)
            if isinstance(row, dict):
                for place in PLACES:
                    if row.get(place) in OUTCOMES:
                        merged["outcomes"][subject][place] = row[place]
            for place in PLACES:
                merged["routing_table_v2"][subject][place] = OUTCOME_TO_LEVEL[
                    merged["outcomes"][subject][place]
                ]

    zone_names = data.get("zone_names")
    if isinstance(zone_names, dict):
        merged["zone_names"] = {
            str(zone): str(name)
            for zone, name in zone_names.items()
            if isinstance(name, str) and name.strip()
        }

    zone_classes = data.get("zone_classes")
    if isinstance(zone_classes, dict):
        merged["zone_classes"] = {
            str(zone): place for zone, place in zone_classes.items() if place in PLACES
        }

    zone_overrides = data.get("zone_overrides")
    if isinstance(zone_overrides, dict):
        cleaned: dict[str, dict[str, str]] = {}
        for zone, row in zone_overrides.items():
            if not isinstance(row, dict):
                continue
            valid_subjects = set(SUBJECTS) | set(SUBJECTS_V3)
            valid_row = {
                subject: level
                for subject, level in row.items()
                if subject in valid_subjects and level in LEVELS
            }
            # An override that ends up empty after filtering (or was saved
            # empty to begin with) is removed, not kept as a no-op entry
            # (design doc §4's "cleaned up on save").
            if valid_row:
                cleaned[str(zone)] = valid_row
        merged["zone_overrides"] = cleaned

    live_activities = data.get("live_activities")
    if isinstance(live_activities, dict):
        # The family booleans and alert_all_changes are retired (one alerts
        # stack, 2026-08-20): incoming values are accepted and ignored --
        # the outcome ladder is the authority now, and the derivation below
        # keeps an older app's UI honest about what the ladder says.
        picks = live_activities.get("opening_picks")
        if isinstance(picks, list):
            merged["live_activities"]["opening_picks"] = [str(p) for p in picks]
        if isinstance(live_activities.get("la_only"), bool):
            merged["live_activities"]["la_only"] = live_activities["la_only"]
        delivery = live_activities.get("delivery")
        if delivery in ("la_first", "notifications"):
            merged["live_activities"]["delivery"] = delivery
    merged["live_activities"] |= _derived_family_booleans(merged["outcomes"])

    escalation_sound = data.get("escalation_sound")
    if isinstance(escalation_sound, str) and escalation_sound:
        merged["escalation_sound"] = escalation_sound

    if isinstance(data.get("mute_sounds"), bool):
        merged["mute_sounds"] = data["mute_sounds"]

    if isinstance(data.get("geometric_dedup"), bool):
        merged["geometric_dedup"] = data["geometric_dedup"]

    quiet_hours = data.get("quiet_hours")
    if quiet_hours is None:
        merged["quiet_hours"] = None
    elif isinstance(quiet_hours, dict):
        start = quiet_hours.get("start", "")
        end = quiet_hours.get("end", "")
        mode = quiet_hours.get("mode", "cap_quiet")
        if _valid_time(start) and _valid_time(end) and mode in ("cap_quiet", "mute_sounds"):
            merged["quiet_hours"] = {"start": start, "end": end, "mode": mode}

    camera_neighbors = data.get("camera_neighbors")
    if isinstance(camera_neighbors, dict):
        cleaned_neighbors: dict[str, list[str]] = {}
        for cam, neighbors in camera_neighbors.items():
            if not isinstance(neighbors, list):
                continue
            names = [str(n) for n in neighbors if isinstance(n, str) and n and n != cam]
            if names:
                cleaned_neighbors[str(cam)] = names
        merged["camera_neighbors"] = cleaned_neighbors

    camera_headings = data.get("camera_headings")
    if isinstance(camera_headings, dict):
        cleaned_headings: dict[str, dict[str, float]] = {}
        for cam, vec in camera_headings.items():
            if not isinstance(vec, dict):
                continue
            dx, dy = vec.get("dx"), vec.get("dy")
            if not (
                isinstance(dx, (int, float))
                and isinstance(dy, (int, float))
                and math.isfinite(dx)
                and math.isfinite(dy)
            ):
                continue
            length = math.hypot(dx, dy)
            if length < 1e-6:
                continue
            cleaned_headings[str(cam)] = {
                "dx": round(dx / length, 4),
                "dy": round(dy / length, 4),
            }
        merged["camera_headings"] = cleaned_headings

    camera_layout = data.get("camera_layout")
    if isinstance(camera_layout, dict):
        cleaned_layout: dict[str, dict[str, Any]] = {}
        for cam, pos in camera_layout.items():
            if not isinstance(pos, dict):
                continue
            x, y = pos.get("x"), pos.get("y")
            if not (
                isinstance(x, (int, float))
                and isinstance(y, (int, float))
                and 0.0 <= x <= 1.0
                and 0.0 <= y <= 1.0
            ):
                continue
            entry: dict[str, Any] = {"x": round(float(x), 4), "y": round(float(y), 4)}
            azimuth = pos.get("azimuth")
            if isinstance(azimuth, (int, float)) and math.isfinite(azimuth):
                entry["azimuth"] = round(float(azimuth) % 360.0, 1)
            fov = pos.get("fov")
            if isinstance(fov, (int, float)) and 10.0 <= fov <= 360.0:
                entry["fov"] = round(float(fov), 1)
            if pos.get("locked") is True:
                entry["locked"] = True
            cleaned_layout[str(cam)] = entry
        merged["camera_layout"] = cleaned_layout

    secure_area = data.get("secure_area")
    if isinstance(secure_area, dict):
        vals = {}
        for k in ("x0", "y0", "x1", "y1"):
            v = secure_area.get(k)
            if isinstance(v, (int, float)) and 0.0 <= v <= 1.0:
                vals[k] = float(v)
        if len(vals) == 4:
            # Normalize corner order so (x0,y0) is always top-left.
            merged["secure_area"] = {
                "x0": round(min(vals["x0"], vals["x1"]), 4),
                "y0": round(min(vals["y0"], vals["y1"]), 4),
                "x1": round(max(vals["x0"], vals["x1"]), 4),
                "y1": round(max(vals["y0"], vals["y1"]), 4),
            }
    elif secure_area is None and "secure_area" in data:
        merged["secure_area"] = None

    map_scale_ft = data.get("map_scale_ft")
    if isinstance(map_scale_ft, (int, float)) and 0 < map_scale_ft <= 100000:
        merged["map_scale_ft"] = round(float(map_scale_ft), 1)
    elif map_scale_ft is None and "map_scale_ft" in data:
        merged["map_scale_ft"] = None

    camera_optics_doc = data.get("camera_optics")
    if isinstance(camera_optics_doc, dict):
        cleaned_optics: dict[str, dict[str, Any]] = {}
        for cam, facts in camera_optics_doc.items():
            if not isinstance(facts, dict):
                continue
            hfov, mount, tilt = facts.get("hfov"), facts.get("mount_ft"), facts.get("tilt_deg")
            if not (
                isinstance(hfov, (int, float))
                and 10.0 < hfov <= 360.0
                and isinstance(mount, (int, float))
                and 0.0 < mount <= 500.0
                and isinstance(tilt, (int, float))
                and -90.0 <= tilt <= 90.0
            ):
                continue
            entry = {
                "hfov": round(float(hfov), 1),
                "mount_ft": round(float(mount), 1),
                "tilt_deg": round(float(tilt), 1),
            }
            vfov = facts.get("vfov")
            if isinstance(vfov, (int, float)) and 5.0 < vfov <= 180.0:
                entry["vfov"] = round(float(vfov), 1)
            if facts.get("faces") in COMPASS_POINTS:
                entry["faces"] = facts["faces"]
            for text_key in ("lens", "note"):
                val = facts.get(text_key)
                if isinstance(val, str) and val.strip():
                    entry[text_key] = val.strip()
            cleaned_optics[str(cam)] = entry
        merged["camera_optics"] = cleaned_optics

    floorplan = data.get("floorplan")
    if isinstance(floorplan, dict):
        ext = floorplan.get("ext")
        w, h = floorplan.get("w"), floorplan.get("h")
        if (
            isinstance(ext, str)
            and ext
            and isinstance(w, int)
            and 0 < w <= 20000
            and isinstance(h, int)
            and 0 < h <= 20000
        ):
            fp: dict[str, Any] = {"ext": ext, "w": w, "h": h, "calibration": None}
            uploaded_at = floorplan.get("uploaded_at")
            if isinstance(uploaded_at, str) and uploaded_at:
                fp["uploaded_at"] = uploaded_at
            rot = floorplan.get("rotation_deg")
            if isinstance(rot, (int, float)) and -360 <= rot <= 360 and rot % 360:
                fp["rotation_deg"] = round(float(rot) % 360, 1)
            cal = floorplan.get("calibration")
            if isinstance(cal, dict):
                vals = {
                    k: float(cal[k])
                    for k in ("x0", "y0", "x1", "y1")
                    if isinstance(cal.get(k), (int, float)) and 0.0 <= cal[k] <= 1.0
                }
                cal_len = cal.get("length_ft")
                if len(vals) == 4 and isinstance(cal_len, (int, float)) and 0 < cal_len <= 100000:
                    fp["calibration"] = {
                        **{k: round(v, 4) for k, v in vals.items()},
                        "length_ft": round(float(cal_len), 1),
                    }
            merged["floorplan"] = fp
    elif floorplan is None and "floorplan" in data:
        merged["floorplan"] = None

    return merged


def derived_camera_heading(
    camera: str,
    settings: dict[str, Any] | None = None,
) -> dict[str, float] | None:
    """The "toward home" image-space unit vector derived from world
    geometry: camera position + pie azimuth (camera_layout) and the
    secure_area rectangle's center, both drawn on /cameras.

    Ground-plane projection: decompose the world direction camera->secure
    center into the camera's view axis (ahead) and right axis. Ahead maps
    to UP in the frame (smaller y), right maps to right — a first-order
    perspective model, plenty for the 60-degree classification bands.
    An explicit camera_headings entry always wins over this."""
    s = settings if settings is not None else get_active()
    layout = s.get("camera_layout", {})
    entry = layout.get(camera) if isinstance(layout, dict) else None
    area = s.get("secure_area")
    if not isinstance(entry, dict) or not isinstance(area, dict):
        return None
    azimuth = entry.get("azimuth")
    if not isinstance(azimuth, (int, float)):
        return None
    cx = (area.get("x0", 0.0) + area.get("x1", 0.0)) / 2.0
    cy = (area.get("y0", 0.0) + area.get("y1", 0.0)) / 2.0
    wx, wy = cx - entry.get("x", 0.0), cy - entry.get("y", 0.0)
    if math.hypot(wx, wy) < 1e-6:
        return None  # camera sits exactly on the secure center
    rad = math.radians(azimuth)
    # Map coords are y-down; compass 0 = north = -y, clockwise.
    view = (math.sin(rad), -math.cos(rad))
    right = (math.cos(rad), math.sin(rad))
    d_along = wx * view[0] + wy * view[1]
    d_right = wx * right[0] + wy * right[1]
    norm = math.hypot(d_along, d_right)
    if norm < 1e-6:
        return None
    return {"dx": round(d_right / norm, 4), "dy": round(-d_along / norm, 4)}


def seeded_camera_optics() -> dict[str, dict[str, Any]]:
    """A fresh `camera_optics` table built from `optics.DEPLOYMENT_SEED` —
    what `startup` writes into a settings file that predates the key."""
    from marcellus.analysis import optics

    return {
        cam["id"]: {
            k: cam[k]
            for k in ("hfov", "vfov", "mount_ft", "tilt_deg", "faces", "lens", "note")
            if k in cam
        }
        for cam in optics.DEPLOYMENT_SEED
    }


def camera_optics(camera: str, settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The camera's physical rig facts (hfov/mount_ft/tilt_deg/...), or None
    when the camera hasn't been onboarded. This is what `ground.camera_ground`
    projects with — settings-backed, so an onboarding/edit on /cameras takes
    effect on the next event with no restart."""
    table = (settings if settings is not None else get_active()).get("camera_optics", {})
    entry = table.get(camera) if isinstance(table, dict) else None
    return dict(entry) if isinstance(entry, dict) else None


def camera_neighbor_set(camera: str, settings: dict[str, Any] | None = None) -> frozenset[str]:
    """Cameras declared adjacent to `camera`, symmetric closure -- declaring
    `a: [b]` makes `b` a neighbor of `a` AND `a` a neighbor of `b`."""
    table = (settings if settings is not None else get_active()).get("camera_neighbors", {})
    if not isinstance(table, dict):
        return frozenset()
    out = {str(n) for n in table.get(camera, []) if n}
    out |= {
        str(cam)
        for cam, neighbors in table.items()
        if isinstance(neighbors, list) and camera in neighbors
    }
    out.discard(camera)
    return frozenset(out)


def load_settings(path: str | Path) -> dict[str, Any]:
    """The persisted settings, merged onto defaults -- or plain defaults if
    the file is missing, unreadable, or not valid JSON. A corrupt settings
    file is not a reason to fail every card evaluation; it's a reason to
    fall back exactly as if the user had never opened settings."""
    p = Path(path)
    if not p.exists():
        return default_settings()
    try:
        with p.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return default_settings()
    if not isinstance(data, dict):
        return default_settings()
    return normalize_settings(data)


def read_rev(path: str | Path) -> int:
    """The optimistic-concurrency revision stored in the settings file.

    Kept in the document itself (not in process memory) so a sidecar restart
    can't reset it to 1 and silently re-admit a PUT holding a pre-restart
    rev. Read raw rather than through `load_settings`: `normalize_settings`
    rebuilds the document from known policy keys and would drop `rev`.
    An unreadable or rev-less file reads as 1, matching what `save_settings`
    writes on its first save of a fresh file.
    """
    try:
        with Path(path).open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 1
    rev = data.get("rev") if isinstance(data, dict) else None
    return rev if isinstance(rev, int) and rev > 0 else 1


def save_settings(path: str | Path, settings: dict[str, Any]) -> int:
    """Write-then-rename so a reader (or a crash mid-write) never observes a
    half-written file.

    Every write bumps the on-disk `rev` (returned) -- any save, whatever the
    caller, is a change another open editor's rev is now stale against. The
    key lives only in the file; the in-memory policy (`apply_settings`) and
    `load_settings` never carry it.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_rev = (read_rev(p) if p.exists() else 0) + 1
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump({**settings, "rev": new_rev}, f, indent=2, sort_keys=True)
    tmp.replace(p)
    return new_rev


#: The live, in-process policy -- what `get_active()` returns and what
#: `delivery_wire.py`/`live_activities.py` actually evaluate cards against.
#: `None` means nothing has called `apply_settings`/`startup` yet.
_active: dict[str, Any] | None = None


def apply_settings(settings: dict[str, Any]) -> None:
    """Make `settings` the live policy. `ladder_policy.set_table`/
    `set_zone_overrides` are called with fresh copies (never the caller's
    own dicts) so a later in-place edit on the caller's side can't silently
    mutate what the evaluator is using. Everything else
    (`zone_classes`/`live_activities`) is read through `get_active()` at
    call time by `delivery_wire.py`, so there's nothing further to push.

    The user-facing authority is `outcomes` (merge_settings derives
    `routing_table_v2` from it), so by the time we get here the two agree;
    the evaluator consumes `routing_table_v2` when present — v2 subjects
    (person/vehicle/animal/thing) — falling back to the legacy
    `routing_table` (stranger/known/animal/thing) otherwise. `off` cells
    exist only in `outcomes` and are applied as pre-evaluation suppression
    via `set_off_cells`."""
    global _active
    table = settings.get("routing_table_v2") or settings["routing_table"]
    ladder_policy.set_table({s: dict(row) for s, row in table.items()})
    outcomes = settings.get("outcomes", {})
    ladder_policy.set_off_cells(
        {
            (subject, place)
            for subject, row in outcomes.items()
            if isinstance(row, dict)
            for place, outcome in row.items()
            if outcome == "off"
        }
    )
    ladder_policy.set_zone_overrides(
        {zone: dict(row) for zone, row in settings.get("zone_overrides", {}).items()}
    )
    _active = settings


def get_active() -> dict[str, Any]:
    """The live policy: whatever `apply_settings`/`startup` last applied, or
    a plain, non-mutating view of the defaults if neither has ever run --
    e.g. a unit test exercising `delivery_wire` directly with no running
    app, or any pre-Phase-4 test that has no idea this module exists.

    Deliberately does **not** call `apply_settings` in that fallback case:
    doing so would push `DEFAULT_ROUTING_TABLE` into `ladder_policy.TABLE`
    on the very first uninitialized call, silently swapping the routing
    engine's own built-in default (which every earlier phase's tests are
    written against) for this module's *different* one. Only an explicit
    `apply_settings`/`startup` call may touch `ladder_policy.TABLE` -- a
    real deployment always makes one from `server.py`'s lifespan before any
    card can be evaluated, so this fallback exists purely for callers that
    never opted into Phase 4 at all.
    """
    if _active is None:
        return default_settings()
    return _active


def save_and_apply(path: str | Path, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Normalize, apply the sticky/nullable-key rules, persist, and make
    live -- the exact sequence `PUT /v1/push/settings` runs, factored out so
    the silence/override endpoints (alerts-slice1 §C) go through the same
    single save path rather than reimplementing it. Callers must run
    `validate_settings(body)` themselves first (this never validates) --
    same division of labor as the route it was extracted from.
    """
    merged = normalize_settings(body)
    la_body = body.get("live_activities") if isinstance(body, dict) else None
    active_la = get_active().get("live_activities", {})
    if not (isinstance(la_body, dict) and isinstance(la_body.get("la_only"), bool)):
        merged["live_activities"]["la_only"] = bool(active_la.get("la_only", False))
    if not (isinstance(la_body, dict) and la_body.get("delivery") in ("la_first", "notifications")):
        merged["live_activities"]["delivery"] = active_la.get("delivery", "la_first")
    for sticky_key in (
        "camera_neighbors",
        "camera_headings",
        "camera_layout",
        "zone_names",
        "camera_optics",
    ):
        if not isinstance(body.get(sticky_key), dict):
            merged[sticky_key] = get_active().get(sticky_key, {})
    for nullable_key in ("secure_area", "map_scale_ft", "floorplan"):
        if nullable_key not in body:
            merged[nullable_key] = get_active().get(nullable_key)
    new_rev = save_settings(path, merged)
    apply_settings(merged)
    return merged, new_rev


def startup(path: str | Path) -> dict[str, Any]:
    """Load from disk (or defaults) and apply. Called once from the app's
    lifespan; `GET /v1/push/settings` calls it too if nothing has loaded a
    file yet, so the very first `GET` on a fresh install both answers the
    request and creates the file (design doc §4).

    Triggers the v1→v2 routing table migration if ``routing_table_v2`` is
    absent from the loaded settings (design brief: "derives it once from
    the legacy table")."""
    import logging

    p = Path(path)
    raw: dict[str, Any] = {}
    if p.exists():
        try:
            with p.open() as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    if not isinstance(raw, dict):
        raw = {}

    needs_migration = "routing_table_v2" not in raw
    settings = normalize_settings(raw) if raw else default_settings()

    if needs_migration and raw.get("routing_table"):
        v2_table, recognition, log_msg = migrate_v1_to_v2(raw["routing_table"])
        settings["routing_table_v2"] = v2_table
        settings["recognition"] = recognition
        logging.getLogger(__name__).info(log_msg)
        save_settings(path, settings)

    # One-time v2 seeding (one alerts stack): a doc written before the V3
    # subject rows existed carries the user's family booleans and
    # alert_all_changes as the authority -- fold them into outcome rows
    # once, then the booleans are derived forever after. Keyed on the raw
    # doc's outcomes lacking the rows (not on "v") so a hand-edited file is
    # never re-seeded over.
    raw_outcomes_val = raw.get("outcomes")
    raw_outcomes: dict[str, Any] = raw_outcomes_val if isinstance(raw_outcomes_val, dict) else {}
    if raw and not any(subject in raw_outcomes for subject in SUBJECTS_V3_EXTRA):
        stored_la = raw.get("live_activities")
        stored_la = stored_la if isinstance(stored_la, dict) else {}
        alert_all = stored_la.get("alert_all_changes") is True
        for bool_key, subject in (
            ("package", "package"),
            ("bins", "bin"),
            ("openings", "opening"),
        ):
            if stored_la.get(bool_key) is False:
                row = {place: "off" for place in PLACES}
            else:
                row = dict(DEFAULT_EXTRA_OUTCOMES[subject])
                if alert_all:
                    # alert_all_changes' observable effect was popping
                    # updates; per-cell that means notify, not glance.
                    row = {
                        place: "notify" if outcome == "glance" else outcome
                        for place, outcome in row.items()
                    }
            settings["outcomes"][subject] = row
            settings["routing_table_v2"][subject] = {
                place: OUTCOME_TO_LEVEL[outcome] for place, outcome in row.items()
            }
        settings["live_activities"] |= _derived_family_booleans(settings["outcomes"])
        settings["v"] = SETTINGS_VERSION
        logging.getLogger(__name__).info(
            "outcomes v3 seeding: package/bin/opening rows from booleans "
            "(package=%s bins=%s openings=%s alert_all=%s)",
            stored_la.get("package", True),
            stored_la.get("bins", True),
            stored_la.get("openings", True),
            alert_all,
        )
        save_settings(path, settings)

    # Seed camera_optics once from the deployed-fleet literals. Keyed on the
    # raw file *lacking* the key entirely (not on emptiness), so a user who
    # later edits — or deletes — cameras is never re-seeded over.
    if "camera_optics" not in raw:
        settings["camera_optics"] = seeded_camera_optics()
        logging.getLogger(__name__).info(
            "camera_optics seeded from DEPLOYMENT_SEED (%d cameras)",
            len(settings["camera_optics"]),
        )
        save_settings(path, settings)

    apply_settings(settings)
    return settings


def reset_for_tests() -> None:
    """Test-only: drop the in-memory cache. `ladder_policy.TABLE` itself is
    restored by `tests/conftest.py`'s autouse fixture, which snapshots
    whatever it actually was before the test ran (not this module's
    idea of a default) -- the one true baseline for a test that never
    touches Phase 4 at all is the routing engine's own, unchanged."""
    global _active
    _active = None


def migrate_v1_to_v2(
    legacy: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, str]], dict[str, str], str]:
    """Derive a v2 routing table and recognition settings from a legacy v1
    table. Returns ``(routing_table_v2, recognition, log_message)``.

    Migration rules (design brief):
    - ``person`` := legacy ``stranger`` row
    - ``vehicle`` := legacy ``thing`` row, bumped one tier at doors/off_limits
    - ``animal`` := legacy ``animal`` row (copy)
    - ``thing`` := legacy ``thing`` row (copy)
    - ``recognition.known_person``: ``relax_one`` if the legacy ``known`` row
      averaged one tier below ``stranger``, else ``off``
    - ``recognition.known_vehicle``: ``relax_one`` (no legacy equivalent)
    """
    level_idx = {lvl: i for i, lvl in enumerate(LEVELS)}

    stranger_row = legacy.get("stranger", DEFAULT_ROUTING_TABLE["stranger"])
    known_row = legacy.get("known", DEFAULT_ROUTING_TABLE["known"])
    animal_row = legacy.get("animal", DEFAULT_ROUTING_TABLE["animal"])
    thing_row = legacy.get("thing", DEFAULT_ROUTING_TABLE["thing"])

    person_row = dict(stranger_row)

    vehicle_row = dict(thing_row)
    for place in ("doors", "off_limits"):
        idx = level_idx.get(vehicle_row.get(place, "log"), 0)
        bumped = min(idx + 1, len(LEVELS) - 1)
        vehicle_row[place] = LEVELS[bumped]

    v2_table = {
        "person": person_row,
        "vehicle": vehicle_row,
        "animal": dict(animal_row),
        "thing": dict(thing_row),
    }

    stranger_avg = sum(level_idx.get(stranger_row.get(p, "log"), 0) for p in PLACES) / len(PLACES)
    known_avg = sum(level_idx.get(known_row.get(p, "log"), 0) for p in PLACES) / len(PLACES)
    gap = stranger_avg - known_avg
    known_person_mode = "relax_one" if gap >= 0.8 else "off"

    recognition = {"known_person": known_person_mode, "known_vehicle": "relax_one"}

    log_msg = (
        f"routing v2 migration: person := stranger row; "
        f"vehicle := thing row bumped one tier at doors/off_limits; "
        f"stranger_avg={stranger_avg:.2f} known_avg={known_avg:.2f} gap={gap:.2f} "
        f"-> known_person={known_person_mode}"
    )
    return v2_table, recognition, log_msg


def probe_recognition_available(config_path: str | Path) -> dict[str, bool]:
    """Check Frigate's config for face recognition and LPR capability."""
    import yaml

    result = {"faces": False, "plates": False}
    p = Path(config_path)
    if not p.exists():
        return result
    try:
        with p.open() as f:
            cfg = yaml.safe_load(f)
    except Exception:
        return result
    if not isinstance(cfg, dict):
        return result
    fr = cfg.get("face_recognition")
    if isinstance(fr, dict) and fr.get("enabled"):
        result["faces"] = True
    lpr = cfg.get("lpr")
    if isinstance(lpr, dict) and lpr.get("enabled"):
        result["plates"] = True
    return result


#: Zone key -> Frigate `friendly_name`, loaded at startup from config.yml.
#: Module-level like the routing table: the copy builder has no path to the
#: Frigate section of settings, and display names change only with Frigate's
#: own config (a sidecar restart follows those anyway).
_zone_display_names: dict[str, str] = {}


def load_zone_display_names(config_path: str | Path) -> None:
    """Read Frigate zone `friendly_name`s for push copy. Missing file or
    names → empty map; the copy builder falls back to humanizing the key.
    Zone names like `front_entry_person` are *rule* names — 'Person at Front
    Entry Person' read like a stutter on a real lock screen (2026-08-14)."""
    from marcellus.zones import load_camera_zones

    names: dict[str, str] = {}
    for _camera, zone_list in load_camera_zones(config_path).items():
        for zone in zone_list:
            friendly = zone.get("friendly_name")
            if friendly:
                names[zone["name"]] = str(friendly)
    global _zone_display_names
    _zone_display_names = names


def zone_display_name(zone: str) -> str | None:
    """Sidecar-edited display name (settings `zone_names`, /zones page) wins;
    Frigate's `friendly_name` is the fallback."""
    configured = get_active().get("zone_names", {})
    if isinstance(configured, dict) and configured.get(zone):
        return str(configured[zone])
    return _zone_display_names.get(zone)


def build_available_zones(config_path: str | Path) -> list[dict[str, Any]]:
    """`available_zones` for the `GET` response: every zone across every
    camera in Frigate's config, with the cameras that see it and a guessed
    place class the user can confirm or correct. Reuses `zones.py`'s
    existing Frigate-config reader rather than re-parsing `config.yml`."""
    from marcellus.zones import load_camera_zones

    cameras_by_zone: dict[str, set[str]] = {}
    for camera, zone_list in load_camera_zones(config_path).items():
        for zone in zone_list:
            cameras_by_zone.setdefault(zone["name"], set()).add(camera)

    return [
        {
            "zone": zone,
            "cameras": sorted(cameras),
            "guessed_class": guess_zone_class(zone, tuple(sorted(cameras))),
            "friendly_name": _zone_display_names.get(zone),
        }
        for zone, cameras in sorted(cameras_by_zone.items())
    ]


def build_available_openings(config_path: str | Path) -> list[str]:
    """`available_openings` for the `GET` response: zone and camera names
    that look like they could be an opening (door/gate/garage/entry), for
    the openings family's per-opening picks."""
    from marcellus.zones import load_camera_zones

    names: set[str] = set()
    for camera, zone_list in load_camera_zones(config_path).items():
        if _looks_like_opening(camera):
            names.add(camera)
        for zone in zone_list:
            if _looks_like_opening(zone["name"]):
                names.add(zone["name"])
    return sorted(names)


def outcome_for(subject: str, place: str) -> str:
    """The merged-ladder outcome for a subject x place cell, for delivery
    surface decisions (delivery_wire's glance gating). Falls back to the
    level-derived outcome when the active doc predates the outcomes table."""
    outcomes = get_active().get("outcomes", {})
    row = outcomes.get(subject)
    if isinstance(row, dict) and row.get(place) in OUTCOMES:
        return str(row[place])
    table = get_active().get("routing_table_v2") or get_active().get("routing_table", {})
    level = table.get(subject, {}).get(place, "log")
    return LEVEL_TO_OUTCOME.get(level, "log")
