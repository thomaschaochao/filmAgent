import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, mkdir, rename, stat, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { JSDOM } from 'jsdom';

// All filesystem operations below are confined to a freshly-created synthetic
// project under the OS temporary directory, never an actual user's folder.
class TestDirectory {
  constructor(path, state) { this.path = path; this.name = 'Synthetic FilmPilot project'; this.state = state; this.kind = 'directory'; }
  async queryPermission() { return this.state.permission; }
  async requestPermission() { this.state.prompts++; return (this.state.permission = 'granted'); }
  async isSameEntry(other) { return this.path === other.path; }
  resolve(name) { assert.match(name, /^[A-Za-z0-9_.-]+$/); assert.notEqual(name, '..'); return join(this.path, name); }
  async getDirectoryHandle(name, { create = false } = {}) {
    const path = this.resolve(name);
    if (create) await mkdir(path, { recursive: true });
    return new TestDirectory(path, this.state);
  }
  async getFileHandle(name, { create = false } = {}) {
    const path = this.resolve(name), state = this.state;
    try { await stat(path); } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      if (!create) throw new DOMException('Missing fixture', 'NotFoundError');
      await writeFile(path, '');
    }
    return {
      kind: 'file', name,
      getFile: async () => {
        state.reads++;
        const bytes = await readFile(path);
        return { size: bytes.length, text: async () => bytes.toString('utf8'), arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) };
      },
      createWritable: async () => {
        let bytes;
        return {
          write: async value => { bytes = value; },
          close: async () => { await writeFile(`${path}.temporary`, bytes); await rename(`${path}.temporary`, path); },
          abort: async () => {},
        };
      },
    };
  }
}

function handleDatabase() {
  const values = new Map();
  const provider = {
    open() {
      const request = {};
      queueMicrotask(() => {
        request.result = {
          objectStoreNames: { contains: () => true }, close() {},
          transaction() {
            const transaction = {};
            transaction.objectStore = () => ({
              get(key) { const r = { result: values.get(key) }; queueMicrotask(() => transaction.oncomplete()); return r; },
              put(value, key) { assert.equal(value.kind, 'directory'); values.set(key, value); const r = {}; queueMicrotask(() => transaction.oncomplete()); return r; },
              delete(key) { values.delete(key); const r = {}; queueMicrotask(() => transaction.oncomplete()); return r; },
            });
            return transaction;
          },
        };
        request.onsuccess();
      });
      return request;
    },
  };
  return { provider, values };
}

test('folder UI persists real synthetic files, handles revocation, and restores on refresh', async () => {
  const path = await mkdtemp(join(tmpdir(), 'filmpilot-folder-ui-'));
  const state = { permission: 'granted', reads: 0, prompts: 0, pickers: 0, cancel: false };
  const folder = new TestDirectory(path, state), db = handleDatabase();
  const original = new Map();
  function install(name, value) {
    if (!original.has(name)) original.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  let dom, timers = [];
  const nativeSetTimeout = globalThis.setTimeout;
  const html = await readFile(new URL('../public/index.html', import.meta.url), 'utf8');
  async function settle() { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); }
  async function until(check) {
    for (let i = 0; i < 100; i++) { if (check()) return; await new Promise(resolve => nativeSetTimeout(resolve, 5)); }
    assert.ok(check(), 'UI state did not settle');
  }
  function close() {
    if (dom) { dom.window.dispatchEvent(new dom.window.Event('pagehide')); dom.window.close(); }
    for (const timer of timers) clearTimeout(timer); timers = [];
  }
  async function open() {
    close();
    dom = new JSDOM(html, { url: 'https://folder.filmpilot.example/', pretendToBeVisual: true });
    for (const name of ['window', 'document', 'navigator', 'FileReader']) install(name, dom.window[name]);
    install('confirm', () => true);
    install('indexedDB', db.provider);
    install('showDirectoryPicker', async () => { state.pickers++; if (state.cancel) throw new DOMException('Cancelled', 'AbortError'); return folder; });
    install('fetch', () => { throw new Error('No network or provider call is allowed in folder UI tests'); });
    install('setTimeout', (callback, delay, ...args) => { const timer = nativeSetTimeout(callback, delay, ...args); timers.push(timer); return timer; });
    dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
    dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
    dom.window.HTMLElement.prototype.scrollIntoView = function () {};
    await import(`../public/app.mjs?folder-ui=${crypto.randomUUID()}`);
    await until(() => !$('#new-project').disabled); await settle();
  }
  const $ = selector => dom.window.document.querySelector(selector);
  const click = selector => $(selector).click();
  function input(selector, value) { $(selector).value = value; $(selector).dispatchEvent(new dom.window.Event('input', { bubbles: true })); }
  try {
    await open();
    click('#new-project'); input('#new-name', '文件夹回归项目');
    $('#new-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
    await until(() => !$('#save-project').disabled);
    assert.equal(JSON.parse(await readFile(join(path, 'project.json'), 'utf8')).name, '文件夹回归项目');
    assert.equal(JSON.parse(await readFile(join(path, 'manifest.json'), 'utf8')).projectFile, 'project.json');
    assert.equal(db.values.size, 1); assert.equal(db.values.get('project-directory'), folder);

    input('#script-text', '第一版剧本。'); input('#deepseek-key', 'dummy-transient-key');
    click('#save-project'); await until(() => !$('#save-project').disabled);
    let source = await readFile(join(path, 'project.json'), 'utf8');
    assert.equal(JSON.parse(source).script.text, '第一版剧本。'); assert.equal(source.includes('dummy-transient-key'), false);

    state.permission = 'denied'; input('#script-text', '撤权后的未保存修改。');
    click('#save-project'); await until(() => !$('#save-project').disabled);
    assert.match($('#notice').textContent, /权限/); assert.match($('#save-status').textContent, /未保存/);
    assert.equal(JSON.parse(await readFile(join(path, 'project.json'), 'utf8')).script.text, '第一版剧本。');
    let switchConfirmations = 0;
    install('confirm', () => { switchConfirmations++; return false; });
    click('#notice button'); await until(() => !$('#save-project').disabled); assert.equal(state.prompts, 1);
    assert.equal(switchConfirmations, 0, 'reconnect preserves the active project and must not ask to leave it');
    assert.equal($('#script-text').value, '撤权后的未保存修改。');
    install('confirm', () => true);
    click('#save-project'); await until(() => !$('#save-project').disabled);
    assert.equal(JSON.parse(await readFile(join(path, 'project.json'), 'utf8')).script.text, '撤权后的未保存修改。');

    const pickerCount = state.pickers;
    await open();
    assert.equal($('#project-title').textContent, '文件夹回归项目');
    assert.equal($('#script-text').value, '撤权后的未保存修改。');
    assert.equal($('#deepseek-key').value, ''); assert.equal(state.pickers, pickerCount); assert.equal(state.prompts, 1);

    state.permission = 'prompt'; const reads = state.reads;
    await open();
    assert.equal(state.reads, reads); assert.equal(state.prompts, 1);
    assert.match($('#notice').textContent, /重新授权/);
    click('#notice button'); await until(() => !$('#new-project').disabled);
    assert.equal($('#project-title').textContent, '文件夹回归项目'); assert.equal(state.prompts, 2);

    state.cancel = true; click('#open-folder'); await until(() => !$('#open-folder').disabled);
    assert.equal($('#project-title').textContent, '文件夹回归项目');
    assert.equal($('#script-text').value, '撤权后的未保存修改。');
  } finally {
    close();
    for (const [name, descriptor] of original) { if (descriptor) Object.defineProperty(globalThis, name, descriptor); else delete globalThis[name]; }
    await rm(path, { recursive: true, force: true });
  }
});
