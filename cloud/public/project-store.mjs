/**
 * Local-only project persistence. Call folder pickers/reconnect directly from a
 * user click. No folder enumeration, credential storage, or network requests.
 *
 * Memory/package image: { mimeType, dataUrl }.
 * Folder image: { mimeType, path: 'assets/<id>-<sha256>-<nonce>.<ext>', byteLength }.
 * project.json is the commit point; immutable image files are written first.
 * Old image files are intentionally not deleted, allowing safe interrupted saves.
 */

export const LIMITS = Object.freeze({
  imageBytes: 8 * 1024 * 1024,
  totalImageBytes: 32 * 1024 * 1024,
  metadataBytes: 2 * 1024 * 1024,
  packageBytes: 50 * 1024 * 1024,
  assets: 100,
  scripts: 100,
  shots: 500,
});

const MIME_EXT = Object.freeze({ 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp' });
const MANIFEST = Object.freeze({ format: 'filmpilot-project', schemaVersion: 1, projectFile: 'project.json' });
const MANIFEST_TEXT = JSON.stringify(MANIFEST, null, 2) + '\n';
const encoder = new TextEncoder();
const SECRET_VALUE = /(?:\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:api[_ -]?key|access[_ -]?token)\s*[:=]\s*["']?[A-Za-z0-9_-]{16,})/i;

export class ProjectStoreError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ProjectStoreError';
    this.code = code;
  }
}

function fail(code, message) { throw new ProjectStoreError(code, message); }
function invalid(message = 'The project contains an unsupported or invalid field.') { fail('INVALID_PROJECT', message); }
function sizeOf(value) { return encoder.encode(value).byteLength; }
function own(value, key) { return Object.hasOwn(value, key); }

function record(value, allowed, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      ![Object.prototype, null].includes(Object.getPrototypeOf(value))) invalid(`${label} must be a plain object.`);
  const descriptors = Object.getOwnPropertyDescriptors(value);
  for (const key of Reflect.ownKeys(descriptors)) {
    if (typeof key !== 'string' || !allowed.includes(key) || !('value' in descriptors[key])) {
      invalid(`${label} contains an unsupported field. Credentials cannot be saved in projects.`);
    }
  }
  return value;
}

function string(value, label, max = 10000, { required = false } = {}) {
  if (typeof value !== 'string' || value.length > max || (required && !value.trim()) || value.includes('\0')) {
    invalid(`${label} must be text within the supported length.`);
  }
  if (SECRET_VALUE.test(value)) invalid('Remove credential-like text before saving or importing this project.');
  return value;
}

function id(value, label) {
  string(value, label, 100, { required: true });
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/.test(value)) invalid(`${label} has an invalid identifier.`);
  return value;
}

function number(value, label, min, max, integer = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value))) {
    invalid(`${label} is outside the supported range.`);
  }
  return value;
}

function array(value, label, max, map) {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype || value.length > max) {
    invalid(`${label} has too many entries or is not an array.`);
  }
  const descriptors = Object.getOwnPropertyDescriptors(value);
  for (const key of Reflect.ownKeys(descriptors)) {
    if (typeof key !== 'string' || (key !== 'length' && (!/^(0|[1-9][0-9]*)$/.test(key) || Number(key) >= value.length)) ||
        !('value' in descriptors[key])) invalid(`${label} contains an unsupported field.`);
  }
  return Array.from({ length: value.length }, (_, i) => {
    if (!own(descriptors, String(i))) invalid(`${label} must not contain empty entries.`);
    return map(descriptors[i].value, i);
  });
}

function copyText(source, target, key, max = 10000) {
  if (own(source, key)) target[key] = string(source[key], key, max);
}

function timestamp(value, label) {
  string(value, label, 40, { required: true });
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value) || !Number.isFinite(Date.parse(value))) {
    invalid(`${label} must be a UTC date and time.`);
  }
  return value;
}

function checkImageBytes(bytes, mimeType) {
  const match = mimeType === 'image/png'
    ? bytes.length >= 45 && [137, 80, 78, 71, 13, 10, 26, 10].every((n, i) => bytes[i] === n) &&
      [0, 0, 0, 13, 73, 72, 68, 82].every((n, i) => bytes[i + 8] === n) &&
      [0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130].every((n, i) => bytes[bytes.length - 12 + i] === n)
    : mimeType === 'image/jpeg'
      ? bytes.length >= 12 && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 && bytes.at(-2) === 255 && bytes.at(-1) === 217
      : bytes.length >= 20 && [82, 73, 70, 70].every((n, i) => bytes[i] === n) && [87, 69, 66, 80].every((n, i) => bytes[i + 8] === n) &&
        new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(4, true) === bytes.length - 8 &&
        ['VP8 ', 'VP8L', 'VP8X'].includes(String.fromCharCode(...bytes.subarray(12, 16)));
  if (!match) invalid('An image file does not match its declared PNG, JPEG, or WebP format.');
}

function decodeDataUrl(dataUrl, mimeType) {
  if (typeof dataUrl !== 'string' || dataUrl.length > Math.ceil(LIMITS.imageBytes / 3) * 4 + 64) {
    invalid('An image exceeds the 8 MiB limit.');
  }
  const prefix = `data:${mimeType};base64,`;
  if (!dataUrl.startsWith(prefix)) invalid('Images must be embedded PNG, JPEG, or WebP data URLs.');
  const encoded = dataUrl.slice(prefix.length);
  if (!encoded.length || encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) invalid('An image has invalid base64 data.');
  let binary;
  try { binary = atob(encoded); } catch { invalid('An image has invalid base64 data.'); }
  if (!binary.length || binary.length > LIMITS.imageBytes || btoa(binary) !== encoded) invalid('An image has invalid or oversized data.');
  const bytes = Uint8Array.from(binary, char => char.charCodeAt(0));
  checkImageBytes(bytes, mimeType);
  return bytes;
}

function dataUrl(bytes, mimeType) {
  let binary = '';
  for (let start = 0; start < bytes.length; start += 0x8000) binary += String.fromCharCode(...bytes.subarray(start, start + 0x8000));
  return `data:${mimeType};base64,${btoa(binary)}`;
}

function image(value, assetId, mode, budget) {
  record(value, mode === 'disk' ? ['mimeType', 'path', 'byteLength'] : ['mimeType', 'dataUrl'], 'Image');
  if (typeof value.mimeType !== 'string' || !own(MIME_EXT, value.mimeType)) invalid('Only PNG, JPEG, and WebP images are supported.');
  let result;
  if (mode === 'disk') {
    const path = string(value.path, 'Image path', 260, { required: true });
    if (!imagePathHash(path, assetId, value.mimeType)) invalid('An image has an unsafe path or mismatched extension.');
    const byteLength = number(value.byteLength, 'Image byte length', 1, LIMITS.imageBytes, true);
    budget.imageBytes += byteLength;
    result = { mimeType: value.mimeType, path, byteLength };
  } else {
    const bytes = decodeDataUrl(value.dataUrl, value.mimeType);
    budget.imageBytes += bytes.byteLength;
    result = { mimeType: value.mimeType, dataUrl: value.dataUrl };
  }
  if (budget.imageBytes > LIMITS.totalImageBytes) invalid('The project exceeds the 32 MiB total image limit.');
  return result;
}

function imagePathHash(path, assetId, mimeType) {
  return new RegExp(`^assets/${assetId}-([a-f0-9]{64})-[a-f0-9]{32}\\.${MIME_EXT[mimeType]}$`).exec(path)?.[1];
}

function normalizeProject(input, mode = 'memory') {
  record(input, ['schemaVersion', 'id', 'name', 'description', 'createdAt', 'updatedAt', 'settings', 'script', 'scripts', 'assets', 'shots'], 'Project');
  if (input.schemaVersion !== 1) invalid('This project version is not supported.');
  const output = { schemaVersion: 1, id: id(input.id, 'Project ID'), name: string(input.name, 'Project name', 200, { required: true }) };
  copyText(input, output, 'description', 20000);
  for (const key of ['createdAt', 'updatedAt']) if (own(input, key)) output[key] = timestamp(input[key], key);
  if (own(input, 'settings')) {
    record(input.settings, ['visualStyle', 'aspectRatio', 'language'], 'Settings');
    output.settings = {};
    for (const key of ['visualStyle', 'aspectRatio', 'language']) copyText(input.settings, output.settings, key, key === 'visualStyle' ? 2000 : 100);
  }
  if (own(input, 'script')) {
    record(input.script, ['text', 'status'], 'Script');
    if (!['draft', 'approved'].includes(input.script.status)) invalid('Script status is invalid.');
    output.script = { text: string(input.script.text, 'Script text', 500000), status: input.script.status };
  }
  if (own(input, 'scripts')) {
    output.scripts = array(input.scripts, 'Scripts', LIMITS.scripts, item => {
      record(item, ['id', 'title', 'content', 'approved'], 'Script');
      if (typeof item.approved !== 'boolean') invalid('Script approval must be true or false.');
      return { id: id(item.id, 'Script ID'), title: string(item.title, 'Script title', 200), content: string(item.content, 'Script content', 500000), approved: item.approved };
    });
    unique(output.scripts, 'Scripts');
  }
  const budget = { imageBytes: 0 };
  output.assets = array(input.assets, 'Assets', LIMITS.assets, item => {
    record(item, ['id', 'type', 'name', 'description', 'prompt', 'image'], 'Asset');
    if (!['character', 'scene', 'prop'].includes(item.type)) invalid('Asset type must be character, scene, or prop.');
    const result = { id: id(item.id, 'Asset ID'), type: item.type, name: string(item.name, 'Asset name', 200, { required: true }) };
    copyText(item, result, 'description', 20000);
    copyText(item, result, 'prompt', 20000);
    if (own(item, 'image') && item.image !== null) result.image = image(item.image, result.id, mode, budget);
    return result;
  });
  unique(output.assets, 'Assets');
  const assetIds = new Set(output.assets.map(item => item.id));
  output.shots = array(input.shots, 'Shots', LIMITS.shots, item => {
    record(item, ['id', 'number', 'sequence', 'duration', 'description', 'dialogue', 'assetIds', 'prompt', 'initialFramePrompt', 'storyboardPrompt'], 'Shot');
    const result = { id: id(item.id, 'Shot ID') };
    for (const key of ['number', 'sequence']) if (own(item, key)) result[key] = number(item[key], key, 1, 100000, true);
    if (own(item, 'duration')) result.duration = number(item.duration, 'Shot duration', 0, 3600);
    for (const key of ['description', 'dialogue', 'prompt', 'initialFramePrompt', 'storyboardPrompt']) copyText(item, result, key, 20000);
    if (own(item, 'assetIds')) {
      result.assetIds = array(item.assetIds, 'Shot asset references', LIMITS.assets, value => id(value, 'Referenced asset ID'));
      if (new Set(result.assetIds).size !== result.assetIds.length || result.assetIds.some(value => !assetIds.has(value))) invalid('A shot refers to a missing or duplicate asset.');
    }
    return result;
  });
  unique(output.shots, 'Shots');
  const metadata = { ...output, assets: output.assets.map(item => ({ ...item, ...(item.image ? { image: { mimeType: item.image.mimeType } } : {}) })) };
  if (sizeOf(JSON.stringify(metadata)) > LIMITS.metadataBytes) invalid('Project text and metadata exceed the 2 MiB limit.');
  return output;
}

function unique(items, label) {
  if (new Set(items.map(item => item.id)).size !== items.length) invalid(`${label} must have unique IDs.`);
}

/** Validate and clone only explicitly supported, non-credential project fields. */
export function validateProject(project) { return normalizeProject(project); }

/** Stable JSON representation of the in-memory project (including image data). */
export function serializeProject(project) { return JSON.stringify(validateProject(project), null, 2) + '\n'; }

/** Self-contained, bounded portable package. Does not access the filesystem. */
export function serializePackage(project) {
  const text = JSON.stringify({ format: 'filmpilot-package', schemaVersion: 1, project: validateProject(project) }, null, 2) + '\n';
  if (sizeOf(text) > LIMITS.packageBytes) invalid('The project package exceeds the 50 MiB limit.');
  return text;
}

export function parsePackage(text) {
  if (typeof text !== 'string' || text.length > LIMITS.packageBytes || sizeOf(text) > LIMITS.packageBytes) invalid('The project package exceeds the 50 MiB limit.');
  const payload = parseJson(text, 'The project package is not valid JSON.');
  record(payload, ['format', 'schemaVersion', 'project'], 'Package');
  if (payload.format !== 'filmpilot-package' || payload.schemaVersion !== 1) invalid('This is not a supported FilmPilot project package.');
  return validateProject(payload.project);
}

function parseJson(text, message) {
  try { return JSON.parse(text); } catch { fail('CORRUPT_PROJECT', message); }
}

function mapError(error, fallback = 'The project could not be saved. Your last completed save has been kept.') {
  if (error instanceof ProjectStoreError) return error;
  if (['NotAllowedError', 'SecurityError'].includes(error?.name)) return new ProjectStoreError('PERMISSION_DENIED', 'Folder access is unavailable. Reconnect or choose the folder again from a button click.');
  if (error?.name === 'AbortError') return new ProjectStoreError('ABORTED', 'The file operation was cancelled. Your last completed save has been kept.');
  if (error?.name === 'QuotaExceededError') return new ProjectStoreError('STORAGE_FULL', 'There is not enough space to save this project. Your last completed save has been kept.');
  if (error?.name === 'NotFoundError') return new ProjectStoreError('CORRUPT_PROJECT', 'A required project file or image is missing. The project was not overwritten.');
  return new ProjectStoreError('FILE_ERROR', fallback);
}

async function fileExists(directory, name) {
  try { await directory.getFileHandle(name); return true; } catch (error) { if (error?.name === 'NotFoundError') return false; throw error; }
}

async function readFile(directory, name, maxBytes) {
  const handle = await directory.getFileHandle(name);
  const file = await handle.getFile();
  if (!Number.isFinite(file.size) || file.size < 0 || file.size > maxBytes) fail('CORRUPT_PROJECT', 'A project file exceeds its supported size.');
  return file;
}

async function readText(directory, name, maxBytes) {
  const file = await readFile(directory, name, maxBytes);
  const text = await file.text();
  if (sizeOf(text) > maxBytes) fail('CORRUPT_PROJECT', 'A project file exceeds its supported size.');
  return text;
}

async function writeFile(directory, name, contents) {
  const handle = await directory.getFileHandle(name, { create: true });
  let writable;
  try {
    writable = await handle.createWritable();
    await writable.write(contents);
    await writable.close();
  } catch (error) {
    try { await writable?.abort(); } catch { /* Keep original error; never expose platform error contents. */ }
    throw error;
  }
}

function validateManifest(text) {
  const value = parseJson(text, 'The folder manifest is damaged. Open a valid project or import a package.');
  record(value, ['format', 'schemaVersion', 'projectFile'], 'Manifest');
  if (value.format !== MANIFEST.format || value.schemaVersion !== 1 || value.projectFile !== 'project.json') {
    fail('CORRUPT_PROJECT', 'The folder does not contain a supported FilmPilot manifest.');
  }
}

async function sha256(bytes, crypto) {
  if (!crypto?.subtle) fail('UNSUPPORTED', 'Image folder saves require a secure browser connection. Export a project package instead.');
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), byte => byte.toString(16).padStart(2, '0')).join('');
}

/** IndexedDB contains one directory handle only, never project data or keys. */
export function createDirectoryHandleStore(indexedDB) {
  let provider;
  try { provider = indexedDB === undefined ? globalThis.indexedDB : indexedDB; } catch { return null; }
  if (!provider || typeof provider.open !== 'function') return null;
  const database = () => new Promise((resolve, reject) => {
    const request = provider.open('filmpilot-folder-handle', 1);
    let settled = false;
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains('handles')) request.result.createObjectStore('handles');
    };
    request.onsuccess = () => {
      if (settled) { request.result.close(); return; }
      settled = true;
      resolve(request.result);
    };
    request.onerror = request.onblocked = () => { settled = true; reject(new Error('Folder handle storage is unavailable.')); };
  });
  const operation = async (mode, action) => {
    const db = await database();
    try {
      return await new Promise((resolve, reject) => {
        const transaction = db.transaction('handles', mode);
        let request;
        transaction.oncomplete = () => resolve(request?.result);
        transaction.onerror = transaction.onabort = () => reject(new Error('Folder handle storage is unavailable.'));
        request = action(transaction.objectStore('handles'));
      });
    } finally { db.close(); }
  };
  return {
    get: () => operation('readonly', store => store.get('project-directory')),
    set: handle => operation('readwrite', store => store.put(handle, 'project-directory')),
    clear: () => operation('readwrite', store => store.delete('project-directory')),
  };
}

/**
 * Dependencies can be injected for tests. restore() is safe on page load: it
 * only queries existing permissions. Pickers/reconnect require a user click.
 */
export class ProjectStore {
  constructor({ picker = globalThis.showDirectoryPicker?.bind(globalThis), locks = globalThis.navigator?.locks, crypto = globalThis.crypto, handleStore = createDirectoryHandleStore() } = {}) {
    this._picker = picker;
    this._locks = locks;
    this._crypto = crypto;
    this._directory = null;
    this._pendingDirectory = null;
    this._source = null;
    this._manifest = null;
    this._diskProject = null;
    this._permission = false;
    this._busy = false;
    this._handleStore = handleStore;
    this._rememberQueue = Promise.resolve();
    this._rememberWarning = null;
    this._pendingCreation = null;
  }

  get supported() { return typeof this._picker === 'function'; }
  get mode() { return this._directory || this._pendingDirectory ? 'folder' : 'package'; }
  get status() {
    return {
      mode: this.mode,
      folderName: this._directory?.name ?? this._pendingDirectory?.name ?? null,
      canSave: Boolean(this._directory && this._permission),
      reconnectNeeded: Boolean(this._pendingDirectory || this._directory && !this._permission),
      rememberWarning: this._rememberWarning,
    };
  }

  async _remember(directory) {
    if (!this._handleStore) {
      this._rememberWarning = 'This browser cannot remember folder access. Your files are saved; open the folder again after refreshing.';
      return;
    }
    this._rememberQueue = this._rememberQueue.catch(() => {}).then(() => this._handleStore.set(directory));
    try { await this._rememberQueue; this._rememberWarning = null; }
    catch { this._rememberWarning = 'The browser could not remember folder access. Your files are saved; open the folder again after refreshing.'; }
  }

  async _forget() {
    if (!this._handleStore) return;
    this._rememberQueue = this._rememberQueue.catch(() => {}).then(() => this._handleStore.clear());
    try { await this._rememberQueue; this._rememberWarning = null; }
    catch { this._rememberWarning = 'The browser could not forget the previous folder. It may be offered again after refreshing.'; }
  }

  disconnect() {
    if (this._busy) fail('BUSY', 'Wait for the current folder operation to finish.');
    this._directory = null;
    this._pendingDirectory = null;
    this._source = null;
    this._manifest = null;
    this._diskProject = null;
    this._permission = false;
    return this._forget();
  }

  async chooseFolder({ mode = 'open', project } = {}) {
    if (!['open', 'create'].includes(mode)) invalid('Choose an open or create folder operation.');
    return mode === 'create' ? this.createFolder(project) : this.openFolder();
  }

  async _operation(action) {
    if (this._busy) fail('BUSY', 'Wait for the current folder operation to finish.');
    this._busy = true;
    try { return await action(); } catch (error) {
      const safeError = mapError(error);
      if (safeError.code === 'PERMISSION_DENIED') this._permission = false;
      throw safeError;
    } finally { this._busy = false; }
  }

  async _pick() {
    if (!this.supported) fail('UNSUPPORTED', 'This browser does not support local project folders. Import or export a project package instead.');
    try { return await this._picker({ id: 'filmpilot-project', mode: 'readwrite' }); }
    catch (error) { if (error?.name === 'AbortError') return null; throw error; }
  }

  async _hasPermission(directory, mode = 'readwrite') {
    // A picker-granted handle may omit the optional Permissions API in older implementations.
    return typeof directory.queryPermission !== 'function' || await directory.queryPermission({ mode }) === 'granted';
  }

  async _lock(action) {
    // Serialize all FilmPilot folder writes on this origin. The second source
    // comparison still catches out-of-app edits and browsers without Web Locks.
    return this._locks?.request ? this._locks.request('filmpilot-local-project-writes', { mode: 'exclusive' }, action) : action();
  }

  async openFolder() {
    return this._operation(async () => {
      const directory = await this._pick();
      if (!directory) return null;
      if (!await this._hasPermission(directory, 'read')) fail('PERMISSION_DENIED', 'Read access to the selected folder was not granted.');
      const loaded = await this._readFolder(directory);
      const writable = await this._hasPermission(directory);
      this._directory = directory;
      this._pendingDirectory = null;
      this._manifest = loaded.manifest;
      this._source = loaded.source;
      this._diskProject = loaded.disk;
      this._permission = writable;
      await this._remember(directory);
      return loaded.project;
    });
  }

  async _readFolder(directory) {
    const manifest = await readText(directory, 'manifest.json', 4096);
    validateManifest(manifest);
    const source = await readText(directory, 'project.json', LIMITS.metadataBytes + 128 * 1024);
    const disk = normalizeProject(parseJson(source, 'The project file is damaged. Recover a saved package instead of overwriting it.'), 'disk');
    const project = { ...disk, assets: [] };
    let assetsDirectory;
    for (const asset of disk.assets) {
      if (!asset.image) { project.assets.push(asset); continue; }
      assetsDirectory ??= await directory.getDirectoryHandle('assets');
      const file = await readFile(assetsDirectory, asset.image.path.slice('assets/'.length), LIMITS.imageBytes);
      if (file.size !== asset.image.byteLength) fail('CORRUPT_PROJECT', 'A project image is missing or damaged. The folder was not changed.');
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (bytes.byteLength !== asset.image.byteLength) fail('CORRUPT_PROJECT', 'A project image is missing or damaged. The folder was not changed.');
      checkImageBytes(bytes, asset.image.mimeType);
      const hash = await sha256(bytes, this._crypto);
      if (imagePathHash(asset.image.path, asset.id, asset.image.mimeType) !== hash) fail('CORRUPT_PROJECT', 'A project image has changed outside FilmPilot. The folder was not changed.');
      project.assets.push({ ...asset, image: { mimeType: asset.image.mimeType, dataUrl: dataUrl(bytes, asset.image.mimeType) } });
    }
    return { project: validateProject(project), disk, source, manifest };
  }

  async createFolder(project) {
    // Validate before asking for a folder. A failed picker leaves the current project attached.
    const normalized = validateProject(project);
    return this._operation(async () => {
      const directory = await this._pick();
      if (!directory) return null;
      if (!await this._hasPermission(directory)) fail('PERMISSION_DENIED', 'Write access to the selected folder was not granted.');
      return this._lock(async () => {
        let pending = this._pendingCreation;
        const resuming = pending && (pending.directory === directory ||
          typeof pending.directory.isSameEntry === 'function' && await pending.directory.isSameEntry(directory));
        if (resuming) {
          if (pending.projectId !== normalized.id) fail('PROJECT_MISMATCH', 'The interrupted save belongs to another project. Choose a different folder.');
          await this._checkPendingCreation(directory, pending);
        } else {
          if (await fileExists(directory, 'manifest.json') || await fileExists(directory, 'project.json')) {
            fail('EXISTING_PROJECT', 'This folder already has a manifest or project file. Open it, or choose a different folder.');
          }
          pending = { directory, projectId: normalized.id, source: null, projectStarted: false, manifestStarted: false };
          this._pendingCreation = pending;
        }
        const saved = await this._writeProject(directory, normalized, async source => {
          await this._checkPendingCreation(directory, pending);
          pending.source = source;
          pending.projectStarted = true;
        });
        // The manifest marks a completed initial save. An interrupted initial save
        // can resume only in this session, after matching our own exact writes.
        pending.manifestStarted = true;
        await writeFile(directory, 'manifest.json', MANIFEST_TEXT);
        this._directory = directory;
        this._pendingDirectory = null;
        this._manifest = MANIFEST_TEXT;
        this._source = saved.source;
        this._diskProject = saved.disk;
        this._permission = true;
        this._pendingCreation = null;
        await this._remember(directory);
        return normalized;
      });
    });
  }

  async _checkPendingCreation(directory, pending) {
    for (const [name, started, expected] of [
      ['project.json', pending.projectStarted, pending.source],
      ['manifest.json', pending.manifestStarted, MANIFEST_TEXT],
    ]) {
      if (!await fileExists(directory, name)) continue;
      if (!started) fail('CONFLICT', 'A project file appeared during the save. Choose a different folder to keep both versions.');
      const source = await readText(directory, name, name === 'manifest.json' ? 4096 : LIMITS.metadataBytes + 128 * 1024);
      if (source !== '' && source !== expected) fail('CONFLICT', 'The interrupted save was changed outside FilmPilot. It was not overwritten.');
    }
  }

  async save(project) {
    const normalized = validateProject(project);
    return this._operation(async () => {
      if (!this._directory) fail('NO_FOLDER', 'Choose a project folder before saving, or export a project package.');
      this._permission = await this._hasPermission(this._directory);
      if (!this._permission) fail('PERMISSION_DENIED', 'Folder permission was removed. Reconnect or open the folder again, then save.');
      return this._lock(async () => {
        await this._checkUnchanged();
        if (normalized.id !== this._diskProject.id) fail('PROJECT_MISMATCH', 'Save a different project into its own folder.');
        const saved = await this._writeProject(this._directory, normalized, () => this._checkUnchanged());
        this._source = saved.source;
        this._diskProject = saved.disk;
        return normalized;
      });
    });
  }

  async _checkUnchanged() {
    const manifest = await readText(this._directory, 'manifest.json', 4096);
    const source = await readText(this._directory, 'project.json', LIMITS.metadataBytes + 128 * 1024);
    if (manifest !== this._manifest || source !== this._source) {
      fail('CONFLICT', 'This project changed in another tab or app. Export your unsaved work, then reopen the folder to review the changes.');
    }
  }

  async _writeProject(directory, project, beforeCommit) {
    const disk = { ...project, assets: [] };
    let assetsDirectory;
    const previous = this._directory === directory ? this._diskProject : null;
    for (const asset of project.assets) {
      if (!asset.image) { disk.assets.push(asset); continue; }
      const bytes = decodeDataUrl(asset.image.dataUrl, asset.image.mimeType);
      const hash = await sha256(bytes, this._crypto);
      const reusable = previous?.assets.find(item => item.id === asset.id && item.image?.mimeType === asset.image.mimeType &&
        imagePathHash(item.image.path, asset.id, asset.image.mimeType) === hash);
      const nonce = reusable ? '' : Array.from(this._crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
      const name = reusable ? reusable.image.path.slice('assets/'.length) : `${asset.id}-${hash}-${nonce}.${MIME_EXT[asset.image.mimeType]}`;
      const path = `assets/${name}`;
      assetsDirectory ??= await directory.getDirectoryHandle('assets', { create: true });
      const declared = Boolean(reusable);
      if (declared) {
        // Only re-read a previously declared image. Never inspect colliding,
        // unrelated files, even if their names resemble generated asset names.
        const file = await readFile(assetsDirectory, name, LIMITS.imageBytes);
        if (file.size !== bytes.byteLength || await sha256(new Uint8Array(await file.arrayBuffer()), this._crypto) !== hash) {
          fail('CORRUPT_PROJECT', 'A saved image was changed or damaged. Export a package before reopening the folder.');
        }
      } else {
        if (await fileExists(assetsDirectory, name)) fail('CONFLICT', 'An image filename already exists in this folder. Choose a new project folder to keep both versions.');
        await writeFile(assetsDirectory, name, bytes);
      }
      disk.assets.push({ ...asset, image: { mimeType: asset.image.mimeType, path, byteLength: bytes.byteLength } });
    }
    const source = JSON.stringify(normalizeProject(disk, 'disk'), null, 2) + '\n';
    if (sizeOf(source) > LIMITS.metadataBytes + 128 * 1024) invalid('Project metadata exceeds the supported folder file size.');
    if (beforeCommit) await beforeCommit(source);
    await writeFile(directory, 'project.json', source);
    return { disk, source };
  }

  /**
   * Page-load recovery. Caller must adopt the returned project only if its UI
   * is still clean/unchanged since this call began. No permission prompts occur.
   */
  async restore() {
    return this._operation(async () => {
      if (this._directory || this._pendingDirectory) return { project: null, reconnectNeeded: this.status.reconnectNeeded };
      if (!this._handleStore) return { project: null, reconnectNeeded: false };
      let directory;
      try {
        await this._rememberQueue.catch(() => {});
        directory = await this._handleStore.get();
      } catch {
        this._rememberWarning = 'The browser could not restore folder access. Open your project folder or import a project package.';
        return { project: null, reconnectNeeded: false };
      }
      if (!directory) return { project: null, reconnectNeeded: false };
      if (directory.kind !== 'directory' || typeof directory.getFileHandle !== 'function' || typeof directory.getDirectoryHandle !== 'function') {
        this._rememberWarning = 'The remembered folder is unavailable. Open your project folder again.';
        return { project: null, reconnectNeeded: false };
      }
      this._pendingDirectory = directory;
      this._permission = false;
      if (typeof directory.queryPermission !== 'function' || !await this._hasPermission(directory)) {
        return { project: null, reconnectNeeded: true };
      }
      return this._loadRememberedFolder(directory);
    });
  }

  async _loadRememberedFolder(directory) {
    const loaded = await this._readFolder(directory);
    this._directory = directory;
    this._pendingDirectory = null;
    this._manifest = loaded.manifest;
    this._source = loaded.source;
    this._diskProject = loaded.disk;
    this._permission = true;
    return { project: loaded.project, reconnectNeeded: false };
  }

  /** Explicit Reconnect button only; never call from page load or autosave. */
  async reconnect() {
    return this._operation(async () => {
      const directory = this._directory ?? this._pendingDirectory;
      if (!directory) fail('NO_FOLDER', 'Open a project folder first.');
      if (!await this._hasPermission(directory)) {
        this._permission = false;
        if (typeof directory.requestPermission !== 'function' || await directory.requestPermission({ mode: 'readwrite' }) !== 'granted') {
          fail('PERMISSION_DENIED', 'Write access was not granted. You can still export your project package.');
        }
      }
      if (this._pendingDirectory) return this._loadRememberedFolder(directory);
      // Never rebase a currently attached project: the UI may have unsaved edits,
      // and its original source is needed for save conflict detection.
      this._permission = true;
      return { project: null, reconnectNeeded: false };
    });
  }

  async importPackage(fileOrText) {
    return this._operation(async () => {
      let text;
      if (typeof fileOrText === 'string') text = fileOrText;
      else {
        if (!fileOrText || typeof fileOrText.name !== 'string' || !/\.filmpilot\.json$/i.test(fileOrText.name) ||
            !Number.isFinite(fileOrText.size) || fileOrText.size > LIMITS.packageBytes || typeof fileOrText.text !== 'function') {
          invalid('Choose a .filmpilot.json project package smaller than 50 MiB.');
        }
        text = await fileOrText.text();
      }
      const project = parsePackage(text);
      this._directory = null;
      this._pendingDirectory = null;
      this._source = null;
      this._manifest = null;
      this._diskProject = null;
      this._permission = false;
      await this._forget();
      return project;
    });
  }

  exportPackage(project) {
    const text = serializePackage(project);
    const stem = project.name.replace(/[^a-zA-Z0-9\u4e00-\u9fff_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80) || 'project';
    return { blob: new Blob([text], { type: 'application/json' }), filename: `${stem}.filmpilot.json` };
  }
}
