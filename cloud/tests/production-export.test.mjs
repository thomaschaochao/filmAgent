import test from 'node:test';
import assert from 'node:assert/strict';
import { validateProject, ProjectStoreError } from '../public/project-store.mjs';
import { EXPORT_LIMITS, exportProductionBrief, exportShotListCsv } from '../public/production-export.mjs';

// All inputs, including image bytes and deliberately fake credentials, are
// synthetic in-memory fixtures. No secret files, environment variables, browser
// storage, filesystem handles, or external services are accessed by these tests.
const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';
const exporters = [exportProductionBrief, exportShotListCsv];
const bytes = text => new TextEncoder().encode(text).byteLength;
const project = () => ({
  schemaVersion: 1,
  id: 'project-1',
  name: '海边，"灯塔" 🎬',
  description: 'A keeper watches the sea.\n海风拂过。',
  createdAt: '2026-10-11T07:00:00.000Z',
  updatedAt: '2026-10-11T08:00:00.000Z',
  settings: { visualStyle: '水彩，细腻光线', aspectRatio: '16:9', language: '简体中文' },
  script: { text: 'EXT. LIGHTHOUSE\n守塔人说："你好，海洋。"', status: 'approved' },
  scripts: [{ id: 'old-script', title: 'History title omitted', content: 'OLD VERSION MUST STAY OUT', approved: false }],
  assets: [
    { id: 'keeper', type: 'character', name: '守塔人', description: '蓝外套', prompt: 'Asset prompt', image: { mimeType: 'image/png', dataUrl: PNG } },
    { id: 'lighthouse', type: 'scene', name: '灯塔', description: 'White tower', prompt: 'White lighthouse' },
  ],
  shots: [
    { id: 'last-by-id', number: 9, sequence: 3, duration: 2.5, description: 'Wide, "ocean"\n海岸', dialogue: 'Keep going.\r\n继续。', assetIds: ['lighthouse', 'keeper'], prompt: 'Shot prompt', initialFramePrompt: 'First frame', storyboardPrompt: 'Storyboard prompt' },
    { id: 'first-by-id', number: 1, duration: 0, description: 'Second in project order', assetIds: [] },
  ],
});

// Strictly reads our all-quoted CSV: embedded commas, double quotes, and line
// breaks remain inside fields; every record must end with CRLF.
function parseCsv(text) {
  assert.equal(text[0], '\uFEFF');
  const rows = [];
  let row = [], index = 1;
  while (index < text.length) {
    assert.equal(text[index++], '"');
    let field = '', closed = false;
    while (index < text.length) {
      const character = text[index++];
      if (character !== '"') field += character;
      else if (text[index] === '"') { field += '"'; index++; }
      else { closed = true; break; }
    }
    assert.equal(closed, true);
    row.push(field);
    if (text[index] === ',') index++;
    else {
      assert.equal(text.slice(index, index + 2), '\r\n');
      index += 2;
      rows.push(row);
      row = [];
    }
  }
  assert.equal(row.length, 0);
  for (const entry of rows) assert.equal(entry.length, rows[0].length);
  return rows;
}

function records(text) {
  const [headers, ...rows] = parseCsv(text);
  return rows.map(row => Object.fromEntries(headers.map((header, index) => [header, row[index]])));
}

test('production brief includes current settings, script, assets, shots, and all prompt kinds', () => {
  const source = project();
  const result = exportProductionBrief(source);
  assert.equal(result.mimeType, 'text/plain;charset=utf-8');
  assert.match(result.filename, /-production-brief\.txt$/);
  for (const text of [source.name, source.description, source.createdAt, source.updatedAt,
    ...Object.values(source.settings), source.script.text,
    '剧本状态：已确认', '资产数量：2', '镜头数量：2', '时长（秒）：0',
    '参考图：已有参考图（图片内容未包含）', '灯塔 (lighthouse)、守塔人 (keeper)',
    'Asset prompt', 'Shot prompt', 'First frame', 'Storyboard prompt',
  ]) assert.ok(result.text.includes(text), `Missing handoff content: ${text}`);
  assert.ok(result.text.endsWith('\n'));
  assert.match(result.text, /不能用于恢复项目/);
  assert.ok(bytes(result.text) < EXPORT_LIMITS.bytes);
});

test('CSV has a UTF-8 BOM, fixed RFC 4180 columns, quoted Unicode/newlines, and numeric values', () => {
  const source = project();
  const result = exportShotListCsv(source);
  assert.equal(result.mimeType, 'text/csv;charset=utf-8');
  assert.match(result.filename, /-shot-list\.csv$/);
  assert.deepEqual([...new TextEncoder().encode(result.text).slice(0, 3)], [239, 187, 191]);
  assert.ok(result.text.includes('"Wide, ""ocean""\n海岸"'));
  const rows = records(result.text);
  assert.equal(rows.length, 2);
  assert.equal(rows[0]['项目名称'], source.name);
  assert.equal(rows[0]['画面与动作'], source.shots[0].description);
  assert.equal(rows[0]['对白 / 旁白'], source.shots[0].dialogue);
  assert.equal(rows[0]['视觉风格'], source.settings.visualStyle);
  assert.equal(rows[0]['画幅'], '16:9');
  assert.equal(rows[0]['语言'], '简体中文');
  assert.equal(rows[0]['剧本状态'], '已确认');
  assert.equal(rows[0]['引用资产 ID'], 'lighthouse\nkeeper');
  assert.equal(rows[0]['引用资产名称'], '灯塔\n守塔人');
  assert.equal(rows[0]['通用提示词'], 'Shot prompt');
  assert.equal(rows[0]['首帧提示词'], 'First frame');
  assert.equal(rows[0]['故事板提示词'], 'Storyboard prompt');
  assert.equal(rows[0]['序列号'], '3');
  assert.equal(rows[0]['时长（秒）'], '2.5');
  assert.equal(rows[1]['时长（秒）'], '0');
});

test('both handoffs are deterministic and preserve asset, shot, and reference order', () => {
  const source = project();
  for (const exporter of exporters) assert.deepEqual(exporter(source), exporter(source));
  const text = exportProductionBrief(source).text;
  assert.ok(text.indexOf('资产 ID：keeper') < text.indexOf('资产 ID：lighthouse'));
  assert.ok(text.indexOf('镜头 ID：last-by-id') < text.indexOf('镜头 ID：first-by-id'));
  const rows = records(exportShotListCsv(source).text);
  assert.deepEqual(rows.map(row => row['镜头 ID']), ['last-by-id', 'first-by-id']);
  assert.deepEqual(rows.map(row => row['镜头顺序']), ['1', '2']);
  assert.deepEqual(rows.map(row => row['镜头编号']), ['9', '1']);
});

test('minimal valid projects and missing optional fields export without invented content', () => {
  const source = { schemaVersion: 1, id: 'minimal', name: 'Minimal', assets: [], shots: [] };
  const brief = exportProductionBrief(source).text;
  assert.match(brief, /剧本状态：未设置/);
  assert.match(brief, /视觉风格：未填写/);
  assert.match(brief, /暂无资产/);
  assert.match(brief, /暂无镜头/);
  assert.doesNotMatch(brief, /undefined|null/);
  assert.equal(parseCsv(exportShotListCsv(source).text).length, 1);

  source.assets.push({ id: 'prop', type: 'prop', name: 'Compass' });
  source.shots.push({ id: 'shot' });
  const [shot] = records(exportShotListCsv(source).text);
  assert.equal(shot['剧本状态'], '未设置');
  for (const key of ['画幅', '语言', '时长（秒）', '镜头编号', '引用资产 ID', '故事板提示词']) assert.equal(shot[key], '');
  const withShot = exportProductionBrief(source).text;
  assert.match(withShot, /类别：道具/);
  assert.match(withShot, /引用资产：无/);
  assert.match(withShot, /时长（秒）：未填写/);
  source.script = { text: '', status: 'draft' };
  assert.match(exportProductionBrief(source).text, /剧本状态：草稿/);
});

test('CSV neutralizes formula markers after whitespace and leading tabs/control characters', () => {
  const dangerous = [
    '=1+1', '+SUM(A1:A2)', '-1+2', '@SUM(1,2)', '   =HYPERLINK("https://invalid.example")',
    '\u00a0+1', '\u3000-1', '\tordinary', ' \r\nordinary', '\x01ordinary', ' \x7fordinary',
    '\u0085ordinary', '\u200bordinary', '\uFEFF=1', '\u202e=1',
  ];
  const source = project();
  source.shots = dangerous.map((description, index) => ({ id: `shot-${index}`, description }));
  const rows = records(exportShotListCsv(source).text);
  for (let index = 0; index < dangerous.length; index++) assert.equal(rows[index]['画面与动作'], `'${dangerous[index]}`);
});

test('formula protection applies to all user-controlled CSV columns, including project and asset names', () => {
  const source = project();
  source.name = '=project';
  source.settings = { visualStyle: ' +style', aspectRatio: '-ratio', language: '@language' };
  source.assets[0].name = '=asset';
  source.shots = [{ id: 'shot-1', description: '=description', dialogue: '+dialogue', assetIds: ['keeper'], prompt: '-prompt', initialFramePrompt: '@frame', storyboardPrompt: '\tboard' }];
  const [row] = records(exportShotListCsv(source).text);
  for (const key of ['项目名称', '视觉风格', '画幅', '语言', '画面与动作', '对白 / 旁白', '引用资产名称', '通用提示词', '首帧提示词', '故事板提示词']) {
    assert.equal(row[key][0], "'", `Unprotected column: ${key}`);
  }
  // A text brief is not a spreadsheet: preserve creative text exactly there.
  assert.ok(exportProductionBrief(source).text.includes('名称：=asset'));
});

test('CSV leaves ordinary prose, embedded formula markers, quotes, and apostrophes unchanged', () => {
  const texts = [' Ordinary text', '1+2', 'text = 1', 'a\tb', 'a\nb', "'=already-text", '镜头 🎬, "你好"'];
  const source = project();
  source.shots = texts.map((description, index) => ({ id: `s${index}`, description }));
  assert.deepEqual(records(exportShotListCsv(source).text).map(row => row['画面与动作']), texts);
});

test('filenames are safe single components across platforms and retain useful Unicode', () => {
  const names = ['../CON: <film>"/\\|?*\t\n.', '...', '   /\\   ', 'CON', 'con.txt', 'NUL', 'COM1', 'LPT9', 'ＣＯＮ', '-hidden', '.hidden', '\u202efilm', '电影🎬'.repeat(50)];
  for (const name of names) {
    const source = project();
    source.name = name;
    for (const exporter of exporters) {
      const { filename } = exporter(source);
      assert.doesNotMatch(filename, /[<>:"/\\|?*\p{Cc}\p{Cf}\p{Cs}]/u);
      assert.doesNotMatch(filename, /^[. -]|[. ]$/);
      assert.doesNotMatch(filename, /^(?:con|prn|aux|nul|clock\$|com[0-9]|lpt[0-9])(?:\.|$)/i);
      assert.ok(bytes(filename) <= EXPORT_LIMITS.filenameBytes);
    }
  }
  assert.match(exportProductionBrief(project()).filename, /海边/);
  const source = project();
  source.name = '...';
  assert.equal(exportProductionBrief(source).filename, 'film-project-production-brief.txt');
});

test('image payloads and historical titles/content never appear in either handoff', () => {
  const source = project();
  for (const exporter of exporters) {
    const { text } = exporter(source);
    for (const excluded of [PNG, PNG.split(',')[1], 'data:image/', 'History title omitted', 'OLD VERSION MUST STAY OUT', 'old-script']) {
      assert.equal(text.includes(excluded), false);
    }
  }
  const csv = exportShotListCsv(source).text;
  assert.equal(csv.includes(source.script.text), false);
  assert.equal(csv.includes(source.assets[0].prompt), false);
});

test('malformed projects, unknown fields, bad references, and accessors are rejected before export', () => {
  const invalid = [null, {}, { ...project(), schemaVersion: 2 }, { ...project(), assets: undefined }, { ...project(), shots: [{ id: 'shot', assetIds: ['missing'] }] }];
  const unknown = project();
  unknown.settings.provider = 'unsupported';
  invalid.push(unknown);
  const accessor = project();
  Object.defineProperty(accessor, 'name', { get() { throw new Error('Accessor must not execute'); } });
  invalid.push(accessor);
  for (const source of invalid) for (const exporter of exporters) {
    assert.throws(() => exporter(source), error => error instanceof ProjectStoreError && error.code === 'INVALID_PROJECT');
  }
});

test('credentials are rejected in exported fields and even in excluded history/image fields', () => {
  const fakeKey = `sk-${'synthetic'.repeat(4)}`;
  const mutations = [
    source => { source.apiKey = fakeKey; },
    source => { source.script.text = fakeKey; },
    source => { source.assets[0].prompt = fakeKey; },
    source => { source.scripts[0].content = fakeKey; },
    source => { source.assets[0].image.token = fakeKey; },
  ];
  for (const mutate of mutations) {
    const source = project();
    mutate(source);
    for (const exporter of exporters) assert.throws(() => exporter(source), error =>
      error instanceof ProjectStoreError && error.code === 'INVALID_PROJECT' && !error.message.includes(fakeKey));
  }
});

test('large valid creative content exports completely without silent truncation', () => {
  const source = project();
  source.script.text = 'S'.repeat(450000) + ' SCRIPT-END';
  source.shots[0].storyboardPrompt = '界'.repeat(19000) + ' PROMPT-END';
  validateProject(source);
  const brief = exportProductionBrief(source).text;
  assert.ok(brief.includes(source.script.text));
  assert.ok(brief.includes(source.shots[0].storyboardPrompt));
  assert.ok(bytes(brief) < EXPORT_LIMITS.bytes);
  const csv = exportShotListCsv(source).text;
  assert.equal(records(csv)[0]['故事板提示词'], source.shots[0].storyboardPrompt);
  assert.ok(bytes(csv) < EXPORT_LIMITS.bytes);
});

test('independent export cap rejects UTF-8 expansion of valid metadata, rather than returning a partial handoff', () => {
  const source = project();
  source.assets = Array.from({ length: 80 }, (_, index) => ({ id: `a${index}`, type: 'prop', name: '界'.repeat(150) + index }));
  source.shots = Array.from({ length: 140 }, (_, index) => ({ id: `s${index}`, assetIds: source.assets.map(asset => asset.id) }));
  validateProject(source); // Input is valid and well below the metadata limit.
  const repeatedNames = source.shots.map(() => source.assets.map(asset => asset.name).join('\n')).join('\n');
  assert.ok(repeatedNames.length < EXPORT_LIMITS.bytes);
  assert.ok(bytes(repeatedNames) > EXPORT_LIMITS.bytes);
  for (const exporter of exporters) assert.throws(() => exporter(source), error =>
    error instanceof ProjectStoreError && error.code === 'EXPORT_TOO_LARGE' && !error.message.includes('界'));
});

test('exports are pure, work with deeply frozen input, and never call the network', t => {
  const fetch = t.mock.method(globalThis, 'fetch', () => { throw new Error('Network must never be accessed'); });
  const source = project();
  const before = structuredClone(source);
  function freeze(value) {
    if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  }
  freeze(source);
  for (const exporter of exporters) {
    assert.deepEqual(Object.keys(exporter(source)).sort(), ['filename', 'mimeType', 'text']);
    assert.deepEqual(source, before);
  }
  assert.equal(fetch.mock.callCount(), 0);
});
