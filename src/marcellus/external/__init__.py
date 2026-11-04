"""External event sources: non-Frigate cameras/sensors whose events Marcellus
ingests, journals and serves (guide_content's `external-sources.md`).

Each source is an `ExternalEventSource` (types.py) registered by `kind`
(registry.py); everything else -- the MQTT hub, the sighting journal, the
media relay, `/v1/sightings`, `/healthz` -- is generic. Clients only ever
talk to Marcellus: upstream URLs never leave this package.
"""

from __future__ import annotations
