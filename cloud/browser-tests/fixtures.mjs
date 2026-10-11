import { test as base, expect } from '@playwright/test';
import { ORIGIN, PUBLIC_FILES } from './static-server.mjs';

export { expect };
export const SYNTHETIC_KEY = 'synthetic-browser-test-key-never-valid';
export const test = base.extend({
  network: [async ({ context }, use) => {
    const network = { apiCalls: 0, unexpected: 0, reply: null };
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== ORIGIN) { network.unexpected++; await route.abort('blockedbyclient'); return; }
      if (url.pathname.startsWith('/api/')) {
        network.apiCalls++;
        if (request.method() === 'POST' && ['/api/chat', '/api/images'].includes(url.pathname) && network.reply) {
          await network.reply(route); return;
        }
        network.unexpected++; await route.abort('blockedbyclient'); return;
      }
      if (['GET', 'HEAD'].includes(request.method()) && Object.hasOwn(PUBLIC_FILES, url.pathname)) {
        await route.continue(); return;
      }
      // A missing favicon is harmless, and is also kept off the network.
      if (url.pathname === '/favicon.ico') { await route.fulfill({ status: 204 }); return; }
      network.unexpected++; await route.abort('blockedbyclient');
    });
    await context.routeWebSocket(/.*/, socket => { network.unexpected++; socket.close(); });
    await use(network);
    expect(network.unexpected, 'No external network or unmocked API is allowed').toBe(0);
  }, { auto: true }],
  browserErrors: [async ({ page }, use) => {
    let errors = 0;
    page.on('pageerror', () => { errors++; });
    await use();
    expect(errors, 'No uncaught browser errors').toBe(0);
  }, { auto: true }],
});

export async function openApp(page) {
  await page.goto('/');
  await expect(page.locator('#new-project')).toBeEnabled();
}
export async function loadDemo(page) {
  await openApp(page);
  await page.locator('#load-demo').click();
  await expect(page.locator('#project-title')).toHaveText('示例 · 最后一班渡船');
}
export async function navigate(page, view) {
  await page.locator(`.sidebar [data-view="${view}"]`).click();
  await expect(page.locator(`#view-${view}`)).toBeVisible();
}
export async function setSyntheticKey(page) {
  await page.locator('#settings-open').click();
  await page.locator('#deepseek-key').fill(SYNTHETIC_KEY);
  await page.locator('#settings-dialog').getByRole('button', { name: '完成', exact: true }).click();
}
export async function confirmClick(page, selector, accept) {
  const dialogPromise = page.waitForEvent('dialog');
  const clickPromise = page.locator(selector).click();
  const dialog = await dialogPromise;
  expect(dialog.type()).toBe('confirm');
  await (accept ? dialog.accept() : dialog.dismiss());
  await clickPromise;
}
export function generated(script) {
  return { choices: [{ message: { content: JSON.stringify({ script }) } }], usage: { total_tokens: 12 } };
}
