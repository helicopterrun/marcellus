---
title: Event search
section: daily-review
order: 3
routes: []
config: []
---

The search box in the header (and the sheet it opens on narrow screens) is
available from any sidecar page. It queries `/v1/events/search`, which reads
Frigate's `event` table directly.

Typing free text (`q`) matches against label, sub_label, and zone names.
Structured filters -- cameras, labels, zones (comma-separated), a sub_label
substring, a `min_score`, a time window, `has_snapshot` -- are also supported
by the endpoint for API/client use, though the header box only drives `q`
and a result `limit`. Results always come back as `search_source:
"structured"` with `search_distance: null` -- there is no semantic/embedding
search here, only exact structured matching over Frigate's own columns.

Picking a result jumps to that event; leaving the box empty and opening the
sheet shows the most recent events instead of "no results."

Cross-camera "related events" for a single event live with
[Triage](/guide/triage), where you actually look at one event at a time.
