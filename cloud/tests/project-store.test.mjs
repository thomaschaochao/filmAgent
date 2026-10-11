import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import {
  LIMITS, ProjectStore, ProjectStoreError, validateProject, serializeProject,
  serializePackage, parsePackage, createDirectoryHandleStore,
} from '../public/project-store.mjs';

const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';
const MANIFEST = JSON.stringify({ format: 'filmpilot-project', schemaVersion: 1, projectFile: 'project.json' });
const project = () => ({
  schemaVersion: 1,
  id: 'project-1',
  name: 'First film',
  description: 'A small film about a lighthouse.',
  createdAt: '2026-10-11T07:00:00.000Z',
  updatedAt: '2026-10-11T07:00:00.000Z',
  settings: { visualStyle: 'watercolor', aspectRatio: '16:9', language: 'zh-CN' },
  script: { text: 'A keeper watches the ocean.', status: 'draft' },
  assets: [{ id: 'keeper', type: 'character', name: 'Keeper', description: 'Quiet', prompt: 'A lighthouse keeper', image: { mimeType: 'image/png', dataUrl: PNG } }],
  shots: [{ id: 'shot-1', number: 1, duration: 5, description: 'Ocean at dusk', dialogue: '', assetIds: ['keeper'], initialFramePrompt: 'Ocean', storyboardPrompt: 'Wide shot' }],
});

function error(name, message = 'Platform error with details that must not escape') { return Object.assign(new Error(message), { name }); }
const isCode = code => value => value instanceof ProjectStoreError && value.code === code;

/** In-memory browser filesystem mock. No real user file or directory is read. */
class MemoryDirectory {
  constructor(name = 'My project', root = null, path = '') {
    this.name = name;
    this.kind = 'directory';
    this.files = new Map();
    this.directories = new Map();
    this.root = root ?? this;
    this.path = path;
    if (!root) {
      this.log = [];
      this.permission = 'granted';
      this.readPermission = 'granted';
      this.requestResult = 'granted';
      this.failWrite = null;
      this.failClose = null;
      this.beforeCommit = null;
    }
  }
  seed(name, value) { this.files.set(name, new Blob([value])); }
  async queryPermission({ mode }) { this.root.log.push(['queryPermission', mode]); return mode === 'read' ? this.root.readPermission : this.root.permission; }
  async requestPermission({ mode }) { this.root.log.push(['requestPermission', mode]); this.root.permission = this.root.requestResult; return this.root.requestResult; }
  async getDirectoryHandle(name, { create = false } = {}) {
    const path = `${this.path}${name}`;
    this.root.log.push(['directory', path, create]);
    if (!this.directories.has(name)) {
      if (!create) throw error('NotFoundError');
      this.directories.set(name, new MemoryDirectory(name, this.root, `${path}/`));
    }
    return this.directories.get(name);
  }
  async getFileHandle(name, { create = false } = {}) {
    assert.ok(!/[\\/]/.test(name), 'File handles must be one path component');
    const path = `${this.path}${name}`;
    this.root.log.push(['file', path, create]);
    if (!this.files.has(name)) {
      if (!create) throw error('NotFoundError');
      this.files.set(name, new Blob());
    }
    const directory = this;
    return {
      name,
      kind: 'file',
      async getFile() { directory.root.log.push(['read', path]); return directory.files.get(name); },
      async createWritable() {
        directory.root.log.push(['writable', path]);
        let staged;
        let aborted = false;
        return {
          async write(value) {
            directory.root.log.push(['write', path]);
            if (directory.root.failWrite === path) throw error('QuotaExceededError');
            staged = new Blob([value]);
          },
          async close() {
            directory.root.log.push(['close', path]);
            if (directory.root.failClose === path) throw error('NotAllowedError');
            if (directory.root.beforeCommit) await directory.root.beforeCommit(path);
            if (!aborted) directory.files.set(name, staged);
          },
          async abort() { directory.root.log.push(['abort', path]); aborted = true; },
        };
      },
    };
  }
  async entries() { throw new Error('Directory enumeration is forbidden.'); }
  async values() { throw new Error('Directory enumeration is forbidden.'); }
}

class MemoryHandleStore {
  constructor(handle = null) { this.handle = handle; this.log = []; }
  async get() { this.log.push(['get']); return this.handle; }
  async set(handle) { this.log.push(['set', handle]); this.handle = handle; }
  async clear() { this.log.push(['clear']); this.handle = null; }
}

function storeFor(directory, options = {}) {
  return new ProjectStore({ picker: async config => {
    directory.root.log.push(['picker', config]);
    return directory;
  }, locks: null, crypto: webcrypto, handleStore: new MemoryHandleStore(), ...options });
}

test('allowlisted project validation returns a detached normalized copy', () => {
  const source = project();
  const normalized = validateProject(source);
  assert.deepEqual(normalized, source);
  assert.notEqual(normalized, source);
  assert.notEqual(normalized.assets, source.assets);
  normalized.shots[0].assetIds.push('new-id');
  assert.equal(source.shots[0].assetIds.length, 1);
  assert.deepEqual(JSON.parse(serializeProject(source)), source);
});

test('all supported normal fields and multiple scripts are accepted', () => {
  const value = project();
  value.scripts = [{ id: 'script-1', title: 'Original', content: 'A story', approved: true }];
  value.shots[0].sequence = 1;
  value.shots[0].prompt = 'Soft lighting';
  assert.deepEqual(validateProject(value), value);
  value.assets[0].image = null;
  assert.equal('image' in validateProject(value).assets[0], false);
});

test('credential fields and unknown fields are rejected at every nesting level', () => {
  const mutations = [
    value => { value.apiKey = 'private'; },
    value => { value.settings.token = 'private'; },
    value => { value.script.api_key = 'private'; },
    value => { value.assets[0].authorization = 'private'; },
    value => { value.assets[0].image.secret = 'private'; },
    value => { value.shots[0].credentials = {}; },
    value => { value.extra = { ordinary: 'data' }; },
  ];
  for (const mutate of mutations) {
    const value = project();
    mutate(value);
    assert.throws(() => validateProject(value), isCode('INVALID_PROJECT'));
  }
});

test('credential-like text is never serialized or included in errors', () => {
  const value = project();
  const fakeKey = `sk-${'example'.repeat(6)}`;
  value.script.text = `Use ${fakeKey}`;
  assert.throws(() => serializeProject(value), caught => isCode('INVALID_PROJECT')(caught) && !caught.message.includes(fakeKey));
});

test('prototype pollution, accessors, symbols, exotic objects, and sparse arrays are rejected', () => {
  for (const field of ['__proto__', 'constructor', 'prototype']) {
    const value = JSON.parse(JSON.stringify(project()));
    Object.defineProperty(value.assets[0], field, { value: { polluted: true }, enumerable: true });
    assert.throws(() => validateProject(value), isCode('INVALID_PROJECT'));
  }
  assert.equal({}.polluted, undefined);
  const accessor = project();
  Object.defineProperty(accessor, 'name', { get() { throw new Error('Getter must not execute'); } });
  assert.throws(() => validateProject(accessor), isCode('INVALID_PROJECT'));
  const symbol = project();
  symbol[Symbol('private')] = 'no';
  assert.throws(() => validateProject(symbol), isCode('INVALID_PROJECT'));
  const inherited = Object.assign(Object.create({ polluted: true }), project());
  assert.throws(() => validateProject(inherited), isCode('INVALID_PROJECT'));
  const sparse = project();
  sparse.shots = new Array(3);
  assert.throws(() => validateProject(sparse), isCode('INVALID_PROJECT'));
  const extras = project();
  extras.shots.apiKey = 'private';
  assert.throws(() => validateProject(extras), isCode('INVALID_PROJECT'));
  const circular = project();
  circular.settings = circular;
  assert.throws(() => validateProject(circular), isCode('INVALID_PROJECT'));
});

test('unsupported versions, invalid IDs, duplicate references, and oversized records fail', () => {
  const mutations = [
    value => { value.schemaVersion = 2; },
    value => { value.id = '../.env'; },
    value => { value.name = ''; },
    value => { value.createdAt = 'yesterday'; },
    value => { value.script.text = 'x'.repeat(500001); },
    value => { value.assets.push(value.assets[0]); },
    value => { value.shots[0].assetIds = ['missing']; },
    value => { value.shots[0].assetIds = ['keeper', 'keeper']; },
    value => { value.shots[0].duration = Infinity; },
    value => { value.shots[0].number = -1; },
    value => { value.shots = Array.from({ length: LIMITS.shots + 1 }, (_, index) => ({ id: `s-${index}` })); },
    value => { value.assets[0].type = 'video'; },
  ];
  for (const mutate of mutations) {
    const value = project();
    mutate(value);
    assert.throws(() => validateProject(value), isCode('INVALID_PROJECT'));
  }
});

test('non-image data, external image URLs, MIME confusion, and invalid base64 fail', () => {
  for (const image of [
    { mimeType: 'image/svg+xml', dataUrl: 'data:image/svg+xml;base64,PHN2Zz4=' },
    { mimeType: 'image/png', dataUrl: 'https://untrusted.invalid/image.png' },
    { mimeType: 'image/png', dataUrl: 'data:image/png;base64,c2VjcmV0' },
    { mimeType: 'image/png', dataUrl: 'data:image/png;base64,%%%%' },
    { mimeType: 'image/png', dataUrl: PNG.replace('png', 'jpeg') },
    { mimeType: 'image/jpeg', dataUrl: PNG },
    { mimeType: 'image/png', dataUrl: 'data:image/png;base64,a===' },
    { mimeType: {}, dataUrl: PNG },
    { mimeType: 'image/png', dataUrl: PNG, path: '.env' },
  ]) {
    const value = project();
    value.assets[0].image = image;
    assert.throws(() => validateProject(value), isCode('INVALID_PROJECT'));
  }
});

test('per-image, combined image, and text metadata limits are enforced', () => {
  const tooBig = project();
  tooBig.assets[0].image.dataUrl = `data:image/png;base64,${'A'.repeat(Math.ceil(LIMITS.imageBytes / 3) * 4 + 100)}`;
  assert.throws(() => validateProject(tooBig), isCode('INVALID_PROJECT'));
  const bytes = Buffer.alloc(LIMITS.imageBytes - 3);
  const sample = Buffer.from(PNG.split(',')[1], 'base64');
  sample.copy(bytes);
  sample.subarray(sample.length - 12).copy(bytes, bytes.length - 12);
  const longImage = `data:image/png;base64,${bytes.toString('base64')}`;
  const combined = project();
  combined.shots = [];
  combined.assets = Array.from({ length: 5 }, (_, i) => ({ id: `asset-${i}`, type: 'scene', name: `Scene ${i}`, image: { mimeType: 'image/png', dataUrl: longImage } }));
  assert.throws(() => validateProject(combined), isCode('INVALID_PROJECT'));
  const text = project();
  text.scripts = Array.from({ length: 5 }, (_, i) => ({ id: `script-${i}`, title: 'Long', content: 'x'.repeat(500000), approved: false }));
  assert.throws(() => validateProject(text), isCode('INVALID_PROJECT'));
});

test('portable package roundtrips all project data and raw images', async () => {
  const store = new ProjectStore({ picker: null });
  assert.equal(store.supported, false);
  assert.deepEqual(store.status, { mode: 'package', folderName: null, canSave: false, reconnectNeeded: false, rememberWarning: null });
  const value = project();
  const exported = store.exportPackage(value);
  assert.equal(exported.filename, 'First-film.filmpilot.json');
  assert.equal(exported.blob.type, 'application/json');
  assert.deepEqual(parsePackage(await exported.blob.text()), value);
  const file = new File([exported.blob], exported.filename);
  assert.deepEqual(await store.importPackage(file), value);
  assert.deepEqual(await store.importPackage(serializePackage(value)), value);
  await assert.rejects(store.openFolder(), isCode('UNSUPPORTED'));
  await assert.rejects(store.save(value), isCode('NO_FOLDER'));
});

test('package rejects arbitrary JSON, unsafe metadata, oversized files before reading, and credential filenames', async () => {
  const store = new ProjectStore({ picker: null });
  assert.throws(() => parsePackage('{'), isCode('CORRUPT_PROJECT'));
  assert.throws(() => parsePackage(JSON.stringify(project())), isCode('INVALID_PROJECT'));
  const envelope = JSON.parse(serializePackage(project()));
  envelope.token = 'private';
  assert.throws(() => parsePackage(JSON.stringify(envelope)), isCode('INVALID_PROJECT'));
  for (const name of ['.env', 'config.local.env', 'deepseekapi.txt', 'credentials.json', 'project.json']) {
    await assert.rejects(store.importPackage({ name, size: 1, text() { throw new Error('Must not read this file'); } }), isCode('INVALID_PROJECT'));
  }
  await assert.rejects(store.importPackage({ name: 'large.filmpilot.json', size: LIMITS.packageBytes + 1, text() { throw new Error('Must not read this file'); } }), isCode('INVALID_PROJECT'));
});

test('create saves manifest, project, and only declared binary images; reopen is complete', async () => {
  const folder = new MemoryDirectory();
  folder.seed('.env', 'Never read this');
  folder.seed('deepseekapi.txt', 'Never read this');
  folder.seed('unrelated.json', '{}');
  const store = storeFor(folder);
  assert.deepEqual(await store.createFolder(project()), project());
  assert.deepEqual(store.status, { mode: 'folder', folderName: 'My project', canSave: true, reconnectNeeded: false, rememberWarning: null });
  assert.equal(folder.files.has('manifest.json'), true);
  const disk = JSON.parse(await folder.files.get('project.json').text());
  assert.equal(disk.assets[0].image.dataUrl, undefined);
  assert.match(disk.assets[0].image.path, /^assets\/keeper-[a-f0-9]{64}-[a-f0-9]{32}\.png$/);
  assert.equal(disk.assets[0].image.byteLength, Buffer.from(PNG.split(',')[1], 'base64').length);
  const closes = folder.log.filter(item => item[0] === 'close').map(item => item[1]);
  assert.ok(closes[0].startsWith('assets/'));
  assert.deepEqual(closes.slice(1), ['project.json', 'manifest.json']);
  folder.log.length = 0;
  const reopened = storeFor(folder);
  assert.deepEqual(await reopened.openFolder(), project());
  assert.deepEqual(folder.log.filter(item => item[0] === 'read').map(item => item[1]), ['manifest.json', 'project.json', disk.assets[0].image.path]);
  assert.equal(folder.log.some(item => item[0] === 'requestPermission'), false);
});

test('folder cancellation leaves the current project attachment unchanged', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  await store.createFolder(project());
  store._picker = async () => { throw error('AbortError'); };
  assert.equal(await store.openFolder(), null);
  assert.equal(await store.createFolder(project()), null);
  assert.equal(store.status.folderName, 'My project');
  assert.equal(store.status.canSave, true);
});

test('create refuses existing manifest or project without reading/overwriting it', async () => {
  for (const name of ['manifest.json', 'project.json']) {
    const folder = new MemoryDirectory();
    folder.seed(name, 'Unrelated content');
    const store = storeFor(folder);
    await assert.rejects(store.createFolder(project()), isCode('EXISTING_PROJECT'));
    assert.equal(await folder.files.get(name).text(), 'Unrelated content');
    assert.equal(folder.log.some(item => ['read', 'write'].includes(item[0])), false);
  }
});

test('save reuses immutable image files and commits project.json only after checks', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  await store.createFolder(project());
  folder.log.length = 0;
  const value = project();
  value.script.text = 'Updated story';
  await store.save(value);
  const writes = folder.log.filter(item => item[0] === 'write').map(item => item[1]);
  assert.deepEqual(writes, ['project.json']);
  assert.equal(JSON.parse(await folder.files.get('project.json').text()).script.text, 'Updated story');
});

test('permission revocation causes a safe failure, and only explicit reconnect requests access', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  await store.createFolder(project());
  folder.root.permission = 'prompt';
  folder.log.length = 0;
  await assert.rejects(store.save(project()), isCode('PERMISSION_DENIED'));
  assert.equal(store.status.canSave, false);
  assert.equal(folder.log.some(item => item[0] === 'requestPermission'), false);
  assert.equal(folder.log.some(item => item[0] === 'write'), false);
  await store.reconnect();
  assert.equal(folder.log.filter(item => item[0] === 'requestPermission').length, 1);
  assert.equal(store.status.canSave, true);
  await store.save(project());
  folder.permission = 'denied';
  folder.requestResult = 'denied';
  await assert.rejects(store.reconnect(), isCode('PERMISSION_DENIED'));
  assert.equal(store.status.canSave, false);
});

test('failed writes keep the old project and allow a retry without orphan filename collisions', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  await store.createFolder(project());
  const original = await folder.files.get('project.json').text();
  const value = project();
  value.assets.push({ id: 'lighthouse', type: 'scene', name: 'Lighthouse', image: { mimeType: 'image/png', dataUrl: PNG } });
  folder.failWrite = 'project.json';
  await assert.rejects(store.save(value), isCode('STORAGE_FULL'));
  assert.equal(await folder.files.get('project.json').text(), original);
  assert.ok(folder.log.some(item => item[0] === 'abort' && item[1] === 'project.json'));
  folder.failWrite = null;
  await store.save(value);
  assert.equal(JSON.parse(await folder.files.get('project.json').text()).assets.length, 2);
});

test('failed close keeps the old project and reflects permission loss', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  await store.createFolder(project());
  const original = await folder.files.get('project.json').text();
  folder.failClose = 'project.json';
  const changed = project();
  changed.name = 'Changed';
  await assert.rejects(store.save(changed), isCode('PERMISSION_DENIED'));
  assert.equal(await folder.files.get('project.json').text(), original);
  assert.equal(store.status.canSave, false);
});

test('read-only folder can be opened, but save cannot silently ask for access', async () => {
  const folder = new MemoryDirectory();
  await storeFor(folder).createFolder(project());
  folder.permission = 'denied';
  const readonly = storeFor(folder);
  assert.deepEqual(await readonly.openFolder(), project());
  assert.equal(readonly.status.canSave, false);
  await assert.rejects(readonly.save(project()), isCode('PERMISSION_DENIED'));
  assert.equal(folder.log.some(item => item[0] === 'requestPermission'), false);
});

test('same-store concurrent operations are rejected without duplicate prompts', async () => {
  let resolvePicker;
  let picks = 0;
  const store = new ProjectStore({ picker: () => { picks++; return new Promise(resolve => { resolvePicker = resolve; }); } });
  const pending = store.openFolder();
  await assert.rejects(store.openFolder(), isCode('BUSY'));
  assert.throws(() => store.disconnect(), isCode('BUSY'));
  assert.equal(picks, 1);
  resolvePicker(null);
  assert.equal(await pending, null);
});

test('stale tabs cannot overwrite a newer save; Web Locks serializes writes', async () => {
  const folder = new MemoryDirectory();
  let queue = Promise.resolve();
  const calls = [];
  const locks = { request(name, options, action) {
    calls.push({ name, options });
    const result = queue.then(action);
    queue = result.catch(() => {});
    return result;
  } };
  const first = storeFor(folder, { locks });
  await first.createFolder(project());
  const second = storeFor(folder, { locks });
  await second.openFolder();
  const firstProject = project(); firstProject.name = 'First writer';
  const secondProject = project(); secondProject.name = 'Second writer';
  const results = await Promise.allSettled([first.save(firstProject), second.save(secondProject)]);
  assert.equal(results[0].status, 'fulfilled');
  assert.equal(results[1].status, 'rejected');
  assert.equal(results[1].reason.code, 'CONFLICT');
  assert.equal(JSON.parse(await folder.files.get('project.json').text()).name, 'First writer');
  assert.ok(calls.every(item => item.name === 'filmpilot-local-project-writes' && item.options.mode === 'exclusive'));
});

test('external edits to project or manifest are reported and never overwritten', async () => {
  for (const file of ['project.json', 'manifest.json']) {
    const folder = new MemoryDirectory();
    const store = storeFor(folder);
    await store.createFolder(project());
    folder.seed(file, 'External edits');
    folder.log.length = 0;
    await assert.rejects(store.save(project()), isCode('CONFLICT'));
    assert.equal(await folder.files.get(file).text(), 'External edits');
    assert.equal(folder.log.some(item => item[0] === 'write'), false);
  }
});

test('different projects cannot overwrite an attached folder', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  await store.createFolder(project());
  const other = project(); other.id = 'other-project';
  await assert.rejects(store.save(other), isCode('PROJECT_MISMATCH'));
});

test('corrupt manifests cannot cause credential reads or arbitrary project paths', async () => {
  for (const path of ['.env', 'config.local.env', '../project.json', '/project.json', 'assets/secret.png']) {
    const folder = new MemoryDirectory();
    folder.seed('manifest.json', JSON.stringify({ format: 'filmpilot-project', schemaVersion: 1, projectFile: path }));
    const store = storeFor(folder);
    await assert.rejects(store.openFolder(), isCode('CORRUPT_PROJECT'));
    assert.deepEqual(folder.log.filter(item => item[0] === 'read').map(item => item[1]), ['manifest.json']);
  }
});

test('unsafe image paths and extensions are rejected before opening any assets directory', async () => {
  for (const path of ['../.env', 'assets/../.env', '/etc/passwd', 'assets/secret.svg', 'assets/a.png', 'assets\\secret.png']) {
    const folder = new MemoryDirectory();
    const disk = project();
    disk.assets[0].image = { mimeType: 'image/png', path, byteLength: 10 };
    folder.seed('manifest.json', MANIFEST);
    folder.seed('project.json', JSON.stringify(disk));
    const store = storeFor(folder);
    await assert.rejects(store.openFolder(), isCode('INVALID_PROJECT'));
    assert.equal(folder.log.some(item => item[0] === 'directory'), false);
    assert.deepEqual(folder.log.filter(item => item[0] === 'read').map(item => item[1]), ['manifest.json', 'project.json']);
  }
});

test('missing, modified, and mismatched images cannot be opened or silently repaired by save', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  await store.createFolder(project());
  const disk = JSON.parse(await folder.files.get('project.json').text());
  const assets = folder.directories.get('assets');
  const filename = disk.assets[0].image.path.slice(7);
  const original = Buffer.from(await assets.files.get(filename).arrayBuffer());
  const modified = Buffer.from(original);
  modified[modified.length - 14] ^= 1;
  assets.seed(filename, modified);
  await assert.rejects(storeFor(folder).openFolder(), isCode('CORRUPT_PROJECT'));
  await assert.rejects(store.save(project()), isCode('CORRUPT_PROJECT'));
  assets.files.delete(filename);
  await assert.rejects(storeFor(folder).openFolder(), isCode('CORRUPT_PROJECT'));
});

test('malformed or oversized project data is reported before loading asset bytes', async () => {
  for (const text of ['{', 'x'.repeat(LIMITS.metadataBytes + 128 * 1024 + 1)]) {
    const folder = new MemoryDirectory();
    folder.seed('manifest.json', MANIFEST);
    folder.seed('project.json', text);
    await assert.rejects(storeFor(folder).openFolder(), isCode('CORRUPT_PROJECT'));
    assert.equal(folder.log.some(item => item[0] === 'directory'), false);
  }
});

test('invalid package import preserves the current attachment; valid import detaches it', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  await store.createFolder(project());
  await assert.rejects(store.importPackage('{}'), isCode('INVALID_PROJECT'));
  assert.equal(store.mode, 'folder');
  assert.deepEqual(await store.importPackage(serializePackage(project())), project());
  assert.equal(store.mode, 'package');
  await assert.rejects(store.save(project()), isCode('NO_FOLDER'));
});

test('generic platform error details are not exposed in status messages', async () => {
  const store = new ProjectStore({ picker: async () => { throw error('UnknownError', 'path=/private/sensitive-details'); } });
  await assert.rejects(store.openFolder(), caught => isCode('FILE_ERROR')(caught) && !caught.message.includes('sensitive-details'));
});

test('refresh restores a remembered handle with granted permission, without picker or permission prompt', async () => {
  const folder = new MemoryDirectory();
  const handles = new MemoryHandleStore();
  const first = storeFor(folder, { handleStore: handles });
  await first.createFolder(project());
  assert.equal(handles.log.length, 1);
  assert.equal(handles.log[0][0], 'set');
  assert.equal(handles.log[0][1], folder, 'Only the handle, not a project or credential wrapper, is remembered');
  folder.log.length = 0;
  const refreshed = storeFor(folder, { handleStore: handles, picker: () => { throw new Error('Restore must not open a picker'); } });
  assert.deepEqual(await refreshed.restore(), { project: project(), reconnectNeeded: false });
  assert.equal(refreshed.status.canSave, true);
  assert.deepEqual(folder.log[0], ['queryPermission', 'readwrite']);
  assert.equal(folder.log.some(item => ['requestPermission', 'write', 'picker'].includes(item[0])), false);
  folder.log.length = 0;
  assert.deepEqual(await refreshed.restore(), { project: null, reconnectNeeded: false });
  assert.equal(folder.log.length, 0, 'A second restore never rebases an active project');
});

test('refresh with prompt or revoked permission offers reconnect without any file reads', async () => {
  for (const permission of ['prompt', 'denied']) {
    const folder = new MemoryDirectory();
    const handles = new MemoryHandleStore();
    await storeFor(folder, { handleStore: handles }).createFolder(project());
    folder.permission = permission;
    folder.log.length = 0;
    const refreshed = storeFor(folder, { handleStore: handles });
    assert.deepEqual(await refreshed.restore(), { project: null, reconnectNeeded: true });
    assert.deepEqual(folder.log, [['queryPermission', 'readwrite']]);
    assert.equal(refreshed.status.reconnectNeeded, true);
    assert.equal(refreshed.status.canSave, false);
    assert.equal(refreshed.status.folderName, 'My project');
    assert.deepEqual(await refreshed.reconnect(), { project: project(), reconnectNeeded: false });
    assert.equal(folder.log.filter(item => item[0] === 'requestPermission').length, 1);
    assert.equal(refreshed.status.canSave, true);
  }
});

test('declining reconnect keeps the pending remembered folder and does not read project files', async () => {
  const folder = new MemoryDirectory();
  const handles = new MemoryHandleStore();
  await storeFor(folder, { handleStore: handles }).createFolder(project());
  folder.permission = 'prompt';
  folder.requestResult = 'denied';
  const refreshed = storeFor(folder, { handleStore: handles });
  await refreshed.restore();
  folder.log.length = 0;
  await assert.rejects(refreshed.reconnect(), isCode('PERMISSION_DENIED'));
  assert.deepEqual(folder.log, [['queryPermission', 'readwrite'], ['requestPermission', 'readwrite']]);
  assert.equal(refreshed.status.reconnectNeeded, true);
  assert.equal(refreshed.status.canSave, false);
});

test('reconnect of an active folder preserves dirty edits and the original conflict baseline', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  await store.createFolder(project());
  const dirty = project(); dirty.script.text = 'My unsaved work';
  const external = JSON.parse(await folder.files.get('project.json').text());
  external.name = 'Someone else changed this';
  folder.seed('project.json', JSON.stringify(external));
  folder.permission = 'prompt';
  folder.log.length = 0;
  assert.deepEqual(await store.reconnect(), { project: null, reconnectNeeded: false });
  assert.equal(folder.log.some(item => item[0] === 'read'), false);
  assert.equal(dirty.script.text, 'My unsaved work');
  await assert.rejects(store.save(dirty), isCode('CONFLICT'));
  assert.equal(JSON.parse(await folder.files.get('project.json').text()).name, external.name);
});

test('refresh missing or corrupt files reports failure without overwriting files or reading credentials', async () => {
  for (const corruption of ['missing-project', 'bad-manifest', 'bad-project']) {
    const folder = new MemoryDirectory();
    const handles = new MemoryHandleStore();
    await storeFor(folder, { handleStore: handles }).createFolder(project());
    folder.seed('.env', 'Must not read');
    if (corruption === 'missing-project') folder.files.delete('project.json');
    if (corruption === 'bad-manifest') folder.seed('manifest.json', 'Invalid');
    if (corruption === 'bad-project') folder.seed('project.json', '{');
    folder.log.length = 0;
    const refreshed = storeFor(folder, { handleStore: handles });
    await assert.rejects(refreshed.restore(), isCode('CORRUPT_PROJECT'));
    assert.equal(refreshed.status.canSave, false);
    assert.equal(refreshed.status.reconnectNeeded, true);
    assert.equal(folder.log.some(item => ['write', 'requestPermission'].includes(item[0])), false);
    assert.equal(folder.log.some(item => item[1] === '.env'), false);
  }
});

test('unavailable or failing IndexedDB does not block local save or package fallback', async () => {
  assert.equal(createDirectoryHandleStore(null), null);
  assert.equal(createDirectoryHandleStore({}), null);
  const folder = new MemoryDirectory();
  const withoutIDB = storeFor(folder, { handleStore: null });
  assert.deepEqual(await withoutIDB.restore(), { project: null, reconnectNeeded: false });
  await withoutIDB.createFolder(project());
  assert.match(withoutIDB.status.rememberWarning, /cannot remember/);
  assert.equal(withoutIDB.status.canSave, true);
  const failures = { get: async () => { throw error('SecurityError'); }, set: async () => { throw error('DataCloneError'); }, clear: async () => { throw error('SecurityError'); } };
  const other = new MemoryDirectory('Other');
  const failedIDB = storeFor(other, { handleStore: failures });
  assert.deepEqual(await failedIDB.restore(), { project: null, reconnectNeeded: false });
  assert.match(failedIDB.status.rememberWarning, /could not restore/);
  await failedIDB.createFolder(project());
  assert.match(failedIDB.status.rememberWarning, /could not remember/);
  const saved = JSON.parse(await other.files.get('project.json').text());
  assert.equal(saved.name, project().name);
  assert.deepEqual(parsePackage(await failedIDB.exportPackage(project()).blob.text()), project());
  await failedIDB.disconnect();
  assert.match(failedIDB.status.rememberWarning, /could not forget/);
});

test('disconnect and successful package import forget remembered folders, without writing project data to IDB', async () => {
  const folder = new MemoryDirectory();
  const handles = new MemoryHandleStore();
  const store = storeFor(folder, { handleStore: handles });
  await store.createFolder(project());
  await store.disconnect();
  assert.equal(handles.handle, null);
  assert.equal(store.mode, 'package');
  await store.openFolder();
  assert.equal(handles.handle, folder);
  await assert.rejects(store.importPackage('{}'), isCode('INVALID_PROJECT'));
  assert.equal(handles.handle, folder);
  await store.importPackage(serializePackage(project()));
  assert.equal(handles.handle, null);
  assert.deepEqual(handles.log.map(item => item[0]), ['set', 'clear', 'set', 'clear']);
  assert.ok(handles.log.filter(item => item[0] === 'set').every(item => item.length === 2 && item[1] === folder));
});

test('pending remembered folder is cleared when the user imports another project', async () => {
  const folder = new MemoryDirectory();
  const handles = new MemoryHandleStore();
  await storeFor(folder, { handleStore: handles }).createFolder(project());
  folder.permission = 'prompt';
  const refreshed = storeFor(folder, { handleStore: handles });
  await refreshed.restore();
  await refreshed.importPackage(serializePackage(project()));
  assert.equal(refreshed.status.reconnectNeeded, false);
  assert.equal(handles.handle, null);
  await assert.rejects(refreshed.reconnect(), isCode('NO_FOLDER'));
});

test('corrupt saved handles and handles without queryPermission are never read automatically', async () => {
  const invalid = new MemoryHandleStore({ kind: 'file', name: '.env' });
  const store = new ProjectStore({ handleStore: invalid, picker: null });
  assert.deepEqual(await store.restore(), { project: null, reconnectNeeded: false });
  assert.match(store.status.rememberWarning, /unavailable/);
  const folder = new MemoryDirectory();
  folder.queryPermission = undefined;
  const withoutQuery = storeFor(folder, { handleStore: new MemoryHandleStore(folder) });
  assert.deepEqual(await withoutQuery.restore(), { project: null, reconnectNeeded: true });
  assert.equal(folder.log.length, 0);
});

test('interrupted initial project or manifest close can safely retry in the same session', async () => {
  for (const failedFile of ['project.json', 'manifest.json']) {
    const folder = new MemoryDirectory();
    const handles = new MemoryHandleStore();
    const store = storeFor(folder, { handleStore: handles });
    folder.failClose = failedFile;
    await assert.rejects(store.createFolder(project()), isCode('PERMISSION_DENIED'));
    assert.equal(handles.handle, null, 'An incomplete folder is never remembered');
    folder.failClose = null;
    assert.deepEqual(await store.createFolder(project()), project());
    assert.deepEqual(await storeFor(folder).openFolder(), project());
    assert.equal(handles.handle, folder);
  }
});

test('interrupted creation never overwrites an externally changed partial project', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  folder.failClose = 'manifest.json';
  await assert.rejects(store.createFolder(project()), isCode('PERMISSION_DENIED'));
  folder.failClose = null;
  folder.seed('project.json', 'External data');
  folder.log.length = 0;
  await assert.rejects(store.createFolder(project()), isCode('CONFLICT'));
  assert.equal(await folder.files.get('project.json').text(), 'External data');
  assert.equal(folder.log.some(item => item[0] === 'write'), false);
});

test('interrupted creation retry preserves its prior completed project even after another failed changed save', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  folder.failClose = 'manifest.json';
  await assert.rejects(store.createFolder(project()), isCode('PERMISSION_DENIED'));
  const firstSource = await folder.files.get('project.json').text();
  folder.failClose = 'project.json';
  const changed = project(); changed.name = 'Revised after interruption';
  await assert.rejects(store.createFolder(changed), isCode('PERMISSION_DENIED'));
  assert.equal(await folder.files.get('project.json').text(), firstSource);
  folder.failClose = null;
  await store.createFolder(changed);
  assert.deepEqual(await storeFor(folder).openFolder(), changed);
});

test('an externally emptied completed project is not treated as an interrupted empty new file', async () => {
  const folder = new MemoryDirectory();
  const store = storeFor(folder);
  folder.failClose = 'manifest.json';
  await assert.rejects(store.createFolder(project()), isCode('PERMISSION_DENIED'));
  folder.failClose = null;
  folder.seed('project.json', '');
  await assert.rejects(store.createFolder(project()), isCode('CONFLICT'));
});
