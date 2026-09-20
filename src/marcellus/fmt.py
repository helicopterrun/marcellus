"""Display formatters shared by templates.

Registered once as Jinja globals in server.py, so every template can use
fmt_ts / fmt_score / fmt_duration / fmt_bytes without each route having to
pass them in its context (they used to be duplicated per-route).
"""

from __future__ import annotations

from datetime import datetime


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ts)


def fmt_score(s: float | None) -> str:
    if s is None:
        return "—"
    try:
        return f"{float(s):.3f}"
    except (TypeError, ValueError):
        return str(s)


def fmt_duration(start: float | None, end: float | None) -> str:
    if not start or not end:
        return "—"
    try:
        return f"{float(end) - float(start):.1f}s"
    except (TypeError, ValueError):
        return "—"


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
