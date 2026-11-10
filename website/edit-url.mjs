// Route middleware: point "Edit page" at the real repo file, since guide/ and
// reference/ are symlinks into src/marcellus/guide_content and docs/.
import { defineRouteMiddleware } from '@astrojs/starlight/route-data';

const REPO = 'https://github.com/helicopterrun/marcellus/edit/main/';

export const onRequest = defineRouteMiddleware((context) => {
  const route = context.locals.starlightRoute;
  const id = route.entry.id;
  let path = null;
  if (id.startsWith('guide/')) path = `src/marcellus/guide_content/${id.slice(6)}.md`;
  else if (id.startsWith('reference/')) path = `docs/${id.slice(10)}.md`;
  route.editUrl = path ? new URL(path, REPO) : undefined;
});
