"""Generic types for external event sources.

A source adapter (e.g. `bernardo.py`) turns its device's own wire shapes
into these; nothing outside an adapter knows a device's field names.

Two ingest paths feed the same journal:

- MQTT is a *trigger*: `parse_mqtt` turns a message into a `Signal`. A
  `SightingHint` may carry a partial sighting, but the hub always follows it
  with an HTTP fetch -- the HTTP record is authoritative.
- HTTP is the *record*: `fetch_recent` / `fetch_one` return full `Sighting`s.

Media paths inside a `Sighting` (`poster`, `stills`, `clip`, box stills) are
UPSTREAM paths relative to the source's `base_url`. They are stored in the
journal but never put on the wire: clients address media by `MediaRef`
(kind + stream + index), which the relay resolves against the stored
sighting via `media_upstream_path`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import httpx

#: Normalised subject vocabulary. Matches the alert ladder's subject words
#: (`animal`/`person`/`vehicle`) so a later phase can route sightings
#: through it; `none` = the source looked and found nothing, `unknown` =
#: not (yet) classified.
Subject = Literal["animal", "person", "vehicle", "none", "unknown"]
SUBJECTS: tuple[Subject, ...] = ("animal", "person", "vehicle", "none", "unknown")

#: What a stream sees: an infrared/NoIR sensor, a colour sensor, or unknown.
StreamRole = Literal["ir", "color", "unknown"]

#: Lifecycle, normalised: `pending` = media and/or classification may still
#: change; `complete` = settled; `failed` = the source gave up on it.
SightingStatus = Literal["pending", "complete", "failed"]

#: Which ingest path last wrote a journal row.
Origin = Literal["mqtt", "poll"]

MediaKind = Literal["poster", "still", "clip"]


@dataclass(frozen=True)
class SightingStream:
    name: str
    role: StreamRole
    stills: tuple[str, ...] = ()
    clip: str | None = None
    has_audio: bool = False


@dataclass(frozen=True)
class SightingBox:
    label: str
    conf: float
    #: Normalised [x0, y0, x1, y1] in 0..1.
    box: tuple[float, float, float, float]
    #: Upstream path of the still the box was drawn on, if known.
    still: str | None = None


@dataclass(frozen=True)
class Sighting:
    source_id: str
    #: The source's stable per-event key; journal primary key with source_id.
    native_key: str
    start: float
    #: The source's own numeric/opaque id for `fetch_one`, if distinct.
    native_id: str | None = None
    end: float | None = None
    #: What fired the event on the device (e.g. "pir").
    trigger: str = ""
    subject: Subject = "unknown"
    subject_conf: float | None = None
    labels: tuple[str, ...] = ()
    model: str | None = None
    status: SightingStatus = "pending"
    #: True once media + classification are settled; never goes back.
    final: bool = False
    is_dark: bool | None = None
    primary_stream: str | None = None
    poster: str | None = None
    streams: tuple[SightingStream, ...] = ()
    boxes: tuple[SightingBox, ...] = ()
    #: The source's raw record, kept for debugging/re-parsing. Never on the wire.
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def wire_id(self) -> str:
        return f"{self.source_id}:{self.native_key}"

    def stream(self, name: str) -> SightingStream | None:
        return next((s for s in self.streams if s.name == name), None)

    def preferred_clip_stream(self) -> SightingStream | None:
        """The stream whose clip a list row should link: the primary stream
        if it has a clip, else the first stream that does."""
        if self.primary_stream:
            primary = self.stream(self.primary_stream)
            if primary is not None and primary.clip:
                return primary
        return next((s for s in self.streams if s.clip), None)


@dataclass(frozen=True)
class MediaRef:
    kind: MediaKind
    stream: str | None = None
    index: int | None = None


def media_upstream_path(sighting: Sighting, ref: MediaRef) -> str | None:
    """Resolve a client-facing media reference to the sighting's upstream
    path, or None if the sighting has no such media. Only paths already in
    the stored sighting can ever be returned -- the relay never accepts an
    arbitrary upstream path from a client."""
    if ref.kind == "poster":
        return sighting.poster
    if ref.stream is None:
        return None
    stream = sighting.stream(ref.stream)
    if stream is None:
        return None
    if ref.kind == "clip":
        return stream.clip
    if ref.index is None or not 0 <= ref.index < len(stream.stills):
        return None
    return stream.stills[ref.index]


# --------------------------------------------------------------------------
# MQTT signals
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Availability:
    source_id: str
    online: bool


@dataclass(frozen=True)
class StateReport:
    source_id: str
    payload: Mapping[str, Any]
    #: Device-reported problems (empty = healthy as far as it knows).
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class SightingHint:
    """ "Something happened" -- schedule an HTTP fetch. `sighting` is a
    best-effort partial built from the message, if it carried enough to
    key one; it is upserted as `origin="mqtt"` and later superseded."""

    source_id: str
    native_key: str | None = None
    native_id: str | None = None
    sighting: Sighting | None = None


@dataclass(frozen=True)
class AlertSignal:
    source_id: str
    code: str
    active: bool
    message: str = ""


Signal = Availability | StateReport | SightingHint | AlertSignal


# --------------------------------------------------------------------------
# The adapter protocol
# --------------------------------------------------------------------------


@runtime_checkable
class ExternalEventSource(Protocol):
    """One configured device. Implementations are pure adapters: no state,
    no tasks, no DB -- `service.py` owns all of that."""

    id: str
    kind: str
    name: str
    #: Upstream origin media paths are relative to, e.g. "http://pi:8000".
    base_url: str
    #: Cap on concurrent upstream connections (media relay + polling).
    max_connections: int
    #: Subjects this source can produce (for the capability advert).
    subjects: tuple[Subject, ...]
    #: Media this source can serve: any of "poster", "stills", "clip", "audio".
    media: tuple[str, ...]

    def mqtt_topics(self) -> list[str]:
        """Topic filters to subscribe to; empty = HTTP-only source."""
        ...

    def parse_mqtt(self, topic: str, payload: bytes) -> Signal | None:
        """Map one message to a Signal, or None to ignore it. Must not raise
        on malformed payloads (log and return None)."""
        ...

    async def fetch_recent(self, client: httpx.AsyncClient, limit: int) -> list[Sighting]:
        """Newest-first recent sightings. Raises httpx.HTTPError on transport
        failure; skips (and logs) individual malformed rows."""
        ...

    async def fetch_one(self, client: httpx.AsyncClient, native_id: str) -> Sighting | None:
        """One sighting by `native_id`, or None if the source no longer has it."""
        ...

    async def probe(self, client: httpx.AsyncClient) -> bool:
        """Cheap reachability check (True = the source answered sanely)."""
        ...


# --------------------------------------------------------------------------
# Journal (de)serialisation -- the one JSON shape `sighting_json` holds.
# --------------------------------------------------------------------------


def sighting_to_json(s: Sighting) -> dict[str, Any]:
    return {
        "source_id": s.source_id,
        "native_key": s.native_key,
        "native_id": s.native_id,
        "start": s.start,
        "end": s.end,
        "trigger": s.trigger,
        "subject": s.subject,
        "subject_conf": s.subject_conf,
        "labels": list(s.labels),
        "model": s.model,
        "status": s.status,
        "final": s.final,
        "is_dark": s.is_dark,
        "primary_stream": s.primary_stream,
        "poster": s.poster,
        "streams": [
            {
                "name": st.name,
                "role": st.role,
                "stills": list(st.stills),
                "clip": st.clip,
                "has_audio": st.has_audio,
            }
            for st in s.streams
        ],
        "boxes": [
            {"label": b.label, "conf": b.conf, "box": list(b.box), "still": b.still}
            for b in s.boxes
        ],
    }


_STATUSES: dict[str, SightingStatus] = {
    "pending": "pending",
    "complete": "complete",
    "failed": "failed",
}


def _box4(v: Any) -> tuple[float, float, float, float]:
    return (float(v[0]), float(v[1]), float(v[2]), float(v[3]))


def sighting_from_json(d: Mapping[str, Any], raw: Mapping[str, Any] | None = None) -> Sighting:
    subject = d.get("subject") or "unknown"
    status = d.get("status") or "pending"
    return Sighting(
        source_id=str(d["source_id"]),
        native_key=str(d["native_key"]),
        native_id=d.get("native_id"),
        start=float(d["start"]),
        end=None if d.get("end") is None else float(d["end"]),
        trigger=str(d.get("trigger") or ""),
        subject=subject if subject in SUBJECTS else "unknown",
        subject_conf=d.get("subject_conf"),
        labels=tuple(d.get("labels") or ()),
        model=d.get("model"),
        status=_STATUSES.get(status, "pending"),
        final=bool(d.get("final")),
        is_dark=d.get("is_dark"),
        primary_stream=d.get("primary_stream"),
        poster=d.get("poster"),
        streams=tuple(
            SightingStream(
                name=str(st["name"]),
                role=st.get("role") if st.get("role") in ("ir", "color") else "unknown",
                stills=tuple(st.get("stills") or ()),
                clip=st.get("clip"),
                has_audio=bool(st.get("has_audio")),
            )
            for st in d.get("streams") or ()
        ),
        boxes=tuple(
            SightingBox(
                label=str(b["label"]),
                conf=float(b["conf"]),
                box=_box4(b["box"]),
                still=b.get("still"),
            )
            for b in d.get("boxes") or ()
        ),
        raw=dict(raw or {}),
    )
