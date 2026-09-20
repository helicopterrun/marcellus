"""Pin the sidecar's :root tokens to the Elsinore app's brand tokens.

Mirror of Elsinore/FrigateKit/ElsinoreBrandTokens.swift -- update both
together when the app retunes its palette.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = (
    REPO_ROOT / "src" / "marcellus" / "static" / "css" / "triage.css"
)
BASE_HTML_PATH = (
    REPO_ROOT / "src" / "marcellus" / "templates" / "base.html"
)
JS_DIR = REPO_ROOT / "src" / "marcellus" / "static" / "js"

# Expected values, mirroring ElsinoreBrandTokens.swift.
EXPECTED_TOKENS = {
    "--panel": "#111216",
    "--surface": "#1B1C21",
    "--surface-2": "#24262C",
    "--stroke": "#303238",
    "--text": "#ECE9E3",
    "--muted": "#96979D",
    "--accent": "#E2AA52",
    "--accent-2": "#ECB758",
    "--parchment": "#EBCF9D",
    "--ember": "#C35B2E",
    "--live": "#FF5D57",
    "--live-now": "#4CAF7A",
    "--lane-person": "#66B9EE",
    "--lane-vehicle": "#B594E8",
    "--lane-animal": "#69C48B",
    "--lane-package": "#E3B341",
    "--on-accent": "#111216",
    "--radius-lg": "10px",
}

DEAD_THEME_SLUGS = (
    "kronborg-signal",
    "moss-terracotta",
    "oresund-harbor",
    "midnight-fjord",
)


def _parse_root(css: str) -> dict[str, str]:
    root_match = re.search(r":root\s*\{([^}]+)\}", css)
    assert root_match, "no :root block found in triage.css"
    token_re = r"(--[\w-]+)\s*:\s*([^;]+);"
    return {k: v.strip() for k, v in re.findall(token_re, root_match.group(1))}


def test_root_tokens_match_brand_tokens() -> None:
    tokens = _parse_root(CSS_PATH.read_text())
    errors: list[str] = []
    for name, expected in EXPECTED_TOKENS.items():
        actual = tokens.get(name)
        if actual is None:
            errors.append(f"{name}: missing from :root")
            continue
        if actual.lower() != expected.lower():
            errors.append(f"{name}: expected {expected}, got {actual}")
    assert not errors, "Brand token drift:\n" + "\n".join(errors)


def test_no_theme_override_blocks() -> None:
    css = CSS_PATH.read_text()
    assert "[data-theme=" not in css, (
        "the theme picker was removed; no [data-theme=...] blocks should remain"
    )


def test_base_html_theme_color_matches_surface() -> None:
    css = CSS_PATH.read_text()
    tokens = _parse_root(css)
    surface = tokens["--surface"]
    html = BASE_HTML_PATH.read_text()
    m = re.search(r'<meta name="theme-color" content="(#[0-9A-Fa-f]{6})"', html)
    assert m, "no theme-color meta tag found in base.html"
    assert m.group(1).lower() == surface.lower(), (
        f"base.html theme-color ({m.group(1)}) != --surface ({surface})"
    )


def test_no_dead_theme_slugs_in_js() -> None:
    errors: list[str] = []
    for path in JS_DIR.rglob("*.js"):
        if path.name == "tokens.js":
            continue
        text = path.read_text()
        for slug in DEAD_THEME_SLUGS:
            if slug in text:
                errors.append(f"{path.relative_to(REPO_ROOT)}: contains {slug!r}")
    assert not errors, "Dead theme slugs still referenced:\n" + "\n".join(errors)
