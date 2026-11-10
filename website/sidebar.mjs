// Builds the Starlight sidebar from guide_content frontmatter. Section order and
// labels are parsed out of SECTIONS in src/marcellus/guide.py so they cannot drift.
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../src/marcellus/', import.meta.url));
const contentDir = root + 'guide_content/';
const referenceDir = fileURLToPath(new URL('../docs/', import.meta.url));

export function parseSections() {
  const py = readFileSync(root + 'guide.py', 'utf8');
  const block = /^SECTIONS[^=]*=\s*\(([\s\S]*?)^\)/m.exec(py);
  if (!block) throw new Error('Could not find SECTIONS tuple in guide.py');
  const sections = [...block[1].matchAll(/\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)/g)].map((m) => [m[1], m[2]]);
  if (!sections.length) throw new Error('SECTIONS tuple in guide.py parsed to nothing');
  return sections;
}

export function buildSidebar() {
  const sections = parseSections();
  const known = new Set(sections.map(([s]) => s));
  const topics = readdirSync(contentDir)
    .filter((f) => f.endsWith('.md'))
    .map((f) => {
      const fm = /^---\n([\s\S]*?)\n---/.exec(readFileSync(contentDir + f, 'utf8'));
      if (!fm) throw new Error(`${f}: missing frontmatter`);
      const section = /^section:\s*(.+)$/m.exec(fm[1])?.[1].trim();
      const order = Number(/^order:\s*(\d+)/m.exec(fm[1])?.[1]);
      if (!section || !known.has(section)) {
        throw new Error(`${f}: unknown section "${section}" (known: ${[...known].join(', ')})`);
      }
      if (Number.isNaN(order)) throw new Error(`${f}: missing numeric order`);
      const stem = f.replace(/\.md$/, '');
      return { slug: `guide/${stem}`, section, order };
    });
  const guide = sections
    .map(([section, label]) => ({
      label,
      items: topics
        .filter((t) => t.section === section)
        .sort((a, b) => a.order - b.order)
        .map((t) => ({ slug: t.slug })),
    }))
    .filter((g) => g.items.length);
  return [...guide, buildReference()];
}

// Reference group: docs/*.md, deployment first (user-facing ops docs), then the
// design notes alphabetically by title.
function buildReference() {
  const specs = readdirSync(referenceDir)
    .filter((f) => f.endsWith('.md'))
    .map((f) => {
      const fm = /^---\n([\s\S]*?)\n---/.exec(readFileSync(referenceDir + f, 'utf8'));
      const title = fm && /^title:\s*"?(.+?)"?\s*$/m.exec(fm[1])?.[1];
      if (!title) throw new Error(`docs/${f}: missing frontmatter title`);
      return { slug: `reference/${f.replace(/\.md$/, '')}`, title };
    })
    .sort((a, b) => {
      const rank = (t) => (t.slug === 'reference/deployment' ? 0 : 1);
      return rank(a) - rank(b) || a.title.localeCompare(b.title);
    });
  return { label: 'Reference', collapsed: true, items: specs.map((t) => ({ slug: t.slug })) };
}
