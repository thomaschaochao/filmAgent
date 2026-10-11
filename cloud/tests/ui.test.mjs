import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';

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
  const downloads = [];
  const oldCreate = URL.createObjectURL;
  const oldRevoke = URL.revokeObjectURL;
  URL.createObjectURL = blob => { downloads.push(blob); return 'blob:test-download'; };
  URL.revokeObjectURL = () => {};
  window.HTMLAnchorElement.prototype.click = function () {};
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
