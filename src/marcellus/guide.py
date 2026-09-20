"""User guide loader: markdown topics -> validated, rendered registry.

Topics live as markdown files with YAML frontmatter in guide_content/ (inside
the package so `pip install .` deployments ship them). They are loaded once at
startup; a malformed topic fails the app fast rather than 404ing at read time.

Two small extensions are pre-processed on the markdown source before render:

- ``{{stat:key}}`` becomes a ``<span class="guide-stat">`` placeholder that
  static/js/guide.js fills from /guide/stats.json — live numbers never enter
  the render, so topic HTML is pure and cacheable.
- A fenced ``walkthrough`` block (one ``- step`` per line) becomes a tickable
  checklist; guide.js persists progress per topic in localStorage.

tests/test_guide.py is the maintenance contract: it cross-checks every
topic's `routes`/`config` frontmatter against the live FastAPI route table
and the Settings model, so shipping a page or config section without a guide
entry fails CI.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

GUIDE_DIR = Path(__file__).parent / "guide_content"

# Section slugs in display order. The index page and prev/next links follow
# this order; frontmatter `section` must be one of these.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("getting-started", "Getting started"),
    ("sidecar", "Sidecar pages"),
    ("faces", "Face pipeline"),
    ("analysis", "Analysis & tuning"),
    ("notifications", "Notifications"),
    ("elsinore", "Elsinore app"),
    ("operations", "Operations"),
)
SECTION_TITLES = dict(SECTIONS)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_H2_RE = re.compile(r"<h2>(.*?)</h2>")
_TAG_RE = re.compile(r"<[^>]+>")
_STAT_RE = re.compile(r"\{\{stat:([a-z0-9_]+)\}\}")
_WALKTHROUGH_RE = re.compile(r"^```walkthrough\n(.*?)^```$", re.DOTALL | re.MULTILINE)
_LINK_RE = re.compile(r"\]\((/[^)\s#?]*)")


class TopicMeta(BaseModel):
    """Frontmatter for one guide topic."""

    title: str
    section: str
    order: int
    # Sidecar UI paths this topic documents (test_guide.py demands every HTML
    # page appear in some topic's list) and config sections it covers.
    routes: list[str] = Field(default_factory=list)
    config: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Topic:
    slug: str
    meta: TopicMeta
    html: str
    stats_used: frozenset[str]
    walkthrough_steps: int
    internal_links: frozenset[str]
    # (anchor, plain text) per h2, for the "On this page" box and deep anchors.
    headings: tuple[tuple[str, str], ...]
    # Tag-stripped body text for client-side full-text search.
    search_text: str


@dataclass(frozen=True)
class GuideRegistry:
    topics: dict[str, Topic] = field(default_factory=dict)

    def by_section(self) -> list[tuple[str, str, list[Topic]]]:
        """(section_slug, section_title, topics ordered by `order`)."""
        out = []
        for slug, title in SECTIONS:
            topics = sorted(
                (t for t in self.topics.values() if t.meta.section == slug),
                key=lambda t: t.meta.order,
            )
            if topics:
                out.append((slug, title, topics))
        return out

    def ordered(self) -> list[Topic]:
        return [t for _, _, topics in self.by_section() for t in topics]

    def numbers(self) -> dict[str, str]:
        """slug -> "2.4"-style chapter number, following section order."""
        out: dict[str, str] = {}
        for si, (_slug, _title, topics) in enumerate(self.by_section(), start=1):
            for ti, topic in enumerate(topics, start=1):
                out[topic.slug] = f"{si}.{ti}"
        return out

    def neighbors(self, slug: str) -> tuple[Topic | None, Topic | None]:
        flat = self.ordered()
        idx = next((i for i, t in enumerate(flat) if t.slug == slug), None)
        if idx is None:
            return None, None
        prev_t = flat[idx - 1] if idx > 0 else None
        next_t = flat[idx + 1] if idx + 1 < len(flat) else None
        return prev_t, next_t


class GuideError(ValueError):
    """A topic file is malformed (bad frontmatter, unknown section, ...)."""


def _render_walkthrough(body: str, index: int) -> str:
    steps = [ln[2:].strip() for ln in body.strip().splitlines() if ln.startswith("- ")]
    if not steps:
        raise GuideError("walkthrough block has no '- step' lines")
    items = "".join(
        f'<li><label><input type="checkbox" data-step="{i}">'
        f"<span>{html.escape(s)}</span></label></li>"
        for i, s in enumerate(steps)
    )
    # No blank lines: the whole thing must stay one HTML block for markdown-it.
    return (
        f'<div class="walkthrough" data-walkthrough="{index}">'
        f'<div class="walkthrough-head"><span class="walkthrough-progress"></span>'
        f'<button type="button" class="walkthrough-reset">reset</button></div>'
        f"<ol>{items}</ol></div>"
    )


def _load_topic(path: Path) -> Topic:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise GuideError(f"{path.name}: missing '---' frontmatter block")
    try:
        meta = TopicMeta.model_validate(yaml.safe_load(m.group(1)))
    except Exception as exc:  # pydantic/yaml errors -> one uniform failure
        raise GuideError(f"{path.name}: bad frontmatter: {exc}") from exc
    if meta.section not in SECTION_TITLES:
        raise GuideError(f"{path.name}: unknown section {meta.section!r}")

    body = text[m.end() :]
    stats_used = frozenset(_STAT_RE.findall(body))
    internal_links = frozenset(_LINK_RE.findall(body))

    counter = 0

    def _walkthrough_sub(match: re.Match[str]) -> str:
        nonlocal counter
        rendered = _render_walkthrough(match.group(1), counter)
        counter += 1
        return rendered

    body = _WALKTHROUGH_RE.sub(_walkthrough_sub, body)
    body = _STAT_RE.sub(
        lambda s: f'<span class="guide-stat" data-stat="{s.group(1)}">–</span>', body
    )

    rendered_html = MarkdownIt("commonmark").enable("table").render(body)

    # Anchor every h2 and collect them for the topic page's "On this page"
    # box. Anchors are slugified heading text, de-duplicated with a suffix.
    headings: list[tuple[str, str]] = []

    def _h2_sub(match: re.Match[str]) -> str:
        plain = _TAG_RE.sub("", match.group(1)).strip()
        anchor = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-") or "section"
        if any(a == anchor for a, _ in headings):
            anchor = f"{anchor}-{len(headings)}"
        headings.append((anchor, plain))
        return f'<h2 id="{anchor}">{match.group(1)}</h2>'

    rendered_html = _H2_RE.sub(_h2_sub, rendered_html)
    search_text = " ".join(_TAG_RE.sub(" ", rendered_html).split())
    return Topic(
        slug=path.stem,
        meta=meta,
        html=rendered_html,
        stats_used=stats_used,
        walkthrough_steps=counter,
        internal_links=internal_links,
        headings=tuple(headings),
        search_text=search_text,
    )


def load_guide(directory: Path = GUIDE_DIR) -> GuideRegistry:
    topics: dict[str, Topic] = {}
    for path in sorted(directory.glob("*.md")):
        topic = _load_topic(path)
        topics[topic.slug] = topic
    if not topics:
        raise GuideError(f"no guide topics found under {directory}")
    return GuideRegistry(topics=topics)
