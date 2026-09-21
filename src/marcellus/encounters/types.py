"""Shared value types for the encounters package that both `linker.py` and
`transitions.py` need -- split out to avoid a circular import: `linker.py`
needs `TransitionStats` (M3, `LinkerConfig.transitions`) but `transitions.py`
already imports `linker.family_of`/`LABEL_FAMILIES`, so `TransitionStats`
can't live in either module without the other importing back into it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransitionStats:
    p10: float
    p50: float
    p90: float
    samples: int
    source: str  # "learned" | "config" | "default"
