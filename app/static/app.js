const API = "/api/v1";

const state = {
  projects: [], project: null, scripts: [], script: null, assets: [], scenes: [],
  prompts: new Map(), step: "project", scriptMode: "manual",
  assetSelectionMode: false, selectedAssetIds: new Set(),
  imageProviders: [], assetImages: new Map(), imagePollTimer: null,
  metrics: null, returnStep: "project",
  promptMode: "initial_frame", promptFrameCount: null, promptOverrides: new Map(),
  chatThreads: [], chatThread: null, chatTarget: null,
  agentSessions: [], agentSession: null, retrievalStatus: null, crewStatus: null, crewPreflight: null, showMasterAgent: false,
  archivedAgentSessions: [], showArchivedAgentSessions: false, expandedAgentMessages: new Set(),
  agentThinking: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value = "") => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const formatPromptPreview = (value = "") => String(value)
  .replace(/\r\n?/g, "\n")
  .replace(/([。！？；])/g, "$1\n")
  .replace(/([,，])\s*/g, "$1\n")
  .replace(/\n{3,}/g, "\n\n")
  .trim();
const statusLabels = { DRAFT_SCRIPT: "项目草稿", SCRIPT_REVIEW: "剧本待确认", SCRIPT_APPROVED: "剧本已确认", ASSET_REVIEW: "资产整理中", SHOT_LIST_REVIEW: "分镜设计中", PROMPT_REVIEW: "提示词制作中", COMPLETED: "制作完成" };
const assetTypeLabels = { character: "人物", location: "场景", prop: "道具" };
const agentStageLabels = {
  project_script: "创建项目与剧本",
  assets: "提取资产与提示词",
  shots: "拆分分镜",
  prompts: "生成镜头提示词",
  images: "图片批次建议",
};
const agentTaskStatusLabels = {
  pending: "未开始",
  awaiting_approval: "等待确认",
  running: "执行中",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};
const agentSessionStatusLabels = {
  clarifying: "澄清需求中",
  plan_ready: "可生成计划",
  awaiting_approval: "等待确认",
  awaiting_stage_approval: "等待阶段确认",
  completed: "已完成",
  cancelled: "已取消",
};
const automaticFrameCount = duration => duration <= 3 ? 4 : duration <= 6 ? 6 : 9;

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { const data = await response.json(); message = data.detail || message; } catch (_) {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toast-region").append(node);
  setTimeout(() => node.remove(), 3500);
}

function loading(show, title = "AI 正在工作", copy = "这可能需要一点时间，请不要关闭页面") {
  $("#loading-title").textContent = title;
  $("#loading-copy").textContent = copy;
  $("#loading-layer").hidden = !show;
}

async function setStep(step) {
  if (step === "metrics" && state.step !== "metrics") state.returnStep = state.step;
  state.step = step;
  const order = ["project", "script", "assets", "shots", "prompts"];
  const current = order.indexOf(step);
  $$(".workflow-step").forEach((node, index) => {
    node.classList.toggle("active", index === current);
    node.classList.toggle("done", index < current);
  });
  if (step === "prompts" && state.scenes.length) {
    loading(true, "正在读取提示词", "正在同步全部镜头的提示词历史");
    try {
      await loadAllPrompts();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      loading(false);
    }
  }
  if (step === "metrics" && state.project) {
    try {
      state.metrics = await request(`/projects/${state.project.id}/agent-metrics`);
    } catch (error) {
      toast(error.message, "error");
    }
  }
  render();
}

function renderSidebar() {
  $("#project-count").textContent = state.projects.length;
  $("#project-list").innerHTML = state.projects.length ? state.projects.map(project => `
    <button type="button" class="project-item ${state.project?.id === project.id ? "active" : ""}" data-project-id="${project.id}">
      <strong>${esc(project.name)}</strong><small>${esc(statusLabels[project.status] || project.status)} · ${esc(project.aspect_ratio)}</small>
    </button>`).join("") : `<div class="prompt-empty">还没有项目，先创建一部影片。</div>`;
}

function renderHeader() {
  $("#current-project-title").textContent = state.project?.name || "选择一个项目";
  $("#project-meta").innerHTML = state.project ? `<span class="meta-chip">${esc(state.project.aspect_ratio)}</span><span class="meta-chip">${esc(state.project.language)}</span><span class="meta-chip">${esc(statusLabels[state.project.status] || state.project.status)}</span>` : "";
  $("[data-action='open-metrics']").disabled = !state.project;
}

function renderWorkflow() {
  const approvedScript = state.scripts.some(script => script.is_approved);
  const availability = {
    project: true,
    script: Boolean(state.project),
    assets: approvedScript,
    shots: approvedScript,
    prompts: state.scenes.length > 0,
  };
  $$(".workflow-step").forEach(node => {
    node.disabled = !availability[node.dataset.step];
    node.title = node.disabled ? {
      script: "请先创建或选择项目",
      assets: "请先确认一个剧本版本",
      shots: "请先确认一个剧本版本",
      prompts: "请先生成分镜",
    }[node.dataset.step] : "";
  });
}

function render() {
  renderSidebar(); renderHeader(); renderWorkflow();
  if (!state.project || state.showMasterAgent) {
    renderMasterAgent();
    $("#chat-launcher").hidden = true;
    return;
  }
  const views = { project: renderProject, script: renderScript, assets: renderAssets, shots: renderShots, prompts: renderPrompts, metrics: renderMetrics };
  views[state.step]();
  const chatAvailable = state.project && ["script", "assets", "shots", "prompts"].includes(state.step);
  $("#chat-launcher").textContent = `全局 AI 修改 · ${chatPage()}`;
  $("#chat-launcher").hidden = !chatAvailable || $("#chat-drawer").classList.contains("open");
}

function renderMetrics() {
  if (!state.project) return renderProject();
  const metrics = state.metrics;
  if (!metrics) {
    $("#content").innerHTML = `<div class="empty-state"><div><h2>正在读取 Agent 指标</h2></div></div>`;
    return;
  }
  const statusLabel = { passed: "通过", validation_failed: "校验失败", request_failed: "请求失败" };
  const operationLabel = { script_generation: "剧本生成", asset_extraction: "资产提取", asset_prompt_generation: "资产提示词", storyboard_generation: "分镜生成", shot_prompt_generation: "镜头提示词", image_generation: "图片生成", chat_edit_generation: "AI 修改助手" };
  $("#content").innerHTML = `
    <div class="section-head"><div><span class="eyebrow">AGENT OBSERVATORY</span><h2>运行监控与 Agent 评估</h2><p>独立监控 DeepSeek、OpenAI、Seedream 等外部模型接口的调用与质量结果。</p></div><div class="actions"><button class="button" data-action="close-metrics">← 返回制作</button><button class="button primary" data-action="refresh-metrics">刷新指标</button></div></div>
    <div class="metrics-grid">
      <article class="panel metric-card"><span>总生成次数</span><strong>${metrics.total_runs}</strong><small>包含成功与失败运行</small></article>
      <article class="panel metric-card"><span>总体通过率</span><strong>${metrics.pass_rate}%</strong><small>${metrics.passed_runs} 通过 / ${metrics.failed_runs} 失败</small></article>
      <article class="panel metric-card"><span>重复生成次数</span><strong>${metrics.regeneration_count}</strong><small>同一剧本首次生成之后的运行</small></article>
      <article class="panel metric-card"><span>校验失败</span><strong>${metrics.validation_failed_count}</strong><small>DeepSeek 返回有效，但未达质量门槛</small></article>
      <article class="panel metric-card"><span>请求失败</span><strong>${metrics.request_failed_count}</strong><small>网络、接口或结构解析失败</small></article>
      <article class="panel metric-card"><span>累计 Token</span><strong>${metrics.total_input_tokens + metrics.total_output_tokens}</strong><small>输入 ${metrics.total_input_tokens} / 输出 ${metrics.total_output_tokens}</small></article>
      <article class="panel metric-card"><span>平均延迟</span><strong>${metrics.average_latency_ms}ms</strong><small>仅统计供应商返回了耗时的运行</small></article>
    </div>
    <div class="metrics-layout">
      <section class="panel metrics-panel"><div class="panel-heading"><h3>各项校验通过率</h3></div>${metrics.validations.length ? `<div class="validation-list">${metrics.validations.map(item => `<div class="validation-row"><div><strong>${esc(item.label)}</strong><small>${item.passed}/${item.total} 次通过</small></div><div class="metric-progress"><i style="width:${item.pass_rate}%"></i></div><b>${item.pass_rate}%</b></div>`).join("")}</div>` : `<p class="prompt-empty">生成一次分镜后，这里会出现编号、覆盖率、引用匹配和重复镜头指标。</p>`}</section>
      <section class="panel metrics-panel"><div class="panel-heading"><h3>最近运行</h3><small>点击任意记录查看请求与校验详情</small></div>${metrics.recent_runs.length ? `<div class="run-list">${metrics.recent_runs.map(run => `<article class="run-row ${run.status}" data-agent-run="${run.id}" role="button" tabindex="0"><div><strong>${operationLabel[run.operation] || esc(run.operation)} · ${statusLabel[run.status] || esc(run.status)}${run.is_regeneration ? " · 重复生成" : ""}</strong><small>${new Date(run.created_at).toLocaleString("zh-CN")} · ${esc(run.model || "unknown model")}</small></div><span>${run.error_message ? esc(run.error_message) : `耗时 ${run.latency_ms ?? "-"}ms · Token ${(run.input_tokens || 0) + (run.output_tokens || 0)}`} · 查看详情 →</span></article>`).join("")}</div>` : `<p class="prompt-empty">尚无 Agent 运行记录。</p>`}</section>
    </div>`;
}

async function openAgentRunDetail(runId) {
  const run = await request(`/agent-runs/${runId}`);
  const checks = run.validation_results || [];
  $("#agent-run-detail").innerHTML = `
    <div class="dialog-head"><div><span class="eyebrow">REQUEST TRACE</span><h2>${esc(run.operation)} · ${esc(run.status)}</h2></div><button class="icon-button" type="button" data-close-dialog aria-label="关闭">×</button></div>
    <div class="run-detail-meta"><span>供应商 <strong>${esc(run.provider)}</strong></span><span>模型 <strong>${esc(run.model || "-")}</strong></span><span>耗时 <strong>${run.latency_ms ?? "-"}ms</strong></span><span>尝试 <strong>${run.attempt_count}</strong></span><span>Token <strong>${(run.input_tokens || 0) + (run.output_tokens || 0)}</strong></span><span>Request ID <strong>${esc(run.request_id || "-")}</strong></span></div>
    <section class="run-detail-section"><h3>校验结果</h3>${checks.length ? `<div class="run-check-list">${checks.map(check => `<article class="run-check ${check.passed ? "passed" : "failed"}"><strong>${check.passed ? "✓" : "×"} ${esc(check.label)}</strong><code>${esc(check.key)}</code><p>${esc(check.detail)}</p><small>实际值：${esc(check.value)} · 门槛：${esc(check.threshold ?? "-")}</small></article>`).join("")}</div>` : `<p class="prompt-empty">本次调用没有独立业务校验项；状态由接口请求结果决定。</p>`}</section>
    ${run.error_message ? `<section class="run-detail-section error-block"><h3>错误阶段：${esc(run.error_type || "unknown")}</h3><p>${esc(run.error_message)}</p></section>` : ""}
    <section class="run-detail-section"><h3>System Prompt</h3><pre>${esc(run.system_prompt || "（该接口没有 system prompt）")}</pre></section>
    <section class="run-detail-section"><h3>User Prompt / 请求正文</h3><pre>${esc(run.user_prompt || "（未记录请求正文）")}</pre></section>
    <section class="run-detail-section"><h3>原始返回</h3><pre>${esc(run.raw_response || "（接口未返回正文）")}</pre></section>
    <div class="dialog-actions"><button class="button" type="button" data-close-dialog>关闭</button></div>`;
  $("#agent-run-dialog").showModal();
}

function renderProject() {
  if (!state.project) {
    $("#content").innerHTML = `<div class="empty-state"><div><div class="empty-state-icon">◉</div><h2>从一个故事开始</h2><p>创建影片项目，确定画幅和视觉方向，再让 AI 帮你完成剧本与分镜。</p><button class="button primary" data-action="new-project">创建第一个项目</button></div></div>`;
    return;
  }
  const p = state.project;
  $("#content").innerHTML = `
    <div class="section-head"><div><span class="eyebrow">PRODUCTION OVERVIEW</span><h2>项目设定</h2><p>这是后续剧本、分镜和提示词共用的创作基准。</p></div><div class="actions"><button class="button" data-action="open-master-agent">总控智能体</button><button class="button" data-action="edit-project">编辑项目设定</button><button class="button primary" data-step-next="script">进入剧本创作 →</button></div></div>
    <div class="overview-grid">
      <article class="panel project-card"><span class="tag">${esc(statusLabels[p.status] || p.status)}</span><h3>${esc(p.name)}</h3><p>${esc(p.description || "尚未填写故事简介。你仍然可以在剧本阶段直接开始创作。")}</p></article>
      <aside class="panel details-card"><span class="eyebrow">CREATIVE PROFILE</span><div class="details-list"><div class="detail-row"><span>世界与地域设定</span><strong>${esc(p.world_setting || "尚未设定")}</strong></div><div class="detail-row"><span>视觉风格</span><strong>${esc(p.visual_style)}</strong></div><div class="detail-row"><span>输出画幅</span><strong>${esc(p.aspect_ratio)}</strong></div><div class="detail-row"><span>创作语言</span><strong>${esc(p.language)}</strong></div><div class="detail-row"><span>当前阶段</span><strong>${esc(statusLabels[p.status] || p.status)}</strong></div></div></aside>
    </div>`;
}

function getLatestAssistantMessage(messages = []) {
    return [...messages].reverse().find(message => message.role === "assistant");
  }

function shouldCollapseAgentMessage(message) {
  return message.role === "user" && (message.content.length > 600 || message.content.split(/\r?\n/).length > 8);
}

function agentMessagePreview(content) {
  const lines = content.split(/\r?\n/);
  const previewLines = lines.slice(0, 4).join("\n");
  return previewLines.length > 300 ? `${previewLines.slice(0, 300)}…` : previewLines;
}

function renderAgentMessage(message) {
  const collapsible = shouldCollapseAgentMessage(message);
  const expanded = state.expandedAgentMessages.has(message.id);
  const content = collapsible && !expanded ? agentMessagePreview(message.content) : message.content;
  return `<div class="master-message ${message.role} ${collapsible && !expanded ? "collapsed" : ""}">
    <small>${message.role === "user" ? "你" : "智能体"}</small>
    <div class="master-message-content">${esc(content)}</div>
    ${collapsible ? `<button class="message-toggle" type="button" data-toggle-agent-message="${message.id}">${expanded ? "收起" : "展开全文"}</button>` : ""}
  </div>`;
}

function renderAgentReplyOptions(message) {
  const options = message?.metadata_json?.reply_options || [];
  if (!options.length) return "";
  return `<div class="agent-reply-options" aria-label="建议回复">${options.map((option, index) => `
    <button class="agent-option-button" type="button" data-agent-option="${index}">
      <strong>${esc(option.label)}</strong>
      ${option.description ? `<span>${esc(option.description)}</span>` : ""}
      ${option.fact_chips?.length ? `<div class="agent-option-facts">${option.fact_chips.map(chip => `<em>${esc(chip.label)}：${esc(chip.value)}</em>`).join("")}</div>` : ""}
    </button>`).join("")}</div>`;
}

async function sendAgentMessage(content, facts = {}) {
    if (!content || !state.agentSession) return;
    state.agentThinking = true;
    renderMasterAgent();
    try {
      state.agentSession = await request(`/agent/sessions/${state.agentSession.id}/messages`, {
        method: "POST", body: JSON.stringify({ content, facts }),
      });
      await refreshAgentSessionLists();
    } finally {
      state.agentThinking = false;
      renderMasterAgent();
    }
  }

async function handleAgentOption(button) {
    const latest = getLatestAssistantMessage(state.agentSession?.messages || []);
    const option = latest?.metadata_json?.reply_options?.[Number(button.dataset.agentOption)];
    if (!option) return;
    if (option.action === "generate_plan") {
      await generateAgentPlan();
      return;
    }
    if (option.action === "approve_plan") {
      await approveAgentPlan(option.plan_id);
      return;
    }
    if (option.action === "approve_stage") {
      await approveAgentStage(option.plan_id, option.stage);
      return;
    }
    if (option.action === "research_web") {
      await researchAgentSession(option.query || option.content || option.label);
      return;
    }
    if (option.action === "defer_plan") {
      toast("计划已保留，可稍后从这个对话继续执行");
      return;
    }
    if (option.custom) {
      const input = $("#master-agent-input");
      if (input) {
        input.placeholder = option.placeholder || "输入你的其它想法……";
        input.focus();
      }
      return;
    }
    await sendAgentMessage(option.content || option.label, option.facts || {});
  }

function renderAgentSessionHistory() {
  const sessions = state.showArchivedAgentSessions ? state.archivedAgentSessions : state.agentSessions;
  return `<section class="panel agent-context-card agent-session-history">
    <div class="agent-history-head"><div><span class="eyebrow">CONVERSATIONS</span><h3>${state.showArchivedAgentSessions ? "已归档对话" : "对话历史"}</h3></div><button class="button" data-action="toggle-agent-archive-view">${state.showArchivedAgentSessions ? "查看当前" : "查看归档"}</button></div>
    ${sessions.length ? `<div class="agent-session-list">${sessions.map(item => `
      <article class="agent-session-item ${state.agentSession?.id === item.id ? "active" : ""}">
        <button type="button" data-agent-session-id="${item.id}">
          <strong>${esc(item.title)}</strong>
          <small>${esc(agentSessionStatusLabels[item.status] || item.status)} · ${item.messages?.length || 0} 条 · ${new Date(item.updated_at).toLocaleString("zh-CN", {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit"})}</small>
        </button>
        <div class="agent-session-actions">
          ${state.showArchivedAgentSessions ? `<button class="button" data-agent-unarchive-session="${item.id}">恢复</button>` : `<button class="button" data-agent-archive-session="${item.id}">归档</button>`}
          <button class="button danger" data-agent-delete-session="${item.id}">删除</button>
        </div>
      </article>`).join("")}</div>` : `<p>${state.showArchivedAgentSessions ? "暂无归档对话。" : "暂无历史对话。"}</p>`}
  </section>`;
}

function renderMasterAgent() {
  const session = state.agentSession;
  const status = state.retrievalStatus;
  const crew = state.crewStatus;
  const retrievalRows = status ? [
    ["Embedding", status.enabled ? "enabled" : "disabled"],
    ["Vector DB", status.qdrant_local ? "Qdrant Local" : "unavailable"],
    ["Model", status.model || "-"],
    ["Device", status.device || "-"],
    ["Index", status.index_status || "-"],
    ["Jobs", `${status.pending_jobs || 0} pending / ${status.failed_jobs || 0} failed`],
    ["DeepSeek Thinking", status.deepseek_thinking_enabled ? `enabled · ${status.deepseek_reasoning_effort || "high"}` : "disabled"],
    ["Web Search", status.web_search_configured ? "configured" : "unavailable"],
  ] : [];
  const crewRows = crew ? [
    ["Framework", crew.framework],
    ["Installed", crew.installed ? "yes" : "no"],
    ["Runtime", crew.active ? "CrewAI active" : `fallback: ${crew.fallback || "orchestrator"}`],
    ["Roles", `${crew.roles?.length || 0}`],
    ["Tasks", `${crew.tasks?.length || 0}`],
  ] : [];
  const plan = session?.plans?.at(-1);
  const activeTask = plan?.tasks?.find(task => ["failed", "running", "awaiting_approval"].includes(task.status));
  const showCheckpoint = activeTask && ["failed", "running", "cancelled"].includes(activeTask.status);
  const memories = session?.memories || [];
  const missing = session?.messages?.at(-1)?.metadata_json?.missing_information || [];
  const messages = session?.messages || [];
  const latestAssistant = getLatestAssistantMessage(messages);
  const thinkingHtml = state.agentThinking ? `<div class="master-message assistant agent-thinking"><small>智能体</small><i></i><i></i><i></i><b>正在思考下一步……</b></div>` : "";
  $("#content").innerHTML = `
    <div class="agent-home">
      <div class="section-head"><div><span class="eyebrow">MASTER PRODUCTION AGENT</span><h2>从一句话开始完成整部影片的前期工作</h2><p>先澄清创作设定、生成计划，再由你逐阶段确认执行。</p></div><div class="actions">${state.project ? `<button class="button" data-action="close-master-agent">返回项目</button>` : ""}<button class="button" data-action="new-agent-session">新会话</button></div></div>
      <div class="agent-home-grid">
        <section class="panel master-agent-panel">
          <header><div><span class="eyebrow">${session ? esc(agentSessionStatusLabels[session.status] || session.status) : "READY"}</span><h3>${esc(session?.title || "总控智能体")}</h3></div><span class="rag-badge ${status?.available ? "ready" : "fallback"}">${status?.available ? "BGE-M3 · QDRANT" : "关键词检索模式"}</span></header>
          <div class="master-agent-messages">${messages.length ? messages.map(renderAgentMessage).join("") : `<div class="agent-welcome"><strong>描述你的故事，或粘贴完整剧本</strong><p>我会识别视觉风格、时间地点和画幅；不确定的信息会先询问，不会直接创建项目。首帧/故事板会在后续按每个镜头复杂度自动判断。</p></div>`}</div>
          ${session ? `<form id="master-agent-form" class="master-agent-form">${state.agentThinking ? "" : renderAgentReplyOptions(latestAssistant)}<textarea id="master-agent-input" required rows="4" placeholder="继续描述故事、回答设定问题，或粘贴剧本；项目标题、风格、地点和画幅都可以直接用对话确认……"></textarea><div class="master-agent-compose"><label class="button">上传 .txt / .md<input id="master-agent-file" type="file" accept=".txt,.md,text/plain,text/markdown" hidden></label><button class="button primary" type="submit">发送</button></div></form>` : `<div class="agent-start"><button class="button primary" data-action="new-agent-session">开始与智能体对话</button></div>`}
        </section>
        <aside class="agent-context-column">
          ${renderAgentSessionHistory()}
          <section class="panel agent-context-card"><span class="eyebrow">CREATIVE MEMORY</span><h3>已确认创作信息</h3>${memories.length ? memories.map(item => `<div class="memory-row"><span>${esc(item.key)}</span><strong>${esc(item.value)}</strong></div>`).join("") : `<p>对话后会在这里形成可追溯的项目记忆。</p>`}${missing.length ? `<div class="missing-box"><strong>仍需确认</strong><p>${missing.map(esc).join("、")}</p></div>` : ""}</section>
          <section class="panel agent-context-card"><span class="eyebrow">WORKFLOW PLAN</span><h3>制作计划</h3>${plan ? `<div class="agent-stage-list">${plan.tasks.map(task => `<div class="agent-stage ${task.status}"><span>${String(task.sequence).padStart(2,"0")}</span><div><strong>${esc(agentStageLabels[task.stage] || task.stage)}</strong><small>${esc(agentTaskStatusLabels[task.status] || task.status)}</small></div></div>`).join("")}</div>${!session.project_id ? `<p>计划已生成，请在对话中选择是否立即执行。</p>` : activeTask ? `<button class="button primary full" data-agent-stage="${activeTask.stage}" data-agent-plan="${plan.id}" ${activeTask.status === "running" ? "disabled" : ""}>${activeTask.status === "failed" ? "重试" : "确认执行"} · ${esc(agentStageLabels[activeTask.stage] || activeTask.stage)}</button>` : ""}` : session && session.status === "plan_ready" ? `<p>可以在对话中选择“生成完整制作计划”。</p>` : `<p>关键信息齐全后才会生成计划。</p>`}</section>
        </aside>
      </div>
    </div>`;
  const messageContainer = $(".master-agent-messages");
  if (messageContainer) {
    if (state.agentThinking) {
      messageContainer.insertAdjacentHTML("beforeend", thinkingHtml);
    }
    messageContainer.scrollTop = messageContainer.scrollHeight;
  }
  if (state.agentThinking) {
    const input = $("#master-agent-input");
    if (input) input.disabled = true;
    $("#master-agent-form button[type='submit']")?.setAttribute("disabled", "disabled");
  }
  const agentContextColumn = $(".agent-context-column");
  if (agentContextColumn && status) {
    agentContextColumn.insertAdjacentHTML("beforeend", `
      <section class="panel agent-context-card"><span class="eyebrow">LOCAL RAG</span><h3>Retrieval Status</h3>
        ${retrievalRows.map(([key, value]) => `<div class="memory-row"><span>${esc(key)}</span><strong>${esc(value)}</strong></div>`).join("")}
        ${!status.available ? `<div class="missing-box"><strong>Fallback active</strong><p>Embedding is unavailable, so the agent will use keyword retrieval.</p></div>` : ""}
      </section>`);
  }
  if (agentContextColumn && crew) {
    const preflight = state.crewPreflight;
    agentContextColumn.insertAdjacentHTML("beforeend", `
      <section class="panel agent-context-card"><span class="eyebrow">AGENT RUNTIME</span><h3>CrewAI Status</h3>
        ${crewRows.map(([key, value]) => `<div class="memory-row"><span>${esc(key)}</span><strong>${esc(value)}</strong></div>`).join("")}
        ${preflight ? `<div class="missing-box"><strong>${preflight.can_exercise_workflow ? "Ready to verify" : "Needs attention"}</strong><p>${esc(preflight.message)}</p></div>` : ""}
        <button class="button full" data-action="crew-preflight">运行预检</button>
      </section>`);
  }
  if (agentContextColumn && showCheckpoint) {
    const checkpoint = activeTask.result_data?.checkpoint || {};
    const recovery = activeTask.result_data?.recovery || {};
    const resumeDisabled = activeTask.status === "completed" || activeTask.status === "cancelled" || recovery.retryable === false;
    const cancelDisabled = activeTask.status === "completed" || activeTask.status === "cancelled";
    agentContextColumn.insertAdjacentHTML("beforeend", `
      <section class="panel agent-context-card"><span class="eyebrow">CHECKPOINT</span><h3>Resume State</h3>
        <div class="memory-row"><span>Agent</span><strong>${esc(checkpoint.agent_key || "-")}</strong></div>
        <div class="memory-row"><span>Status</span><strong>${esc(checkpoint.status || activeTask.status)}</strong></div>
        <div class="memory-row"><span>Last safe step</span><strong>${esc(checkpoint.last_safe_step || "not_started")}</strong></div>
        ${recovery.error_type ? `<div class="missing-box"><strong>${esc(recovery.error_type)}</strong><p>${esc(activeTask.error_message || "Workflow task interrupted.")}</p></div>` : ""}
        <div class="actions"><button class="button" data-agent-resume-task="${activeTask.id}" ${resumeDisabled ? "disabled" : ""}>继续执行</button><button class="button danger" data-agent-cancel-task="${activeTask.id}" ${cancelDisabled ? "disabled" : ""}>取消任务</button></div>
      </section>`);
  }
}

function renderScript() {
  if (!state.project) return renderProject();
  const current = state.script || state.scripts[0];
  const approved = current?.is_approved;
  $("#content").innerHTML = `
    <div class="section-head"><div><span class="eyebrow">SCREENPLAY DESK</span><h2>剧本创作</h2><p>手动撰写，或给 AI 一个故事梗概。确认后才能进入分镜。</p></div><div class="actions"><div class="mode-tabs"><button class="mode-tab ${state.scriptMode === "manual" ? "active" : ""}" data-script-mode="manual">手动撰写</button><button class="mode-tab ${state.scriptMode === "ai" ? "active" : ""}" data-script-mode="ai">AI 生成</button></div>${current ? `<button class="button primary" data-action="approve-script" ${approved ? "disabled" : ""}>${approved ? "✓ 已确认" : "确认此版剧本"}</button>` : ""}</div></div>
    ${state.scriptMode === "ai" ? `<div class="panel ai-brief"><div class="form-grid"><label>剧本标题<input id="ai-title" value="${esc(current?.title || state.project.name)}"></label><label>额外要求<input id="ai-instructions" placeholder="例如：约 3 分钟，减少对白"></label></div><label>故事梗概<textarea id="ai-brief" rows="4" placeholder="描述人物、冲突和你想要的结局……">${esc(state.project.description || "")}</textarea></label><button class="button primary" data-action="generate-script">让 AI 生成剧本</button></div>` : ""}
    <div class="script-layout">
      <section class="panel editor-panel"><div class="editor-toolbar"><input id="script-title" value="${esc(current?.title || state.project.name)}" aria-label="剧本标题"><span>${current ? `VERSION ${current.version}` : "NEW DRAFT"}</span></div><textarea id="script-content" class="script-editor" placeholder="场景一：\n\n在这里开始写你的故事……">${esc(current?.content || "")}</textarea><div class="editor-toolbar"><span id="script-stats">${current?.content?.length || 0} 字</span><button class="button" data-action="save-script">保存为新版本</button></div></section>
      <aside class="panel version-panel"><h3>剧本版本</h3><div class="version-list">${state.scripts.length ? state.scripts.map(s => `<button class="version-item ${current?.id === s.id ? "active" : ""}" data-script-id="${s.id}"><strong>V${s.version} · ${esc(s.title)}</strong>${s.is_approved ? `<span class="approved-badge">✓</span>` : ""}<small>${s.source_type === "ai" ? "AI 生成" : "手动创作"} · ${new Date(s.created_at).toLocaleString("zh-CN", {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit"})}</small></button>`).join("") : `<p class="prompt-empty">保存后会在这里记录版本。</p>`}</div></aside>
    </div>`;
}

function renderAssets() {
  if (!state.project) return renderProject();
  const approved = state.scripts.some(script => script.is_approved);
  const allAssetsSelected = state.assets.length > 0 && state.selectedAssetIds.size === state.assets.length;
  $("#content").innerHTML = `
    <div class="section-head"><div><span class="eyebrow">VISUAL ASSET LIBRARY</span><h2>资产管理</h2><p>统一管理人物、场景和道具。镜头提示词会使用 @名称 引用对应资产。</p></div><div class="actions">${state.assetSelectionMode ? `<button class="button" data-action="select-all-assets">${allAssetsSelected ? "取消全选" : "全选"}</button><button class="button danger" data-action="delete-selected-assets" ${state.selectedAssetIds.size ? "" : "disabled"}>删除选中资产 (${state.selectedAssetIds.size})</button><button class="button" data-action="toggle-asset-selection">退出批量管理</button>` : `<button class="button" data-action="new-asset">＋ 手动添加</button><button class="button" data-action="extract-assets" ${approved ? "" : "disabled"}>AI 从剧本提取</button>${state.assets.length ? `<button class="button" data-action="toggle-asset-selection">批量管理</button><button class="button primary" data-action="generate-all-asset-prompts">一键生成全部资产提示词</button>` : ""}<button class="button" data-step-next="shots">进入分镜设计 →</button>`}</div></div>
    ${!approved ? `<div class="empty-state"><div><div class="empty-state-icon">03</div><h2>需要先确认剧本</h2><p>确认剧本后才能提取其中的人物、场景与关键道具。</p><button class="button primary" data-step-next="script">返回剧本</button></div></div>` : !state.assets.length ? `<div class="empty-state"><div><div class="empty-state-icon">@</div><h2>建立视觉资产库</h2><p>让 AI 从剧本提取资产，或手动添加一个人物、场景或道具。</p><div class="actions"><button class="button" data-action="new-asset">手动添加</button><button class="button primary" data-action="extract-assets">AI 从剧本提取</button></div></div></div>` : ["character", "location", "prop"].map(type => {
      const assets = state.assets.filter(asset => asset.asset_type === type);
      if (!assets.length) return "";
      return `<section class="asset-section"><header class="asset-section-head"><h3>${assetTypeLabels[type]}</h3><span>${assets.length} ASSETS</span></header><div class="asset-grid">${assets.map(asset => `
        <article class="panel asset-card ${state.selectedAssetIds.has(asset.id) ? "selected" : ""}">${state.assetSelectionMode ? `<label class="asset-selector"><input type="checkbox" data-select-asset="${asset.id}" ${state.selectedAssetIds.has(asset.id) ? "checked" : ""}> 选择</label>` : ""}<div class="asset-image">${asset.image_url ? `<img src="${esc(asset.image_url)}" alt="${esc(asset.name)}参考图">` : `<span>${type === "character" ? "人" : type === "location" ? "景" : "物"}</span>`}</div><div class="asset-content"><span class="asset-ref">@${esc(asset.name)}</span><h4>${esc(asset.name)}</h4><p>${esc(asset.description || "尚未填写视觉描述")}</p><div class="asset-prompt-preview">${esc(asset.prompt ? asset.prompt.slice(0, 110) : "尚未生成资产提示词")}${asset.prompt?.length > 110 ? "…" : ""}</div></div>${state.assetSelectionMode ? "" : `<div class="asset-actions"><button class="button" data-edit-asset="${asset.id}">编辑</button><button class="button" data-asset-prompt="${asset.id}">AI 生成提示词</button>${asset.prompt ? `<button class="button primary" data-asset-images="${asset.id}">生成图片</button><button class="button" data-preview-asset-prompt="${asset.id}">完整预览</button><button class="button" data-copy-asset-prompt="${asset.id}">复制提示词</button>` : ""}${asset.image_url ? `<button class="button" data-asset-images="${asset.id}">候选图库</button>` : ""}<label class="button upload-button">上传参考图<input type="file" accept="image/jpeg,image/png,image/webp,image/gif" data-asset-upload="${asset.id}"></label></div>`}</article>`).join("")}</div></section>`;
    }).join("")}`;
}

function renderShots() {
  if (!state.project) return renderProject();
  const approved = state.scripts.find(s => s.is_approved);
  $("#content").innerHTML = `
    <div class="section-head"><div><span class="eyebrow">DIRECTOR'S BOARD</span><h2>分镜设计</h2><p>${state.scenes.length ? `共 ${state.scenes.length} 个场次、${state.scenes.reduce((n,s)=>n+s.shots.length,0)} 个镜头。点击卡片可编辑。` : "确认剧本后，让 AI 将叙事拆解为可绘制的镜头。"}</p></div><div class="actions"><button class="button primary" data-action="generate-shots" ${approved ? "" : "disabled"}>${state.scenes.length ? "重新拆分分镜" : "AI 拆分分镜"}</button><button class="button" data-step-next="prompts" ${state.scenes.length ? "" : "disabled"}>查看提示词 →</button></div></div>
    ${!approved ? `<div class="empty-state"><div><div class="empty-state-icon">02</div><h2>需要先确认剧本</h2><p>返回剧本创作，选择一个满意版本并确认。</p><button class="button primary" data-step-next="script">返回剧本</button></div></div>` : !state.scenes.length ? `<div class="empty-state"><div><div class="empty-state-icon">◫</div><h2>导演板还是空的</h2><p>AI 会分析已确认剧本，并创建场次与镜头卡片。</p><button class="button primary" data-action="generate-shots">开始拆分分镜</button></div></div>` : state.scenes.map(scene => `
      <section class="scene"><header class="scene-head"><span class="scene-number">SCENE ${String(scene.sequence).padStart(2,"0")}</span><h3>${esc(scene.heading)}</h3><small>${esc([scene.location, scene.time_of_day].filter(Boolean).join(" · "))}</small></header><div class="shots-grid">${scene.shots.map(shot => `
        <article class="panel shot-card ${shot.is_locked ? "locked" : ""}" data-shot-id="${shot.id}"><div class="shot-top"><span class="shot-id">SHOT ${String(shot.sequence).padStart(2,"0")}</span><div class="shot-tags"><span>${esc(shot.shot_size)}</span><span>${esc(shot.camera_angle)}</span><span>${Number(shot.duration_seconds || 4).toFixed(1)}s</span></div></div><h4>${esc(shot.subject)}</h4><p>${esc(shot.action)}</p><div class="shot-footer"><span>${esc(shot.camera_motion)}</span><span>${shot.is_locked ? "● 已锁定" : "编辑镜头 ↗"}</span></div></article>`).join("")}</div></section>`).join("")}`;
}

function renderPrompts() {
  if (!state.project) return renderProject();
  const shots = state.scenes.flatMap(scene => scene.shots.map(shot => ({ ...shot, scene })));
  $("#content").innerHTML = `
    <div class="section-head"><div><span class="eyebrow">PROMPT LAB</span><h2>镜头提示词</h2><p>首帧模式用于视频起始画面；故事板模式在一张图中展示镜头从开始到结束的连续帧。</p></div><div class="actions"><button class="button" data-step-next="shots">← 返回分镜</button>${shots.length ? `<button class="button primary" data-action="generate-all-prompts">一键生成全部提示词</button>` : ""}</div></div>
    ${shots.length ? `<section class="panel prompt-mode-panel"><div><span class="eyebrow">PAGE DEFAULT</span><strong>页面默认生成模式</strong></div><div class="mode-tabs"><button class="mode-tab ${state.promptMode === "initial_frame" ? "active" : ""}" data-prompt-default-mode="initial_frame">视频首帧</button><button class="mode-tab ${state.promptMode === "storyboard" ? "active" : ""}" data-prompt-default-mode="storyboard">连续帧故事板</button></div><label class="prompt-frame-select">故事板帧数<select data-prompt-default-frames ${state.promptMode === "storyboard" ? "" : "disabled"}><option value="auto" ${state.promptFrameCount === null ? "selected" : ""}>自动 · 按镜头时长</option><option value="4" ${state.promptFrameCount === 4 ? "selected" : ""}>4 帧 · 2×2</option><option value="6" ${state.promptFrameCount === 6 ? "selected" : ""}>6 帧 · 2×3</option><option value="9" ${state.promptFrameCount === 9 ? "selected" : ""}>9 帧 · 3×3</option></select></label></section>` : ""}
    ${!shots.length ? `<div class="empty-state"><div><div class="empty-state-icon">03</div><h2>还没有可用镜头</h2><p>先完成分镜拆分，再为每个镜头生成提示词。</p><button class="button primary" data-step-next="shots">前往分镜设计</button></div></div>` : `<div class="prompt-list">${shots.map((shot, index) => { const versions = state.prompts.get(shot.id) || []; const prompt = versions[0]; return `
      ${(() => { const metadata = prompt?.prompt_metadata || {}; const promptMode = metadata.mode || "initial_frame"; const frames = metadata.frames || []; const directorOverhead = metadata.director_overhead; const override = state.promptOverrides.get(shot.id) || { mode: "default", frame_count: "inherit" }; const effectiveMode = override.mode === "default" ? state.promptMode : override.mode; const frameControlEnabled = effectiveMode === "storyboard"; const effectiveFrameCount = override.frame_count === "inherit" ? state.promptFrameCount : override.frame_count; const autoFrames = automaticFrameCount(Number(shot.duration_seconds || 4)); return `<article class="panel prompt-card"><div class="prompt-shot"><span class="shot-id">${esc(shot.scene.heading)} · SHOT ${String(shot.sequence).padStart(2,"0")} · ${Number(shot.duration_seconds || 4).toFixed(1)}s</span><h3>${esc(shot.subject)}</h3><p>${esc(shot.action)}</p><div class="shot-prompt-options"><label>本镜头模式<select data-shot-prompt-mode="${shot.id}"><option value="default" ${override.mode === "default" ? "selected" : ""}>跟随页面</option><option value="initial_frame" ${override.mode === "initial_frame" ? "selected" : ""}>视频首帧</option><option value="storyboard" ${override.mode === "storyboard" ? "selected" : ""}>连续帧故事板</option></select></label><label>帧数<select data-shot-frame-count="${shot.id}" ${frameControlEnabled ? "" : "disabled"}><option value="inherit" ${override.frame_count === "inherit" ? "selected" : ""}>跟随页面</option><option value="auto" ${override.frame_count === null ? "selected" : ""}>自动 ${autoFrames}</option><option value="4" ${override.frame_count === 4 ? "selected" : ""}>4</option><option value="6" ${override.frame_count === 6 ? "selected" : ""}>6</option><option value="9" ${override.frame_count === 9 ? "selected" : ""}>9</option></select></label></div></div><div class="prompt-body">${prompt ? `<label>${promptMode === "storyboard" ? `STORYBOARD · ${metadata.frame_count || frames.length} FRAMES · ${esc(metadata.layout || "")} · ${metadata.frame_count_source === "duration_auto" ? "AUTO" : "MANUAL"}` : "INITIAL FRAME"} · V${prompt.version}</label><div class="prompt-text">${esc(prompt.positive_prompt)}</div>${frames.length ? `<details class="storyboard-frames"><summary>查看 ${frames.length} 个连续帧</summary>${frames.map(frame => `<div class="storyboard-frame"><div><strong>FRAME ${frame.index} · ${esc(frame.phase)}</strong><p>${esc(frame.description)}</p></div><button class="button" data-copy-frame="${shot.id}" data-frame-index="${frame.index}">复制此帧</button></div>`).join("")}</details>` : ""}${directorOverhead ? `<details class="storyboard-frames"><summary>导演俯视参考图提示词</summary><div class="storyboard-frame"><div><strong>DIRECTOR OVERHEAD · ${esc(directorOverhead.purpose || "blocking")}</strong><p>${esc(directorOverhead.positive_prompt || "")}</p></div><button class="button" data-copy-director-overhead="${shot.id}">复制俯视图</button></div>${directorOverhead.negative_prompt ? `<div class="storyboard-frame"><div><strong>NEGATIVE</strong><p>${esc(directorOverhead.negative_prompt)}</p></div></div>` : ""}</details>` : ""}${prompt.negative_prompt ? `<label>NEGATIVE PROMPT</label><div class="prompt-text negative">${esc(prompt.negative_prompt)}</div>` : ""}` : `<div class="prompt-empty">这个镜头尚未生成提示词。</div>`}</div><div class="prompt-card-actions">${prompt ? `<button class="button copy-button" data-copy-prompt="${shot.id}">${promptMode === "storyboard" ? "复制整版" : "复制首帧"}</button>${directorOverhead ? `<button class="button" data-copy-director-overhead="${shot.id}">复制俯视图</button>` : ""}` : ""}<button class="button ${prompt ? "" : "primary"}" data-generate-prompt="${shot.id}">${prompt ? "重新生成" : "生成提示词"}</button></div></article>`; })()}`; }).join("")}</div>`}`;
  window.currentPromptShots = shots;
}

async function loadProjects(selectId = null) {
  [state.projects, state.agentSessions, state.archivedAgentSessions, state.retrievalStatus, state.crewStatus] = await Promise.all([
    request("/projects"), request("/agent/sessions"), request("/agent/sessions?archived=true"), request("/agent/retrieval/status"), request("/agent/crew/status"),
  ]);
  state.agentSession = state.agentSessions[0] || null;
  if (selectId) await selectProject(selectId);
  else if (state.projects.length) await selectProject(state.projects[0].id);
  else render();
}

async function refreshAgentSessionLists() {
  [state.agentSessions, state.archivedAgentSessions] = await Promise.all([
    request("/agent/sessions"),
    request("/agent/sessions?archived=true"),
  ]);
}

async function createAgentSession() {
  const session = await request("/agent/sessions", {
    method: "POST", body: JSON.stringify({ title: "新的影片计划" }),
  });
  await refreshAgentSessionLists();
  state.agentSession = session; state.showMasterAgent = true; state.showArchivedAgentSessions = false; render();
}

async function selectAgentSession(sessionId) {
  state.agentSession = await request(`/agent/sessions/${sessionId}`);
  state.showMasterAgent = true;
  render();
}

async function refreshAgentSession() {
  if (!state.agentSession) return;
  state.agentSession = await request(`/agent/sessions/${state.agentSession.id}`);
  await refreshAgentSessionLists();
}

async function archiveAgentSession(sessionId) {
  await request(`/agent/sessions/${sessionId}/archive`, { method: "POST" });
  await refreshAgentSessionLists();
  if (state.agentSession?.id === sessionId) state.agentSession = state.agentSessions[0] || null;
  state.showArchivedAgentSessions = false;
  render();
  toast("对话已归档");
}

async function unarchiveAgentSession(sessionId) {
  const session = await request(`/agent/sessions/${sessionId}/unarchive`, { method: "POST" });
  await refreshAgentSessionLists();
  state.agentSession = session;
  state.showArchivedAgentSessions = false;
  state.showMasterAgent = true;
  render();
  toast("对话已恢复");
}

async function deleteAgentSession(sessionId) {
  if (!window.confirm("确定永久删除这个 Agent 对话吗？该操作不会删除已创建的项目。")) return;
  await request(`/agent/sessions/${sessionId}`, { method: "DELETE" });
  state.expandedAgentMessages.clear();
  await refreshAgentSessionLists();
  if (state.agentSession?.id === sessionId) state.agentSession = state.agentSessions[0] || null;
  if (state.showArchivedAgentSessions && !state.archivedAgentSessions.length) state.showArchivedAgentSessions = false;
  render();
  toast("对话已删除");
}

async function generateAgentPlan() {
  const plan = await request(`/agent/sessions/${state.agentSession.id}/plan`, { method: "POST", body: "{}" });
  await refreshAgentSession(); renderMasterAgent(); toast(`计划 V${plan.version} 已生成`);
}

async function runCrewPreflight() {
  state.crewPreflight = await request("/agent/crew/preflight", { method: "POST" });
  renderMasterAgent();
  toast(state.crewPreflight.can_exercise_workflow ? "CrewAI preflight ready" : "CrewAI preflight needs attention");
}

async function researchAgentSession(query) {
  if (!state.agentSession || !query) return;
  loading(true, "正在联网搜索", "正在检索公开资料并整理为对话摘要");
  try {
    state.agentSession = await request(`/agent/sessions/${state.agentSession.id}/research`, {
      method: "POST", body: JSON.stringify({ query }),
    });
    await refreshAgentSessionLists();
    renderMasterAgent();
    toast("联网搜索完成");
  } finally {
    loading(false);
  }
}

async function approveAgentPlan(planId) {
  loading(true, "正在创建项目与剧本", "完成后将建立本地语义索引");
  await request("/agent/crew/tools/create_project/execute", {
    method: "POST", body: JSON.stringify({ plan_id: planId }),
  });
  await refreshAgentSession();
  state.projects = await request("/projects");
  state.project = state.projects.find(item => item.id === state.agentSession.project_id) || null;
  if (state.project) await selectProject(state.project.id);
  state.showMasterAgent = true; loading(false); render(); toast("项目与剧本已创建");
}

async function approveAgentStage(planId, stage) {
  const stageTool = {
    assets: "extract_assets",
    shots: "generate_storyboard",
    prompts: "generate_shot_prompts",
  }[stage];
  loading(true, `正在执行 ${stage}`, "该阶段可能调用外部模型，请稍候");
  if (stageTool) {
    await request(`/agent/crew/tools/${stageTool}/execute`, {
      method: "POST", body: JSON.stringify({ plan_id: planId }),
    });
  } else {
    await request(`/agent/plans/${planId}/stages/${stage}/approve`, { method: "POST" });
  }
  await refreshAgentSession();
  if (state.agentSession.project_id) await selectProject(state.agentSession.project_id);
  state.showMasterAgent = true; loading(false); render(); toast(`${stage} 阶段已完成`);
}

async function resumeAgentTask(taskId) {
  await request(`/agent/tasks/${taskId}/resume`, { method: "POST" });
  await refreshAgentSession();
  state.showMasterAgent = true;
  render();
  toast("任务已恢复到等待确认状态");
}

async function cancelAgentTask(taskId) {
  if (!window.confirm("确定取消这个工作流任务吗？")) return;
  await request(`/agent/tasks/${taskId}/cancel`, { method: "POST" });
  await refreshAgentSession();
  state.showMasterAgent = true;
  render();
  toast("任务已取消");
}

async function selectProject(id) {
  state.project = state.projects.find(p => p.id === id) || await request(`/projects/${id}`);
  [state.scripts, state.assets, state.scenes] = await Promise.all([
    request(`/projects/${id}/scripts`),
    request(`/projects/${id}/assets`),
    request(`/projects/${id}/scenes`),
  ]);
  state.script = state.scripts[0] || null;
  state.metrics = null;
  state.prompts.clear();
  state.promptOverrides.clear();
  state.assetSelectionMode = false;
  state.selectedAssetIds.clear();
  render();
}

async function loadAllPrompts() {
  const shots = state.scenes.flatMap(s => s.shots);
  await Promise.all(shots.map(async shot => state.prompts.set(shot.id, await request(`/shots/${shot.id}/prompts`))));
}

function openShotDialog(shot) {
  $("#shot-form").innerHTML = `<div class="dialog-head"><div><span class="eyebrow">SHOT EDITOR</span><h2>编辑镜头 ${String(shot.sequence).padStart(2,"0")}</h2></div><button class="icon-button" type="button" data-close-dialog>×</button></div><input type="hidden" name="id" value="${shot.id}"><div class="shot-form-grid">
    <label>主体<input name="subject" value="${esc(shot.subject)}"></label><label>动作<input name="action" value="${esc(shot.action)}"></label><label class="span-2">环境<textarea name="environment" rows="2">${esc(shot.environment)}</textarea></label><label>景别<input name="shot_size" value="${esc(shot.shot_size)}"></label><label>机位角度<input name="camera_angle" value="${esc(shot.camera_angle)}"></label><label>镜头运动<input name="camera_motion" value="${esc(shot.camera_motion)}"></label><label>镜头时长（秒）<input name="duration_seconds" type="number" min="0.5" max="60" step="0.5" value="${Number(shot.duration_seconds || 4)}"></label><label>情绪<input name="emotion" value="${esc(shot.emotion)}"></label><label>光线<input name="lighting" value="${esc(shot.lighting)}"></label><label>对白<input name="dialogue" value="${esc(shot.dialogue)}"></label><label class="span-2">连续性要求<textarea name="continuity" rows="2">${esc(shot.continuity)}</textarea></label><label><input type="checkbox" name="is_locked" ${shot.is_locked ? "checked" : ""}> 锁定这个镜头</label></div><div class="dialog-actions"><button class="button danger" type="button" data-delete-shot="${shot.id}">删除镜头</button><button class="button ghost" type="button" data-close-dialog>取消</button><button class="button primary" type="submit" value="default">保存修改</button></div>`;
  const dialogueInput = $("#shot-form [name=dialogue]");
  const dialogueTextarea = document.createElement("textarea");
  dialogueTextarea.name = "dialogue";
  dialogueTextarea.rows = 4;
  dialogueTextarea.value = shot.dialogue || "";
  dialogueTextarea.setAttribute("aria-label", "对白（支持多句与换行）");
  dialogueInput.replaceWith(dialogueTextarea);
  $("#shot-dialog").showModal();
}

function openProjectDialog(project = null) {
  const form = $("#project-form");
  form.dataset.projectId = project?.id || "";
  $("#project-dialog-eyebrow").textContent = project ? "EDIT PRODUCTION" : "NEW PRODUCTION";
  $("#project-dialog-title").textContent = project ? "编辑项目设定" : "创建影片项目";
  $("#project-submit-button").textContent = project ? "保存设定" : "创建项目";
  form.elements.name.value = project?.name || "";
  form.elements.description.value = project?.description || "";
  form.elements.aspect_ratio.value = project?.aspect_ratio || "16:9";
  form.elements.language.value = project?.language || "zh-CN";
  form.elements.visual_style.value = project?.visual_style || "电影感写实故事板";
  form.elements.world_setting.value = project?.world_setting || "";
  $("#project-dialog").showModal();
}

function openAssetDialog(asset = null) {
  const form = $("#asset-form");
  form.dataset.assetId = asset?.id || "";
  $("#asset-dialog-eyebrow").textContent = asset ? "EDIT ASSET" : "NEW ASSET";
  $("#asset-dialog-title").textContent = asset ? `编辑 @${asset.name}` : "新建视觉资产";
  form.elements.asset_type.value = asset?.asset_type || "character";
  form.elements.name.value = asset?.name || "";
  form.elements.description.value = asset?.description || "";
  form.elements.prompt.value = asset?.prompt || "";
  $("#delete-asset-button").hidden = !asset;
  $("#asset-dialog").showModal();
}

function openAssetPromptPreview(asset) {
  $("#asset-prompt-preview-title").textContent = `@${asset.name} · 完整提示词`;
  $("#asset-prompt-preview-content").textContent = formatPromptPreview(asset.prompt);
  $("#asset-prompt-dialog").dataset.assetId = asset.id;
  $("#asset-prompt-dialog").showModal();
}

function renderAssetImageStudio(assetId) {
  const asset = state.assets.find(item => item.id === assetId);
  const images = state.assetImages.get(assetId) || [];
  $("#asset-images-title").textContent = `@${asset?.name || "资产"} · 图片候选`;
  const providerSelect = $("#asset-image-provider");
  providerSelect.innerHTML = state.imageProviders.map(provider => `
    <option value="${provider.id}" ${provider.configured ? "" : "disabled"}>${esc(provider.name)}${provider.configured ? "" : "（未配置）"}</option>`).join("");
  const firstConfigured = state.imageProviders.find(provider => provider.configured);
  if (firstConfigured) providerSelect.value = firstConfigured.id;
  $("#generate-asset-image-button").disabled = !firstConfigured || !asset?.prompt;
  $("#asset-image-provider-hint").textContent = firstConfigured
    ? `将使用当前资产的完整提示词。模型：${firstConfigured.model}`
    : "请在项目根目录 config.local.env 中填写 OpenAI 或火山方舟 API Key，然后重启服务。";
  $("#asset-image-gallery").innerHTML = images.length ? images.map(image => `
    <article class="asset-image-candidate ${image.is_primary ? "primary" : ""}">
      ${image.image_url ? `<a href="${esc(image.image_url)}" target="_blank" rel="noopener"><img src="${esc(image.image_url)}" alt="资产图片候选"></a>` : `<div class="asset-image-status ${image.status === "failed" ? "generation-error" : ""}">${image.status === "failed" ? esc(image.error_message || "生成失败") : image.status === "generating" ? "正在生成高质量图片…" : "等待生成…"}</div>`}
      <div class="asset-image-meta"><strong>${image.source === "upload" ? "用户上传" : esc(image.provider === "openai" ? "GPT Image 2" : "Seedream")}</strong>${image.is_primary ? `<span class="primary-badge">● 主参考图</span>` : ""}<br>${esc(image.size)} · ${esc(image.quality)}${image.local_path ? `<div class="asset-local-path" title="${esc(image.local_path)}"><code>${esc(image.local_path)}</code><button class="button" data-copy-local-path="${image.id}">复制地址</button></div>` : ""}</div>
      <div class="asset-actions">${image.status === "ready" && !image.is_primary ? `<button class="button" data-set-primary-image="${image.id}">设为主图</button>` : ""}${image.status === "failed" && image.provider ? `<button class="button" data-retry-image="${image.provider}">重试</button>` : ""}<button class="button danger" data-delete-asset-image="${image.id}">删除</button></div>
    </article>`).join("") : `<div class="prompt-empty">还没有图片。选择模型后生成，或从资产卡上传参考图。</div>`;
}

async function openAssetImageStudio(assetId) {
  const [providers, images] = await Promise.all([
    state.imageProviders.length ? state.imageProviders : request("/image-providers"),
    request(`/assets/${assetId}/images`),
  ]);
  state.imageProviders = providers;
  state.assetImages.set(assetId, images);
  $("#asset-images-dialog").dataset.assetId = assetId;
  renderAssetImageStudio(assetId);
  $("#asset-images-dialog").showModal();
  startAssetImagePolling(assetId);
}

function startAssetImagePolling(assetId) {
  clearInterval(state.imagePollTimer);
  if (!(state.assetImages.get(assetId) || []).some(image => ["pending", "generating"].includes(image.status))) return;
  state.imagePollTimer = setInterval(async () => {
    if (!$("#asset-images-dialog").open) return clearInterval(state.imagePollTimer);
    try {
      const images = await request(`/assets/${assetId}/images`);
      state.assetImages.set(assetId, images);
      renderAssetImageStudio(assetId);
      if (!images.some(image => ["pending", "generating"].includes(image.status))) {
        clearInterval(state.imagePollTimer);
        state.assets = await request(`/projects/${state.project.id}/assets`);
        renderAssets();
      }
    } catch (error) { clearInterval(state.imagePollTimer); toast(error.message, "error"); }
  }, 2000);
}

async function generateAssetImage(assetId, provider) {
  const image = await request(`/assets/${assetId}/images/generate`, {
    method: "POST",
    body: JSON.stringify({ provider }),
  });
  state.assetImages.set(assetId, [image, ...(state.assetImages.get(assetId) || [])]);
  renderAssetImageStudio(assetId);
  startAssetImagePolling(assetId);
  toast("图片生成任务已开始");
}

function chatPage() { return state.step === "prompts" ? "prompts" : state.step; }

function injectChatTargetButtons() {
  $$("[data-edit-asset]").forEach(anchor => {
    const host = anchor.closest(".asset-actions");
    if (host?.querySelector("[data-chat-target]")) return;
    const asset = state.assets.find(item => item.id === anchor.dataset.editAsset);
    host?.insertAdjacentHTML("afterbegin", `<button class="button" data-chat-target="${anchor.dataset.editAsset}" data-chat-type="asset" data-chat-title="与 AI 修改 · @${esc(asset?.name || "资产")}">与 AI 修改</button>`);
  });
  $$(".shot-card[data-shot-id]").forEach(card => {
    if (card.querySelector("[data-chat-target]")) return;
    const shot = state.scenes.flatMap(scene => scene.shots).find(item => item.id === card.dataset.shotId);
    card.insertAdjacentHTML("beforeend", `<button class="button card-chat-button" data-chat-target="${card.dataset.shotId}" data-chat-type="shot" data-chat-title="与 AI 修改 · 镜头 ${shot?.sequence || ""}">与 AI 修改</button>`);
  });
  $$("[data-generate-prompt]").forEach(anchor => {
    const host = anchor.closest(".prompt-card-actions");
    if (host?.querySelector("[data-chat-target]")) return;
    const versions = state.prompts.get(anchor.dataset.generatePrompt) || [];
    const prompt = versions[0];
    if (prompt) host?.insertAdjacentHTML("afterbegin", `<button class="button" data-chat-target="${prompt.id}" data-chat-type="prompt" data-chat-title="与 AI 修改 · 提示词">与 AI 修改</button>`);
  });
}

new MutationObserver(injectChatTargetButtons).observe($("#content"), {
  childList: true, subtree: true,
});

async function openChat(targetType = null, targetId = null, title = null) {
  state.chatTarget = { targetType, targetId, title };
  const query = new URLSearchParams({ page: chatPage() });
  query.set("scope", targetId ? "object" : "page");
  if (targetId) query.set("target_id", targetId);
  state.chatThreads = await request(`/projects/${state.project.id}/chat/threads?${query}`);
  if (!state.chatThreads.length) await createChatThread();
  else await selectChatThread(state.chatThreads[0].id);
  $("#chat-title").textContent = title || `全局 AI 修改 · ${chatPage()}`;
  $("#chat-drawer").classList.add("open");
  $("#chat-drawer").setAttribute("aria-hidden", "false");
  $("#chat-launcher").hidden = true;
  $("#chat-input").focus();
}

async function createChatThread() {
  const target = state.chatTarget || {};
  const payload = {
    page: chatPage(),
    scope: target.targetId ? "object" : "page",
    target_type: target.targetType || null,
    target_id: target.targetId || null,
    title: target.title || `全局修改 · ${chatPage()}`,
  };
  const thread = await request(`/projects/${state.project.id}/chat/threads`, {
    method: "POST", body: JSON.stringify(payload),
  });
  state.chatThreads.unshift(thread);
  await selectChatThread(thread.id);
}

async function selectChatThread(threadId) {
  state.chatThread = await request(`/chat/threads/${threadId}`);
  renderChat();
}

function renderChat() {
  const thread = state.chatThread;
  $("#chat-thread-select").innerHTML = state.chatThreads.map(item =>
    `<option value="${item.id}" ${item.id === thread?.id ? "selected" : ""}>${esc(item.title)}</option>`
  ).join("");
  if (!thread) return;
  const globalScope = thread.scope === "page";
  $("#chat-scope-label").textContent = globalScope
    ? `全局修改：可同时修改本页多个${chatPage() === "assets" ? "资产" : chatPage() === "shots" ? "镜头" : "对象"}`
    : `单对象修改：${thread.target_type} · ${thread.target_id}`;
  $("#chat-global-thread").hidden = globalScope;
  const messages = thread.messages.map(message =>
    `<div class="chat-message ${message.role}">${esc(message.content)}</div>`
  ).join("");
  const proposals = thread.proposals.map(proposal => {
    const summary = proposal.after_preview?.summary || "修改提案";
    const actions = proposal.status === "draft"
      ? `<button class="button primary" data-chat-apply="${proposal.id}">应用修改</button><button class="button" data-chat-reject="${proposal.id}">放弃</button>`
      : proposal.status === "applied"
        ? `<button class="button" data-chat-revert="${proposal.id}">撤销上次修改</button>` : "";
    return `<article class="proposal-card"><strong>${esc(summary)}</strong><small> · ${esc(proposal.status)}</small><pre>${esc(JSON.stringify(proposal.operations, null, 2))}</pre><div class="proposal-actions">${actions}</div>${proposal.error_message ? `<p class="generation-error">${esc(proposal.error_message)}</p>` : ""}</article>`;
  }).join("");
  $("#chat-messages").innerHTML = messages + proposals || `<p class="prompt-empty">描述你想修改的内容，AI 会先给出可预览的提案。</p>`;
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
}

async function refreshAfterChatChange() {
  const threadId = state.chatThread.id;
  await selectProject(state.project.id);
  await selectChatThread(threadId);
}

$("#chat-launcher").addEventListener("click", () => openChat().catch(error => toast(error.message, "error")));
$("#chat-close").addEventListener("click", () => {
  $("#chat-drawer").classList.remove("open");
  $("#chat-drawer").setAttribute("aria-hidden", "true");
  $("#chat-launcher").hidden = !state.project || !["script", "assets", "shots", "prompts"].includes(state.step);
  $("#chat-launcher").focus();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("#chat-drawer").classList.contains("open")) {
    event.preventDefault();
    $("#chat-close").click();
  }
});
$("#chat-new-thread").addEventListener("click", () => createChatThread().catch(error => toast(error.message, "error")));
$("#chat-global-thread").addEventListener("click", () => openChat(null, null, `全局 AI 修改 · ${chatPage()}`).catch(error => toast(error.message, "error")));
$("#chat-thread-select").addEventListener("change", event => selectChatThread(event.target.value).catch(error => toast(error.message, "error")));
$("#chat-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = $("#chat-input");
  const content = input.value.trim();
  if (!content || !state.chatThread) return;
  input.disabled = true;
  try {
    state.chatThread = await request(`/chat/threads/${state.chatThread.id}/messages`, {
      method: "POST", body: JSON.stringify({ content }),
    });
    input.value = "";
    renderChat();
  } catch (error) { toast(error.message, "error"); }
  finally { input.disabled = false; input.focus(); }
});

document.addEventListener("click", async event => {
  const targetButton = event.target.closest("[data-chat-target]");
  const applyButton = event.target.closest("[data-chat-apply]");
  const rejectButton = event.target.closest("[data-chat-reject]");
  const revertButton = event.target.closest("[data-chat-revert]");
  try {
    if (targetButton) {
      event.stopPropagation();
      await openChat(
        targetButton.dataset.chatType,
        targetButton.dataset.chatTarget,
        targetButton.dataset.chatTitle,
      );
    } else if (applyButton) {
      const proposal = state.chatThread.proposals.find(item => item.id === applyButton.dataset.chatApply);
      if (proposal.operations.some(item => item.action === "delete") && !window.confirm("该提案包含删除操作，确认继续吗？")) return;
      await request(`/chat/proposals/${proposal.id}/apply`, { method: "POST" });
      await refreshAfterChatChange();
      toast("AI 修改已应用");
    } else if (rejectButton) {
      await request(`/chat/proposals/${rejectButton.dataset.chatReject}/reject`, { method: "POST" });
      await selectChatThread(state.chatThread.id);
    } else if (revertButton) {
      await request(`/chat/proposals/${revertButton.dataset.chatRevert}/revert`, { method: "POST" });
      await refreshAfterChatChange();
      toast("上次 AI 修改已撤销");
    }
  } catch (error) { toast(error.message, "error"); }
});

// Closing a dialog must never enter form validation or asynchronous business handling.
document.addEventListener("click", event => {
  const closeButton = event.target.closest("[data-close-dialog]");
  if (!closeButton) return;
  event.preventDefault();
  event.stopPropagation();
  closeButton.closest("dialog")?.close();
}, true);

document.addEventListener("click", async event => {
  const agentOptionButton = event.target.closest("[data-agent-option]");
  const approveMasterPlan = event.target.closest("[data-agent-approve-plan]");
  const approveMasterStage = event.target.closest("[data-agent-stage]");
  const chatTargetButton = event.target.closest("[data-chat-target]");
  const projectButton = event.target.closest("[data-project-id]");
  const stepButton = event.target.closest("[data-step], [data-step-next]");
  const actionButton = event.target.closest("[data-action]");
  const modeButton = event.target.closest("[data-script-mode]");
  const versionButton = event.target.closest("[data-script-id]");
  const editAssetButton = event.target.closest("[data-edit-asset]");
  const assetPromptButton = event.target.closest("[data-asset-prompt]");
  const previewAssetPromptButton = event.target.closest("[data-preview-asset-prompt]");
  const copyAssetPromptButton = event.target.closest("[data-copy-asset-prompt]");
  const assetImagesButton = event.target.closest("[data-asset-images]");
  const primaryImageButton = event.target.closest("[data-set-primary-image]");
  const deleteAssetImageButton = event.target.closest("[data-delete-asset-image]");
  const retryImageButton = event.target.closest("[data-retry-image]");
  const copyLocalPathButton = event.target.closest("[data-copy-local-path]");
  const promptDefaultModeButton = event.target.closest("[data-prompt-default-mode]");
  const generatePromptButton = event.target.closest("[data-generate-prompt]");
  const copyPromptButton = event.target.closest("[data-copy-prompt]");
  const copyFrameButton = event.target.closest("[data-copy-frame]");
  const copyDirectorOverheadButton = event.target.closest("[data-copy-director-overhead]");
  const deleteShotButton = event.target.closest("[data-delete-shot]");
  const shotCard = chatTargetButton ? null : event.target.closest("[data-shot-id]");
  const agentRun = event.target.closest("[data-agent-run]");
  const agentSessionButton = event.target.closest("[data-agent-session-id]");
  const archiveAgentButton = event.target.closest("[data-agent-archive-session]");
  const unarchiveAgentButton = event.target.closest("[data-agent-unarchive-session]");
  const deleteAgentButton = event.target.closest("[data-agent-delete-session]");
  const toggleAgentMessageButton = event.target.closest("[data-toggle-agent-message]");
  try {
    if (agentOptionButton) await handleAgentOption(agentOptionButton);
    else if (agentSessionButton) await selectAgentSession(agentSessionButton.dataset.agentSessionId);
    else if (archiveAgentButton) await archiveAgentSession(archiveAgentButton.dataset.agentArchiveSession);
    else if (unarchiveAgentButton) await unarchiveAgentSession(unarchiveAgentButton.dataset.agentUnarchiveSession);
    else if (deleteAgentButton) await deleteAgentSession(deleteAgentButton.dataset.agentDeleteSession);
    else if (toggleAgentMessageButton) {
      const id = toggleAgentMessageButton.dataset.toggleAgentMessage;
      if (state.expandedAgentMessages.has(id)) state.expandedAgentMessages.delete(id);
      else state.expandedAgentMessages.add(id);
      renderMasterAgent();
    }
    else if (approveMasterPlan) await approveAgentPlan(approveMasterPlan.dataset.agentApprovePlan);
    else if (approveMasterStage) await approveAgentStage(approveMasterStage.dataset.agentPlan, approveMasterStage.dataset.agentStage);
    else if (agentRun) await openAgentRunDetail(agentRun.dataset.agentRun);
    else if (projectButton) { await selectProject(projectButton.dataset.projectId); setStep("project"); }
    else if (stepButton && !stepButton.disabled) setStep(stepButton.dataset.step || stepButton.dataset.stepNext);
    else if (modeButton) { state.scriptMode = modeButton.dataset.scriptMode; renderScript(); }
    else if (versionButton) { state.script = state.scripts.find(s => s.id === versionButton.dataset.scriptId); renderScript(); }
    else if (editAssetButton) openAssetDialog(state.assets.find(asset => asset.id === editAssetButton.dataset.editAsset));
    else if (assetPromptButton) await generateAssetPrompt(assetPromptButton.dataset.assetPrompt);
    else if (previewAssetPromptButton) openAssetPromptPreview(state.assets.find(asset => asset.id === previewAssetPromptButton.dataset.previewAssetPrompt));
    else if (copyAssetPromptButton) {
      const asset = state.assets.find(item => item.id === copyAssetPromptButton.dataset.copyAssetPrompt);
      await navigator.clipboard.writeText(asset.prompt);
      toast(`@${asset.name} 的提示词已复制`);
    }
    else if (assetImagesButton) await openAssetImageStudio(assetImagesButton.dataset.assetImages);
    else if (primaryImageButton) {
      const assetId = $("#asset-images-dialog").dataset.assetId;
      const asset = await request(`/assets/${assetId}/images/${primaryImageButton.dataset.setPrimaryImage}/primary`, { method: "PATCH" });
      state.assets = state.assets.map(item => item.id === asset.id ? asset : item);
      state.assetImages.set(assetId, await request(`/assets/${assetId}/images`));
      renderAssetImageStudio(assetId); renderAssets(); toast("主参考图已更新");
    }
    else if (deleteAssetImageButton) {
      if (!window.confirm("确定删除这张候选图吗？")) return;
      const assetId = $("#asset-images-dialog").dataset.assetId;
      await request(`/assets/${assetId}/images/${deleteAssetImageButton.dataset.deleteAssetImage}`, { method: "DELETE" });
      state.assetImages.set(assetId, await request(`/assets/${assetId}/images`));
      state.assets = await request(`/projects/${state.project.id}/assets`);
      renderAssetImageStudio(assetId); renderAssets(); toast("候选图已删除");
    }
    else if (retryImageButton) await generateAssetImage($("#asset-images-dialog").dataset.assetId, retryImageButton.dataset.retryImage);
    else if (copyLocalPathButton) {
      const assetId = $("#asset-images-dialog").dataset.assetId;
      const image = (state.assetImages.get(assetId) || []).find(item => item.id === copyLocalPathButton.dataset.copyLocalPath);
      if (image?.local_path) { await navigator.clipboard.writeText(image.local_path); toast("本地保存地址已复制"); }
    }
    else if (deleteShotButton) await deleteShot(deleteShotButton.dataset.deleteShot);
    else if (shotCard) openShotDialog(state.scenes.flatMap(s => s.shots).find(s => s.id === shotCard.dataset.shotId));
    else if (event.target.closest("#new-project-button") || actionButton?.dataset.action === "new-project") openProjectDialog();
    else if (actionButton?.dataset.action === "new-agent-session") await createAgentSession();
    else if (actionButton?.dataset.action === "open-master-agent") { state.showMasterAgent = true; render(); }
    else if (actionButton?.dataset.action === "close-master-agent") { state.showMasterAgent = false; render(); }
    else if (actionButton?.dataset.action === "generate-agent-plan") await generateAgentPlan();
    else if (actionButton?.dataset.action === "defer-agent-plan") toast("计划已保留，可稍后从这里继续执行");
    else if (actionButton?.dataset.action === "toggle-agent-archive-view") { state.showArchivedAgentSessions = !state.showArchivedAgentSessions; renderMasterAgent(); }
    else if (actionButton?.dataset.action === "crew-preflight") await runCrewPreflight();
    else if (event.target.closest("[data-agent-resume-task]")) await resumeAgentTask(event.target.closest("[data-agent-resume-task]").dataset.agentResumeTask);
    else if (event.target.closest("[data-agent-cancel-task]")) await cancelAgentTask(event.target.closest("[data-agent-cancel-task]").dataset.agentCancelTask);
    else if (actionButton?.dataset.action === "edit-project") openProjectDialog(state.project);
    else if (actionButton?.dataset.action === "toggle-asset-selection") {
      state.assetSelectionMode = !state.assetSelectionMode;
      state.selectedAssetIds.clear();
      renderAssets();
    }
    else if (actionButton?.dataset.action === "select-all-assets") {
      if (state.selectedAssetIds.size === state.assets.length) state.selectedAssetIds.clear();
      else state.selectedAssetIds = new Set(state.assets.map(asset => asset.id));
      renderAssets();
    }
    else if (actionButton?.dataset.action === "delete-selected-assets") await deleteSelectedAssets();
    else if (actionButton?.dataset.action === "new-asset") openAssetDialog();
    else if (actionButton?.dataset.action === "extract-assets") await extractProjectAssets();
    else if (actionButton?.dataset.action === "generate-all-asset-prompts") await generateAllAssetPrompts();
    else if (actionButton?.dataset.action === "save-script") await saveScript();
    else if (actionButton?.dataset.action === "generate-script") await generateScript();
    else if (actionButton?.dataset.action === "approve-script") await approveScript();
    else if (actionButton?.dataset.action === "generate-shots") await generateShots();
    else if (actionButton?.dataset.action === "open-metrics") await setStep("metrics");
    else if (actionButton?.dataset.action === "close-metrics") await setStep(state.returnStep || "project");
    else if (actionButton?.dataset.action === "refresh-metrics") await setStep("metrics");
    else if (actionButton?.dataset.action === "generate-all-prompts") await generateAllPrompts();
    else if (promptDefaultModeButton) {
      state.promptMode = promptDefaultModeButton.dataset.promptDefaultMode;
      renderPrompts();
    }
    else if (generatePromptButton) await generatePrompt(generatePromptButton.dataset.generatePrompt);
    else if (copyPromptButton) {
      const prompt = (state.prompts.get(copyPromptButton.dataset.copyPrompt) || [])[0];
      if (prompt) { await navigator.clipboard.writeText(prompt.positive_prompt); toast("提示词已复制"); }
    }
    else if (copyFrameButton) {
      const prompt = (state.prompts.get(copyFrameButton.dataset.copyFrame) || [])[0];
      const frame = (prompt?.prompt_metadata?.frames || []).find(item => item.index === Number(copyFrameButton.dataset.frameIndex));
      if (frame) { await navigator.clipboard.writeText(frame.description); toast(`第 ${frame.index} 帧描述已复制`); }
    }
    else if (copyDirectorOverheadButton) {
      const prompt = (state.prompts.get(copyDirectorOverheadButton.dataset.copyDirectorOverhead) || [])[0];
      const directorOverhead = prompt?.prompt_metadata?.director_overhead;
      if (directorOverhead?.positive_prompt) {
        await navigator.clipboard.writeText(directorOverhead.positive_prompt);
        toast("导演俯视参考图提示词已复制");
      }
    }
  } catch (error) { loading(false); toast(error.message, "error"); }
});

document.addEventListener("submit", async event => {
  if (event.target.id !== "master-agent-form") return;
  event.preventDefault();
  const input = $("#master-agent-input");
  const content = input.value.trim();
  if (!content || !state.agentSession) return;
  input.disabled = true;
  try {
    await sendAgentMessage(content);
    input.value = "";
  } catch (error) { toast(error.message, "error"); }
  finally { input.disabled = false; }
});

document.addEventListener("change", async event => {
  if (event.target.id !== "master-agent-file") return;
  const file = event.target.files?.[0];
  if (!file) return;
  if (!/\.(txt|md)$/i.test(file.name) || file.size > 5 * 1024 * 1024) {
    toast("请选择不超过 5MB 的 .txt 或 .md 文件", "error"); return;
  }
  $("#master-agent-input").value = await file.text();
  toast(`已读取 ${file.name}，发送前可以继续编辑`);
});

$("#asset-image-generate-form").addEventListener("submit", async event => {
  event.preventDefault();
  const assetId = $("#asset-images-dialog").dataset.assetId;
  try { await generateAssetImage(assetId, $("#asset-image-provider").value); }
  catch (error) { toast(error.message, "error"); }
});

$("#asset-image-provider").addEventListener("change", event => {
  const provider = state.imageProviders.find(item => item.id === event.target.value);
  $("#asset-image-provider-hint").textContent = provider ? `将使用当前资产的完整提示词。模型：${provider.model}` : "";
});

$("#asset-images-dialog").addEventListener("close", () => clearInterval(state.imagePollTimer));

$("#project-form").addEventListener("submit", async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  const projectId = event.currentTarget.dataset.projectId;
  try {
    const project = await request(projectId ? `/projects/${projectId}` : "/projects", {
      method: projectId ? "PATCH" : "POST",
      body: JSON.stringify(data),
    });
    $("#project-dialog").close();
    event.currentTarget.reset();
    event.currentTarget.dataset.projectId = "";
    if (projectId) {
      state.project = project;
      state.projects = state.projects.map(item => item.id === project.id ? project : item);
      render();
      toast("项目设定已更新");
    } else {
      await loadProjects(project.id);
      setStep("project");
      toast("项目创建成功");
    }
  } catch (error) { toast(error.message, "error"); }
});

$("#asset-form").addEventListener("submit", async event => {
  event.preventDefault();
  const assetId = event.currentTarget.dataset.assetId;
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    const asset = await request(assetId ? `/assets/${assetId}` : `/projects/${state.project.id}/assets`, {
      method: assetId ? "PATCH" : "POST",
      body: JSON.stringify(data),
    });
    if (assetId) state.assets = state.assets.map(item => item.id === asset.id ? asset : item);
    else state.assets.push(asset);
    $("#asset-dialog").close();
    renderAssets();
    toast(assetId ? "资产已更新" : "资产已创建");
  } catch (error) { toast(error.message, "error"); }
});

$("#delete-asset-button").addEventListener("click", async () => {
  const assetId = $("#asset-form").dataset.assetId;
  if (!assetId || !window.confirm("确定删除这个资产及其参考图吗？")) return;
  try {
    await request(`/assets/${assetId}`, { method: "DELETE" });
    state.assets = state.assets.filter(asset => asset.id !== assetId);
    $("#asset-dialog").close();
    renderAssets();
    toast("资产已删除");
  } catch (error) { toast(error.message, "error"); }
});

$("#copy-previewed-asset-prompt").addEventListener("click", async () => {
  const asset = state.assets.find(item => item.id === $("#asset-prompt-dialog").dataset.assetId);
  if (!asset?.prompt) return;
  try {
    await navigator.clipboard.writeText(asset.prompt);
    toast(`@${asset.name} 的完整提示词已复制`);
  } catch (error) { toast("复制失败，请检查浏览器剪贴板权限", "error"); }
});

document.addEventListener("change", async event => {
  const defaultFrames = event.target.closest("[data-prompt-default-frames]");
  if (defaultFrames) {
    state.promptFrameCount = defaultFrames.value === "auto" ? null : Number(defaultFrames.value);
    renderPrompts();
    return;
  }
  const shotPromptMode = event.target.closest("[data-shot-prompt-mode]");
  if (shotPromptMode) {
    const existing = state.promptOverrides.get(shotPromptMode.dataset.shotPromptMode) || { frame_count: "inherit" };
    state.promptOverrides.set(shotPromptMode.dataset.shotPromptMode, { ...existing, mode: shotPromptMode.value });
    renderPrompts();
    return;
  }
  const shotFrameCount = event.target.closest("[data-shot-frame-count]");
  if (shotFrameCount) {
    const existing = state.promptOverrides.get(shotFrameCount.dataset.shotFrameCount) || { mode: "storyboard" };
    state.promptOverrides.set(shotFrameCount.dataset.shotFrameCount, { ...existing, frame_count: shotFrameCount.value === "inherit" ? "inherit" : shotFrameCount.value === "auto" ? null : Number(shotFrameCount.value) });
    renderPrompts();
    return;
  }
  const selection = event.target.closest("[data-select-asset]");
  if (selection) {
    if (selection.checked) state.selectedAssetIds.add(selection.dataset.selectAsset);
    else state.selectedAssetIds.delete(selection.dataset.selectAsset);
    renderAssets();
    return;
  }
  const input = event.target.closest("[data-asset-upload]");
  if (!input || !input.files?.[0]) return;
  const file = input.files[0];
  try {
    loading(true, `正在上传 @${state.assets.find(asset => asset.id === input.dataset.assetUpload)?.name || "资产"}`, "参考图将保存在本地资产库");
    const asset = await request(`/assets/${input.dataset.assetUpload}/image`, {
      method: "PUT",
      headers: { "Content-Type": file.type },
      body: file,
    });
    state.assets = state.assets.map(item => item.id === asset.id ? asset : item);
    loading(false);
    renderAssets();
    toast("参考图已上传");
  } catch (error) { loading(false); toast(error.message, "error"); }
});

$("#shot-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget); const id = form.get("id"); const payload = Object.fromEntries(form); delete payload.id; payload.is_locked = form.has("is_locked"); payload.duration_seconds = Number(payload.duration_seconds);
  try { await request(`/shots/${id}`, { method: "PATCH", body: JSON.stringify(payload) }); $("#shot-dialog").close(); state.scenes = await request(`/projects/${state.project.id}/scenes`); renderShots(); toast("镜头修改已保存"); } catch (error) { toast(error.message, "error"); }
});

async function saveScript() {
  const title = $("#script-title").value.trim(); const content = $("#script-content").value.trim();
  if (!content) return toast("请先填写剧本内容", "error");
  const script = await request(`/projects/${state.project.id}/scripts`, { method: "POST", body: JSON.stringify({ title: title || "未命名剧本", content, source_type: "user" }) });
  state.scripts = await request(`/projects/${state.project.id}/scripts`); state.script = script; state.project.status = "SCRIPT_REVIEW"; renderScript(); renderSidebar(); renderHeader(); toast("已保存为新版本");
}

async function generateScript() {
  const brief = $("#ai-brief").value.trim(); if (!brief) return toast("请填写故事梗概", "error");
  loading(true, "AI 正在创作剧本", "正在组织场景、人物与对白");
  const script = await request(`/projects/${state.project.id}/scripts/generate`, { method: "POST", body: JSON.stringify({ brief, title: $("#ai-title").value.trim() || state.project.name, instructions: $("#ai-instructions").value.trim() }) });
  loading(false); state.scripts = await request(`/projects/${state.project.id}/scripts`); state.script = script; state.scriptMode = "manual"; renderScript(); toast("AI 剧本已生成");
}

async function approveScript() {
  if (!state.script) return;
  state.script = await request(`/scripts/${state.script.id}/approve`, { method: "POST" }); state.scripts = await request(`/projects/${state.project.id}/scripts`); state.project.status = "ASSET_REVIEW"; render(); toast("剧本已确认，可以开始整理视觉资产");
}

async function generateShots() {
  const approved = state.scripts.find(s => s.is_approved); if (!approved) return toast("请先确认剧本", "error");
  loading(true, "AI 正在设计分镜", "正在分析场次、动作、景别与镜头连续性");
  state.scenes = await request(`/scripts/${approved.id}/shots/generate`, { method: "POST" }); loading(false); state.project.status = "SHOT_LIST_REVIEW"; render(); toast("分镜拆分完成");
}

async function deleteShot(shotId) {
  const shot = state.scenes.flatMap(scene => scene.shots).find(item => item.id === shotId);
  if (!window.confirm(`确定删除镜头 ${shot?.sequence || ""} 吗？该镜头已有的提示词也会被删除。`)) return;
  loading(true, "正在删除镜头", "正在更新同场次的镜头顺序");
  await request(`/shots/${shotId}`, { method: "DELETE" });
  state.scenes = await request(`/projects/${state.project.id}/scenes`);
  state.prompts.delete(shotId);
  $("#shot-dialog").close();
  loading(false);
  renderShots();
  toast("镜头已删除，序号已重新整理");
}

async function extractProjectAssets() {
  loading(true, "AI 正在分析视觉资产", "正在识别人物、场景和关键道具");
  state.assets = await request(`/projects/${state.project.id}/assets/extract`, { method: "POST" });
  loading(false);
  renderAssets();
  toast(`已整理 ${state.assets.length} 个视觉资产`);
}

async function generateAssetPrompt(assetId) {
  const current = state.assets.find(asset => asset.id === assetId);
  loading(true, `正在生成 @${current?.name || "资产"} 的提示词`, "将结合项目地域、时代和视觉风格");
  const asset = await request(`/assets/${assetId}/prompt/generate`, { method: "POST" });
  state.assets = state.assets.map(item => item.id === asset.id ? asset : item);
  loading(false);
  renderAssets();
  toast(`@${asset.name} 的资产提示词已生成`);
}

async function generateAllAssetPrompts() {
  if (!state.assets.length) return toast("请先创建或提取资产", "error");
  const failed = [];
  loading(true, `正在生成资产提示词 0/${state.assets.length}`, "将按资产顺序逐一生成，请稍候");
  for (let index = 0; index < state.assets.length; index += 1) {
    const current = state.assets[index];
    $("#loading-title").textContent = `正在生成资产提示词 ${index + 1}/${state.assets.length}`;
    $("#loading-copy").textContent = `当前资产：@${current.name}`;
    try {
      const asset = await request(`/assets/${current.id}/prompt/generate`, { method: "POST" });
      state.assets = state.assets.map(item => item.id === asset.id ? asset : item);
    } catch (error) {
      failed.push(`@${current.name}: ${error.message}`);
    }
  }
  loading(false);
  renderAssets();
  if (failed.length) {
    toast(`已完成 ${state.assets.length - failed.length}/${state.assets.length} 个资产，${failed.length} 个失败`, "error");
  } else {
    toast(`全部 ${state.assets.length} 个资产提示词已生成`);
  }
}

async function deleteSelectedAssets() {
  const selectedIds = [...state.selectedAssetIds];
  if (!selectedIds.length) return;
  if (!window.confirm(`确定删除选中的 ${selectedIds.length} 个资产及其参考图吗？`)) return;

  const failedIds = [];
  loading(true, `正在删除资产 0/${selectedIds.length}`, "请稍候");
  for (let index = 0; index < selectedIds.length; index += 1) {
    const assetId = selectedIds[index];
    const asset = state.assets.find(item => item.id === assetId);
    $("#loading-title").textContent = `正在删除资产 ${index + 1}/${selectedIds.length}`;
    $("#loading-copy").textContent = asset ? `当前资产：@${asset.name}` : "正在处理选中资产";
    try {
      await request(`/assets/${assetId}`, { method: "DELETE" });
      state.assets = state.assets.filter(item => item.id !== assetId);
    } catch (error) {
      failedIds.push(assetId);
    }
  }
  loading(false);
  state.selectedAssetIds = new Set(failedIds);
  state.assetSelectionMode = failedIds.length > 0;
  renderAssets();
  if (failedIds.length) {
    toast(`已删除 ${selectedIds.length - failedIds.length}/${selectedIds.length} 个资产，${failedIds.length} 个失败`, "error");
  } else {
    toast(`已删除 ${selectedIds.length} 个资产`);
  }
}

async function generatePrompt(shotId) {
  const override = state.promptOverrides.get(shotId) || { mode: "default", frame_count: "inherit" };
  const mode = override.mode === "default" ? state.promptMode : override.mode;
  const frameCount = override.frame_count === "inherit" ? state.promptFrameCount : override.frame_count;
  const shot = state.scenes.flatMap(scene => scene.shots).find(item => item.id === shotId);
  const resolvedFrames = frameCount ?? automaticFrameCount(Number(shot?.duration_seconds || 4));
  loading(true, mode === "storyboard" ? "AI 正在设计连续帧故事板" : "AI 正在设计视频首帧", mode === "storyboard" ? `镜头 ${Number(shot?.duration_seconds || 4).toFixed(1)} 秒，将规划 ${resolvedFrames} 个连续画面` : "正在组合起始姿态、构图、光线与视觉风格");
  await request(`/shots/${shotId}/prompts/generate`, { method: "POST", body: JSON.stringify({ mode, frame_count: frameCount }) });
  state.prompts.set(shotId, await request(`/shots/${shotId}/prompts`));
  loading(false); renderPrompts(); toast(mode === "storyboard" ? `${frameCount} 帧故事板提示词已生成` : "首帧提示词已生成");
}

async function generateAllPrompts() {
  const shots = state.scenes.flatMap(scene => scene.shots);
  if (!shots.length) return toast("请先生成分镜", "error");

  const failed = [];
  loading(true, `正在生成提示词 0/${shots.length}`, "将按镜头顺序逐一生成，请稍候");
  for (let index = 0; index < shots.length; index += 1) {
    const shot = shots[index];
    $("#loading-title").textContent = `正在生成提示词 ${index + 1}/${shots.length}`;
    try {
      const prompt = await request(`/shots/${shot.id}/prompts/generate`, { method: "POST", body: JSON.stringify({ mode: state.promptMode, frame_count: state.promptFrameCount }) });
      state.prompts.set(shot.id, [prompt, ...(state.prompts.get(shot.id) || [])]);
    } catch (error) {
      failed.push(`镜头 ${shot.sequence}: ${error.message}`);
    }
  }
  loading(false);
  renderPrompts();
  if (failed.length) {
    toast(`已完成 ${shots.length - failed.length}/${shots.length} 个镜头，${failed.length} 个失败`, "error");
  } else {
    toast(`全部 ${shots.length} 个镜头的提示词已生成`);
  }
}

$("#content").addEventListener("input", event => { if (event.target.id === "script-content") $("#script-stats").textContent = `${event.target.value.length} 字`; });
loadProjects().catch(error => { toast(`无法连接后端：${error.message}`, "error"); render(); });
