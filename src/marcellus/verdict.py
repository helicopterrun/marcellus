"""Plain-English verdict for the Status page, derived from `/healthz` checks.

`build_verdict` is a pure function over the same `checks` dict (plus the
`ok` flag and `failing` list) that `routes/health.compute_health` produces for
`/healthz`, so the status page can never disagree with the health endpoint:
a check that gates the 503 is level "bad"; informational problems (Frigate
unreachable, cameras not recording, an external source offline) are "warn".
Overall level is "ok" / "warn" / "bad"; `ok=False` always yields "bad".
"""

from __future__ import annotations

from typing import Any

# Values that mean "nothing to report" for a string-valued check.
_QUIET_VALUES = frozenset(
    {"ok", "connected", "starting", "disabled", "off", "idle", "online", "healthy"}
)
_AGE_SUFFIXES = ("_age_s", "_last_reconcile")
_SEVERITY = {"bad": 0, "warn": 1}


def humanise(name: str) -> str:
    """`face_enrich` -> `Face enrich`; unknown names still read sensibly."""
    text = name.replace("_", " ").replace(":", " ").strip()
    return (text[:1].upper() + text[1:]) if text else name


def _source_label(source_id: str) -> str:
    return humanise(source_id)


def _problem(
    check: str, message: str, level: str, href: str | None = None
) -> dict[str, Any]:
    p: dict[str, Any] = {"check": check, "message": message, "level": level}
    if href:
        p["href"] = href
    return p


def _known_problem(name: str, value: Any, level: str) -> dict[str, Any] | None:
    """A sentence for checks we know; None when the check isn't a known one
    or is healthy."""
    if name == "frigate":
        if value == "proxy_stalled":
            return _problem(
                name,
                "Marcellus's connection to Frigate is stuck; a restart should clear it.",
                level,
            )
        if value in ("unreachable", "error"):
            return _problem(name, "Marcellus can't reach Frigate.", level)
    elif name == "mqtt":
        if value == "disconnected":
            return _problem(
                name, "Push alerts are paused: the MQTT broker is disconnected.", level
            )
    elif name == "db":
        if value == "error":
            return _problem(name, "Marcellus's own database can't be opened.", level)
    elif name == "scrub":
        if value == "stale":
            return _problem(
                name, "Scrub cache generation has stalled.", level, "/cameras"
            )
        if value == "locked":
            return _problem(
                name,
                "Scrub cache is locked by another process, so it isn't generating.",
                level,
            )
    elif name == "face_enrich":
        if value == "stale":
            return _problem(name, "Face enrichment has stopped running.", level)
    elif name == "encounters":
        if value == "error":
            return _problem(name, "Encounter tracking hit an error.", level)
    elif name == "unifi_protect":
        if value == "down":
            return _problem(name, "The UniFi Protect connection is down.", level)
        if value == "degraded":
            return _problem(name, "The UniFi Protect connection is degraded.", level)
    elif name in ("upstream_pool", "api_pool"):
        return _problem(
            name, "Marcellus's connections to Frigate are exhausted; requests may hang.", level
        )
    return None


def _recording_problem(value: Any, level: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    stalled = value.get("stalled") or []
    if not stalled:
        return None
    cams = [str(c.get("camera", "?")) if isinstance(c, dict) else str(c) for c in stalled]
    n = len(cams)
    shown = ", ".join(cams[:4]) + (f" and {n - 4} more" if n > 4 else "")
    noun = "camera has" if n == 1 else "cameras have"
    return _problem("recording", f"{n} {noun} stopped recording: {shown}.", level, "/cameras")


def _source_problem(
    sid: str, state: Any, alerts: list[Any], level: str
) -> dict[str, Any] | None:
    label = _source_label(sid)
    check = f"source:{sid}"
    if state in _QUIET_VALUES or state is None:
        return None
    if state == "alert":
        detail = f": {', '.join(str(a) for a in alerts)}" if alerts else ""
        return _problem(check, f"{label} is reporting an alert{detail}.", level)
    if state in ("offline", "unreachable"):
        return _problem(check, f"{label} is {state}.", level)
    if state == "stale":
        return _problem(check, f"{label} hasn't reported recently.", level)
    return _problem(check, f"{label}: {state}.", level)


def build_verdict(
    checks: dict[str, Any], ok: bool = True, failing: list[str] | None = None
) -> dict[str, Any]:
    """Turn `/healthz` checks into `{level, problems, notes}`.

    problems: `{check, message, level, href?}`, most serious first (stable
    within a level). notes: low-key strings that never change the level.
    """
    failing_set = set(failing or ())
    problems: list[dict[str, Any]] = []
    notes: list[str] = []

    def lvl(name: str) -> str:
        return "bad" if name in failing_set else "warn"

    for name, value in checks.items():
        if name.startswith("source:"):
            parts = name.split(":")
            if len(parts) == 2:
                sid = parts[1]
                alerts = checks.get(f"{name}:alerts") or []
                p = _source_problem(sid, value, list(alerts), lvl(name))
                if p:
                    problems.append(p)
            elif parts[-1] == "notices" and isinstance(value, list):
                label = _source_label(":".join(parts[1:-1]))
                for msg in value:
                    notes.append(f"{label}: {msg}")
            continue
        if name.endswith(_AGE_SUFFIXES) or name.startswith("encounters_last"):
            continue
        if name == "recording":
            p = _recording_problem(value, lvl(name))
            if p:
                problems.append(p)
            continue
        known = _known_problem(name, value, lvl(name))
        if known is not None:
            # Pool dicts only matter when they actually gated the 503.
            if name in ("upstream_pool", "api_pool") and name not in failing_set:
                continue
            problems.append(known)
            continue
        if name in ("frigate", "mqtt", "db", "scrub", "face_enrich", "encounters",
                    "unifi_protect", "upstream_pool", "api_pool"):
            continue  # known check, healthy value
        # Unknown check: fall back to a humanised name plus its status.
        if isinstance(value, str) and value not in _QUIET_VALUES:
            problems.append(_problem(name, f"{humanise(name)}: {value}.", lvl(name)))
        elif name in failing_set:
            problems.append(_problem(name, f"{humanise(name)} is failing.", "bad"))

    covered = {p["check"] for p in problems}
    for name in failing_set - covered:
        problems.append(_problem(name, f"{humanise(name)} is failing.", "bad"))
    if not ok and not any(p["level"] == "bad" for p in problems):
        problems.append(_problem("health", "Marcellus reports itself degraded.", "bad"))

    problems.sort(key=lambda p: _SEVERITY.get(p["level"], 2))
    level = "ok"
    if problems:
        level = "bad" if problems[0]["level"] == "bad" else "warn"
    return {"level": level, "problems": problems, "notes": notes}
