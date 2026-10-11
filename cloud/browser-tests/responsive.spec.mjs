import { mkdir, stat } from 'node:fs/promises';
import { test, expect, loadDemo, navigate } from './fixtures.mjs';

// Only original, no-key demo content is captured; no automatic failure
// screenshots, traces, videos, provider responses, or user data are uploaded.
test('clean demo is usable at desktop and mobile widths', async ({ page, network }) => {
  await loadDemo(page);
  await mkdir('browser-artifacts', { recursive: true });
  for (const [label, viewport] of [
    ['desktop', { width: 1440, height: 1000 }],
    ['mobile', { width: 390, height: 844 }],
  ]) {
    await page.setViewportSize(viewport);
    await navigate(page, 'overview');
    for (const view of ['overview', 'script', 'assets', 'shots']) {
      await navigate(page, view);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `${label} ${view} has no horizontal overflow`).toBe(true);
    }
    await navigate(page, 'overview');
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.locator('#deepseek-key')).toHaveValue('');
    await expect(page.locator('#toast')).not.toBeVisible({ timeout: 10_000 });
    if (label === 'mobile') {
      await expect(page.locator('.sidebar nav .nav-item').last()).toHaveCSS('white-space', 'nowrap');
      await expect(page.locator('#settings-open')).toHaveCSS('white-space', 'nowrap');
    }
    const path = `browser-artifacts/demo-${label}.png`;
    await page.screenshot({ path, fullPage: false, animations: 'disabled' });
    expect((await stat(path)).size).toBeLessThan(2 * 1024 * 1024);
  }
  expect(network.apiCalls).toBe(0);
});
