import { readFile } from 'node:fs/promises';
import { test, expect, SYNTHETIC_KEY, loadDemo, navigate, setSyntheticKey, confirmClick, generated } from './fixtures.mjs';

test('offline demo works without a key or any model request', async ({ page, context, network }) => {
  await loadDemo(page);
  await context.setOffline(true);
  await expect(page.locator('#notice')).toContainText('未调用任何模型');
  await navigate(page, 'script');
  await expect(page.locator('#script-text')).toHaveValue(/最后一班渡船/);
  await expect(page.locator('#script-badge')).toHaveText('草稿');
  await page.locator('#generate-script').click();
  await expect(page.locator('#settings-dialog')).toBeVisible();
  await page.locator('#settings-dialog').getByRole('button', { name: '完成', exact: true }).click();
  await navigate(page, 'assets');
  await expect(page.locator('.asset-card')).toHaveCount(4);
  await navigate(page, 'shots');
  await expect(page.locator('.shot-card')).toHaveCount(5);
  await expect(page.locator('#shot-duration')).toContainText('60');
  expect(network.apiCalls).toBe(0);
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test('named script snapshots preview exact text and require unlock plus restore confirmation', async ({ page }) => {
  await loadDemo(page);
  await navigate(page, 'script');
  const first = '  第一版 <img src=x onerror=alert(1)>\n保留换行和空格。  ';
  const second = '第二版需要能够完整恢复。';
  await page.locator('#script-text').fill(first);
  await page.locator('#script-version-title').fill('<b>第一版</b>');
  await page.locator('#save-script-version').click();
  const firstId = await page.locator('#script-version-select').inputValue();
  await expect(page.locator('#script-version-select option:checked')).toHaveText('<b>第一版</b>');
  expect(await page.locator('#script-version-preview').textContent()).toBe(first);
  await expect(page.locator('#script-version-preview > *')).toHaveCount(0);
  const count = await page.locator('#script-version-select option').count();
  await expect(page.locator('#save-script-version')).toBeDisabled();
  await expect(page.locator('#script-version-select option')).toHaveCount(count);
  await page.locator('#script-text').fill(second);
  await page.locator('#script-version-title').fill('第二版');
  await page.locator('#save-script-version').click();
  await page.locator('#script-version-select').selectOption(firstId);
  await expect(page.locator('#script-text')).toHaveValue(second);
  await page.locator('#approve-script').click();
  await expect(page.locator('#script-text')).toHaveJSProperty('readOnly', true);
  await expect(page.locator('#restore-script-version')).toBeDisabled();
  await expect(page.locator('#script-history-lock')).toBeVisible();
  await confirmClick(page, '#unlock-script', false);
  await expect(page.locator('#script-text')).toHaveJSProperty('readOnly', true);
  await confirmClick(page, '#unlock-script', true);
  await expect(page.locator('#restore-script-version')).toBeEnabled();
  await confirmClick(page, '#restore-script-version', false);
  await expect(page.locator('#script-text')).toHaveValue(second);
  await confirmClick(page, '#restore-script-version', true);
  await expect(page.locator('#script-text')).toHaveValue(first);
  await expect(page.locator('#script-badge')).toHaveText('草稿');
  await expect(page.locator('#save-status')).toContainText('未保存');
  await expect(page.locator('#notice')).toContainText('一致性');
  await expect(page.locator('.asset-card')).toHaveCount(4);
  await expect(page.locator('.shot-card')).toHaveCount(5);
});

test('real JSON download and file import roundtrip preserves history and excludes session keys', async ({ page, network }) => {
  await loadDemo(page);
  await navigate(page, 'script');
  await page.locator('#script-version-title').fill('浏览器导出版本');
  await page.locator('#save-script-version').click();
  const script = await page.locator('#script-text').inputValue();
  const versions = await page.locator('#script-version-select option').count();
  await setSyntheticKey(page);
  // Observe, without intercepting, the filename supplied to the real download.
  // Keep Chinese text here to catch regressions in Chromium's OS-locale setup.
  await page.evaluate(() => {
    window.syntheticDownloadName = null;
    document.addEventListener('click', event => {
      const anchor = event.target.closest('a[download]');
      if (anchor) window.syntheticDownloadName = anchor.download;
    }, { capture: true });
  });
  const downloadPromise = page.waitForEvent('download');
  await page.locator('#export-project').click();
  const download = await downloadPromise;
  const suppliedFilename = await page.evaluate(() => window.syntheticDownloadName);
  expect(suppliedFilename).toBe('示例-最后一班渡船.filmpilot.json');
  expect(download.suggestedFilename()).toMatch(/\.filmpilot\.json$/);
  expect(download.suggestedFilename()).toBe(suppliedFilename);
  const bytes = await readFile(await download.path());
  const source = bytes.toString('utf8'), data = JSON.parse(source);
  expect(data.format).toBe('filmpilot-package');
  expect(data.project.scripts).toHaveLength(versions);
  expect(source).not.toContain(SYNTHETIC_KEY);
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  await expect(page.locator('#save-status')).toContainText('未保存');
  await page.locator('#notice').getByRole('button', { name: '我已保存项目包' }).click();
  await expect(page.locator('#save-status')).not.toContainText('未保存');
  await page.reload();
  await expect(page.locator('#new-project')).toBeEnabled();
  await expect(page.locator('#welcome')).toBeVisible();
  await expect(page.locator('#deepseek-key')).toHaveValue('');
  await page.locator('#import-file').setInputFiles({ name: download.suggestedFilename(), mimeType: 'application/json', buffer: bytes });
  await expect(page.locator('#project-title')).toHaveText(data.project.name);
  await expect(page.locator('#save-project')).toBeEnabled();
  await navigate(page, 'script');
  await expect(page.locator('#script-text')).toHaveValue(script);
  await expect(page.locator('#script-version-select option')).toHaveCount(versions);
  await expect(page.locator('#script-version-select option:checked')).toHaveText('浏览器导出版本');
  await page.locator('#import-file').setInputFiles({ name: 'invalid.filmpilot.json', mimeType: 'application/json', buffer: Buffer.from('{"format":"filmpilot-package","schemaVersion":1,"project":{"apiKey":"synthetic-invalid-field"}}') });
  await expect(page.locator('#notice')).toContainText('校验');
  await expect(page.locator('#project-title')).toHaveText(data.project.name);
  await expect(page.locator('#script-text')).toHaveValue(script);
  expect(network.apiCalls).toBe(0);
});

test('cancelled late completion cannot replace a newer explicit generation', async ({ page, network }) => {
  // Deliberately cancellation-insensitive synthetic transport: native fetch would
  // reject on abort before the app's stale-result guard could be exercised.
  await page.addInitScript(() => {
    const fetch = window.fetch.bind(window);
    window.syntheticRequests = [];
    window.fetch = (input, options) => {
      if (new URL(input, location.href).pathname !== '/api/chat') return fetch(input, options);
      return new Promise(resolve => window.syntheticRequests.push({ signal: options.signal, resolve }));
    };
  });
  await loadDemo(page);
  await navigate(page, 'script');
  const original = await page.locator('#script-text').inputValue();
  await setSyntheticKey(page);
  await page.locator('#generate-script').click();
  await expect.poll(() => page.evaluate(() => window.syntheticRequests.length)).toBe(1);
  await expect(page.locator('#generate-script')).toBeDisabled();
  await page.locator('#cancel-request').click();
  expect(await page.evaluate(() => window.syntheticRequests[0].signal.aborted)).toBe(true);
  await expect(page.locator('#generate-script')).toBeEnabled();
  await expect(page.locator('#activity-list')).toContainText('仍可能产生费用');
  await page.locator('#generate-script').click();
  await expect.poll(() => page.evaluate(() => window.syntheticRequests.length)).toBe(2);
  await page.evaluate(async response => {
    window.syntheticRequests[0].resolve(Response.json(response));
    await new Promise(resolve => setTimeout(resolve, 0));
  }, generated('已取消的旧请求不能产生建议。'));
  await expect(page.locator('#proposal-dialog')).not.toBeVisible();
  await expect(page.locator('#script-text')).toHaveValue(original);
  await expect(page.locator('#generate-script')).toBeDisabled();
  await page.evaluate(response => window.syntheticRequests[1].resolve(Response.json(response)), generated('只展示用户手动重新发起的结果。'));
  await expect(page.locator('#proposal-dialog')).toBeVisible();
  await expect(page.locator('#proposal-content')).toContainText('手动重新发起');
  await expect(page.locator('#script-text')).toHaveValue(original);
  await page.locator('#discard-proposal').click();
  await expect(page.locator('#script-text')).toHaveValue(original);
  expect(network.apiCalls).toBe(0);
});

test('provider errors are sanitized and a successful suggestion requires explicit apply', async ({ page, network }) => {
  await loadDemo(page);
  await navigate(page, 'script');
  await setSyntheticKey(page);
  const original = await page.locator('#script-text').inputValue();
  network.reply = async route => {
    const request = route.request();
    expect(request.headers().authorization === `Bearer ${SYNTHETIC_KEY}`).toBe(true);
    expect(request.postData().includes(SYNTHETIC_KEY)).toBe(false);
    await route.fulfill({ status: 429, contentType: 'text/plain', body: `unsafe provider echo ${SYNTHETIC_KEY}` });
  };
  await page.locator('#generate-script').click();
  await expect(page.locator('#notice')).toContainText('额度');
  await expect(page.locator('#generate-script')).toBeEnabled();
  await expect(page.locator('#notice')).not.toContainText(SYNTHETIC_KEY);
  await expect(page.locator('#activity-list')).not.toContainText('unsafe provider echo');
  await expect(page.locator('#script-text')).toHaveValue(original);
  await expect(page.locator('#proposal-dialog')).not.toBeVisible();
  expect(network.apiCalls).toBe(1);
  network.reply = route => route.fulfill({ json: generated('经过审核才应用的合成剧本。') });
  await page.locator('#generate-script').click();
  await expect(page.locator('#proposal-dialog')).toBeVisible();
  await expect(page.locator('#script-text')).toHaveValue(original);
  await page.locator('#apply-proposal').click();
  await expect(page.locator('#script-text')).toHaveValue('经过审核才应用的合成剧本。');
  expect(network.apiCalls).toBe(2);
});
