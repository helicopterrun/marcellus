---
title: Event search
section: sidecar
order: 7
routes: []
config: []
---

The search box in the header (and the sheet it opens on narrow screens) is
available from any sidecar page. It queries `/v1/events/search`, which reads
Frigate's `event` table directly.

Typing free text (`q`) matches against label, sub_label, and zone names.
Structured filters — cameras, labels, zones (comma-separated), a sub_label
substring, a `min_score`, a time window, `has_snapshot` — are also supported
by the endpoint for API/client use, though the header box only drives `q`
and a result `limit`. Results always come back as `search_source:
"structured"` with `search_distance: null` — there is no semantic/embedding
search here, only exact structured matching over Frigate's own columns.

Picking a result jumps to that event; leaving the box empty and opening the
sheet shows the most recent events instead of "no results."

## Related events

`/v1/events/{event_id}/related` finds other cameras that likely saw the same
object: events the push pipeline's cross-camera dedup already linked to the
same card (`linked`), unioned with same-label events on other cameras whose
time span overlaps within ±20s (`overlap`). Events on the same camera as the
one you're looking at are never included.
