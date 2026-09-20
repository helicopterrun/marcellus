"""Encounters: group Frigate review-segment activity into human-legible
"one thing happened" chains across cameras and time (docs/encounters.md).

`event`/`reviewsegment` in Frigate's own DB stay the source of truth and are
only ever read -- this package is a read-mostly overlay in the sidecar DB.
"""

from __future__ import annotations
