---
title: Tuning detection thresholds
section: analysis
order: 1
routes: ["/score-histogram"]
---

[Scores](/score-histogram) plots the confidence-score distribution for any
camera × label pair and suggests thresholds, with confidence banding. Paired
with [Triage](/triage) labels, it turns threshold setting from guesswork
into a measurement.

The logic: false positives cluster at low scores, real detections at high
scores. Labeling a batch of events real/false-alarm shows you where the two
populations separate — that's your threshold.

## Walkthrough: tune a threshold

```walkthrough
- Pick the camera + label that's producing junk alerts
- Open Triage filtered to that camera/label and label 30-50 events real/false-alarm
- Open the Scores page for the same camera/label
- Find where false-alarm density falls off and real density holds
- Set that value as the label's threshold in Frigate's config for the camera
- Restart Frigate and re-check the feed over the next day or two
```

## Reading the table

Filter by `days`, `camera`, `label`, and `min_samples` (cells with fewer
events than this show no suggestion at all). Each row's **confidence** comes
from whether triage labels exist for that camera/label: `high`/`med` once
enough `tp` events are labeled, `low` when suggestions fall back to the raw
score distribution, `sparse` under 10 samples.

An expandable **"Top-score buckets"** `<details>` block sits below the table:
open it to see the raw histogram bucket counts behind each row's percentiles,
per camera/label cell — useful when a suggested threshold looks off and you
want to see the actual distribution shape rather than trust the summary
percentiles.

## Keeping it tuned

Re-run the loop seasonally — foliage, light, and camera changes shift score
distributions over time. If a camera's histogram shows no separation at all,
the problem is usually upstream: masking and placement
([Motion & zones](/guide/motion-and-zones),
[Camera health](/guide/camera-health)) before thresholds.
