import { createServer } from 'node:http';
import { readFile, realpath } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

export const ORIGIN = 'http://127.0.0.1:4173';
export const PUBLIC_FILES = Object.freeze({
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/index.html': ['index.html', 'text/html; charset=utf-8'],
  '/styles.css': ['styles.css', 'text/css; charset=utf-8'],
  '/app.mjs': ['app.mjs', 'text/javascript; charset=utf-8'],
  '/project-store.mjs': ['project-store.mjs', 'text/javascript; charset=utf-8'],
  '/workflows.mjs': ['workflows.mjs', 'text/javascript; charset=utf-8'],
  '/production-export.mjs': ['production-export.mjs', 'text/javascript; charset=utf-8'],
  '/demo-project.mjs': ['demo-project.mjs', 'text/javascript; charset=utf-8'],
  '/shot-editing.mjs': ['shot-editing.mjs', 'text/javascript; charset=utf-8'],
});

// Load only the explicit public assets. Never serve a checkout, directory listing,
// provider route, arbitrary path, symlink target, or user-controlled file.
export async function loadPublicAssets() {
  const root = await realpath(fileURLToPath(new URL('../public/', import.meta.url)));
  const readPublic = async filename => {
    const target = await realpath(join(root, filename));
    if (dirname(target) !== root) throw new Error('Public asset escaped its directory');
    return readFile(target);
  };
  const headers = { 'Cache-Control': 'no-store' };
  for (const line of (await readPublic('_headers')).toString('utf8').split('\n')) {
    const match = line.match(/^\s+([^:]+):\s*(.+)$/);
    if (match) headers[match[1]] = match[2];
  }
  const assets = new Map();
  for (const [path, [filename, mime]] of Object.entries(PUBLIC_FILES)) {
    assets.set(path, { body: await readPublic(filename), headers: { ...headers, 'Content-Type': mime } });
  }
  return assets;
}

export function publicResponse(assets, method, requestUrl) {
  if (!['GET', 'HEAD'].includes(method)) return { status: 405, headers: { Allow: 'GET, HEAD' }, body: '' };
  let path;
  try { path = decodeURIComponent(requestUrl.split('?')[0]); } catch { return { status: 400, headers: {}, body: '' }; }
  const asset = assets.get(path);
  if (!asset) return { status: 404, headers: {}, body: '' };
  return { status: 200, headers: asset.headers, body: method === 'HEAD' ? '' : asset.body };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const assets = await loadPublicAssets();
  const server = createServer((request, response) => {
    const result = publicResponse(assets, request.method, request.url);
    response.writeHead(result.status, result.headers);
    response.end(result.body);
  });
  server.listen(4173, '127.0.0.1');
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => server.close());
}
