"""Adapter registry: config entries -> `ExternalEventSource` instances."""

from __future__ import annotations

import time
from collections.abc import Callable

from marcellus.config import BernardoSourceConfig, ExternalSourcesSection
from marcellus.external.bernardo import BernardoSource
from marcellus.external.types import ExternalEventSource

_ADAPTERS: dict[str, Callable[..., ExternalEventSource]] = {"bernardo": BernardoSource}


def build_sources(
    section: ExternalSourcesSection, *, clock: Callable[[], float] = time.time
) -> list[ExternalEventSource]:
    """One adapter per ENABLED source config; unknown `kind` -> ValueError."""
    out: list[ExternalEventSource] = []
    for cfg in section.sources:
        if not cfg.enabled:
            continue
        factory = _ADAPTERS.get(cfg.kind)
        if factory is None:
            raise ValueError(f"unknown external source kind {cfg.kind!r} (source {cfg.id!r})")
        out.append(factory(cfg, clock=clock))
    return out


__all__ = ["BernardoSourceConfig", "build_sources"]
