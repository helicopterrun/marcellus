"""The starter situation library and the sound catalog.

Two small, static answers to two onboarding questions the app can't answer on
its own:

* `GET /v1/push/situations/library` -- "what should I even set up?" (plan §1:
  the library *is* the onboarding answer). A new user enables one with a tap
  and adjusts its zones.
* `GET /v1/push/sounds` -- which per-situation sounds the app is known to
  bundle (plan §3: a distinct sound per situation category is most of what
  makes a push feel specific rather than generic).

Both are content the *app* ultimately owns; the sidecar publishes the set so
the situation editor has a vocabulary to render. Keyed on `app_version`
because the bundled `.caf` assets ship with the app, not the server -- a
sidecar that advertised a sound an older build doesn't contain would produce a
silent notification, which reads as broken.
"""

from __future__ import annotations

import logging
from typing import Any

from marcellus.push.situations import Escalation, Situation

logger = logging.getLogger(__name__)

# Zones and cameras below are *placeholders*, using the plan's own example
# names (§1). The app's situation editor replaces them with real ones read
# from the user's Frigate `/api/config` before the situation is registered --
# a starter enabled without that step matches only if the user happens to
# have a zone by the same name, which fails silent rather than firehose.
STARTER_SITUATIONS: tuple[Situation, ...] = (
    Situation(
        id="at-the-door",
        name="At the door",
        # Present, not Interrupt, as of Phase 2 -- and this one is the reason
        # the tier exists. Somebody walking up to the door becomes a Live
        # Activity with a snapshot and a timer; only if they are still there
        # five seconds later does it escalate into a buzz. Plan §3's
        # walkthrough is exactly this situation, and it can't be that while
        # authored at Interrupt, which fires once and is over.
        #
        # Retiering a shipped starter does change behaviour for anyone who had
        # it enabled: they get a Live Activity where they used to get a banner
        # at the same five-second mark, and no banner at all if their device
        # can't run activities... except that the fallback rule covers exactly
        # that, so those devices keep the old behaviour unchanged.
        tier="present",
        cameras=("doorbell",),
        labels=("person",),
        zones=("porch",),
        loiter_seconds=5.0,
        audio_events=("doorbell",),
        escalation=Escalation(
            from_tier="present", to_tier="escalated", kind="loiter_exceeds", threshold=5.0
        ),
        sound="at-the-door",
    ),
    Situation(
        id="near-my-car",
        name="Near my car",
        tier="interrupt",
        cameras=("driveway",),
        labels=("person",),
        zones=("driveway", "charger"),
        loiter_seconds=8.0,
        # Attention-getting, not alarming: somebody by the car warrants a look.
        sound="elevated",
    ),
    Situation(
        id="package-delivery",
        name="Package delivery",
        # Present, not Interrupt: a package arriving is worth *seeing*, not
        # worth being interrupted for (plan §2's tier table). It has no
        # `escalation` block for the same reason -- it runs as a Live Activity
        # and is never meant to become a buzz.
        tier="present",
        cameras=("doorbell",),
        labels=("person",),
        zones=("porch", "package_drop"),
        loiter_seconds=3.0,
        sound="package-delivery",
    ),
    Situation(
        id="unknown-vehicle-parked",
        name="Unknown vehicle parked",
        tier="present",
        cameras=("driveway",),
        labels=("car",),
        zones=("driveway",),
        loiter_seconds=60.0,
        # Both of these are Phase-1 no-ops (Frigate's review topic carries
        # neither a stationary flag nor, usefully, a sub-label): persisted
        # here so the rule reads correctly and the later phase that can honour
        # them doesn't have to re-author the starter.
        require_stationary=True,
        sub_label_deny=(),
        # Present tier, low urgency -- a parked car is a thing to know, not
        # a thing to jump at.
        sound="watch",
    ),
)


def starter_library() -> list[dict[str, Any]]:
    return [s.to_dict() for s in STARTER_SITUATIONS]


#: The `.caf` files the app actually bundles (Elsinore `e5b0fe1`, present in
#: both `Elsinore/Sounds/` and `ElsinoreNSE/Sounds/` -- iOS resolves the sound
#: against the *delivering* process's bundle, so lock-screen delivery through
#: the NSE needs its own copy, plan §9).
#:
#: The ids are named for the *situation* they suit rather than the noise they
#: make, so a starter's `sound` reads as intent. The parenthetical is the
#: underlying asset, kept here because it is the only place the two are
#: written down together.
_CATALOG_E5B0FE1: tuple[tuple[str, str], ...] = (
    ("at-the-door", "At the door"),        # bbc_chime
    ("package-delivery", "Package delivery"),  # ups_scanner
    ("watch", "Watch"),                    # ding -- low-urgency notice
    ("investigate", "Investigate"),        # boing -- curious / questioning
    ("general", "General"),                # phone_ring -- the fallback
    ("elevated", "Elevated"),              # dive_horn -- night-tightened
    ("urgent", "Urgent"),                  # alarm_clock -- interrupt now
    ("confirmation", "Confirmation"),      # bird_chirp -- positive resolution
)

#: The id used when a situation names no sound, or names one this catalog
#: doesn't have. A real bundled file, deliberately: see `sound_file`.
FALLBACK_SOUND = "general"

#: app_version -> catalog. One catalog today; the mapping exists so adding a
#: sound in a later app build is a one-line change here rather than a protocol
#: change, and so an older build is never advertised a file it doesn't ship.
_CATALOGS: dict[str, tuple[tuple[str, str], ...]] = {}
_DEFAULT_CATALOG = _CATALOG_E5B0FE1

SOUND_IDS = frozenset(sid for sid, _ in _CATALOG_E5B0FE1)

#: Unknown ids already warned about, so a misconfigured situation logs once
#: rather than on every push it fires.
_warned_sounds: set[str] = set()


def sound_catalog(app_version: str = "") -> list[dict[str, str]]:
    catalog = _CATALOGS.get(app_version.strip(), _DEFAULT_CATALOG)
    return [{"id": sid, "name": name} for sid, name in catalog]


def sound_file(sound_id: str) -> str:
    """The `aps.sound` value for a situation's chosen sound.

    An id this catalog doesn't have falls back to `general.caf` -- a file the
    app definitely ships -- rather than to the id itself. Naming a `.caf` iOS
    cannot resolve delivers the notification *silently*, which is the worst
    available outcome: the user is told nothing at the moment they most need
    telling, and nothing anywhere reports a failure. Falling back keeps the
    notification audible; the warning is how the misconfiguration surfaces.

    Note this is not iOS's `"default"` system sound. That would also be
    audible, but the app's own fallback is the one the sound design chose.
    """
    sid = (sound_id or "").strip()
    if not sid:
        return f"{FALLBACK_SOUND}.caf"
    if sid not in SOUND_IDS:
        if sid not in _warned_sounds:
            _warned_sounds.add(sid)
            logger.warning(
                "push: situation sound %r is not in the app's catalog (%s) -- "
                "falling back to %r so the push is not delivered silently",
                sid, ", ".join(sorted(SOUND_IDS)), FALLBACK_SOUND,
            )
        return f"{FALLBACK_SOUND}.caf"
    return f"{sid}.caf"
