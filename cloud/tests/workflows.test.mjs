import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { LIMITS, createProject, cleanText, sanitizeError, parseProposal, createProposal, applyProposal, saveScriptVersion, restoreScriptVersion, deleteScriptVersion, buildMessages, imageFromResponse } from '../public/workflows.mjs';
import { validateProject, serializeProject, serializePackage, parsePackage, LIMITS as STORE_LIMITS } from '../public/project-store.mjs';
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

test('manual snapshots retain exact text and approval with only storage-supported fields', () => {
  const value = project(); value.script = { text: '\n 场景一：<img src=x onerror=alert(1)>\n对白。  ', status: 'approved' };
  value.assets = [{ ...assetDraft.assets[0], id: 'a', image: { mimeType: 'image/png', dataUrl: `data:image/png;base64,${png}` } }];
  const original = structuredClone(value), next = saveScriptVersion(value, '  首稿  ');
  assert.deepEqual(value, original);
  assert.equal(next.scripts.length, 1);
  assert.deepEqual(Object.keys(next.scripts[0]).sort(), ['approved', 'content', 'id', 'title']);
  assert.equal(next.scripts[0].content, value.script.text); assert.equal(next.scripts[0].approved, true); assert.equal(next.scripts[0].title, '首稿');
  assert.deepEqual(next.script, value.script); assert.deepEqual(next.assets, value.assets);
  assert.equal(JSON.stringify(next.scripts).includes('base64'), false); validateProject(next);
});
test('manual snapshots reject empty text, duplicate content and invalid titles without changing input', () => {
  for (const text of ['', '  \n ']) { const value = project(); value.script.text = text; assert.throws(() => saveScriptVersion(value), /先写下/); assert.equal(value.scripts, undefined); }
  const value = project(); value.script.text = '第一幕';
  const next = saveScriptVersion(value, '首稿'), original = structuredClone(next);
  assert.throws(() => saveScriptVersion(next, '同样内容的新名字'), /相同内容/); assert.deepEqual(next, original);
  for (const title of ['', 'x'.repeat(201), 42]) { assert.throws(() => saveScriptVersion(value, title)); assert.equal(value.scripts, undefined); }
});
test('script AI replacement creates an immutable prior version only for nonempty distinct text', () => {
  const value = project(); value.script.text = '\n旧剧本\n';
  const original = structuredClone(value), proposal = createProposal(value, 2, 'script', { text: '新剧本' });
  const next = applyProposal(value, 2, proposal);
  assert.deepEqual(value, original); assert.equal(next.script.text, '新剧本'); assert.equal(next.scripts.length, 1);
  assert.equal(next.scripts[0].content, '\n旧剧本\n'); assert.equal(next.scripts[0].approved, false);
  assert.match(next.scripts[0].title, /AI 替换前/);
  const stored = saveScriptVersion(value), replaced = applyProposal(stored, 2, createProposal(stored, 2, 'script', { text: '新剧本' }));
  assert.deepEqual(replaced.scripts, stored.scripts);
  const blank = project(), first = applyProposal(blank, 1, createProposal(blank, 1, 'script', { text: '第一版' }));
  assert.equal(first.scripts, undefined);
  const same = applyProposal(first, 2, createProposal(first, 2, 'script', { text: '第一版' })); assert.equal(same.scripts, undefined);
});
test('restore snapshots the current script, restores a draft and keeps assets and shots untouched', () => {
  const value = project(); value.script = { text: '已审核首稿', status: 'approved' };
  const stored = saveScriptVersion(value); stored.script = { text: '正在编辑的新版\n ', status: 'draft' };
  stored.assets = [{ ...assetDraft.assets[0], id: 'a', image: { mimeType: 'image/png', dataUrl: `data:image/png;base64,${png}` } }];
  stored.shots = [{ id: 'shot1', number: 1, duration: 5, description: '旧镜头需重新核对', assetIds: ['a'] }];
  const original = structuredClone(stored), next = restoreScriptVersion(stored, stored.scripts[0].id);
  assert.deepEqual(stored, original); assert.deepEqual(next.script, { text: '已审核首稿', status: 'draft' });
  assert.deepEqual(next.assets, stored.assets); assert.deepEqual(next.shots, stored.shots); assert.equal(next.scripts.length, 2);
  assert.equal(next.scripts[1].content, '正在编辑的新版\n '); assert.match(next.scripts[1].title, /恢复前/);
  assert.deepEqual(next.scripts[0], stored.scripts[0]); assert.equal(next.scripts[0].approved, true); validateProject(next);
});
test('restore requires explicit unlock, refuses missing/empty/unchanged versions and leaves input untouched', () => {
  let value = project(); value.script.text = '旧版'; value = saveScriptVersion(value);
  const versionId = value.scripts[0].id;
  assert.throws(() => restoreScriptVersion(value, versionId), /相同/);
  value.script = { text: '已确认的新版', status: 'approved' }; let original = structuredClone(value);
  assert.throws(() => restoreScriptVersion(value, versionId), /解锁/); assert.deepEqual(value, original);
  value.script.status = 'draft'; original = structuredClone(value);
  assert.throws(() => restoreScriptVersion(value, 'missing'), /不存在/); assert.deepEqual(value, original);
  value.scripts[0].content = ' \n '; original = structuredClone(value);
  assert.throws(() => restoreScriptVersion(value, versionId), /没有剧本内容/); assert.deepEqual(value, original);
});
test('history persists unchanged through project JSON and portable package roundtrips', () => {
  let value = project(); value.script.text = '第一版\n'; value = saveScriptVersion(value, '最初版本');
  value.script = { text: '第二版', status: 'approved' }; value = saveScriptVersion(value, '确认版本');
  assert.deepEqual(JSON.parse(serializeProject(value)), value);
  const imported = parsePackage(serializePackage(value)); assert.deepEqual(imported, value);
  imported.script.status = 'draft';
  const restored = restoreScriptVersion(imported, imported.scripts[0].id);
  assert.deepEqual(restored.scripts, imported.scripts); assert.equal(restored.script.text, '第一版\n');
});
test('100-version cap blocks new snapshots and replacements without deleting any history', () => {
  const value = project(); value.script.text = '当前未留存内容';
  value.scripts = Array.from({ length: STORE_LIMITS.scripts }, (_, i) => ({ id: `v${i}`, title: `版本 ${i}`, content: `历史内容 ${i}`, approved: false }));
  const original = structuredClone(value);
  for (const mutate of [p => saveScriptVersion(p), p => restoreScriptVersion(p, 'v0'), p => applyProposal(p, 7, createProposal(p, 7, 'script', { text: 'AI 新剧本' }))]) {
    assert.throws(() => mutate(value), /100.*导出项目包.*删除/); assert.deepEqual(value, original);
  }
  value.script.text = value.scripts[99].content;
  const restored = restoreScriptVersion(value, 'v0'); assert.equal(restored.scripts.length, STORE_LIMITS.scripts); assert.deepEqual(restored.scripts, value.scripts);
  const replaced = applyProposal(value, 7, createProposal(value, 7, 'script', { text: 'AI 新剧本' })); assert.deepEqual(replaced.scripts, value.scripts);
});
test('metadata UTF-8 cap is atomic and provides export/prune guidance for manual, AI and restore', () => {
  const value = project(); value.script.text = '当'.repeat(120000);
  value.scripts = Array.from({ length: 4 }, (_, i) => ({ id: `large${i}`, title: '大型历史版本', content: `${i}${'文'.repeat(120000)}`, approved: false }));
  validateProject(value); const original = structuredClone(value);
  assert.throws(() => saveScriptVersion(value), /2 MiB.*导出.*删除/); assert.deepEqual(value, original);
  assert.throws(() => restoreScriptVersion(value, 'large0'), /2 MiB.*导出.*删除/); assert.deepEqual(value, original);
  assert.throws(() => applyProposal(value, 1, createProposal(value, 1, 'script', { text: '新'.repeat(120000) })), /2 MiB.*导出.*删除/); assert.deepEqual(value, original);
});
test('explicit pruning removes only one selected historical version and creates room safely', () => {
  let value = project(); value.script.text = '第一版'; value = saveScriptVersion(value);
  value.script.text = '第二版'; value = saveScriptVersion(value); const original = structuredClone(value);
  const next = deleteScriptVersion(value, value.scripts[0].id);
  assert.deepEqual(value, original); assert.deepEqual(next.scripts, [value.scripts[1]]); assert.deepEqual(next.script, value.script);
  assert.throws(() => deleteScriptVersion(value, 'missing'), /不存在/); assert.deepEqual(value, original);
  const empty = deleteScriptVersion(next, next.scripts[0].id); assert.deepEqual(empty.scripts, []); assert.deepEqual(empty.script, value.script);
});
test('explicit pruning can recover over-budget manual edits, but never applies an invalid candidate', () => {
  const value = project(); value.script.text = '当'.repeat(120000);
  value.scripts = Array.from({ length: 5 }, (_, i) => ({ id: `large${i}`, title: '历史版本', content: `${i}${'文'.repeat(120000)}`, approved: false }));
  const original = structuredClone(value); assert.throws(() => validateProject(value));
  const next = deleteScriptVersion(value, 'large0'); validateProject(next); assert.deepEqual(value, original); assert.equal(next.scripts.length, 4);
  value.name = ''; const invalid = structuredClone(value); assert.throws(() => deleteScriptVersion(value, 'large0')); assert.deepEqual(value, invalid);
});
test('older packages with history but no current script can restore a draft', () => {
  const value = project(); delete value.script; value.scripts = [{ id: 'v1', title: '旧格式版本', content: '需要继续打磨的故事', approved: true }];
  const next = restoreScriptVersion(value, 'v1'); assert.deepEqual(next.script, { text: value.scripts[0].content, status: 'draft' }); assert.equal(value.script, undefined);
});
test('history helpers reject credential-like strings and unsupported snapshot payloads', () => {
  const value = project(); value.script.text = 'sk-proj-00000000000000000000000000000000'; const original = structuredClone(value);
  assert.throws(() => saveScriptVersion(value)); assert.deepEqual(value, original);
  value.script.text = '普通剧本'; const stored = saveScriptVersion(value);
  for (const field of ['image', 'apiKey', 'project']) {
    const invalid = structuredClone(stored); invalid.scripts[0][field] = 'not allowed'; const before = structuredClone(invalid);
    assert.throws(() => restoreScriptVersion(invalid, invalid.scripts[0].id)); assert.deepEqual(invalid, before);
  }
});
test('AI context includes only current script and excludes prior versions', () => {
  let value = project(); value.script.text = '历史不应发送给服务商'; value = saveScriptVersion(value);
  value.script.text = '当前创作剧本'; const messages = JSON.stringify(buildMessages('script', value));
  assert.equal(messages.includes('历史不应发送给服务商'), false); assert.equal(messages.includes('当前创作剧本'), true);
});
