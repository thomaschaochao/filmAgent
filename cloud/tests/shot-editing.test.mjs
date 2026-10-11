import test from 'node:test';
import assert from 'node:assert/strict';
import { moveShot, duplicateShot } from '../public/shot-editing.mjs';
import { LIMITS as WORKFLOW_LIMITS } from '../public/workflows.mjs';
import {
  LIMITS as STORE_LIMITS, ProjectStoreError, validateProject,
  serializeProject, serializePackage, parsePackage,
} from '../public/project-store.mjs';
import { exportProductionBrief, exportShotListCsv } from '../public/production-export.mjs';

// Synthetic, in-memory projects only. These tests never use credentials,
// provider calls, browser storage, or real project files.
const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';
const project = () => ({
  schemaVersion: 1,
  id: 'project-1',
  name: '灯塔短片',
  description: 'A keeper watches the sea.',
  createdAt: '2026-10-11T07:00:00.000Z',
  updatedAt: '2026-10-11T08:00:00.000Z',
  settings: { visualStyle: '水彩', aspectRatio: '16:9', language: '简体中文' },
  script: { text: 'The light turns toward the sea.', status: 'approved' },
  scripts: [{ id: 'history-1', title: '旧版本', content: 'The light is still.', approved: false }],
  assets: [
    { id: 'keeper', type: 'character', name: '守塔人', description: '蓝外套', prompt: 'Keeper portrait', image: { mimeType: 'image/png', dataUrl: PNG } },
    { id: 'tower', type: 'scene', name: '灯塔', prompt: 'White lighthouse' },
  ],
  shots: [
    { id: 'shot-z', number: 42, sequence: 7, duration: 5, description: 'Wide sea', dialogue: '你好，海洋。', assetIds: ['tower', 'keeper'], prompt: 'Soft light', initialFramePrompt: 'First frame', storyboardPrompt: 'Four frames' },
    { id: 'shot-a', number: 2, sequence: 7, duration: 0, description: 'Keeper turns', dialogue: '', assetIds: ['keeper'], prompt: 'Close-up', initialFramePrompt: 'Keeper', storyboardPrompt: 'The turn' },
    { id: 'shot-m', sequence: 3, duration: 2.5, description: 'The lamp shines', dialogue: 'Good night.', assetIds: [] },
  ],
});
const operations = [source => moveShot(source, 'shot-a', -1), source => duplicateShot(source, 'shot-a')];
const invalidProject = error => error instanceof ProjectStoreError && error.code === 'INVALID_PROJECT';
const withoutNumber = ({ number, ...shot }) => shot;

function deepFreeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}

test('moves find a stable ID rather than the shot number and renumber the whole resulting order', () => {
  for (const [shotId, direction, ids] of [
    ['shot-a', -1, ['shot-a', 'shot-z', 'shot-m']],
    ['shot-z', 1, ['shot-a', 'shot-z', 'shot-m']],
    ['shot-a', 1, ['shot-z', 'shot-m', 'shot-a']],
    ['shot-m', -1, ['shot-z', 'shot-m', 'shot-a']],
  ]) {
    const source = deepFreeze(project());
    const before = structuredClone(source);
    const next = moveShot(source, shotId, direction);
    assert.deepEqual(next.shots.map(shot => shot.id), ids);
    assert.deepEqual(next.shots.map(shot => shot.number), [1, 2, 3]);
    for (const shot of next.shots) assert.deepEqual(withoutNumber(shot), withoutNumber(source.shots.find(item => item.id === shot.id)));
    assert.deepEqual(source, before);
    assert.deepEqual({ ...next, shots: source.shots }, source);
    assert.deepEqual(validateProject(next), next);
  }
});

test('boundary moves are detached no-ops and do not normalize existing shot numbers', () => {
  for (const [shotId, direction] of [['shot-z', -1], ['shot-m', 1]]) {
    const source = deepFreeze(project());
    const next = moveShot(source, shotId, direction);
    assert.deepEqual(next, source);
    assert.notEqual(next, source);
    assert.notEqual(next.shots, source.shots);
    assert.notEqual(next.shots[0], source.shots[0]);
    assert.notEqual(next.shots[0].assetIds, source.shots[0].assetIds);
    assert.notEqual(next.assets[0].image, source.assets[0].image);
    next.shots[0].assetIds.pop();
    assert.equal(source.shots[0].assetIds.length, 2);
  }
  const single = { schemaVersion: 1, id: 'single', name: 'One shot', assets: [], shots: [{ id: 'only', sequence: 4 }] };
  for (const direction of [-1, 1]) assert.deepEqual(moveShot(single, 'only', direction), single);
});

test('both helpers reject unknown or non-string IDs without coercion or input mutation', () => {
  const source = deepFreeze(project());
  const before = structuredClone(source);
  const ids = ['missing', '', null, undefined, 1, ['shot-a'], { toString() { throw new Error('Do not coerce IDs'); } }];
  for (const shotId of ids) {
    assert.throws(() => moveShot(source, shotId, -1), /不存在/);
    assert.throws(() => duplicateShot(source, shotId), /不存在/);
  }
  const empty = { ...project(), shots: [] };
  assert.throws(() => moveShot(empty, 'missing', 1), /不存在/);
  assert.throws(() => duplicateShot(empty, 'missing'), /不存在/);
  assert.deepEqual(source, before);
});

test('moves accept only numeric -1 or 1 directions and leave the input unchanged on rejection', () => {
  const source = deepFreeze(project());
  const before = structuredClone(source);
  for (const direction of [0, 2, -2, 0.5, NaN, Infinity, '-1', '1', 'up', null, undefined, true, {}, []]) {
    assert.throws(() => moveShot(source, 'shot-a', direction), /方向/);
    assert.deepEqual(source, before);
  }
});

test('duplicates are inserted directly after the target and receive fresh independent UUIDs', () => {
  const source = deepFreeze(project());
  const before = structuredClone(source);
  for (const shotId of source.shots.map(shot => shot.id)) {
    const index = source.shots.findIndex(shot => shot.id === shotId);
    const next = duplicateShot(source, shotId);
    const copy = next.shots[index + 1];
    assert.match(copy.id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.equal(source.shots.some(shot => shot.id === copy.id), false);
    assert.deepEqual(next.shots.map(shot => shot.number), [1, 2, 3, 4]);
    assert.deepEqual(next.shots.filter(shot => shot.id !== copy.id).map(shot => shot.id), source.shots.map(shot => shot.id));
    assert.deepEqual(withoutNumber({ ...copy, id: shotId }), withoutNumber(source.shots[index]));
    for (const shot of next.shots.filter(shot => shot.id !== copy.id)) {
      assert.deepEqual(withoutNumber(shot), withoutNumber(source.shots.find(item => item.id === shot.id)));
    }
    assert.deepEqual({ ...next, shots: source.shots }, source);
    assert.deepEqual(source, before);
    const again = duplicateShot(next, shotId);
    assert.notEqual(again.shots[index + 1].id, copy.id);
    assert.equal(new Set(again.shots.map(shot => shot.id)).size, again.shots.length);
  }
});

test('edited project data, duplicate references and prompts never share mutable input objects', () => {
  for (const edit of operations) {
    const source = deepFreeze(project());
    const before = structuredClone(source);
    const next = edit(source);
    assert.notEqual(next.settings, source.settings);
    assert.notEqual(next.script, source.script);
    assert.notEqual(next.scripts, source.scripts);
    assert.notEqual(next.scripts[0], source.scripts[0]);
    assert.notEqual(next.assets, source.assets);
    assert.notEqual(next.assets[0].image, source.assets[0].image);
    const changed = next.shots.find(shot => shot.id === 'shot-a');
    assert.notEqual(changed.assetIds, source.shots[1].assetIds);
    changed.assetIds.push('tower');
    changed.initialFramePrompt = 'Edited frame';
    changed.storyboardPrompt = 'Edited board';
    next.settings.language = 'English';
    next.script.text = 'Changed script';
    next.scripts[0].content = 'Changed history';
    next.assets[0].image.dataUrl = 'Changed image';
    assert.deepEqual(source, before);
  }
  const source = project();
  const next = duplicateShot(source, 'shot-z');
  assert.notEqual(next.shots[0].assetIds, next.shots[1].assetIds);
  next.shots[1].assetIds.pop();
  next.shots[1].prompt = 'Changed copy';
  next.shots[1].initialFramePrompt = 'Changed copy frame';
  next.shots[1].storyboardPrompt = 'Changed copy board';
  assert.deepEqual(withoutNumber(next.shots[0]), withoutNumber(source.shots[0]));
});

test('duplication preserves missing optional fields except the new sequential number', () => {
  const source = { schemaVersion: 1, id: 'minimal', name: 'Minimal', assets: [], shots: [{ id: 'only' }] };
  const next = duplicateShot(source, 'only');
  assert.deepEqual(next.shots[0], { id: 'only', number: 1 });
  assert.deepEqual(next.shots[1], { id: next.shots[1].id, number: 2 });
  assert.deepEqual({ ...next, shots: source.shots }, source);
});

test('the 300-shot workspace cap permits the last duplicate and rejects growth at or above the cap', () => {
  assert.equal(WORKFLOW_LIMITS.shots, 300);
  const source = project();
  source.shots = Array.from({ length: WORKFLOW_LIMITS.shots - 1 }, (_, index) => ({ id: `shot-${index}`, sequence: 2 }));
  const next = duplicateShot(source, 'shot-20');
  assert.equal(next.shots.length, WORKFLOW_LIMITS.shots);
  assert.deepEqual(next.shots.map(shot => shot.number), Array.from({ length: WORKFLOW_LIMITS.shots }, (_, index) => index + 1));
  for (const count of [WORKFLOW_LIMITS.shots, WORKFLOW_LIMITS.shots + 1, STORE_LIMITS.shots]) {
    const large = project();
    large.shots = Array.from({ length: count }, (_, index) => ({ id: `shot-${index}`, sequence: 2 }));
    const before = structuredClone(large);
    validateProject(large);
    assert.throws(() => duplicateShot(large, 'shot-20'), /300.*上限/);
    assert.deepEqual(large, before);
    const moved = moveShot(large, 'shot-20', -1);
    assert.equal(moved.shots.length, count);
    assert.equal(moved.shots[19].id, 'shot-20');
    assert.equal(moved.shots.at(-1).number, count);
  }
});

test('both helpers validate inputs before editing, including boundary moves', () => {
  const mutations = [
    value => { value.name = ''; },
    value => { value.shots[1].assetIds = ['missing']; },
    value => { value.shots[1].id = value.shots[0].id; },
    value => { value.shots[1].number = -1; },
    value => { value.shots[1].sequence = 0; },
    value => { value.shots[1].description = `sk-${'synthetic'.repeat(6)}`; },
    value => { value.shots[1].unsupported = 'not allowed'; },
  ];
  for (const mutate of mutations) {
    const source = project();
    mutate(source);
    const before = structuredClone(source);
    for (const edit of [...operations, value => moveShot(value, 'shot-z', -1)]) assert.throws(() => edit(source), invalidProject);
    assert.deepEqual(source, before);
  }
  const source = project();
  Object.defineProperty(source.shots[1], 'description', { get() { throw new Error('Getters must not execute'); } });
  for (const edit of operations) assert.throws(() => edit(source), invalidProject);
});

test('duplicate final-candidate validation rejects metadata growth atomically', () => {
  const source = project();
  source.scripts = Array.from({ length: 4 }, (_, index) => ({ id: `version-${index}`, title: 'History', content: 'x'.repeat(490000), approved: false }));
  for (const key of ['description', 'dialogue', 'prompt', 'initialFramePrompt', 'storyboardPrompt']) source.shots[1][key] = 'x'.repeat(20000);
  validateProject(source);
  const before = structuredClone(source);
  assert.throws(() => duplicateShot(source, 'shot-a'), error => invalidProject(error) && /2 MiB/.test(error.message));
  assert.deepEqual(source, before);
});

test('move final-candidate validation rejects renumbering growth atomically', () => {
  const source = {
    schemaVersion: 1, id: 'at-budget', name: 'At metadata limit',
    script: { text: '', status: 'draft' },
    scripts: Array.from({ length: 4 }, (_, index) => ({ id: `version-${index}`, title: '', content: 'x'.repeat(500000), approved: false })),
    assets: [], shots: [{ id: 'first' }, { id: 'second' }],
  };
  source.script.text = 'x'.repeat(STORE_LIMITS.metadataBytes - new TextEncoder().encode(JSON.stringify(source)).byteLength);
  validateProject(source);
  const before = structuredClone(source);
  assert.throws(() => moveShot(source, 'second', -1), error => invalidProject(error) && /2 MiB/.test(error.message));
  assert.deepEqual(source, before);
});

test('duplicate final-candidate validation rejects a UUID collision rather than returning invalid IDs', context => {
  const source = project();
  const before = structuredClone(source);
  context.mock.method(globalThis.crypto, 'randomUUID', () => source.shots[0].id);
  assert.throws(() => duplicateShot(source, 'shot-a'), invalidProject);
  assert.deepEqual(source, before);
});

test('shot edits do not call providers or access the DOM, browser storage or folder pickers', context => {
  const forbidden = () => { throw new Error('Shot edits must stay offline and in memory.'); };
  const fetch = context.mock.method(globalThis, 'fetch', forbidden);
  const names = ['document', 'localStorage', 'sessionStorage', 'indexedDB', 'showDirectoryPicker', 'showOpenFilePicker'];
  const descriptors = names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  try {
    for (const name of names) Object.defineProperty(globalThis, name, { configurable: true, get: forbidden });
    for (const edit of operations) validateProject(edit(project()));
    assert.equal(fetch.mock.callCount(), 0);
  } finally {
    for (const [name, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
});

test('edited projects round-trip through project JSON and packages and export in the resulting order', () => {
  let source = project();
  source = moveShot(source, 'shot-a', -1);
  source = duplicateShot(source, 'shot-z');
  const restored = parsePackage(serializePackage(source));
  assert.deepEqual(restored, source);
  assert.deepEqual(JSON.parse(serializeProject(source)), source);
  const brief = exportProductionBrief(restored).text;
  const csv = exportShotListCsv(restored).text;
  let previousBrief = -1;
  let previousCsv = -1;
  for (const shot of restored.shots) {
    const briefPosition = brief.indexOf(`镜头 ID：${shot.id}\n`);
    const csvPosition = csv.indexOf(`"${shot.id}"`);
    assert.ok(briefPosition > previousBrief);
    assert.ok(csvPosition > previousCsv);
    assert.ok(brief.includes(`镜头编号：${shot.number}\n`));
    previousBrief = briefPosition;
    previousCsv = csvPosition;
  }
  assert.deepEqual(restored.shots.map(shot => shot.sequence), [7, 7, 7, 3]);
  assert.equal(brief.includes('The light is still.'), false);
  assert.equal(csv.includes('base64'), false);
});
