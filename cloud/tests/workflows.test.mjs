import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { LIMITS, createProject, cleanText, sanitizeError, parseProposal, createProposal, applyProposal, buildMessages, imageFromResponse } from '../public/workflows.mjs';
import { validateProject, LIMITS as STORE_LIMITS } from '../public/project-store.mjs';
if (!globalThis.crypto) globalThis.crypto = webcrypto;
const project = () => createProject('测试短片');
const assetDraft = { assets: [{ name: '主角', type: 'character', description: '短发，深蓝外套', prompt: '写实角色参考，深蓝外套，自然光' }] };
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV5kAAAAASUVORK5CYII=';

test('new project matches shared store schema, contains no provider settings', () => {
  const value = project(); assert.deepEqual(validateProject(value), value);
  assert.equal(value.script.status, 'draft'); assert.equal(value.schemaVersion, 1);
  assert.ok(!JSON.stringify(value).includes('apiKey')); assert.ok(LIMITS.assets <= STORE_LIMITS.assets);
});
test('valid plain/fenced JSON parsed, malformed response does not mutate project', () => {
  const value = project(), original = structuredClone(value);
  assert.deepEqual(parseProposal('```json\n{"script":" 一段剧本 "}\n```', 'script', value), { text: '一段剧本' });
  assert.throws(() => parseProposal('not json', 'script', value), /JSON/);
  assert.throws(() => parseProposal('[]', 'script', value));
  assert.throws(() => parseProposal('{"script":12}', 'script', value));
  assert.throws(() => parseProposal('{"script":""}', 'script', value));
  assert.deepEqual(value, original);
});
test('output limits checked before JSON decoding and text application', () => {
  assert.throws(() => parseProposal('x'.repeat(500001), 'script', project()));
  assert.throws(() => parseProposal(JSON.stringify({ script: 'x'.repeat(LIMITS.text + 1) }), 'script', project()));
  assert.equal(cleanText('one\u0000\n two'), 'one\n two');
});
test('asset schema rejects invalid types, duplicates and oversized lists', () => {
  const value = project();
  assert.throws(() => parseProposal(JSON.stringify({ assets: [{ ...assetDraft.assets[0], type: 'script' }] }), 'assets', value));
  assert.throws(() => parseProposal(JSON.stringify({ assets: [assetDraft.assets[0], assetDraft.assets[0]] }), 'assets', value), /重复/);
  assert.throws(() => parseProposal(JSON.stringify({ assets: Array.from({ length: 101 }, (_, i) => ({ ...assetDraft.assets[0], name: `name${i}` })) }), 'assets', value));
});
test('asset proposals append and keep existing same-name asset and image intact', () => {
  const value = project(); value.assets = [{ ...assetDraft.assets[0], id: 'existing', image: { mimeType: 'image/png', dataUrl: `data:image/png;base64,${png}` } }];
  const data = parseProposal(JSON.stringify({ assets: [...assetDraft.assets, { ...assetDraft.assets[0], name: '房间', type: 'scene' }] }), 'assets', value);
  const next = applyProposal(value, 1, createProposal(value, 1, 'assets', data));
  assert.equal(next.assets.length, 2); assert.deepEqual(next.assets[0], value.assets[0]); assert.equal(value.assets.length, 1); validateProject(next);
});
test('same-name case normalization preserves existing assets', () => {
  const value = project(); value.assets = [{ id: 'a1', name: 'Hero', type: 'character', description: 'old', prompt: 'old' }];
  const data = parseProposal(JSON.stringify({ assets: [{ name: 'HERO', type: 'character', description: 'new', prompt: 'new' }] }), 'assets', value);
  assert.equal(applyProposal(value, 1, createProposal(value, 1, 'assets', data)).assets[0].prompt, 'old');
});
test('stale proposal cannot apply after edit or project switch, even with same name', () => {
  const value = project(), proposal = createProposal(value, 3, 'script', { text: '新剧本' });
  assert.throws(() => applyProposal(value, 4, proposal), /过期/);
  assert.throws(() => applyProposal(project(), 3, proposal), /过期/);
  assert.equal(value.script.text, '');
});
test('approved script cannot be overwritten, new draft preserves assets and shots', () => {
  const value = project(); value.script = { text: '已确认的故事', status: 'approved' };
  const proposal = createProposal(value, 4, 'script', { text: '新故事' });
  assert.throws(() => applyProposal(value, 4, proposal), /解锁/); assert.equal(value.script.text, '已确认的故事');
  value.script.status = 'draft'; value.assets = [{ ...assetDraft.assets[0], id: 'a' }];
  const next = applyProposal(value, 4, proposal); assert.equal(next.script.status, 'draft'); assert.equal(next.script.text, '新故事'); assert.deepEqual(next.assets, value.assets);
});
test('shot schema validates references, numbers and required visual prompts', () => {
  const value = project(); value.script.status = 'approved'; value.assets = [{ ...assetDraft.assets[0], id: 'a' }];
  const shot = { duration: 5, description: '主角开门', dialogue: '', assetIds: ['a', 'a'], initialFramePrompt: '手放在门把上', storyboardPrompt: '四格：推门，探头，迈步，回望' };
  const data = parseProposal(JSON.stringify({ shots: [shot] }), 'shots', value);
  assert.deepEqual(data.shots[0].assetIds, ['a']); assert.equal(data.shots[0].number, 1);
  const next = applyProposal(value, 1, createProposal(value, 1, 'shots', data)); validateProject(next);
  for (const patch of [{ duration: 0 }, { duration: 121 }, { duration: '5' }, { assetIds: ['unknown'] }, { initialFramePrompt: '' }, { storyboardPrompt: 1 }]) assert.throws(() => parseProposal(JSON.stringify({ shots: [{ ...shot, ...patch }] }), 'shots', value));
});
test('shot proposal cannot apply to unapproved script', () => {
  const value = project(); assert.throws(() => applyProposal(value, 1, createProposal(value, 1, 'shots', { shots: [] })), /确认/);
});
test('targeted proposals change only designated fields; missing targets fail safely', () => {
  const value = project(); value.assets = [{ ...assetDraft.assets[0], id: 'a' }];
  const next = applyProposal(value, 1, createProposal(value, 1, 'assetPrompt', { prompt: '新提示词' }, 'a'));
  assert.equal(next.assets[0].prompt, '新提示词'); assert.equal(value.assets[0].prompt, assetDraft.assets[0].prompt);
  assert.throws(() => applyProposal(value, 1, createProposal(value, 1, 'assetPrompt', { prompt: 'new' }, 'missing')));
});
test('error sanitization never exposes known credentials or markup', () => {
  const secret = 'dummy-secret-for-test';
  assert.equal(sanitizeError(`Error ${secret} ${secret}`, [secret]), 'Error [已隐藏] [已隐藏]');
  assert.ok(!sanitizeError('Bearer abcDEF123 sk-abcdefgh123456').includes('abcDEF123'));
  assert.ok(!sanitizeError('<img src=x onerror=alert(1)>').includes('<'));
  assert.equal(sanitizeError('a'.repeat(500)).length, 240);
});
test('messages exclude embedded images and credentials, with bounded storyboard frames', () => {
  const value = project(); value.assets = [{ ...assetDraft.assets[0], id: 'a', image: { mimeType: 'image/png', dataUrl: `data:image/png;base64,${png}` } }];
  const messages = buildMessages('assetPrompt', value, '', 'a', 999);
  const text = JSON.stringify(messages); assert.ok(!text.includes('base64')); assert.ok(!text.includes('mimeType')); assert.ok(!text.includes('Authorization')); assert.ok(messages[0].content.includes('4 格'));
  assert.equal(messages[1].role, 'user'); assert.throws(() => buildMessages('unknown', value));
});
test('base64 image parsing rejects URL-only, provider errors and non-image content', () => {
  const image = imageFromResponse({ data: [{ b64_json: png }] }); assert.equal(image.mimeType, 'image/png');
  for (const response of [{ data: [{ url: 'https://example.invalid/image.png' }] }, { error: { message: 'sensitive text' } }, { data: [{ error: { message: 'sensitive text' } }] }, { data: [{ b64_json: btoa('<script>bad</script>') }] }, { data: [{ b64_json: 'not base64!!!' }] }]) assert.throws(() => imageFromResponse(response));
  const value = project(); value.assets = [{ ...assetDraft.assets[0], id: 'a' }]; const next = applyProposal(value, 1, createProposal(value, 1, 'image', { image }, 'a')); validateProject(next); assert.equal(value.assets[0].image, undefined);
});
test('unsupported operations fail closed', () => {
  const value = project(); assert.throws(() => applyProposal(value, 1, createProposal(value, 1, 'unsupported', {})));
  assert.throws(() => parseProposal('{}', 'unsupported', value));
});
