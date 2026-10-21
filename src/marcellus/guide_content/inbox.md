---
title: Review inbox
section: daily-review
order: 2
routes: ["/inbox", "/inbox/coverage"]
config: ["triage"]
---

The [inbox](/inbox) is a short daily review, about two minutes, of the
detections where your answer actually teaches Frigate+ something. Each
verdict also queues the Frigate+ upload. There's no separate "submit"
step.

## Why not just review everything

Frigate makes about 500 events a day here, and almost all of them are right.
In the May 2026 labeling pass, 95% of events reviewed newest-first were simply
"real". Each of those took a click and gave the next model nothing new. The
inbox skips confident detections on cameras the model already knows, and
offers only items that carry signal. Each card says why it was chosen:

| reason | what it means |
|---|---|
| **Not this on the phone** | You tapped "Not this" on an alert. The question here is narrower: was the *detection* wrong, or just not worth an alert? |
| **Frigate missed this** | A [wildlife finder](/guide/wildlife) visit: an animal Frigate never made an event of. |
| **borderline** | The top score landed just over the camera's threshold (within `borderline_margin`). Frigate+ asks for true *and* false positives near the threshold, to widen the gap between them. |
| **rare here** | That label appeared at most `rare_label_max` times on that camera in `lookback_days`, like a rabbit on the alley camera. |
| **few Frigate+ images** | The current Frigate+ model has fewer than `camera_target_images` verified images of this camera (the Frigate+ guide recommends at least 100). |

The batch is picked greedily by priority, with two rules for diversity. Each
pick halves the weight of the other items with the same camera, label and
light (day, dusk or night), and one camera can take at most 40% of a batch.
Wildlife visits take at most 60%, so a finder backlog still leaves room for
Frigate's own detections.

## Using it

| key | |
|---|---|
| <kbd>t</kbd> | **Real**, uploaded with its box under the detected label |
| <kbd>f</kbd> | **False alarm**, uploaded as a false positive |
| <kbd>w</kbd> | **Wrong label**: a real object, but not what Frigate called it. Uploaded as a false positive (the Frigate+ guide's own advice). Pick what it really was, so you remember to add that label when you verify it. |
| <kbd>s</kbd> | **Skip**: saved, nothing uploaded |
| <kbd>u</kbd> | Undo the focused (or last) verdict |
| <kbd>j</kbd> / <kbd>k</kbd> | Move between cards |
| <kbd>space</kbd> | Play the event clip |

On a wildlife visit the buttons are **Animal** and **Not an animal**. Animal
keeps the visit and uploads its `wildlife_frames_per_visit` strongest frames.
They go up unlabeled, so you draw the box on Frigate+.

The day's budget is `daily_budget`. Once it's used up, the page offers
**keep going** for another batch. Verdicts also land in the same labels table
as [Triage](/guide/triage), so they feed [threshold tuning](/guide/tuning-scores) too.

## The outbox

An upload waits `plus_delay_s` seconds before it leaves the machine. That is
the undo window, and the card counts it down. The web process checks the
outbox every `outbox_interval_s` seconds. A failed upload is retried with
doubling backoff, up to `outbox_max_attempts` tries. If Frigate answers
"already submitted", that counts as success. A row that still fails shows on
the [coverage page](/inbox/coverage) with Frigate's message and a retry button.

An upload isn't training data yet. Frigate+ only trains on images you
**verify** on [plus.frigate.video](https://plus.frigate.video). The coverage
page counts what's been uploaded since the current model was built, so you
know when it's worth verifying and asking for a new model.

## Coverage

[Coverage](/inbox/coverage) reads the current model's metadata from Frigate.
For each camera it shows how many verified images the model was trained on,
against the target. It also shows how many uploads are waiting since the last
build, and when the uploaded images were taken (day, dusk, night).

Frigate+ counts images under the camera name used **at upload time**, so a
renamed camera starts again at zero. `plus_camera_aliases` maps a current
camera to its older names, for example `gate-walkway: [gate]`. Names that
match nothing are listed as retired cameras.

"Today" is midnight to midnight in `timezone` (falling back to
`wildlife.timezone`, then UTC). Dusk and night use the sun's position when
`encounters.latitude` and `encounters.longitude` are set. Otherwise they use
the clock.

## Settings

| key | default | |
|---|---|---|
| `daily_budget` | 15 | items offered per day before "keep going" |
| `lookback_days` | 7 | how far back candidates come from |
| `borderline_margin` | 0.08 | width of the band above the threshold |
| `rare_label_max` | 5 | at or below this many events, a label is "rare" on a camera |
| `camera_target_images` | 100 | verified images per camera to aim for |
| `plus_delay_s` | 60 | undo window before an upload |
| `outbox_interval_s` | 15 | outbox check cadence |
| `outbox_max_attempts` | 5 | tries before an upload is marked failed |
| `wildlife_frames_per_visit` | 2 | frames uploaded per kept visit |
| `plus_camera_aliases` | `{}` | current camera → older Frigate+ camera names |
| `timezone` | `""` | IANA zone for "today" |

## If it goes wrong

- **Nothing uploads.** The banner says whether Frigate+ is configured
  (Frigate's `plus.enabled`). Failed rows are on the coverage page with
  Frigate's error.
- **Thresholds look like 0.70 everywhere.** Frigate's `/api/config` couldn't
  be read. The page says so, and falls back to defaults until it can.
- **"already in Frigate+ as real"** on a false alarm: that event was uploaded
  as a true positive earlier. Fix the box on plus.frigate.video, because two
  conflicting labels on one image would confuse training.
