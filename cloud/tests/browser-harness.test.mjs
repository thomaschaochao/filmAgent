import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPublicAssets, publicResponse, PUBLIC_FILES } from '../browser-tests/static-server.mjs';

test('browser harness serves only explicitly allowlisted public source files', async () => {
  const assets = await loadPublicAssets();
  assert.equal(assets.size, Object.keys(PUBLIC_FILES).length);
  for (const path of Object.keys(PUBLIC_FILES)) {
    const response = publicResponse(assets, 'GET', path);
    assert.equal(response.status, 200); assert.ok(response.body.length);
    assert.equal(response.headers['Cache-Control'], 'no-store');
    assert.match(response.headers['Content-Security-Policy'], /connect-src 'self'/);
    assert.equal(publicResponse(assets, 'HEAD', path).body, '');
  }
  for (const path of ['/api/chat', '/api/images', '/.env', '/.dev.vars', '/config.local.env', '/deepseekapi.txt', '/README.md', '/_headers', '/../package.json', '/%2e%2e/package.json', '/node_modules/', '/app.mjs/anything', '/app.mjs%00']) {
    assert.equal(publicResponse(assets, 'GET', path).status, 404);
  }
  assert.equal(publicResponse(assets, 'GET', '/%invalid').status, 400);
  assert.equal(publicResponse(assets, 'POST', '/app.mjs').status, 405);
  assert.equal(publicResponse(assets, 'DELETE', '/').status, 405);
});
