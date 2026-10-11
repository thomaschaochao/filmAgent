/**
 * Pure production handoffs, not restorable project packages. No filesystem,
 * browser, network, clipboard, or credential access is performed here.
 *
 * These helpers accept project data only, never provider configuration or keys.
 * validateProject rejects unsupported fields and credential-shaped text. The UI
 * must additionally guard against its known in-memory keys before calling us;
 * arbitrary secrets pasted into ordinary prose cannot be identified reliably.
 * Image payloads and script history are validated but are never exported.
 */
import { validateProject, ProjectStoreError } from './project-store.mjs';

// Separate from project/package limits: repeated asset names and CSV escaping
// can make an otherwise valid project's handoff much larger than its metadata.
export const EXPORT_LIMITS = Object.freeze({ bytes: 4 * 1024 * 1024, filenameBytes: 180 });

const encoder = new TextEncoder();
const ASSET_TYPES = Object.freeze({ character: '人物', scene: '场景', prop: '道具' });
const CSV_HEADERS = Object.freeze([
  '项目名称', '剧本状态', '画幅', '视觉风格', '语言',
  '镜头顺序', '镜头 ID', '镜头编号', '序列号', '时长（秒）',
  '画面与动作', '对白 / 旁白', '引用资产 ID', '引用资产名称',
  '通用提示词', '首帧提示词', '故事板提示词',
]);

function writer() {
  const parts = [];
  let bytes = 0;
  return {
    append(text) {
      bytes += encoder.encode(text).byteLength;
      if (bytes > EXPORT_LIMITS.bytes) {
        throw new ProjectStoreError('EXPORT_TOO_LARGE', '制作资料超过 4 MiB 导出上限。请减少文字或将镜头拆分为较小的项目后重试。');
      }
      parts.push(text);
    },
    text() { return parts.join(''); },
  };
}

function filename(name, suffix) {
  let base = name.normalize('NFKC')
    .replace(/[<>:"/\\|?*\p{Cc}\p{Cf}\p{Cs}]/gu, '_')
    .replace(/\s+/gu, '-')
    .replace(/^[. _-]+|[. _-]+$/gu, '');
  if (!base) base = 'film-project';
  if (/^(?:con|prn|aux|nul|clock\$|com[0-9]|lpt[0-9])(?:\.|$)/i.test(base)) base = `film-${base}`;
  const budget = EXPORT_LIMITS.filenameBytes - encoder.encode(suffix).byteLength;
  let safe = '', bytes = 0;
  for (const character of base) {
    const size = encoder.encode(character).byteLength;
    if (bytes + size > budget) break;
    safe += character;
    bytes += size;
  }
  safe = safe.replace(/[. _-]+$/gu, '') || 'film-project';
  return safe + suffix;
}

function scriptStatus(project) {
  return !project.script ? '未设置' : project.script.status === 'approved' ? '已确认' : '草稿';
}

function supplied(value) { return value === undefined || value === '' ? '未填写' : value; }

/** Complete, human-readable current production brief; throws rather than truncating. */
export function exportProductionBrief(input) {
  const project = validateProject(input);
  const output = writer();
  const line = (label, value) => output.append(`${label}：${supplied(value)}\n`);
  const section = title => output.append(`\n${title}\n${'='.repeat(36)}\n`);
  const assets = new Map(project.assets.map(asset => [asset.id, asset]));

  output.append('FilmPilot 制作简报\n');
  output.append('本文件用于制作交接，不能用于恢复项目；参考图片与历史剧本未包含。请另行保存项目包。\n');
  line('项目名称', project.name);
  line('项目 ID', project.id);
  if (project.createdAt) line('创建时间（UTC）', project.createdAt);
  if (project.updatedAt) line('更新时间（UTC）', project.updatedAt);
  line('资产数量', project.assets.length);
  line('镜头数量', project.shots.length);

  section('项目设定');
  line('故事与创意', project.description);
  line('视觉风格', project.settings?.visualStyle);
  line('画幅', project.settings?.aspectRatio);
  line('语言', project.settings?.language);

  section('当前剧本');
  line('剧本状态', scriptStatus(project));
  output.append(`${supplied(project.script?.text)}\n`);

  section('制作资产');
  if (!project.assets.length) output.append('暂无资产。\n');
  project.assets.forEach((asset, index) => {
    output.append(`\n资产 ${index + 1}\n`);
    line('名称', asset.name);
    line('资产 ID', asset.id);
    line('类别', ASSET_TYPES[asset.type]);
    line('稳定设定与外观', asset.description);
    line('图片提示词', asset.prompt);
    line('参考图', asset.image ? '已有参考图（图片内容未包含）' : '未设置');
  });

  section('镜头与提示词（按项目顺序）');
  if (!project.shots.length) output.append('暂无镜头。\n');
  project.shots.forEach((shot, index) => {
    output.append(`\n镜头 ${index + 1}\n`);
    line('镜头 ID', shot.id);
    line('镜头编号', shot.number);
    line('序列号', shot.sequence);
    line('时长（秒）', shot.duration);
    line('画面与动作', shot.description);
    line('对白 / 旁白', shot.dialogue);
    line('引用资产', shot.assetIds?.length
      ? shot.assetIds.map(id => `${assets.get(id).name} (${id})`).join('、')
      : '无');
    line('通用提示词', shot.prompt);
    line('首帧提示词', shot.initialFramePrompt);
    line('故事板提示词', shot.storyboardPrompt);
  });

  return { text: output.text(), filename: filename(project.name, '-production-brief.txt'), mimeType: 'text/plain;charset=utf-8' };
}

function csvCell(value) {
  let text = value === undefined ? '' : String(value);
  // Quoting alone does not prevent formulas. Prefix the entire original value
  // when a formula marker follows leading whitespace, or when its leading
  // whitespace contains a tab/control/format character that a sheet may strip.
  if (/^[\s\p{Cc}\p{Cf}]*[=+\-@\p{Cc}\p{Cf}]/u.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

/** UTF-8 BOM + RFC 4180 rows; one row per current shot, never history/images. */
export function exportShotListCsv(input) {
  const project = validateProject(input);
  const output = writer();
  const row = values => output.append(values.map(csvCell).join(',') + '\r\n');
  const assets = new Map(project.assets.map(asset => [asset.id, asset]));
  output.append('\uFEFF');
  row(CSV_HEADERS);
  project.shots.forEach((shot, index) => {
    row([
      project.name, scriptStatus(project), project.settings?.aspectRatio,
      project.settings?.visualStyle, project.settings?.language,
      index + 1, shot.id, shot.number, shot.sequence, shot.duration,
      shot.description, shot.dialogue, shot.assetIds?.join('\n'),
      shot.assetIds?.map(id => assets.get(id).name).join('\n'),
      shot.prompt, shot.initialFramePrompt, shot.storyboardPrompt,
    ]);
  });
  return { text: output.text(), filename: filename(project.name, '-shot-list.csv'), mimeType: 'text/csv;charset=utf-8' };
}
