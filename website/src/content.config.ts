import { defineCollection } from 'astro:content';
import { docsSchema } from '@astrojs/starlight/schema';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';

// guide/ is the in-app guide markdown and reference/ is docs/, both symlinked and
// read in place. section/order are enforced for guide/ topics by sidebar.mjs.
export const collections = {
  docs: defineCollection({
    loader: glob({
      base: './src/content/docs',
      // Landing page, guide topics and reference specs (not the .yml sample).
      pattern: '**/*.{md,mdx}',
    }),
    schema: docsSchema({
      extend: z.object({
        section: z.string().optional(),
        order: z.number().optional(),
        routes: z.array(z.string()).default([]),
        config: z.array(z.string()).default([]),
      }),
    }),
  }),
};
