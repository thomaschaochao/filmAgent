import { test, expect, SYNTHETIC_KEY, openApp, navigate, setSyntheticKey } from './fixtures.mjs';

// Real Chromium OPFS files, writable streams, Web Locks, and IndexedDB-cloned
// directory handles are exercised. The OS picker and permission results below
// are synthetic: native directory dialogs and OS permission prompts are NOT
// verified here, and no user filesystem is accessed.
test('synthetic picker with real OPFS survives saves, permission loss, and reload', async ({ page, network }) => {
  await page.addInitScript(() => {
    window.folderFixture = { permission: 'granted', prompts: 0, pickers: 0, cancel: false };
    const fixture = window.folderFixture;
    FileSystemHandle.prototype.queryPermission = async () => fixture.permission;
    FileSystemHandle.prototype.requestPermission = async () => { fixture.prompts++; return (fixture.permission = 'granted'); };
    window.showDirectoryPicker = async () => {
      fixture.pickers++;
      if (fixture.cancel) throw new DOMException('Synthetic picker cancelled', 'AbortError');
      const root = await navigator.storage.getDirectory();
      return root.getDirectoryHandle('synthetic-filmpilot-project', { create: true });
    };
  });
  await openApp(page);
  await page.locator('#new-project').click();
  await page.locator('#new-name').fill('浏览器文件夹回归项目');
  await page.locator('#new-folder').click();
  await expect(page.locator('#project-title')).toHaveText('浏览器文件夹回归项目');
  await expect(page.locator('#save-status')).toHaveText('已保存到文件夹');
  const readProject = () => page.evaluate(async () => {
    const root = await navigator.storage.getDirectory();
    const folder = await root.getDirectoryHandle('synthetic-filmpilot-project');
    return (await (await folder.getFileHandle('project.json')).getFile()).text();
  });
  expect(JSON.parse(await readProject()).name).toBe('浏览器文件夹回归项目');
  await navigate(page, 'script');
  await page.locator('#script-text').fill('第一版剧本。');
  await setSyntheticKey(page);
  await page.locator('#save-project').click();
  await expect(page.locator('#save-status')).toHaveText('已保存到文件夹');
  expect(JSON.parse(await readProject()).script.text).toBe('第一版剧本。');
  expect(await readProject()).not.toContain(SYNTHETIC_KEY);
  await page.evaluate(() => { window.folderFixture.permission = 'denied'; });
  await page.locator('#script-text').fill('模拟撤权后尚未保存的修改。');
  await page.locator('#save-project').click();
  await expect(page.locator('#notice')).toContainText('权限');
  await expect(page.locator('#save-status')).toContainText('未保存');
  expect(JSON.parse(await readProject()).script.text).toBe('第一版剧本。');
  expect(await page.evaluate(() => window.folderFixture.prompts)).toBe(0);
  await page.locator('#notice').getByRole('button', { name: '重新授权' }).click();
  await expect(page.locator('#notice')).toContainText('权限已恢复');
  expect(await page.evaluate(() => window.folderFixture.prompts)).toBe(1);
  await page.locator('#save-project').click();
  await expect(page.locator('#save-status')).toHaveText('已保存到文件夹');
  expect(JSON.parse(await readProject()).script.text).toBe('模拟撤权后尚未保存的修改。');
  await page.reload();
  await expect(page.locator('#project-title')).toHaveText('浏览器文件夹回归项目');
  await expect(page.locator('#save-project')).toBeEnabled();
  await expect(page.locator('#script-text')).toHaveValue('模拟撤权后尚未保存的修改。');
  await expect(page.locator('#deepseek-key')).toHaveValue('');
  expect(await page.evaluate(() => [window.folderFixture.pickers, window.folderFixture.prompts])).toEqual([0, 0]);
  await page.evaluate(() => { window.folderFixture.cancel = true; });
  await page.locator('#open-folder').click();
  await expect(page.locator('#open-folder')).toBeEnabled();
  await expect(page.locator('#project-title')).toHaveText('浏览器文件夹回归项目');
  await expect(page.locator('#script-text')).toHaveValue('模拟撤权后尚未保存的修改。');
  expect(network.apiCalls).toBe(0);
});
