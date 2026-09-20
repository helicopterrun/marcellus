---
title: Triage
section: sidecar
order: 3
routes: ["/triage", "/event/{event_id}"]
---

[Triage](/triage) is for reviewing detections and labeling them
**real** (a true positive), a **false alarm** (false positive), or
**skip**. The labels feed
the [threshold tuning](/guide/tuning-scores) workflow — a few labeling
sessions give you the evidence to set per-camera thresholds instead of
guessing.

## Using it

**Start review.** The button at the top opens the first untriaged event in
your current filter — label it and the page auto-advances to the next one,
so a labeling session is just: tap Start review, then real/false-alarm/skip through
the queue.

**The list.** Filter by camera, object label, triage state, time window, and
sort order. A **session** field in the filter bar lets you tag a batch of
labels (e.g. `2026-08-23a`) so you can tell labeling passes apart later; on
desktop the header shows running real/false-alarm/skip counts.

**The event page.** Tap any event to open `/event/{id}`:

- Snapshot with zone overlays, plus the event clip.
- One-tap **Real / False alarm / Skip** buttons (or clear a label).
- Keyboard navigation walks through your filtered list without going back —
  label, arrow, label, arrow. Reviewing a day takes minutes.

Labels are stored in the sidecar's own database; Frigate's data is never
modified.

**Related events.** `GET /v1/events/{event_id}/related` looks up other
cameras that saw the same object -- cards the push pipeline's cross-camera
dedup already linked, plus same-label events on other cameras whose time
span overlaps this one (padded ±20s). The requested event's own camera is
always excluded.

## If it goes wrong

An empty list usually means the filters are too narrow (widen the day range)
or, on a dev machine, that there's no Frigate database at all — the page
says so explicitly when that's the case.
