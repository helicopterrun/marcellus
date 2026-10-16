"""Bernardo wildlife camera adapter (github.com/helicopterrun/wildlife-cam).

Pure translation between Bernardo's wire shapes (HTTP rows, MQTT payloads)
and the generic types in `types.py`. No state, no tasks, no DB.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import httpx

from marcellus.config import BernardoSourceConfig
from marcellus.external.types import (
    AlertSignal,
    Availability,
    Sighting,
    SightingBox,
    SightingHint,
    SightingStream,
    Signal,
    StateReport,
    StreamRole,
    Subject,
)

logger = logging.getLogger(__name__)

_LABELS: dict[str, Subject] = {
    "animal": "animal",
    "person": "person",
    "vehicle": "vehicle",
    "empty": "none",
}
_CLASSIFIED = frozenset(_LABELS)
_ROLES: dict[str, StreamRole] = {"cam": "ir", "cam2": "color"}
_URL_KEYS = ("poster_url", "clip_url", "page_url")


def _subject(label: Any) -> Subject:
    return _LABELS.get(label, "unknown") if isinstance(label, str) else "unknown"


def _decode_data(data: Any) -> dict[str, Any]:
    if data is None:
        return {}
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TypeError("event data is not an object")
    return data


def _parse_boxes(classification: Mapping[str, Any]) -> tuple[SightingBox, ...]:
    out: list[SightingBox] = []
    for b in classification.get("boxes") or ():
        try:
            box = b["box"]
            still = b.get("still")
            out.append(
                SightingBox(
                    label=str(b["label"]),
                    conf=float(b["conf"]),
                    box=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                    still=str(still) if still else None,
                )
            )
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            continue
    return tuple(out)


def _key_time(key: str) -> float:
    return datetime.strptime(key, "%Y%m%d_%H%M%S").timestamp()


class BernardoSource:
    kind = "bernardo"

    def __init__(
        self, cfg: BernardoSourceConfig, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.id = cfg.id
        self.name = cfg.name
        self.base_url = cfg.base_url.rstrip("/")
        self.max_connections = cfg.max_connections
        self.subjects: tuple[Subject, ...] = (
            "animal",
            "person",
            "vehicle",
            "none",
            "unknown",
        )
        self.media: tuple[str, ...] = ("poster", "stills", "clip", "audio")
        self.mqtt_base_topic = cfg.mqtt_base_topic
        self.settle_after_s = cfg.settle_after_s
        self._clock = clock

    # ---- HTTP -----------------------------------------------------------

    async def fetch_recent(self, client: httpx.AsyncClient, limit: int) -> list[Sighting]:
        resp = await client.get("/api/events", params={"limit": limit})
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            raise ValueError("/api/events did not return a list")
        out: list[Sighting] = []
        for row in rows:
            try:
                out.append(self.parse_row(row))
            except (ValueError, KeyError, TypeError) as exc:
                rid = row.get("id") if isinstance(row, dict) else None
                logger.warning("bernardo %s: skipping malformed row id=%s: %s", self.id, rid, exc)
        return out

    async def fetch_one(self, client: httpx.AsyncClient, native_id: str) -> Sighting | None:
        resp = await client.get(f"/api/events/{native_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self.parse_row(resp.json())

    async def probe(self, client: httpx.AsyncClient) -> bool:
        try:
            resp = await client.get("/api/health")
            return resp.status_code == 200 and isinstance(resp.json(), dict)
        except (httpx.HTTPError, ValueError):
            return False

    # ---- row parsing ----------------------------------------------------

    def parse_row(self, row: Mapping[str, Any]) -> Sighting:
        data = _decode_data(row.get("data"))
        classification = data.get("classification") or {}
        if not isinstance(classification, dict):
            raise TypeError("classification is not an object")
        raw_label = classification.get("label")
        subject = _subject(raw_label)

        audio_dropped = bool(data.get("audio_dropped"))
        streams: list[SightingStream] = []
        for name, st in (data.get("streams") or {}).items():
            clip = st.get("clip") or None
            streams.append(
                SightingStream(
                    name=str(name),
                    role=_ROLES.get(name, "unknown"),
                    stills=tuple(str(s) for s in st.get("stills") or ()),
                    clip=clip,
                    has_audio=bool(clip) and not audio_dropped,
                )
            )

        start = float(row["start_time"])
        end_raw = row.get("end_time")
        has_clip = bool(row.get("has_clip"))
        classified = raw_label in _CLASSIFIED
        terminal = end_raw is not None
        settled = self._clock() - start > self.settle_after_s
        # Bernardo's row has no status; terminal <=> end_time is set, and
        # has_clip separates complete from partial/failed.
        if not terminal:
            status, final = ("failed", True) if settled else ("pending", False)
        elif classified or settled:
            status, final = ("complete" if has_clip else "failed"), True
        else:
            status, final = "pending", False

        conf = classification.get("conf")
        is_dark = data.get("is_dark")
        return Sighting(
            source_id=self.id,
            native_key=str(row["event_key"]),
            native_id=str(row["id"]),
            start=start,
            end=float(end_raw) if end_raw else None,
            trigger=str(row["type"]),
            subject=subject,
            subject_conf=float(conf) if conf is not None else None,
            labels=tuple(str(x) for x in classification.get("labels") or ()),
            model=classification.get("model"),
            status=status,  # type: ignore[arg-type]
            final=final,
            is_dark=None if is_dark is None else bool(is_dark),
            primary_stream=data.get("primary_stream"),
            poster=row.get("thumbnail") or None,
            streams=tuple(streams),
            boxes=_parse_boxes(classification),
            raw={**row, "data": data},
        )

    # ---- MQTT -----------------------------------------------------------

    def mqtt_topics(self) -> list[str]:
        b = self.mqtt_base_topic
        return [f"{b}/availability", f"{b}/state", f"{b}/event", f"{b}/alert"]

    def parse_mqtt(self, topic: str, payload: bytes) -> Signal | None:
        try:
            return self._parse_mqtt(topic, payload)
        except Exception as exc:  # never raise out of a message handler
            logger.warning("bernardo %s: bad MQTT payload on %s: %s", self.id, topic, exc)
            return None

    def _parse_mqtt(self, topic: str, payload: bytes) -> Signal | None:
        b = self.mqtt_base_topic
        if topic == f"{b}/availability":
            text = payload.decode("utf-8").strip()
            if text in ("online", "offline"):
                return Availability(source_id=self.id, online=text == "online")
            return None
        if topic not in (f"{b}/state", f"{b}/event", f"{b}/alert"):
            return None
        obj = json.loads(payload)
        if not isinstance(obj, dict):
            raise ValueError("payload is not a JSON object")
        if topic == f"{b}/state":
            return StateReport(
                source_id=self.id,
                payload=obj,
                problems=tuple(str(p) for p in obj.get("problems") or ()),
            )
        if topic == f"{b}/alert":
            return AlertSignal(
                source_id=self.id,
                code=str(obj["code"]),
                active=bool(obj["active"]),
                message=str(obj.get("message") or ""),
            )
        return self._hint(obj)

    def _hint(self, obj: dict[str, Any]) -> SightingHint:
        key = obj.get("key")
        if not key:
            return SightingHint(source_id=self.id)
        key = str(key)
        start: float | None = None
        t = obj.get("time")
        if t:
            try:
                start = datetime.fromisoformat(str(t)).timestamp()
            except ValueError:
                start = None
        if start is None:
            start = _key_time(key)
        conf = obj.get("confidence")
        sighting = Sighting(
            source_id=self.id,
            native_key=key,
            start=start,
            trigger=str(obj.get("sensor") or ""),
            subject=_subject(obj.get("event_type")),
            subject_conf=float(conf) if conf is not None else None,
            status="pending",
            final=False,
            raw={k: v for k, v in obj.items() if k not in _URL_KEYS},
        )
        return SightingHint(source_id=self.id, native_key=key, sighting=sighting)
