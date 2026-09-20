"""WCAG AA contrast: text tokens must clear 4.5:1 against their backgrounds."""
from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

CSS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "marcellus" / "static" / "css" / "triage.css"
)

# WCAG 2.1 relative-luminance helpers
def _linearize(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)

def _contrast(fg: str, bg: str) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)

# Pairs: (token, background-token, min-ratio)
#
# --text/--muted/--muted-2 carry small text (every use is 9-13px, so none
# qualify for the 3:1 large-text exemption) and must clear AA against every
# surface they are painted on. --muted-3 is deliberately NOT a text token --
# it is borders and decorative glyphs only, so it is held to the 3:1 non-text
# threshold instead. See the ramp comment in triage.css.
_TEXT_TOKENS = ("--text", "--muted", "--muted-2")
_BACKGROUNDS = ("--surface", "--surface-2", "--panel")

_PAIRS = [(t, bg, 4.5) for t in _TEXT_TOKENS for bg in _BACKGROUNDS] + [
    ("--muted-3", "--surface", 3.0),
    ("--muted-3", "--surface-2", 3.0),
]

def _parse_themes(css: str) -> dict[str, dict[str, str]]:
    """Extract CSS custom properties per theme block."""
    themes: dict[str, dict[str, str]] = {}
    # Default :root block
    token_re = r'(--[\w-]+)\s*:\s*(#[0-9A-Fa-f]{6})'
    root_match = re.search(r':root\s*\{([^}]+)\}', css)
    if root_match:
        themes["default"] = dict(re.findall(token_re, root_match.group(1)))
    # Named themes
    for m in re.finditer(r'\[data-theme="([\w-]+)"\]\s*\{([^}]+)\}', css):
        themes[m.group(1)] = dict(re.findall(token_re, m.group(2)))
    return themes

def test_text_contrast_meets_aa() -> None:
    css = CSS_PATH.read_text()
    themes = _parse_themes(css)
    default_tokens = themes.get("default", {})
    errors: list[str] = []
    for name, tokens in themes.items():
        # Named themes inherit from default for any missing token
        merged = {**default_tokens, **tokens} if name != "default" else tokens
        for fg_name, bg_name, min_ratio in _PAIRS:
            fg = merged.get(fg_name)
            bg = merged.get(bg_name)
            if not fg or not bg:
                continue
            ratio = _contrast(fg, bg)
            if ratio < min_ratio:
                errors.append(
                    f"{name}: {fg_name} ({fg}) on {bg_name} ({bg}) = {ratio:.2f}:1 < {min_ratio}"
                )
    assert not errors, "WCAG AA contrast failures:\n" + "\n".join(errors)


def test_neutral_ramp_is_monotonic() -> None:
    """--muted must be lighter than --muted-2, which must be lighter than
    --muted-3, in every theme.

    The ramp inverted once already: raising --muted-3 to fix its contrast made
    the nominally-dimmest step the brightest one, so "muted-3" no longer meant
    anything. Ordering is what makes the token names honest.
    """
    themes = _parse_themes(CSS_PATH.read_text())
    default_tokens = themes.get("default", {})
    errors: list[str] = []
    for name, tokens in themes.items():
        merged = {**default_tokens, **tokens} if name != "default" else tokens
        steps = [(t, merged.get(t)) for t in ("--muted", "--muted-2", "--muted-3")]
        if any(v is None for _, v in steps):
            continue
        for (hi_name, hi), (lo_name, lo) in pairwise(steps):
            if _luminance(hi) <= _luminance(lo):  # type: ignore[arg-type]
                errors.append(
                    f"{name}: {hi_name} ({hi}) is not lighter than {lo_name} ({lo})"
                )
    assert not errors, "Neutral ramp out of order:\n" + "\n".join(errors)
