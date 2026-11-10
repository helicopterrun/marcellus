// Builds the Starlight sidebar from guide_content frontmatter. Section order and
// labels are parsed out of SECTIONS in src/marcellus/guide.py so they cannot drift.
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../src/marcellus/', import.meta.url));
const contentDir = root + 'guide_content/';

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
      return { slug: stem === 'welcome' ? 'index' : stem, section, order };
    });
  return sections
    .map(([section, label]) => ({
      label,
      items: topics
        .filter((t) => t.section === section)
        .sort((a, b) => a.order - b.order)
        .map((t) => ({ slug: t.slug })),
    }))
    .filter((g) => g.items.length);
}
