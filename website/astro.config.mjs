import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';
import { unified } from '@astrojs/markdown-remark';
import remarkMarcellusGuide from './plugins/remark-marcellus-guide.mjs';
import { buildSidebar } from './sidebar.mjs';

export default defineConfig({
  site: 'https://helicopterrun.github.io',
  base: '/marcellus',
  markdown: { processor: unified({ remarkPlugins: [remarkMarcellusGuide] }) },
  integrations: [
    starlight({
      title: 'Marcellus',
      logo: { src: './src/assets/icon.png', alt: 'Marcellus' },
      favicon: '/favicon.png',
      components: { ThemeSelect: './src/components/Empty.astro' },
      head: [{ tag: 'script', content: "document.documentElement.dataset.theme='dark';" }],
      social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/helicopterrun/marcellus' }],
      customCss: ['./src/styles/marcellus.css'],
      sidebar: buildSidebar(),
      editLink: { baseUrl: 'https://github.com/helicopterrun/marcellus/edit/main/' },
      routeMiddleware: './edit-url.mjs',
      plugins: [starlightLinksValidator()],
    }),
  ],
});
