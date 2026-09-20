"""Push notifications (docs/push-notifications.md).

Frigate review events (MQTT `frigate/reviews`) flow through a decision engine
that matches per-device camera/label/severity subscription filters
(`decision.py`), and matching devices are notified through a `PushTransport`
(`transport.py`) -- a mock/log transport for development, or the relay-client
transport that posts the minimal, content-free payload the spec's privacy
model requires. Device registration and opaque handle redemption live behind
`/v1/push/*` (`routes/push.py`); their storage helpers are in `store.py`.
"""

from __future__ import annotations
