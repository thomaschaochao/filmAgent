import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { createDemoProject } from '../public/demo-project.mjs';
import { validateProject, serializePackage, parsePackage } from '../public/project-store.mjs';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const ids = project => [project.id, ...project.assets.map(asset => asset.id), ...project.shots.map(shot => shot.id)];

function objects(value, result = new Set()) {
  if (value && typeof value === 'object') {
    result.add(value);
    for (const child of Object.values(value)) objects(child, result);
  }
  return result;
}

test('offline example is a strictly valid, clearly labelled editable Chinese project', () => {
  const project = createDemoProject();
  assert.deepEqual(validateProject(project), project);
  assert.match(project.name, /示例/);
  assert.match(project.description, /离线原创示例/);
  assert.equal(project.script.status, 'draft');
  assert.equal(project.scripts, undefined);
  assert.equal(project.settings.aspectRatio, '16:9');
  assert.equal(project.settings.language, '简体中文');
  assert.ok(project.settings.visualStyle.length > 30);
  const chineseCharacters = project.script.text.match(/\p{Script=Han}/gu).length;
  assert.ok(chineseCharacters >= 300 && chineseCharacters <= 600);
  assert.ok(project.assets.length >= 3 && project.assets.length <= 4);
  assert.ok(project.shots.length >= 3 && project.shots.length <= 5);
});

test('sample assets and ordered shots are rich, internally linked, and ready for editing', () => {
  const project = createDemoProject();
  const assetIds = new Set(project.assets.map(asset => asset.id));
  const referenced = new Set();
  assert.deepEqual(new Set(project.assets.map(asset => asset.type)), new Set(['character', 'scene', 'prop']));
  for (const asset of project.assets) {
    assert.ok(asset.name.trim());
    assert.ok(asset.description.trim());
    assert.ok(asset.prompt.trim());
    assert.equal(Object.hasOwn(asset, 'image'), false);
  }
  for (const [index, shot] of project.shots.entries()) {
    assert.equal(shot.number, index + 1);
    assert.ok(shot.duration > 0 && shot.duration <= 120);
    assert.ok(shot.description.trim());
    assert.equal(typeof shot.dialogue, 'string');
    assert.ok(shot.initialFramePrompt.trim());
    assert.ok(shot.storyboardPrompt.trim());
    assert.ok(shot.assetIds.length);
    assert.equal(new Set(shot.assetIds).size, shot.assetIds.length);
    for (const assetId of shot.assetIds) {
      assert.ok(assetIds.has(assetId));
      referenced.add(assetId);
    }
  }
  assert.deepEqual(referenced, assetIds);
  assert.ok(project.shots.some(shot => shot.dialogue.trim()));
  assert.equal(project.shots.reduce((total, shot) => total + shot.duration, 0), 60);
});

test('every creation gets fresh IDs and has no shared mutable objects', () => {
  const first = createDemoProject(), second = createDemoProject();
  const allIds = [...ids(first), ...ids(second)];
  assert.equal(new Set(allIds).size, allIds.length);
  const firstObjects = objects(first);
  for (const value of objects(second)) assert.equal(firstObjects.has(value), false);

  const originalSecond = structuredClone(second);
  first.script.text = '我的新草稿';
  first.settings.visualStyle = '黑白';
  first.assets[0].description = '另一位角色';
  first.shots[0].assetIds.pop();
  first.shots.push({ id: 'local-edit' });
  assert.deepEqual(second, originalSecond);
  assert.notEqual(createDemoProject().script.text, first.script.text);
});

test('timestamps are current UTC values created on each call', context => {
  context.mock.timers.enable({ apis: ['Date'], now: Date.UTC(2040, 0, 2, 3, 4, 5) });
  const first = createDemoProject();
  assert.equal(first.createdAt, '2040-01-02T03:04:05.000Z');
  assert.equal(first.updatedAt, first.createdAt);
  context.mock.timers.tick(65001);
  const second = createDemoProject();
  assert.equal(second.createdAt, '2040-01-02T03:05:10.001Z');
  assert.equal(second.updatedAt, second.createdAt);
  assert.deepEqual(validateProject(second), second);
});

test('creation does not fetch, open a folder, or access browser storage', context => {
  const forbidden = () => { throw new Error('Demo creation must be offline and in memory.'); };
  const fetch = context.mock.method(globalThis, 'fetch', forbidden);
  const names = ['localStorage', 'sessionStorage', 'indexedDB', 'showDirectoryPicker', 'showOpenFilePicker'];
  const descriptors = names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  try {
    for (const name of names) Object.defineProperty(globalThis, name, { configurable: true, get: forbidden });
    const project = createDemoProject();
    assert.equal(project.script.status, 'draft');
    assert.equal(fetch.mock.callCount(), 0);
  } finally {
    for (const [name, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
});

test('sample contains no credentials, provider configuration, images, or external resources', () => {
  const project = createDemoProject();
  for (const value of objects(project)) {
    for (const key of Object.keys(value)) {
      assert.doesNotMatch(key, /api.?key|provider|authorization|credential|token|password|secret|endpoint|base.?url|model|image/i);
    }
  }
  assert.deepEqual(Object.keys(project.settings).sort(), ['aspectRatio', 'language', 'visualStyle']);
  assert.doesNotMatch(JSON.stringify(project), /https?:\/\/|data:image\/|Bearer\s|\bsk-[a-z0-9_-]{16,}/i);
});

test('portable package roundtrip retains all sample content as an independent draft', () => {
  const project = createDemoProject();
  const imported = parsePackage(serializePackage(project));
  assert.deepEqual(imported, project);
  assert.equal(imported.script.status, 'draft');
  assert.notEqual(imported.assets, project.assets);
  imported.shots[0].assetIds.pop();
  assert.notDeepEqual(imported.shots[0].assetIds, project.shots[0].assetIds);
});
