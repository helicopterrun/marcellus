// Remark plugin translating the in-app guide's custom syntax (see
// src/marcellus/guide.py) into static-site equivalents.
import { visit, SKIP } from 'unist-util-visit';

const STAT_RE = /\{\{stat:([a-z0-9_]+)\}\}/g;
const BASE = '/marcellus';

function statBadge() {
  return {
    type: 'html',
    value:
      '<span class="mc-live-stat" title="Shown live in your sidecar\'s guide">live</span>',
  };
}

function rewriteGuideLink(url) {
  const m = /^\/guide(?:\/([a-z0-9-]+))?\/?(#.*)?$/.exec(url);
  if (!m) return null;
  const [, slug, hash = ''] = m;
  if (!slug) return `${BASE}/guide/welcome/${hash}`;
  return `${BASE}/guide/${slug}/${hash}`;
}

export default function remarkMarcellusGuide() {
  return (tree) => {
    // b. walkthrough blocks -> numbered steps
    visit(tree, 'code', (node, index, parent) => {
      if (node.lang !== 'walkthrough' || !parent || index == null) return;
      const steps = node.value
        .split('\n')
        .filter((l) => l.startsWith('- '))
        .map((l) => l.slice(2).trim());
      if (!steps.length) throw new Error('walkthrough block has no "- step" lines');
      parent.children[index] = {
        type: 'list',
        ordered: true,
        spread: false,
        data: { hProperties: { className: ['mc-steps'] } },
        children: steps.map((s) => ({
          type: 'listItem',
          spread: false,
          children: [{ type: 'paragraph', children: [{ type: 'text', value: s }] }],
        })),
      };
      return SKIP;
    });

    // Starlight enables remark-directive, which parses ":key" in "{{stat:key}}"
    // as a text directive. Turn those back into text, then merge adjacent text.
    visit(tree, (node) => {
      if (!node.children) return;
      const merged = [];
      for (let child of node.children) {
        const before = merged[merged.length - 1];
        if (
          child.type === 'textDirective' &&
          before?.type === 'text' &&
          before.value.endsWith('{{stat') &&
          !child.children?.length
        ) {
          child = { type: 'text', value: `:${child.name}` };
        } else if (child.type === 'textDirective') {
          // The guide has no directive syntax (the in-app renderer doesn't
          // know it), so any other ":name" in prose is literal text: put it
          // back rather than letting the directive silently vanish.
          const label = child.children?.length ? null : '';
          if (label === null) {
            throw new Error(`guide content uses directive syntax ":${child.name}[...]", which the app renderer can't show`);
          }
          child = { type: 'text', value: `:${child.name}` };
        }
        const prev = merged[merged.length - 1];
        if (child.type === 'text' && prev?.type === 'text') prev.value += child.value;
        else merged.push(child);
      }
      node.children = merged;
    });

    // a. stat tokens (text nodes, including those inside strong/emphasis)
    visit(tree, 'text', (node, index, parent) => {
      if (!parent || index == null || !STAT_RE.test(node.value)) return;
      STAT_RE.lastIndex = 0;
      const out = [];
      let last = 0;
      for (const m of node.value.matchAll(STAT_RE)) {
        if (m.index > last) out.push({ type: 'text', value: node.value.slice(last, m.index) });
        out.push(statBadge());
        last = m.index + m[0].length;
      }
      if (last < node.value.length) out.push({ type: 'text', value: node.value.slice(last) });
      parent.children.splice(index, 1, ...out);
      return index + out.length;
    });

    // c. links
    visit(tree, 'link', (node, index, parent) => {
      const url = node.url;
      if (!url || !url.startsWith('/') || url.startsWith('//') || !parent || index == null) return;
      const guide = rewriteGuideLink(url);
      if (guide) {
        node.url = guide;
        return;
      }
      parent.children[index] = {
        type: 'emphasis', // placeholder node type; hName overrides the element
        data: { hName: 'span', hProperties: { className: ['mc-app-link'], title: 'Page in your Marcellus sidecar' } },
        children: node.children,
      };
    });
  };
}
