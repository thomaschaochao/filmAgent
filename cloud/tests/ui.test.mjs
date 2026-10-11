import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
import { ProjectStore } from '../public/project-store.mjs';

test('workbench DOM flows preserve edits, isolate keys, and require proposal review', async t => {
  const html = await readFile(new URL('../public/index.html', import.meta.url), 'utf8');
  const dom = new JSDOM(html, { url: 'https://filmpilot.example/', pretendToBeVisual: true });
  const window = dom.window;
  const prior = new Map();
  function install(name, value) {
    prior.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  for (const name of ['window', 'document', 'navigator', 'FileReader']) install(name, window[name]);
  install('confirm', () => true);
  install('showDirectoryPicker', undefined);
  install('indexedDB', undefined);
  const originalSetTimeout = globalThis.setTimeout;
  const timers = [];
  install('setTimeout', (callback, delay, ...args) => {
    const timer = originalSetTimeout(callback, delay, ...args); timers.push(timer); return timer;
  });
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  const downloads = [], downloadNames = [];
  const oldCreate = URL.createObjectURL;
  const oldRevoke = URL.revokeObjectURL;
  URL.createObjectURL = blob => { downloads.push(blob); return 'blob:test-download'; };
  URL.revokeObjectURL = () => {};
  window.HTMLAnchorElement.prototype.click = function () { downloadNames.push(this.download); };
  const calls = [];
  let respond = () => Promise.resolve(Response.json({ choices: [{ message: { content: '{}' } }] }));
  install('fetch', (url, options) => { calls.push({ url, options }); return respond(url, options); });
  const $ = selector => window.document.querySelector(selector);
  const tick = () => new Promise(resolve => setImmediate(resolve));
  async function settle() { for (let i = 0; i < 8; i++) await tick(); }
  function input(selector, value) { const node = $(selector); node.value = value; node.dispatchEvent(new window.Event('input', { bubbles: true })); }
  const click = selector => $(selector).click();
  const generated = value => Response.json({ choices: [{ message: { content: JSON.stringify(value) } }], usage: { total_tokens: 12 } });
  const textKey = 'dummy-text-key-for-ui-tests';
  const imageKey = 'dummy-image-key-for-ui-tests';
  try {
    await import(`../public/app.mjs?dom-test=${Date.now()}`);
    await settle();

    await t.test('unsupported browsers keep the complete project-package path', async () => {
      assert.match($('#support-note').textContent, /项目包/);
      click('#new-project'); input('#new-name', '<img src=x> 测试电影'); click('#new-package');
      assert.equal($('#project-title').textContent, '<img src=x> 测试电影');
      assert.equal($('#project-title').children.length, 0);
      assert.match($('#save-status').textContent, /未保存/);
      input('#project-description', '一位飞行员在暴雨中返航。');
    });

    await t.test('a missing key never calls the provider and stale responses never apply', async () => {
      click('#generate-script'); assert.equal(calls.length, 0); assert.equal($('#settings-dialog').open, true);
      input('#deepseek-key', textKey); $('#settings-dialog').close();
      let release;
      respond = () => new Promise(resolve => { release = resolve; });
      click('#generate-script'); click('#generate-script');
      assert.equal(calls.length, 1);
      assert.equal(calls[0].options.headers.Authorization, `Bearer ${textKey}`);
      assert.equal(calls[0].options.body.includes(textKey), false);
      assert.equal(calls[0].options.credentials, 'omit');
      input('#project-description', '生成期间主动修改过的新创意。');
      release(generated({ script: '这个过期结果不能覆盖项目。' })); await settle();
      assert.equal($('#proposal-dialog').open, false);
      assert.equal($('#script-text').value, '');
    });

    await t.test('generated script needs explicit apply and approved scripts stay locked', async () => {
      respond = () => Promise.resolve(generated({ script: '夜。林深驾驶飞机穿过暴雨，终于看到了跑道灯。' }));
      click('#generate-script'); await settle();
      assert.equal($('#proposal-dialog').open, true);
      assert.equal($('#script-text').value, '');
      click('#apply-proposal');
      assert.match($('#script-text').value, /跑道灯/);
      click('#approve-script');
      assert.equal($('#script-text').readOnly, true);
      assert.equal($('#generate-script').disabled, true);
    });

    await t.test('asset and image proposals remain local and packages exclude credentials', async () => {
      respond = () => Promise.resolve(generated({ assets: [{ name: '林深', type: 'character', description: '青年飞行员', prompt: '灰色飞行夹克，正面角色参考图。' }] }));
      click('#extract-assets'); await settle(); click('#apply-proposal');
      assert.equal($('#asset-list').querySelectorAll('.asset-card').length, 1);
      input('#openai-key', imageKey);
      const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOioAAAAASUVORK5CYII=';
      respond = () => Promise.resolve(Response.json({ data: [{ b64_json: png }] }));
      click('[data-action="image"]'); await settle();
      assert.equal(calls.at(-1).options.headers.Authorization, `Bearer ${imageKey}`);
      assert.equal($('#proposal-dialog').open, true); click('#apply-proposal');
      assert.match($('#asset-list img').getAttribute('src'), /^data:image\/png;base64,/);
      click('#export-project'); assert.equal(downloads.length, 1);
      const exported = await downloads[0].text();
      assert.equal(exported.includes(textKey), false); assert.equal(exported.includes(imageKey), false);
      assert.equal(JSON.parse(exported).project.assets.length, 1);
      assert.equal(window.localStorage.length, 0); assert.equal(window.sessionStorage.length, 0);
      assert.match($('#save-status').textContent, /未保存|确认/);
    });

    await t.test('malformed provider content preserves current script and stays sanitized', async () => {
      click('#unlock-script'); const before = $('#script-text').value;
      respond = () => Promise.resolve(Response.json({ choices: [{ message: { content: 'invalid JSON' } }] }));
      click('#generate-script'); await settle();
      assert.equal($('#script-text').value, before);
      assert.equal($('#proposal-dialog').open, false);
      assert.match($('#notice').textContent, /JSON|原内容/);
      assert.equal($('#activity-list').textContent.includes(textKey), false);
    });

    await t.test('invalid imports cannot replace the current project', async () => {
      const before = $('#project-title').textContent;
      const file = { name: 'unsafe.filmpilot.json', size: 64, text: async () => '{"format":"filmpilot-package","schemaVersion":1,"project":{"apiKey":"not-a-real-key"}}' };
      Object.defineProperty($('#import-file'), 'files', { configurable: true, value: [file] });
      $('#import-file').dispatchEvent(new window.Event('change', { bubbles: true })); await settle();
      assert.equal($('#project-title').textContent, before);
      assert.match($('#notice').textContent, /校验|无效|项目/);
    });

    await t.test('a saved project still warns before losing an unreviewed generation', async () => {
      click('#export-project'); click('#notice button');
      assert.doesNotMatch($('#save-status').textContent, /未保存/);
      respond = () => Promise.resolve(generated({ script: '这份尚未审核的结果必须受到离开提醒保护。' }));
      click('#generate-script'); await settle();
      assert.equal($('#proposal-dialog').open, true);
      const event = new window.Event('beforeunload', { cancelable: true });
      window.dispatchEvent(event); assert.equal(event.defaultPrevented, true);
      click('#discard-proposal');
    });

    await t.test('folder save cannot lose a generation finishing while files are busy', async () => {
      const original = ProjectStore.prototype.createFolder;
      let finishSave, snapshot, finishGeneration;
      ProjectStore.prototype.createFolder = project => {
        snapshot = structuredClone(project);
        return new Promise(resolve => { finishSave = () => resolve(snapshot); });
      };
      try {
        const previous = $('#script-text').value;
        respond = () => new Promise(resolve => { finishGeneration = resolve; });
        click('#generate-script'); click('#attach-folder');
        finishGeneration(generated({ script: '保存过程中完成的生成建议。' })); await settle();
        assert.equal($('#proposal-dialog').open, false);
        assert.equal($('#apply-proposal').disabled, true);
        $('#apply-proposal').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
        assert.equal($('#script-text').value, previous);
        finishSave(); await settle();
        assert.equal($('#proposal-dialog').open, true);
        click('#apply-proposal');
        assert.equal($('#script-text').value, '保存过程中完成的生成建议。');
        assert.match($('#save-status').textContent, /未保存/);
      } finally { ProjectStore.prototype.createFolder = original; }
    });

    await t.test('an asynchronous save never marks a newer project revision as saved', async () => {
      const original = ProjectStore.prototype.createFolder;
      let finishSave;
      ProjectStore.prototype.createFolder = project => {
        const snapshot = structuredClone(project);
        return new Promise(resolve => { finishSave = () => resolve(snapshot); });
      };
      try {
        click('#attach-folder');
        // Simulate an asynchronous internal update despite controls being disabled.
        input('#project-name', '保存期间更新的项目名称');
        finishSave(); await settle();
        assert.equal($('#project-title').textContent, '保存期间更新的项目名称');
        assert.match($('#save-status').textContent, /未保存/);
      } finally { ProjectStore.prototype.createFolder = original; }
    });

    await t.test('script history previews exact text, deduplicates and requires unlocked confirmed restoration', async () => {
      const first = '  第一版 <img src=x onerror=alert(1)>\n保留换行和空格。  ';
      const second = '第二版需要能够完整恢复。';
      input('#script-text', first); input('#script-version-title', '<b>第一版</b>'); click('#save-script-version');
      const firstId = $('#script-version-select').value;
      const count = $('#script-version-select').options.length;
      assert.equal($('#script-version-preview').textContent, first);
      assert.equal($('#script-version-preview').children.length, 0);
      assert.equal($('#script-version-select').selectedOptions[0].textContent, '<b>第一版</b>');
      click('#save-script-version'); assert.equal($('#script-version-select').options.length, count);
      input('#script-text', second); input('#script-version-title', '第二版'); click('#save-script-version');
      const secondId = $('#script-version-select').value;
      const select = id => { $('#script-version-select').value = id; $('#script-version-select').dispatchEvent(new window.Event('change', { bubbles: true })); };
      select(firstId); assert.equal($('#script-version-preview').textContent, first);
      assert.equal($('#script-text').value, second);
      click('#approve-script'); assert.equal($('#restore-script-version').disabled, true);
      assert.equal($('#script-history-lock').hidden, false);
      globalThis.confirm = () => false; click('#unlock-script'); assert.equal($('#script-text').readOnly, true);
      globalThis.confirm = () => true; click('#unlock-script'); assert.equal($('#restore-script-version').disabled, false);
      globalThis.confirm = () => false; click('#restore-script-version'); assert.equal($('#script-text').value, second);
      const assetsBefore = $('#asset-list').querySelectorAll('.asset-card').length;
      globalThis.confirm = () => true; click('#restore-script-version');
      assert.equal($('#script-text').value, first); assert.equal($('#script-text').readOnly, false);
      assert.equal($('#asset-list').querySelectorAll('.asset-card').length, assetsBefore);
      assert.match($('#save-status').textContent, /未保存/); assert.match($('#notice').textContent, /一致性/);
      select(secondId); assert.equal($('#script-version-preview').textContent, second);
      const beforeDelete = $('#script-version-select').options.length;
      globalThis.confirm = () => false; click('#delete-script-version'); assert.equal($('#script-version-select').options.length, beforeDelete);
      globalThis.confirm = () => true; click('#delete-script-version');
      assert.equal($('#script-version-select').options.length, beforeDelete - 1); assert.equal($('#script-text').value, first);
    });

    await t.test('history packages roundtrip and memory keys cannot enter a version title', async () => {
      const count = $('#script-version-select').options.length;
      input('#script-text', '用于拒绝含密钥版本标题的安全剧本。'); input('#script-version-title', textKey); click('#save-script-version');
      assert.equal($('#script-version-select').options.length, count);
      assert.equal($('#notice').textContent.includes(textKey), false);
      input('#script-version-title', '安全标题'); click('#save-script-version');
      click('#export-project'); const blob = downloads.at(-1), exported = await blob.text();
      const project = JSON.parse(exported).project;
      assert.equal(project.scripts.length, count + 1); assert.equal(exported.includes(textKey), false);
      const file = { name: 'history.filmpilot.json', size: blob.size, text: async () => exported };
      Object.defineProperty($('#import-file'), 'files', { configurable: true, value: [file] });
      $('#import-file').dispatchEvent(new window.Event('change', { bubbles: true })); await settle();
      assert.equal($('#script-version-select').options.length, project.scripts.length);
      assert.equal($('#script-text').value, project.script.text);
      assert.equal($('#script-version-preview').textContent, project.scripts.at(-1).content);
    });

    await t.test('production downloads are guarded handoffs and never count as saved project backups', async () => {
      assert.equal($('#export-shot-csv').disabled, true);
      input('#script-text', '仅作制作交付的当前剧本。');
      const dirtyStatus = $('#save-status').textContent;
      const priorCalls = calls.length;
      click('#export-production');
      const brief = await downloads.at(-1).text();
      assert.match(downloadNames.at(-1), /\.txt$/); assert.match(brief, /仅作制作交付/);
      assert.equal(brief.includes(textKey), false); assert.equal(brief.includes('data:image/'), false);
      assert.equal($('#save-status').textContent, dirtyStatus); assert.match($('#notice').textContent, /不能代替/);
      assert.equal($('#notice button'), null);
      click('#add-shot'); input('#shot-list [data-field="description"]', '=SUM(1,2)');
      input('#shot-list [data-field="dialogue"]', '第一行\n第二行说“出发”。');
      assert.equal($('#export-shot-csv').disabled, false); click('#export-shot-csv');
      const csv = await downloads.at(-1).text();
      assert.match(downloadNames.at(-1), /\.csv$/); assert.match(csv, /'\=SUM\(1,2\)/);
      assert.match(csv, /第一行\n第二行/); assert.match($('#save-status').textContent, /未保存/);
      const before = downloads.length; input('#script-text', `不得导出的正文 ${textKey}`); click('#export-production');
      assert.equal(downloads.length, before); assert.equal($('#notice').textContent.includes(textKey), false);
      input('#script-text', '已移除密钥的当前剧本。');
      assert.equal(calls.length, priorCalls);
    });

    await t.test('cancellation discards late results and manual retry never reuses an old request', async () => {
      const original = $('#script-text').value, count = calls.length;
      let finishFirst, finishSecond;
      respond = () => new Promise(resolve => { finishFirst = resolve; });
      click('#generate-script'); const first = calls.at(-1);
      click('#cancel-request'); assert.equal(first.options.signal.aborted, true);
      assert.match($('#activity-list').textContent, /仍可能产生费用/);
      respond = () => new Promise(resolve => { finishSecond = resolve; }); click('#generate-script');
      assert.equal(calls.length, count + 2);
      finishFirst(generated({ script: '已取消的旧请求不能产生建议。' })); await settle();
      assert.equal($('#proposal-dialog').open, false); assert.equal($('#script-text').value, original);
      assert.equal($('#generate-script').disabled, true);
      finishSecond(generated({ script: '只展示用户手动重新发起的结果。' })); await settle();
      assert.equal($('#proposal-dialog').open, true); assert.match($('#proposal-content').textContent, /手动重新发起/);
      click('#discard-proposal'); assert.equal($('#script-text').value, original);
    });

    await t.test('provider failure is sanitized and never silently retried', async () => {
      const count = calls.length, original = $('#script-text').value;
      respond = () => Promise.resolve(new Response(`unsafe provider echo ${textKey}`, { status: 429 }));
      click('#generate-script'); await settle();
      assert.equal(calls.length, count + 1); assert.match($('#notice').textContent, /频繁|额度/);
      assert.equal($('#notice').textContent.includes(textKey), false); assert.equal($('#notice').textContent.includes('unsafe provider echo'), false);
      assert.equal($('#script-text').value, original); assert.equal($('#proposal-dialog').open, false);
    });

    await t.test('full history blocks a paid generation before requesting an unsafe replacement', async () => {
      click('#export-project'); const data = JSON.parse(await downloads.at(-1).text());
      data.project.scripts = Array.from({ length: 100 }, (_, i) => ({ id: `version-${i}`, title: `历史 ${i}`, content: `既有历史 ${i}`, approved: false }));
      const text = JSON.stringify(data), file = { name: 'full-history.filmpilot.json', size: new Blob([text]).size, text: async () => text };
      Object.defineProperty($('#import-file'), 'files', { configurable: true, value: [file] });
      $('#import-file').dispatchEvent(new window.Event('change', { bubbles: true })); await settle();
      input('#script-text', '尚未有快照的新编辑，不能付费生成后才发现无法保存。');
      const count = calls.length; click('#generate-script');
      assert.equal(calls.length, count); assert.match($('#notice').textContent, /本次未发送请求/);
      assert.equal($('#proposal-dialog').open, false); assert.match($('#script-text').value, /尚未有快照/);
    });

    await t.test('offline sample needs no key or network and confirms before replacing dirty work', async () => {
      const before = $('#project-title').textContent, count = calls.length;
      globalThis.confirm = () => false; click('#load-demo'); assert.equal($('#project-title').textContent, before);
      globalThis.confirm = () => true; click('#load-demo');
      assert.match($('#project-title').textContent, /示例/); assert.equal($('#script-text').readOnly, false);
      assert.ok($('#asset-list').querySelectorAll('.asset-card').length >= 3);
      assert.ok($('#shot-list').querySelectorAll('.shot-card').length >= 3);
      assert.match($('#save-status').textContent, /未保存/); assert.match($('#notice').textContent, /未调用任何模型/);
      assert.equal(calls.length, count);
      click('#export-project'); const project = JSON.parse(await downloads.at(-1).text()).project;
      assert.equal(project.script.status, 'draft'); assert.match(project.name, /示例/);
      assert.equal(JSON.stringify(project).includes(textKey), false);
    });

    await t.test('clear and pagehide remove credentials without a persisted fallback', async () => {
      click('#clear-keys'); assert.equal($('#deepseek-key').value, ''); assert.equal($('#openai-key').value, '');
      const count = calls.length; click('#generate-script'); assert.equal(calls.length, count);
      input('#deepseek-key', textKey); window.dispatchEvent(new window.Event('pagehide'));
      assert.equal($('#deepseek-key').value, '');
      assert.equal(window.localStorage.length, 0); assert.equal(window.sessionStorage.length, 0);
    });
  } finally {
    window.dispatchEvent(new window.Event('pagehide'));
    dom.window.close();
    for (const timer of timers) clearTimeout(timer);
    URL.createObjectURL = oldCreate; URL.revokeObjectURL = oldRevoke;
    for (const [name, descriptor] of prior) { if (descriptor) Object.defineProperty(globalThis, name, descriptor); else delete globalThis[name]; }
  }
});
