import { defineCollection } from 'astro:content';
import { docsSchema } from '@astrojs/starlight/schema';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';

// The in-app guide markdown is the single source of truth; it is read in place.
export const collections = {
  docs: defineCollection({
    loader: glob({
      base: './src/content/docs',
      pattern: '*.md',
      // welcome.md is the site root.
      generateId: ({ entry }) => {
        const slug = entry.replace(/\.md$/, '');
        return slug === 'welcome' ? 'index' : slug;
      },
    }),
    schema: docsSchema({
      extend: z.object({
        section: z.string(),
        order: z.number(),
        routes: z.array(z.string()).default([]),
        config: z.array(z.string()).default([]),
      }),
    }),
  }),
};
