import { ProjectStore, validateProject, LIMITS as STORE_LIMITS } from './project-store.mjs';
import { LIMITS, createProject, parseProposal, createProposal, applyProposal, buildMessages, imageFromResponse, sanitizeError } from './workflows.mjs';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const store = new ProjectStore();
const state = { project: null, revision: 0, savedRevision: 0, view: 'overview', request: null, fileBusy: false, proposal: null, activity: [], imageTarget: null };
// Deliberately page-memory only. Do not add browser storage or include this object in project data.
const keys = { deepseek: '', openai: '', seedream: '' };
const views = { overview: '项目概览', script: '剧本创作', assets: '资产库', shots: '分镜工作台', activity: '活动记录' };
const types = { character: '人物', scene: '场景', prop: '道具' };
const operationNames = { script: '剧本建议', assets: '资产提取', shots: '镜头拆分', assetPrompt: '资产提示词', shotPrompts: '镜头提示词', image: '资产参考图' };
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const uid = () => crypto.randomUUID();
const dirty = () => Boolean(state.project && state.revision !== state.savedRevision);
let toastTimer;

function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6000); }
function guardProject(project) {
  const serialized = JSON.stringify(project);
  if (Object.values(keys).some(key => key && serialized.includes(key))) throw new Error('项目文字包含本次会话的密钥。请先删除这些内容，密钥不能保存或作为创作上下文发送。');
  return validateProject(project);
}
function safeMessage(error) {
  const messages = {
    PERMISSION_DENIED: '文件夹写入权限已失效或被拒绝。可点击“重新授权”，或先导出项目包保留当前修改。',
    UNSUPPORTED: '此浏览器不支持直接访问文件夹。请使用项目包导入和导出，或在桌面 Chrome / Edge 中打开。',
    CONFLICT: '文件夹内容已改变或与当前项目冲突，未覆盖现有项目。请先导出项目包，再重新打开或选择新的文件夹。',
    EXISTING_PROJECT: '这个文件夹已有项目或文件。请使用“打开文件夹”，或选择新的专用空文件夹，现有文件不会被覆盖。',
    PROJECT_MISMATCH: '当前项目与选定文件夹不匹配。请导出项目包，或选择新的专用文件夹。',
    NOT_EMPTY: '这个文件夹已有内容。请选择专用空文件夹；已有 FilmPilot 项目请使用“打开文件夹”。',
    INVALID_PROJECT: '项目校验未通过：请检查必填名称、长度、图片格式和数量。不要将密钥写入项目文字。当前内容已保留。',
    CORRUPT_PROJECT: '项目或图片文件损坏，无法安全读取。当前项目已保留，请使用有效的备份项目包。',
    INVALID_FOLDER: '没有找到有效的 FilmPilot 项目。请选择包含项目清单的文件夹。',
    NO_FOLDER: '请先选择本地项目文件夹，或导出项目包保存。',
    BUSY: '已有文件操作进行中，请稍候。',
  };
  return messages[error?.code] || sanitizeError(error, Object.values(keys));
}
function activity(message, error = false) {
  state.activity.unshift({ at: new Date(), message: sanitizeError(message, Object.values(keys)), error });
  state.activity = state.activity.slice(0, 60); renderActivity();
}
function report(error) {
  const message = safeMessage(error); activity(message, true); notice(message, true, error?.code === 'PERMISSION_DENIED'); toast(message);
}
function notice(message = '', error = false, reconnect = false) {
  const node = $('#notice'); node.replaceChildren(); node.hidden = !message; node.classList.toggle('error', error);
  if (!message) return;
  const text = document.createElement('span'); text.textContent = message; node.append(text);
  if (reconnect) { const button = document.createElement('button'); button.className = 'button small'; button.textContent = '重新授权'; button.addEventListener('click', reconnectFolder); node.append(button); }
}
function cancelRequest(log = true) {
  if (!state.request) return;
  state.request.controller.abort(); state.request = null;
  if (log) activity('已取消等待。若请求已到达服务商，仍可能产生费用。');
  updateControls();
}
function touch() {
  if (!state.project) return;
  state.revision += 1; state.project.updatedAt = new Date().toISOString();
  if (state.proposal) { state.proposal = null; $('#proposal-dialog').close(); toast('内容已改变，旧建议已失效。'); }
  updateSummary(); updateControls();
}
function ensureCanSwitch() {
  if (state.fileBusy) return false;
  return !(dirty() || state.proposal) || confirm('当前项目有未保存的修改或尚未应用的建议。继续将离开这些内容。请确认已保存或导出项目包。');
}
function normalizeForUI(project) {
  project.script ??= { text: '', status: 'draft' };
  project.settings ??= {};
  project.settings = { visualStyle: '', aspectRatio: '16:9', language: '简体中文', ...project.settings };
  project.description ??= '';
  project.assets = project.assets.map(asset => ({ description: '', prompt: '', ...asset }));
  project.shots = project.shots.map((shot, i) => ({ number: i + 1, duration: 5, description: '', dialogue: '', assetIds: [], initialFramePrompt: '', storyboardPrompt: '', ...shot }));
  return project;
}
function setProject(project, saved = true) {
  cancelRequest(false); state.project = normalizeForUI(project); state.revision += 1; state.savedRevision = saved ? state.revision : state.revision - 1;
  state.proposal = null; $('#proposal-dialog').close(); notice(); state.view = 'overview'; render();
}
function setView(view) {
  if (view !== 'activity' && !state.project && view !== 'overview') { toast('先新建或打开一个创作项目。'); return; }
  state.view = view; renderView();
}
function renderView() {
  const hasProject = Boolean(state.project), activityView = state.view === 'activity';
  $('#welcome').hidden = hasProject || activityView;
  $('#project-workspace').hidden = !hasProject || activityView;
  $('#view-activity').hidden = !activityView;
  for (const view of ['overview', 'script', 'assets', 'shots']) $(`#view-${view}`).hidden = state.view !== view;
  $$('[data-view]').forEach(button => { if (button.classList.contains('nav-item')) { const selected = button.dataset.view === state.view; button.classList.toggle('active', selected); if (selected) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); } });
  $('#breadcrumb').textContent = views[state.view];
}
function updateControls() {
  const project = state.project, busy = Boolean(state.request), fileBusy = state.fileBusy;
  $('#save-project').disabled = !project || fileBusy;
  $('#save-project').textContent = fileBusy ? '文件处理中…' : store.status.mode === 'folder' ? '保存项目 ↗' : '导出以保存 ↗';
  for (const selector of ['#open-folder', '#welcome-open', '#new-project', '#new-project-secondary', '#attach-folder', '#export-project', '#add-asset', '#add-shot', '#approve-script', '#unlock-script']) $(selector).disabled = fileBusy;
  for (const selector of ['#generate-script', '#extract-assets', '#generate-shots']) $(selector).disabled = !project || busy || fileBusy || (selector === '#generate-script' && project.script.status === 'approved');
  $$('[data-action="assetPrompt"], [data-action="image"], [data-action="shotPrompts"], [data-action="importImage"]').forEach(button => { button.disabled = busy || fileBusy; });
  $$('#project-workspace input, #project-workspace textarea, #project-workspace select').forEach(input => { input.disabled = fileBusy; });
  $('#busy-bar').hidden = !busy;
  $('#key-dot').classList.toggle('ready', Boolean(keys.deepseek));
  $('#key-dot').title = keys.deepseek ? 'DeepSeek 密钥已临时填写' : '尚未填写 DeepSeek 密钥';
  $('#new-folder').disabled = !store.supported || fileBusy;
  $('#attach-folder').hidden = !store.supported;
  $('#open-folder').hidden = !store.supported;
  $('#welcome-open').hidden = !store.supported;
}
function updateSummary() {
  const project = state.project; if (!project) return;
  $('#project-title').textContent = project.name || '未命名短片';
  $('#project-location').textContent = store.status.mode === 'folder' ? `本地文件夹 / ${store.status.folderName}` : '项目包模式 / 当前页面内存';
  $('#project-subtitle').textContent = store.status.mode === 'folder' ? '修改后点击保存。刷新后如提示，请重新授权此文件夹。' : '请及时导出项目包；刷新或离开会清空未导出的内容。';
  $('#save-status').textContent = dirty() ? '有未保存的修改' : store.status.mode === 'folder' ? '已保存到文件夹' : '项目包已载入 / 导出';
  $('#save-status').className = `save-state ${dirty() ? 'dirty' : 'saved'}`;
  $('#assets-count').textContent = project.assets.length; $('#shots-count').textContent = project.shots.length;
  $('#asset-total').textContent = `${project.assets.length} 个资产`; $('#shot-total').textContent = `${project.shots.length} 个镜头`;
  $('#shot-duration').textContent = `总时长 ${Number(project.shots.reduce((sum, shot) => sum + Number(shot.duration || 0), 0).toFixed(1))} 秒`;
  const approved = project.script.status === 'approved';
  $('#script-badge').textContent = approved ? '已确认' : '草稿'; $('#script-badge').classList.toggle('approved', approved);
  $('#script-text').readOnly = approved; $('#approve-script').hidden = approved; $('#unlock-script').hidden = !approved;
  $('#script-length').textContent = `${project.script.text.length.toLocaleString('zh-CN')} 字`;
  const steps = [ ['故事设定', Boolean(project.description.trim()), project.description.trim() ? '创意已就位' : '描述你想讲的故事'], ['剧本确认', approved, approved ? '已锁定创作基准' : project.script.text ? '草稿等待确认' : '等待第一版剧本'], ['视觉资产', project.assets.length > 0, `${project.assets.length} 个可复用资产`], ['镜头拆分', project.shots.length > 0, `${project.shots.length} 个镜头待你打磨`] ];
  $('#progress-list').innerHTML = steps.map(([label, done, detail], index) => `<li class="${done ? 'complete' : ''}"><span class="progress-icon">${done ? '✓' : index + 1}</span><strong>${label}</strong><small>${detail}</small></li>`).join('');
}
function render() {
  if (state.project) {
    const p = state.project;
    $('#project-name').value = p.name; $('#project-description').value = p.description;
    $('#project-style').value = p.settings.visualStyle; $('#project-ratio').value = p.settings.aspectRatio;
    $('#script-text').value = p.script.text; updateSummary(); renderAssets(); renderShots();
  }
  renderView(); updateControls();
}
function renderAssets() {
  if (!state.project) return;
  $('#asset-list').innerHTML = state.project.assets.length ? state.project.assets.map(asset => `<article class="asset-card" data-id="${esc(asset.id)}"><div class="asset-image">${asset.image ? `<img src="${esc(asset.image.dataUrl)}" alt="${esc(asset.name)}参考图">` : '<span class="asset-placeholder" aria-hidden="true">◇</span>'}<span class="badge">${types[asset.type]}</span></div><div class="asset-card-body"><label for="asset-name-${esc(asset.id)}">资产名称</label><input id="asset-name-${esc(asset.id)}" data-field="name" maxlength="160" value="${esc(asset.name)}"><label for="asset-type-${esc(asset.id)}">资产类型</label><select id="asset-type-${esc(asset.id)}" data-field="type">${Object.entries(types).map(([value, label]) => `<option value="${value}"${value === asset.type ? ' selected' : ''}>${label}</option>`).join('')}</select><label for="asset-desc-${esc(asset.id)}">稳定设定与外观</label><textarea id="asset-desc-${esc(asset.id)}" data-field="description" rows="3" maxlength="6000">${esc(asset.description)}</textarea><label for="asset-prompt-${esc(asset.id)}">图片提示词</label><textarea id="asset-prompt-${esc(asset.id)}" data-field="prompt" rows="4" maxlength="16000">${esc(asset.prompt)}</textarea><div class="asset-actions"><button class="button" data-action="assetPrompt">✦ 生成提示词</button><button class="button primary" data-action="image">✦ 生成参考图</button></div><div class="card-footer"><button class="text-button" data-action="importImage">导入图片</button><button class="text-button danger-text" data-action="deleteAsset">删除资产</button></div></div></article>`).join('') : '<div class="empty-state"><span>◇</span><h3>先建立故事里的世界</h3><p>确认剧本后提取资产，或手动添加第一个人物、场景或道具。</p></div>';
}
function renderShots() {
  if (!state.project) return;
  $('#shot-list').innerHTML = state.project.shots.length ? state.project.shots.map((shot, index) => `<article class="shot-card" data-id="${esc(shot.id)}"><div class="shot-card-header"><span class="shot-number">SHOT ${String(index + 1).padStart(2, '0')}</span><label for="duration-${esc(shot.id)}">时长 / 秒</label><input id="duration-${esc(shot.id)}" type="number" data-field="duration" min="0.1" max="120" step="0.1" value="${Number(shot.duration)}"><button class="text-button danger-text" data-action="deleteShot">删除</button></div><div class="shot-content-grid"><div><label for="desc-${esc(shot.id)}">画面、动作与镜头语言</label><textarea id="desc-${esc(shot.id)}" data-field="description" rows="4" maxlength="6000">${esc(shot.description)}</textarea></div><div><label for="dialogue-${esc(shot.id)}">对白 / 旁白</label><textarea id="dialogue-${esc(shot.id)}" data-field="dialogue" rows="4" maxlength="6000">${esc(shot.dialogue)}</textarea></div></div><div class="shot-ref-list" role="group" aria-label="此镜头引用的资产">${state.project.assets.map(asset => `<label><input type="checkbox" data-asset-ref="${esc(asset.id)}"${shot.assetIds.includes(asset.id) ? ' checked' : ''}>${esc(asset.name)}</label>`).join('') || '<span class="muted">暂无可引用资产</span>'}</div><details class="shot-prompts"><summary>首帧与故事板提示词</summary><div class="form-columns"><div><label for="first-${esc(shot.id)}">首帧 · 动作开始前</label><textarea id="first-${esc(shot.id)}" data-field="initialFramePrompt" rows="5" maxlength="16000">${esc(shot.initialFramePrompt)}</textarea></div><div><label for="board-${esc(shot.id)}">故事板 · 连续画面</label><textarea id="board-${esc(shot.id)}" data-field="storyboardPrompt" rows="5" maxlength="16000">${esc(shot.storyboardPrompt)}</textarea></div></div></details><div class="shot-card-footer"><span>保持人物、服装、空间和光线的一致性。</span><button class="button" data-action="shotPrompts">✦ 生成两种提示词</button></div></article>`).join('') : '<div class="empty-state"><span>▤</span><h3>下一步，让故事动起来</h3><p>确认剧本、整理资产后，拆分镜头并为每个镜头准备提示词。</p></div>';
}
function renderActivity() {
  $('#activity-list').innerHTML = state.activity.length ? state.activity.map(item => `<div class="activity-row${item.error ? ' error' : ''}"><time>${item.at.toLocaleTimeString('zh-CN', { hour12: false })}</time><span>${esc(item.message)}</span></div>`).join('') : '<div class="activity-empty">操作记录会显示在这里。</div>';
}

async function fileOperation(operation) {
  if (state.fileBusy) return;
  state.fileBusy = true; updateControls();
  try { return await operation(); } catch (error) { report(error); } finally { state.fileBusy = false; updateSummary(); updateControls(); }
}
function newDialog() {
  if (!ensureCanSwitch()) return;
  cancelRequest(false); $('#new-name').value = ''; $('#new-dialog').showModal(); $('#new-name').focus();
}
async function newProject(useFolder) {
  if (state.fileBusy) return;
  const name = $('#new-name').value.trim();
  if (!name) { $('#new-name').reportValidity(); toast('请填写项目名称。'); return; }
  const project = createProject(name);
  if (useFolder) await fileOperation(async () => { const result = await store.createFolder(project); if (result) { setProject(result, true); $('#new-dialog').close(); activity('已在明确选择的本地文件夹创建项目。'); } });
  else { store.disconnect(); setProject(project, false); $('#new-dialog').close(); activity('已创建项目包模式项目，请及时导出保存。'); }
}
async function openFolder() {
  if (!ensureCanSwitch()) return;
  cancelRequest(false);
  await fileOperation(async () => { const project = await store.openFolder(); if (project) { setProject(project, true); activity('已打开选定文件夹中的项目。'); } });
}
async function attachFolder() {
  if (!state.project || state.fileBusy) return;
  await fileOperation(async () => { const result = await store.createFolder(guardProject(state.project)); if (result) { state.project = normalizeForUI(result); state.savedRevision = state.revision; notice(); render(); activity('已保存到新选择的项目文件夹。'); toast('已保存到本地文件夹。'); } });
}
async function saveProject() {
  if (!state.project || state.fileBusy) return;
  if (store.status.mode !== 'folder') { exportProject(); return; }
  await fileOperation(async () => { const revision = state.revision; const result = await store.save(guardProject(state.project)); if (state.revision === revision) { state.project = normalizeForUI(result); state.savedRevision = revision; } notice(); activity('项目已保存到选定的本地文件夹。'); toast('项目已保存。'); });
}
function exportProject() {
  if (!state.project || state.fileBusy) return;
  try {
    const { blob, filename } = store.exportPackage(guardProject(state.project)); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
    // Download initiation is not proof of persistence. Keep the unload warning until explicit acknowledgement.
    const exportedRevision = state.revision, exportedId = state.project.id;
    notice('已发起项目包下载。请检查浏览器下载，确认文件已保存在设备上。');
    if (store.status.mode !== 'folder') { const button = document.createElement('button'); button.className = 'button small'; button.textContent = '我已保存项目包'; button.addEventListener('click', () => { if (state.project?.id === exportedId && state.revision === exportedRevision) { state.savedRevision = exportedRevision; updateSummary(); notice(); toast('已确认项目包备份。'); } else { toast('确认后项目已有新修改，请重新导出。'); } }); $('#notice').append(button); }
    updateSummary(); activity('已发起项目包下载；等待确认浏览器下载完成。'); toast('已发起项目包下载，请确认文件已保存在设备上。');
  } catch (error) { report(error); }
}
async function reconnectFolder() { if (store.status.reconnectNeeded && state.project && !ensureCanSwitch()) return; await fileOperation(async () => { const result = await store.reconnect(); if (result?.project && !state.project) { setProject(result.project, true); activity('已重新授权并恢复最近的本地项目。'); } else { notice('权限已恢复，请点击“保存项目”保存当前修改。'); activity('已重新获得当前文件夹写入权限。'); } }); }
async function importProject(file) {
  if (!file || !ensureCanSwitch()) return;
  cancelRequest(false);
  await fileOperation(async () => { const project = await store.importPackage(file); setProject(project, true); activity('项目包已载入当前页面；可继续导出或选择新的保存文件夹。'); });
}

function requireProject(approved = false) {
  if (!state.project) { toast('请先新建或打开项目。'); return false; }
  if (state.fileBusy || state.request) { toast('请等待当前操作完成，或取消正在生成的请求。'); return false; }
  if (approved && (state.project.script.status !== 'approved' || !state.project.script.text.trim())) { toast('请先在剧本创作中确认剧本。'); setView('script'); return false; }
  return true;
}
function requireKey(provider) { if (keys[provider].trim()) return true; $('#settings-dialog').showModal(); $(`#${provider}-key`).focus(); toast('请填写本次会话要使用的服务商密钥。'); return false; }
async function relay(path, body, key, timeout, request) {
  const timer = setTimeout(() => { request.timedOut = true; request.controller.abort(); }, timeout);
  try {
    const serializedBody = JSON.stringify(body);
    if (Object.values(keys).some(secret => secret && serializedBody.includes(secret))) throw new Error('创作上下文中包含本次会话密钥，请先删除。');
    const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` }, body: serializedBody, signal: request.controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error' });
    if (!response.ok) {
      const errors = { 400: '请求格式或模型配置不受支持，请检查模型设置。', 401: '密钥缺失、无效，或服务商拒绝认证。请检查密钥。', 403: '服务商或中转服务拒绝了此请求。', 404: '模型或 API 路由不可用。请检查模型 ID 与部署配置。', 413: '请求或返回数据超过大小限制。请缩短项目内容或降低图片尺寸。', 429: '请求过于频繁或服务商额度不足，请稍后重试并检查账户额度。', 502: '服务商暂时不可用或返回结果不符合要求。原内容已保留。', 503: '模型服务暂时不可用，请稍后重试。', 504: '服务商请求超时，原内容已保留。' };
      throw new Error(errors[response.status] || `请求未成功（HTTP ${response.status}），原内容已保留。`);
    }
    const max = path === '/api/images' ? 12 * 1024 * 1024 : 2 * 1024 * 1024;
    if (Number(response.headers.get('content-length')) > max) throw new Error('返回内容超过安全大小限制。');
    if (!response.body) throw new Error('服务商返回了空结果。');
    const reader = response.body.getReader(); let received = 0; const chunks = [];
    for (;;) { const { done, value } = await reader.read(); if (done) break; received += value.byteLength; if (received > max) { await reader.cancel(); throw new Error('返回内容超过安全大小限制。'); } chunks.push(value); }
    const bytes = new Uint8Array(received); let offset = 0; for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    try { return JSON.parse(new TextDecoder().decode(bytes)); } catch { throw new Error('服务商返回了无效 JSON，原内容已保留。'); }
  } finally { clearTimeout(timer); }
}
async function generate(kind, targetId = null) {
  if (!requireProject(kind === 'assets' || kind === 'shots')) return;
  const image = kind === 'image', provider = image ? $('#image-provider').value : 'deepseek';
  if (kind === 'script' && state.project.script.status === 'approved') { toast('请先解锁已确认的剧本。'); return; }
  const target = image ? state.project.assets.find(asset => asset.id === targetId) : null;
  if (image && !target?.prompt?.trim()) { toast('请先为资产填写或生成图片提示词。'); return; }
  if (!requireKey(provider)) return;
  const model = image ? $(`#${provider}-model`).value.trim() : $('#deepseek-model').value;
  if (!model) { $('#settings-dialog').showModal(); $(`#${provider}-model`).focus(); toast('请填写服务商账户中可用的图片模型 ID。'); return; }
  let project;
  try { project = guardProject(state.project); } catch (error) { report(error); return; }
  const revision = state.revision;
  const request = { controller: new AbortController(), timedOut: false, projectId: project.id, revision };
  state.request = request; $('#busy-label').textContent = `正在生成${operationNames[kind]}…`; updateControls(); notice();
  const started = performance.now(); activity(`开始生成${operationNames[kind]}。仅在审核应用后修改项目。`);
  try {
    const body = image ? { provider, model, prompt: `${target.prompt}\n项目视觉风格：${project.settings.visualStyle || '保持既定风格'}。请只生成当前资产的参考图。`, size: provider === 'seedream' ? '2048x2048' : '1024x1024', ...(provider === 'openai' ? { quality: $('#image-quality').value } : {}) }
      : { provider, model, messages: buildMessages(kind, project, $('#script-instructions').value, targetId, Number($('#storyboard-frames').value)), max_tokens: 8000, response_format: { type: 'json_object' } };
    const response = await relay(image ? '/api/images' : '/api/chat', body, keys[provider], image ? 190000 : 130000, request);
    if (state.request !== request || !state.project || state.project.id !== project.id || state.revision !== revision) { if (state.request === request) activity('生成期间项目发生变化，已丢弃过期结果。原内容未变。'); return; }
    const data = image ? { image: imageFromResponse(response) } : parseProposal(response?.choices?.[0]?.message?.content, kind, project);
    const proposal = createProposal(project, revision, kind, data, targetId);
    // Validate the complete proposed state before displaying an apply button.
    guardProject(applyProposal(project, revision, proposal));
    state.proposal = proposal;
    const usage = response?.usage; const tokens = Number.isFinite(usage?.total_tokens) ? `，${usage.total_tokens} tokens` : '';
    activity(`${operationNames[kind]}建议已生成，用时 ${Math.round((performance.now() - started) / 1000)} 秒${tokens}，等待审核。`);
    showProposal();
  } catch (error) {
    if (state.request !== request) return;
    if (error?.name === 'AbortError') report(new Error(request.timedOut ? '等待服务商超时。原内容已保留；服务商可能仍会计费。' : '请求已取消，原内容已保留。'));
    else if (image) report(new Error('图片生成未完成或返回数据无效。原参考图已保留；请求可能已计费，请检查服务商记录后再决定是否重试。'));
    else report(error instanceof TypeError ? new Error('网络请求失败。请检查连接及 Worker 部署。原内容已保留。') : error);
  } finally { if (state.request === request) { state.request = null; updateControls(); } }
}
function showProposal() {
  const proposal = state.proposal; if (!proposal) return;
  $('#proposal-title').textContent = `审核${operationNames[proposal.kind]}`;
  const descriptions = { script: '应用后替换当前草稿。现有资产与分镜会保留，请在剧本变更后重新检查一致性。', assets: '追加新资产；同类型同名资产会保留原设定与参考图。', shots: '应用后替换整个现有镜头列表。请先检查时长、对白与资产引用。', assetPrompt: '只替换此资产的图片提示词。参考图会保留。', shotPrompts: '只替换此镜头的首帧与故事板提示词。', image: '应用后替换此资产当前参考图。保存前，请确认这就是你需要的版本。' };
  $('#proposal-description').textContent = descriptions[proposal.kind]; const content = $('#proposal-content'); content.replaceChildren();
  const section = (title, text) => { const element = document.createElement('section'); element.className = 'proposal-section'; const heading = document.createElement('h3'); heading.textContent = title; const paragraph = document.createElement('p'); paragraph.textContent = text; element.append(heading, paragraph); content.append(element); };
  if (proposal.kind === 'image') { const image = document.createElement('img'); image.src = proposal.data.image.dataUrl; image.alt = '待审核的资产参考图'; content.append(image); }
  else if (proposal.kind === 'script') section('剧本草稿', proposal.data.text);
  else if (proposal.kind === 'assets') proposal.data.assets.forEach(asset => section(`${types[asset.type]} · ${asset.name}`, `${asset.description}\n\n图片提示词\n${asset.prompt}`));
  else if (proposal.kind === 'shots') proposal.data.shots.forEach((shot, index) => section(`镜头 ${index + 1} · ${shot.duration} 秒`, `${shot.description}\n\n对白：${shot.dialogue || '无'}\n资产：${shot.assetIds.map(id => state.project.assets.find(asset => asset.id === id)?.name).join('、') || '无'}\n\n首帧：${shot.initialFramePrompt}\n\n故事板：${shot.storyboardPrompt}`));
  else if (proposal.kind === 'assetPrompt') section('图片提示词', proposal.data.prompt);
  else { section('首帧提示词', proposal.data.initialFramePrompt); section('故事板提示词', proposal.data.storyboardPrompt); }
  $('#proposal-dialog').showModal();
}
function applyCurrentProposal() {
  if (!state.proposal || !state.project) return;
  try {
    const kind = state.proposal.kind; const next = guardProject(applyProposal(state.project, state.revision, state.proposal));
    state.project = normalizeForUI(next); state.proposal = null; $('#proposal-dialog').close(); touch(); render(); activity(`已应用${operationNames[kind]}。请保存项目。`); toast('建议已应用，请保存项目。');
  } catch (error) { report(error); }
}
function discardProposal() { state.proposal = null; $('#proposal-dialog').close(); activity('已放弃生成建议，项目内容未变。'); }
function clearKeys() { for (const provider of Object.keys(keys)) { keys[provider] = ''; $(`#${provider}-key`).value = ''; } cancelRequest(false); updateControls(); }

$$('[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
$$('.dialog-close').forEach(button => button.addEventListener('click', () => { const dialog = button.closest('dialog'); if (dialog.id === 'proposal-dialog') discardProposal(); else dialog.close(); }));
$('#proposal-dialog').addEventListener('cancel', event => { event.preventDefault(); discardProposal(); });
$('#settings-open').addEventListener('click', () => $('#settings-dialog').showModal());
$('#clear-keys').addEventListener('click', () => { clearKeys(); toast('本页全部密钥已清除。'); });
$$('[data-key]').forEach(input => { input.value = ''; input.addEventListener('input', () => { keys[input.dataset.key] = input.value.trim(); updateControls(); }); });
$('#new-project').addEventListener('click', newDialog); $('#new-project-secondary').addEventListener('click', newDialog);
$('#new-form').addEventListener('submit', event => { event.preventDefault(); newProject(true); }); $('#new-package').addEventListener('click', () => newProject(false));
$('#open-folder').addEventListener('click', openFolder); $('#welcome-open').addEventListener('click', openFolder);
$('#attach-folder').addEventListener('click', attachFolder); $('#save-project').addEventListener('click', saveProject); $('#export-project').addEventListener('click', exportProject);
$$('.import-trigger').forEach(button => button.addEventListener('click', () => { if (!state.fileBusy) $('#import-file').click(); }));
$('#import-file').addEventListener('change', event => { const file = event.target.files[0]; event.target.value = ''; importProject(file); });
for (const [selector, field] of [['#project-name', 'name'], ['#project-description', 'description']]) $(selector).addEventListener('input', event => { if (state.project) { state.project[field] = event.target.value; touch(); } });
for (const [selector, field] of [['#project-style', 'visualStyle'], ['#project-ratio', 'aspectRatio']]) $(selector).addEventListener('input', event => { if (state.project) { state.project.settings[field] = event.target.value; touch(); } });
$('#script-text').addEventListener('input', event => { if (state.project && state.project.script.status !== 'approved') { state.project.script.text = event.target.value; touch(); } });
$('#approve-script').addEventListener('click', () => { if (!state.project?.script.text.trim()) { toast('请先写下或应用剧本内容。'); return; } state.project.script.status = 'approved'; touch(); activity('剧本已确认并锁定，可继续提取资产和拆分镜头。'); });
$('#unlock-script').addEventListener('click', () => { if (state.project && confirm('解锁后可以修改剧本。现有资产与分镜会保留，需要你重新检查与新剧本的一致性。')) { state.project.script.status = 'draft'; touch(); activity('剧本已解锁，后续资产与分镜请重新检查一致性。'); } });
$('#generate-script').addEventListener('click', () => generate('script')); $('#extract-assets').addEventListener('click', () => generate('assets')); $('#generate-shots').addEventListener('click', () => generate('shots'));
$('#apply-proposal').addEventListener('click', applyCurrentProposal); $('#discard-proposal').addEventListener('click', discardProposal); $('#cancel-request').addEventListener('click', () => cancelRequest());
$('#add-asset').addEventListener('click', () => { if (!state.project || state.fileBusy) return; if (state.project.assets.length >= STORE_LIMITS.assets) { toast('资产数量已达到 100 个上限。'); return; } state.project.assets.push({ id: uid(), name: `新资产 ${state.project.assets.length + 1}`, type: 'character', description: '', prompt: '' }); touch(); renderAssets(); renderShots(); updateControls(); });
$('#asset-list').addEventListener('input', event => { const card = event.target.closest('[data-id]'), field = event.target.dataset.field; if (!card || !field || !state.project) return; const asset = state.project.assets.find(item => item.id === card.dataset.id); if (asset && ['name', 'type', 'description', 'prompt'].includes(field)) { asset[field] = event.target.value; touch(); if (field === 'type') card.querySelector('.badge').textContent = types[asset.type]; } });
$('#asset-list').addEventListener('change', event => { if (event.target.dataset.field === 'name') renderShots(); });
$('#asset-list').addEventListener('click', event => { const button = event.target.closest('[data-action]'), card = button?.closest('[data-id]'); if (!button || !card || !state.project || state.fileBusy) return; const assetId = card.dataset.id;
  if (['assetPrompt', 'image'].includes(button.dataset.action)) generate(button.dataset.action, assetId);
  else if (button.dataset.action === 'importImage') { if (state.request) return; state.imageTarget = { id: assetId, projectId: state.project.id, revision: state.revision }; $('#image-file').click(); }
  else if (button.dataset.action === 'deleteAsset' && confirm('删除此资产和它的当前参考图，并从所有镜头中移除引用？保存前可通过重新打开原项目放弃未保存改动。')) { state.project.assets = state.project.assets.filter(asset => asset.id !== assetId); state.project.shots.forEach(shot => { shot.assetIds = shot.assetIds.filter(id => id !== assetId); }); touch(); renderAssets(); renderShots(); updateControls(); }
});
$('#image-file').addEventListener('change', async event => {
  const file = event.target.files[0], target = state.imageTarget; event.target.value = ''; state.imageTarget = null; if (!file || !target || !state.project) return;
  try {
    if (file.size > LIMITS.imageBytes || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('请选择不超过 8 MiB 的 PNG、JPEG 或 WebP 图片。');
    const dataUrl = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('图片读取失败，原图已保留。')); reader.readAsDataURL(file); });
    if (!state.project || state.project.id !== target.projectId || state.revision !== target.revision) throw new Error('项目已改变，取消此次图片导入。');
    const next = structuredClone(state.project), asset = next.assets.find(item => item.id === target.id); if (!asset) return;
    asset.image = { mimeType: file.type, dataUrl }; guardProject(next);
    state.proposal = createProposal(state.project, state.revision, 'image', { image: asset.image }, target.id); showProposal(); activity('本地参考图已载入，等待审核；原参考图尚未改变。');
  } catch (error) { report(error); }
});
$('#add-shot').addEventListener('click', () => { if (!state.project || state.fileBusy) return; if (state.project.shots.length >= LIMITS.shots) { toast('镜头数量已达到当前工作台 300 个上限。'); return; } state.project.shots.push({ id: uid(), number: state.project.shots.length + 1, duration: 5, description: '', dialogue: '', assetIds: [], initialFramePrompt: '', storyboardPrompt: '' }); touch(); renderShots(); updateControls(); });
$('#shot-list').addEventListener('input', event => { const card = event.target.closest('[data-id]'); if (!card || !state.project) return; const shot = state.project.shots.find(item => item.id === card.dataset.id); if (!shot) return;
  if (event.target.dataset.assetRef) { const assetId = event.target.dataset.assetRef; shot.assetIds = event.target.checked ? [...new Set([...shot.assetIds, assetId])] : shot.assetIds.filter(id => id !== assetId); touch(); }
  else { const field = event.target.dataset.field; if (!['duration', 'description', 'dialogue', 'initialFramePrompt', 'storyboardPrompt'].includes(field)) return; if (field === 'duration') { if (!event.target.checkValidity()) return; shot.duration = Number(event.target.value); } else shot[field] = event.target.value; touch(); }
});
$('#shot-list').addEventListener('change', event => { if (event.target.dataset.field === 'duration' && !event.target.checkValidity()) { toast('镜头时长需在 0.1 到 120 秒之间。'); const shot = state.project.shots.find(item => item.id === event.target.closest('[data-id]').dataset.id); event.target.value = shot.duration; } });
$('#shot-list').addEventListener('click', event => { const button = event.target.closest('[data-action]'), card = button?.closest('[data-id]'); if (!button || !card || !state.project || state.fileBusy) return;
  if (button.dataset.action === 'shotPrompts') generate('shotPrompts', card.dataset.id);
  else if (button.dataset.action === 'deleteShot' && confirm('删除这个镜头？其余镜头会按当前顺序重新编号。')) { state.project.shots = state.project.shots.filter(shot => shot.id !== card.dataset.id).map((shot, index) => ({ ...shot, number: index + 1 })); touch(); renderShots(); updateControls(); }
});
$('#clear-activity').addEventListener('click', () => { state.activity = []; renderActivity(); });
window.addEventListener('beforeunload', event => { if (dirty() || state.request || state.fileBusy) { event.preventDefault(); event.returnValue = ''; } });
window.addEventListener('pagehide', () => clearKeys());
window.addEventListener('pageshow', event => { if (event.persisted) { clearKeys(); toast('页面恢复后密钥已清除，请按需重新填写。'); } });
if (!store.supported) $('#support-note').textContent = '此浏览器不支持直接访问本地文件夹。新建时选择“以项目包开始”，并在离开前导出项目包；稍后导入即可继续创作。';
render(); renderActivity();
if (typeof store.restore === 'function') {
  fileOperation(async () => {
    const result = await store.restore();
    if (result?.project && !state.project) { setProject(result.project, true); activity('已从你之前授权的本地文件夹恢复项目。'); }
    else if (result?.reconnectNeeded) notice('浏览器已记住上次选择的文件夹。请点击“重新授权”以恢复项目，或打开另一个文件夹。', false, true);
    if (store.status.rememberWarning) activity('此浏览器无法记住文件夹授权。刷新后请使用“打开文件夹”重新选择，项目文件不受影响。');
  });
}
