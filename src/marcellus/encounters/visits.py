"""Visits ("stops"): an encounter's members ordered by time, with consecutive
members on the same camera merged into one stop. Pure -- plain data in, plain
data out. The encounter title's route (`title.py`) and the `stops` array on
`EncounterSummary` both build on `order_members`/`zone_label`, so they cannot
disagree about member order or zone naming; they differ on purpose in how they
collapse (title: consecutive duplicate *place names*; stops: consecutive
*same camera*).

    driveway, door, driveway   -> 3 stops (a return visit is its own stop)
    driveway, driveway, door   -> 2 stops

Each stop also names its LEAD event (`event_id`/`label`), chosen within the
stop by the same rule `store.recompute` uses for the encounter's
`primary_event_id`: the first member (in `order_members` order) that is
alert-severity and has event ids, else the first member with any event ids;
that member's `event_ids[0]` (Frigate's first detection of the review). So the
encounter's `primary_event_id` is always some stop's `event_id`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class MemberStop:
    """What route/visit building needs from one encounter member."""

    camera: str
    start: float
    end: float | None = None
    first_zone: str = ""
    zones: Sequence[str] = ()
    joined_at: float = 0.0
    #: Frigate event ids behind the member's review, detection order.
    event_ids: Sequence[str] = ()
    severity: str = "detection"
    #: The review's normalised base labels (`linker.normalise_labels`).
    labels: Sequence[str] = ()
    #: Frigate label of `event_ids[0]`, read at link time
    #: (`observations.Direction.lead_label`); None when not recorded.
    lead_label: str | None = None


@dataclass(frozen=True)
class Stop:
    """One visit: `zone` is a display name (None when the first member had no
    zone); `end` is None while any member of the stop is still open.
    `event_id`/`label` name the stop's lead event (module docstring); None
    when no member has an event id, or the label is not known."""

    camera: str
    zone: str | None
    start: float
    end: float | None
    event_id: str | None = None
    label: str | None = None


def order_members(members: Iterable[MemberStop]) -> list[MemberStop]:
    """By start time, then joined_at (stable beyond that)."""
    return sorted(members, key=lambda m: (m.start, m.joined_at))


def member_zone(member: MemberStop) -> str:
    """The raw zone key a member is placed at: `first_zone`, else its first
    zone, else ""."""
    return member.first_zone or (member.zones[0] if member.zones else "")


def humanise(text: str, separators: str) -> str:
    """Split on `separators`, capitalise only each word's first letter and
    leave the rest as written ("nw_49th_st" -> "Nw 49th St"; `str.title()`
    would give "Nw 49Th St")."""
    return " ".join(w[:1].upper() + w[1:] for w in re.split(f"[{separators}]+", text) if w)


def zone_label(zone: str, zone_display: Callable[[str], str | None]) -> str:
    """A zone's display name: the configured one, else the humanised key."""
    return zone_display(zone) or humanise(zone, "_")


def build_stops(
    members: Iterable[MemberStop], zone_display: Callable[[str], str | None]
) -> list[Stop]:
    """Merge consecutive same-camera members (in `order_members` order) into
    stops. `start` is the stop's earliest member start; `end` the latest member
    end, or None if any member is open; `zone` comes from the stop's first
    member; `event_id`/`label` from its lead member (`_better_lead`)."""
    stops: list[Stop] = []
    leads: list[MemberStop | None] = []
    for m in order_members(members):
        if stops and stops[-1].camera == m.camera:
            prev = stops[-1]
            end = None if prev.end is None or m.end is None else max(prev.end, m.end)
            if _better_lead(leads[-1], m):
                leads[-1] = m
            stops[-1] = _stop(prev.camera, prev.zone, min(prev.start, m.start), end, leads[-1])
            continue
        raw = member_zone(m)
        lead = m if m.event_ids else None
        leads.append(lead)
        stops.append(
            _stop(m.camera, zone_label(raw, zone_display) if raw else None, m.start, m.end, lead)
        )
    return stops


def _better_lead(current: MemberStop | None, candidate: MemberStop) -> bool:
    """Whether `candidate` (a later member of the same stop) replaces the
    stop's lead: only a member with event ids can lead, and the first
    alert-severity one outranks an earlier detection."""
    if not candidate.event_ids:
        return False
    if current is None:
        return True
    return current.severity != "alert" and candidate.severity == "alert"


def lead_event_label(member: MemberStop) -> str | None:
    """The Frigate label of `member.event_ids[0]`: the label recorded at link
    time, else the review's only base label when it has exactly one (every
    detection in that review then carries it), else None -- a review with
    several labels does not say which one its first detection had."""
    if member.lead_label:
        return member.lead_label
    distinct = list(dict.fromkeys(member.labels))
    return distinct[0] if len(distinct) == 1 else None


def _stop(
    camera: str, zone: str | None, start: float, end: float | None, lead: MemberStop | None
) -> Stop:
    if lead is None:
        return Stop(camera, zone, start, end)
    return Stop(camera, zone, start, end, str(lead.event_ids[0]), lead_event_label(lead))
