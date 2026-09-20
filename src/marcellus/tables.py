"""Markdown table rendering used by the CLI subcommands.

Returning structured data from the analysis functions and formatting at the
CLI boundary keeps the same logic reusable for JSON HTTP responses.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def render_table(headers: Sequence[str], rows: Iterable[dict[str, Any]]) -> str:
    """Render a markdown table. Missing keys in a row render as `—`."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        cells = [str(row.get(h, "—")) for h in headers]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
