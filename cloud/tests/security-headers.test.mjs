import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('static security headers keep all executable resources and connections on origin', async () => {
  const source = await readFile(new URL('../public/_headers', import.meta.url), 'utf8');
  const headers = new Headers(source.trim().split('\n').slice(1).map(line => {
    const colon = line.indexOf(':'); return [line.slice(0, colon).trim(), line.slice(colon + 1).trim()];
  }));
  const directives = new Map(headers.get('Content-Security-Policy').split(';').filter(x => x.trim()).map(x => {
    const [name, ...values] = x.trim().split(/\s+/); return [name, values];
  }));
  for (const name of ['default-src', 'script-src', 'style-src', 'connect-src', 'font-src', 'form-action']) {
    assert.deepEqual(directives.get(name), ["'self'"]);
  }
  assert.deepEqual(directives.get('img-src'), ["'self'", 'data:', 'blob:']);
  for (const name of ['object-src', 'base-uri', 'frame-ancestors']) assert.deepEqual(directives.get(name), ["'none'"]);
  assert.equal(headers.get('X-Content-Type-Options'), 'nosniff');
  assert.equal(headers.get('Referrer-Policy'), 'no-referrer');
  assert.equal(headers.get('X-Frame-Options'), 'DENY');
  assert.equal(headers.get('Cross-Origin-Resource-Policy'), 'same-origin');
  assert.equal(headers.get('Strict-Transport-Security'), 'max-age=31536000');
  assert.equal(headers.get('Permissions-Policy'), 'camera=(), microphone=(), geolocation=(), payment=(), usb=()');
});
