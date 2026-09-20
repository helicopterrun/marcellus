"""Correlates push-pipeline log lines with the camera/track/review they
describe.

Every `frigate/events` or `frigate/reviews` frame is handled by its own
dispatch on the MQTT consumer (`push.mqtt`'s `_consume`), but many frames for
different cameras/tracks interleave in the log at once -- without a
correlator, "activity a_xyz ended" and "LA start result ok=False" a few lines
later have no visible connection to the frame that caused them. `push_ctx` is
a `ContextVar` carrying the already-formatted "cam=... track=..." (or
"cam=... review=...") suffix; `set_push_context`/`reset_push_context` bracket
each dispatch, and `PushContextFilter` copies the current value onto every
`LogRecord` as `record.push_ctx` so a formatter can reference `%(push_ctx)s`.

Installed by `server.run()`: `PushContextFilter` goes on the root handler
(handler filters see propagated records; logger filters don't) and the root
format references `%(push_ctx)s`. The value carries its own leading space and
brackets, so records from outside the pipeline render unchanged.
"""

from __future__ import annotations

import contextvars
import logging

push_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("push_ctx", default="")


def set_push_context(
    camera: str | None, track_id: str | None, review_id: str | None
) -> contextvars.Token[str]:
    """Format and install the context for one dispatch. Returns a token for
    `reset_push_context`. Only the parts that are set appear in the string."""
    parts: list[str] = []
    if camera:
        parts.append(f"cam={camera}")
    if track_id:
        parts.append(f"track={track_id}")
    if review_id:
        parts.append(f"review={review_id}")
    return push_ctx.set(" ".join(parts))


def reset_push_context(token: contextvars.Token[str]) -> None:
    push_ctx.reset(token)


class PushContextFilter(logging.Filter):
    """Copies the current `push_ctx` onto every record it sees, as
    `record.push_ctx`, formatted as `" [cam=... track=...]"` -- empty when no
    context is set (outside a dispatch, or a record from another subsystem),
    so the root format's `%(name)s%(push_ctx)s` reads cleanly either way."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = push_ctx.get()
        record.push_ctx = f" [{ctx}]" if ctx else ""
        return True
