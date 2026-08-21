"use strict";

const ROUTES = {
  dashboard: "Dashboard",
  kanban: "Kanban",
  agy: "Agy-gen Chat / Runtime",
  memory: "Memory & Skills",
  connections: "Connections & Credentials",
  reports: "Reports & Jobs",
};
const CARD_COLUMNS = [
  ["BACKLOG", "BACKLOG", ""],
  ["PROCESS", "PROCESS", "process"],
  ["WAITING", "WAITING INPUT", "waiting"],
  ["VERIFY", "VERIFY", "verify"],
  ["DONE", "DONE", "done"],
];
const app = {
  token: sessionStorage.getItem("genos.session") || "",
  owner: null,
  route: routeFromLocation(),
  selectedCard: null,
  selectedLibrary: null,
  toastTimer: null,
};

const main = document.getElementById("main");
const authDialog = document.getElementById("auth-dialog");
const loginForm = document.getElementById("login-form");
const bootstrapForm = document.getElementById("bootstrap-form");
const toast = document.getElementById("toast");

function routeFromLocation() {
  const part = location.pathname.replace(/^\//, "").split("/", 1)[0];
  return Object.hasOwn(ROUTES, part) ? part : "dashboard";
}
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);
}
function fmt(value, fallback = "UNKNOWN") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}
function fmtTime(value) {
  if (!value) return "Chưa có bằng chứng thời gian";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString("vi-VN");
}
function stateClass(value) {
  const s = String(value || "UNKNOWN").toUpperCase();
  if (["PASS","READY","ACTIVE","SUCCEEDED","DONE","AUTHENTICATED","AUTHORIZED","PUBLISHED","SYNCED","RUNNING"].includes(s)) return "badge-good";
  if (["PROCESS","BUSY","QUEUED","INSTALLED","WAITING_BROWSER","WAITING_CODE","WAITING_USER","MCP_GRANT_TESTED"].includes(s)) return "badge-info";
  if (["WARN","WARNING","NEEDS_ACTION","NEEDS_AUTH","DEGRADED","WAITING_INPUT","WAITING_APPROVAL","EXPIRED","DISCONNECTED","UNKNOWN","NOT_CONFIGURED","UNCONFIGURED"].includes(s)) return "badge-warn";
  if (["FAIL","FAILED","ERROR","DENIED","REVOKED","CANCELLED"].includes(s)) return "badge-bad";
  return "badge-unknown";
}
function badge(value) {
  const text = fmt(value);
  return `<span class="badge ${stateClass(text)}">${esc(text)}</span>`;
}
function empty(message) { return `<div class="empty">${esc(message)}</div>`; }
function pageHead(title, subtitle, actions = "") {
  return `<div class="page-head"><div><div class="eyebrow">MISSION CONTROL</div><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div><div class="page-actions">${actions}</div></div>`;
}
function kv(rows) {
  return `<dl class="kv">${rows.map(([k,v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}
function toastMsg(message, bad = false) {
  clearTimeout(app.toastTimer);
  toast.textContent = message;
  toast.className = `toast show${bad ? " bad" : ""}`;
  app.toastTimer = setTimeout(() => { toast.className = "toast"; }, 3600);
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = {Accept: "application/json"};
  if (app.token && options.auth !== false) headers.Authorization = `Bearer ${app.token}`;
  let body;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, {method, headers, body, cache: "no-store"});
  } catch (_err) {
    throw new Error("Không kết nối được dịch vụ GenOS");
  }
  let payload = {};
  try { payload = await response.json(); } catch (_err) { payload = {}; }
  if (response.status === 401 && options.auth !== false) {
    clearSession();
    showAuth();
    throw new Error("Phiên Owner đã hết hạn");
  }
  if (!response.ok) {
    const error = payload.error || `HTTP_${response.status}`;
    throw new Error(error);
  }
  return payload;
}
async function optional(path) {
  try { return await api(path); } catch (_err) { return null; }
}
function clearSession() {
  app.token = ""; app.owner = null; sessionStorage.removeItem("genos.session");
}
function showAuth() {
  if (!authDialog.open) authDialog.showModal();
  loginForm.classList.remove("hidden");
  bootstrapForm.classList.add("hidden");
  document.getElementById("login-password").value = "";
}
function hideAuth() { if (authDialog.open) authDialog.close(); }

function setRoute(route, push = true) {
  app.route = Object.hasOwn(ROUTES, route) ? route : "dashboard";
  if (push) history.pushState({}, "", `/${app.route}`);
  document.getElementById("route-title").textContent = ROUTES[app.route];
  document.querySelectorAll("#nav [data-route]").forEach((button) => button.classList.toggle("active", button.dataset.route === app.route));
  render().catch((err) => renderError(err));
}
function renderError(err) {
  main.innerHTML = pageHead(ROUTES[app.route], "Không thể tải dữ liệu hiện tại.") + `<div class="panel attention"><h3>Không có bằng chứng đủ để hiển thị</h3><p class="muted">${esc(err.message || err)}</p></div>`;
  document.getElementById("global-state").className = "badge badge-bad";
  document.getElementById("global-state").textContent = "DEGRADED";
}
async function render() {
  if (!app.token) { showAuth(); return; }
  main.innerHTML = `<div class="panel">Đang tải dữ liệu authoritative…</div>`;
  if (app.route === "dashboard") await renderDashboard();
  else if (app.route === "kanban") await renderKanban();
  else if (app.route === "agy") await renderAgy();
  else if (app.route === "memory") await renderLibrary();
  else if (app.route === "connections") await renderConnections();
  else if (app.route === "reports") await renderReports();
}

async function renderDashboard() {
  const [obsP, agentP, cardsP, driveP, mcpP, jobsP] = await Promise.all([
    optional("/api/v1/observability"), optional("/api/v1/agents/agy-gen"), optional("/api/v1/cards"),
    optional("/api/v1/drive"), optional("/api/v1/mcp"), optional("/api/v1/jobs"),
  ]);
  const obs = obsP?.observability || {};
  const health = obs.health || {};
  const observations = Array.isArray(obs.observations) ? obs.observations : [];
  const agent = agentP?.agent || {};
  const agentRuntime = agent.status?.runtime || {};
  const drive = driveP?.drive || {};
  const mcp = mcpP?.mcp || {};
  const cards = Array.isArray(cardsP?.cards) ? cardsP.cards : [];
  const jobs = Array.isArray(jobsP?.jobs) ? jobsP.jobs : [];
  const activeCards = cards.filter((c) => !["DONE","CANCELLED"].includes(c.status)).length;
  const activeJobs = jobs.filter((j) => ["PENDING","RUNNING","NEEDS_ACTION"].includes(j.state)).length;
  const overall = health.state || "UNKNOWN";
  document.getElementById("global-state").className = `badge ${stateClass(overall)}`;
  document.getElementById("global-state").textContent = overall;
  const instanceId = obs.instance_id || agent.status?.identity?.instance_id || "UNKNOWN";
  document.getElementById("instance-id").textContent = instanceId;
  document.getElementById("instance-state").textContent = overall;
  document.getElementById("mcp-mini").textContent = `MCP: ${mcp.endpoint || "UNKNOWN"}`;
  main.innerHTML = pageHead("Dashboard", "Quan sát một nguồn truth duy nhất cho health, Agent, công việc, Drive, MCP và JobRun.") + `
    <section class="grid metrics">
      ${metric("SYSTEM HEALTH", overall, health.reason || obs.authority || "genos-observability-v1")}
      ${metric("AGY-GEN", agentRuntime.state || "UNKNOWN", agentRuntime.reason || "Chưa có runtime evidence")}
      ${metric("GOOGLE DRIVE", drive.state || "UNKNOWN", drive.account_email || drive.last_error_code || "Chưa cấu hình")}
      ${metric("MCP HUB", mcp.endpoint ? "READY" : "UNKNOWN", mcp.endpoint || "Endpoint chưa được quan sát")}
      ${metric("ACTIVE CARDS", String(activeCards), `${cards.length} tổng Card`)}
      ${metric("ACTIVE JOBS", String(activeJobs), `${jobs.length} JobRun gần nhất`)}
    </section>
    <section class="grid three">
      <article class="panel"><h2 class="panel-title">SYSTEM OBSERVATIONS</h2>${observations.length ? `<div class="list">${observations.slice(0,14).map((o) => listRow(o.check_id || "unknown", o.source || o.reason || "", badge(o.state))).join("")}</div>` : empty("Chưa có observation từ authoritative collector.")}</article>
      <article class="panel"><h2 class="panel-title">AGENT & INTEGRATIONS</h2>
        ${kv([["Agent", badge(agentRuntime.state)], ["Provider", badge(agent.status?.provider?.state)], ["Auth", badge(agent.auth?.state)], ["Drive", badge(drive.state)], ["MCP", mcp.endpoint ? badge("READY") : badge("UNKNOWN")]])}
        <div class="detail-section"><div class="muted">MCP endpoint</div><div class="endpoint">${esc(mcp.endpoint || "UNKNOWN")}</div></div>
      </article>
      <article class="panel"><h2 class="panel-title">RECENT JOBRUN</h2>${jobs.length ? `<div class="list">${jobs.slice(0,10).map((j) => listRow(j.kind || j.job_id || "job", j.current_step || fmtTime(j.updated_at), badge(j.state))).join("")}</div>` : empty("Chưa có JobRun bền vững.")}</article>
    </section>`;
}
function metric(label, value, note) {
  const s = String(value || "UNKNOWN").toUpperCase();
  const tone = ["PASS","READY","ACTIVE","SUCCEEDED"].includes(s) ? "good" : (["FAIL","FAILED","DEGRADED"].includes(s) ? "bad" : (["UNKNOWN","NEEDS_ACTION","WARN","WARNING"].includes(s) ? "warn" : ""));
  return `<article class="panel metric ${tone}"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="note">${esc(note)}</div></article>`;
}
function listRow(primary, secondary, right) {
  return `<div class="list-row"><div><div>${esc(primary)}</div><div class="secondary">${esc(secondary)}</div></div><div>${right}</div></div>`;
}

function cardColumn(card) {
  if (card.status === "BACKLOG") return "BACKLOG";
  if (card.status === "PROCESS") return "PROCESS";
  if (["WAITING_INPUT","WAITING_APPROVAL","FAILED","CANCELLED"].includes(card.status)) return "WAITING";
  if (card.status === "VERIFY") return "VERIFY";
  if (card.status === "DONE") return "DONE";
  return "WAITING";
}
async function renderKanban() {
  const payload = await api("/api/v1/cards");
  const cards = Array.isArray(payload.cards) ? payload.cards : [];
  let detail = null;
  if (app.selectedCard) detail = await optional(`/api/v1/cards/${encodeURIComponent(app.selectedCard)}`);
  if (!detail && cards[0]) { app.selectedCard = cards[0].card_id; detail = await optional(`/api/v1/cards/${encodeURIComponent(cards[0].card_id)}`); }
  main.innerHTML = pageHead("Kanban", "PostgreSQL là authority; Drive chỉ là collaboration replica. Di chuyển trạng thái bằng typed transition.", `<button class="button" data-action="kanban-sync">Sync Drive Inbox</button><button class="button button-primary" data-action="new-card-focus">Tạo Card</button>`) + `
    <section class="kanban-layout">
      <div>
        <form id="new-card-form" class="panel inline-form" style="margin-bottom:14px">
          <label>Tiêu đề<input id="new-card-title" required maxlength="200" placeholder="Việc cần giao"></label>
          <label>Mô tả<input id="new-card-description" maxlength="4000" placeholder="Outcome / acceptance ngắn"></label>
          <button class="button button-primary" type="submit">Tạo Card</button>
        </form>
        <div class="kanban-board">${CARD_COLUMNS.map(([key,label,tone]) => `<section class="kanban-column"><div class="kanban-head ${tone}"><span>${label}</span><span>${cards.filter((c) => cardColumn(c) === key).length}</span></div>${cards.filter((c) => cardColumn(c) === key).map(renderCardButton).join("") || `<div class="muted" style="font-size:10px">Trống</div>`}</section>`).join("")}</div>
      </div>
      <aside class="panel detail">${renderCardDetail(detail)}</aside>
    </section>`;
  document.getElementById("new-card-form").addEventListener("submit", onCreateCard);
}
function renderCardButton(card) {
  const active = card.card_id === app.selectedCard ? " style=\"border-color:#26d5ff\"" : "";
  return `<button type="button" class="card-item" data-card="${esc(card.card_id)}"${active}><div class="card-id">${esc((card.card_id || "").slice(0,8))}</div><div class="card-title">${esc(card.title || "Không tiêu đề")}</div>${badge(card.status)}<div class="secondary">${esc(card.assignee_agent_id || "Chưa giao Agent")}</div></button>`;
}
function renderCardDetail(detailPayload) {
  const detail = detailPayload?.card || detailPayload || null;
  if (!detail) return `<h2>Chi tiết Card</h2>${empty("Chọn một Card để xem evidence và thao tác.")}`;
  const events = Array.isArray(detail.events) ? detail.events : [];
  const artifacts = Array.isArray(detail.artifacts) ? detail.artifacts : [];
  return `<div class="eyebrow">CARD DETAIL</div><h2>${esc(detail.title || "Card")}</h2>${badge(detail.status)}
    ${kv([["Card ID", `<code>${esc(detail.card_id)}</code>`],["Assignee", esc(detail.assignee_agent_id || "Chưa giao")],["Task", `<code>${esc(detail.agent_task_id || "—")}</code>`],["Drive sync", badge(detail.last_sync_state || "UNKNOWN")]])}
    <div class="detail-section"><div class="muted">Mô tả</div><p>${esc(detail.description || "Không có mô tả")}</p></div>
    <form id="card-transition-form" class="detail-section form-grid"><label>Chuyển trạng thái<select id="card-transition-state">${["BACKLOG","PROCESS","WAITING_INPUT","WAITING_APPROVAL","VERIFY","DONE","FAILED","CANCELLED"].map((s)=>`<option ${s===detail.status?"selected":""}>${s}</option>`).join("")}</select></label><button class="button" type="submit">Áp dụng transition</button></form>
    <form id="card-comment-form" class="detail-section form-grid"><label>Comment<textarea id="card-comment-text" required maxlength="4000"></textarea></label><button class="button" type="submit">Thêm comment</button></form>
    <div class="detail-section"><div class="panel-title">EVIDENCE / EVENTS</div>${events.length ? events.slice(-8).reverse().map((e)=>listRow(e.event_type || "event", JSON.stringify(e.payload || {}).slice(0,160), `<code>${esc((e.event_id||"").slice(0,8))}</code>`)).join("") : empty("Chưa có event")}</div>
    <div class="detail-section"><div class="panel-title">ARTIFACTS</div>${artifacts.length ? artifacts.map((a)=>listRow(a.name || a.artifact_id || "artifact", a.state || a.mime_type || "", `<code>${esc((a.artifact_id||"").slice(0,8))}</code>`)).join("") : empty("Chưa có artifact")}</div>`;
}
async function onCreateCard(event) {
  event.preventDefault();
  const title = document.getElementById("new-card-title").value.trim();
  const description = document.getElementById("new-card-description").value.trim();
  const result = await api("/api/v1/cards", {method:"POST", body:{title,description,assignee_agent_id:"agy-gen"}});
  app.selectedCard = result.card?.card_id || null; toastMsg("Đã tạo Card"); await renderKanban();
}

async function renderAgy() {
  const [agentP, tasksP] = await Promise.all([api("/api/v1/agents/agy-gen"), api("/api/v1/agents/agy-gen/tasks")]);
  const agent = agentP.agent || {};
  const status = agent.status || {};
  const runtime = status.runtime || {};
  const provider = status.provider || {};
  const auth = agent.auth || {};
  const tasks = Array.isArray(tasksP.tasks) ? tasksP.tasks : [];
  const authUrl = [auth.auth_url, auth.url, auth.verification_uri].find((v) => typeof v === "string" && /^https:\/\//.test(v));
  main.innerHTML = pageHead("Agy-gen Chat / Runtime", "Chat và Work là hai cách nhìn cùng durable Agent/task truth. Không có shell tự do từ browser.", `<button class="button" data-action="agent-auth-start">Bắt đầu OAuth</button><button class="button" data-action="agent-verify">Verify model</button><button class="button" data-action="agent-restart">Restart runtime</button>`) + `
    <section class="agy-layout">
      <aside class="panel"><h2 class="panel-title">AGENT PRESENCE</h2><div class="room-list"><div class="room active">agy-gen ${badge(runtime.state)}</div><div class="room">Provider ${badge(provider.state)}</div><div class="room">Auth ${badge(auth.state)}</div></div>${kv([["Model", `<code>${esc(provider.model || status.identity?.provider_target?.model || "UNKNOWN")}</code>`],["Effort", esc(provider.thinking_level || status.identity?.provider_target?.thinking_level || "UNKNOWN")],["tmux", badge(runtime.tmux_state)]])}</aside>
      <article class="panel conversation"><h2 class="panel-title">TALK / ASSIGN</h2><div class="messages">${tasks.length ? tasks.slice().reverse().map(renderTaskMessage).join("") : empty("Chưa có task. Gửi một yêu cầu để tạo durable work item cho agy-gen.")}</div><form id="agent-task-form" class="composer"><textarea id="agent-task-prompt" required maxlength="65536" placeholder="Giao việc cho agy-gen…"></textarea><div class="row-actions"><button class="button button-primary" type="submit">Giao việc</button><span class="muted">concurrency=1 · task được lưu trước khi worker chạy</span></div></form></article>
      <aside class="panel"><h2 class="panel-title">AUTH / RUNTIME EVIDENCE</h2>${authUrl ? `<a class="button button-primary" href="${esc(authUrl)}" target="_blank" rel="noopener noreferrer">Mở trang xác thực</a>` : ""}<div class="terminal">AUTH STATE: ${esc(auth.state || "UNKNOWN")}\nRUNTIME: ${esc(runtime.state || "UNKNOWN")}\nTMUX: ${esc(runtime.tmux_state || "UNKNOWN")}\nPROVIDER: ${esc(provider.state || "UNKNOWN")}\nMODEL: ${esc(provider.model || "UNKNOWN")}\nEVIDENCE: ${esc(provider.evidence || runtime.reason || "UNKNOWN")}</div>${auth.state === "WAITING_CODE" ? `<form id="agent-code-form" class="form-grid" style="margin-top:12px"><label>Authorization code<input id="agent-auth-code" type="password" autocomplete="off" required></label><button class="button" type="submit">Gửi code một chiều</button></form>` : ""}</aside>
    </section>`;
  document.getElementById("agent-task-form").addEventListener("submit", onAgentTask);
  document.getElementById("agent-code-form")?.addEventListener("submit", onAgentCode);
}
function renderTaskMessage(task) {
  const owner = task.prompt ? `<div class="message owner"><div class="who">OWNER · ${esc(fmtTime(task.created_at || task.observed_at))}</div><div class="bubble">${esc(task.prompt)}</div></div>` : "";
  const response = task.output || task.error ? `<div class="message"><div class="who">AGY-GEN · ${esc(task.state || "UNKNOWN")}</div><div class="bubble">${esc(task.output || task.error)}</div></div>` : `<div class="message"><div class="who">AGY-GEN</div><div class="bubble">${badge(task.state)} <span class="muted">Task ${esc((task.task_id||"").slice(0,8))}</span></div></div>`;
  return owner + response;
}
async function onAgentTask(event) {
  event.preventDefault(); const prompt = document.getElementById("agent-task-prompt").value.trim();
  await api("/api/v1/agents/agy-gen/tasks", {method:"POST", body:{prompt}}); toastMsg("Task đã vào durable queue"); await renderAgy();
}
async function onAgentCode(event) {
  event.preventDefault(); const code = document.getElementById("agent-auth-code").value;
  await api("/api/v1/agents/agy-gen/auth/code", {method:"POST", body:{code}}); document.getElementById("agent-auth-code").value=""; toastMsg("Đã chuyển code một chiều vào auth session"); await renderAgy();
}

async function renderLibrary() {
  const payload = await api("/api/v1/agents/agy-gen/library");
  const library = payload.library || {memory:[],skills:[]};
  const items = [...(library.memory || []), ...(library.skills || [])];
  if (!app.selectedLibrary && items[0]) app.selectedLibrary = `${items[0].kind}:${items[0].name}`;
  const selected = items.find((i) => `${i.kind}:${i.name}` === app.selectedLibrary) || null;
  const activeRevision = selected?.revisions?.find((r) => r.active) || selected?.revisions?.[0] || null;
  main.innerHTML = pageHead("Memory & Skills", "Revision authority tách khỏi tmux/provider. Activate, rollback và disable đều là typed binding trên durable Agent state.") + `
    <section class="library-layout">
      <aside class="panel"><h2 class="panel-title">LIBRARY</h2><div class="library-list">${items.length ? items.map((i)=>`<button type="button" data-library="${esc(`${i.kind}:${i.name}`)}" class="${`${i.kind}:${i.name}`===app.selectedLibrary?"active":""}"><strong>${esc(i.name)}</strong><div class="secondary">${esc(i.kind)} · r${esc(i.active_revision)} · ${esc(i.state)}</div></button>`).join("") : empty("Chưa có memory/skill revision")}</div></aside>
      <article class="panel"><h2 class="panel-title">REVISION CONTENT</h2>${selected ? `<div class="row-actions">${badge(selected.state)}<code>${esc(selected.kind)}:${esc(selected.name)}</code></div><div class="revision-content">${esc(activeRevision?.content || "Không có content")}</div><div class="detail-section"><div class="panel-title">HISTORY</div>${selected.revisions.map((r)=>`<div class="list-row"><div><strong>r${esc(r.revision)}</strong><div class="secondary">${esc(r.source || "unknown")} · ${esc(fmtTime(r.created_at))}</div></div><div class="row-actions">${badge(r.state)}<button class="button" type="button" data-library-activate="${esc(selected.kind)}|${esc(selected.name)}|${esc(r.revision)}">Activate</button></div></div>`).join("")}</div>` : empty("Chọn một item")}</article>
      <aside class="panel"><h2 class="panel-title">NEW REVISION</h2><form id="library-form" class="form-grid"><label>Loại<select id="library-kind"><option value="memory">memory</option><option value="skill">skill</option></select></label><label>Tên<input id="library-name" required maxlength="128"></label><label>Nội dung<textarea id="library-content" required rows="9"></textarea></label><button class="button button-primary" type="submit">Lưu & activate</button></form>${selected ? `<div class="detail-section"><button class="button button-danger" type="button" data-library-disable="${esc(selected.kind)}|${esc(selected.name)}">Disable item</button></div>` : ""}</aside>
    </section>`;
  document.getElementById("library-form").addEventListener("submit", onLibraryRevision);
}
async function onLibraryRevision(event) {
  event.preventDefault();
  await api("/api/v1/agents/agy-gen/library/revisions", {method:"POST", body:{kind:document.getElementById("library-kind").value,name:document.getElementById("library-name").value.trim(),content:document.getElementById("library-content").value}});
  app.selectedLibrary = `${document.getElementById("library-kind").value}:${document.getElementById("library-name").value.trim()}`; toastMsg("Đã tạo và activate revision"); await renderLibrary();
}

async function renderConnections() {
  const [credP, driveP, oauthP, mcpP, principalsP, upstreamsP, auditP] = await Promise.all([
    optional("/api/v1/credentials"), optional("/api/v1/drive"), optional("/api/v1/drive/oauth"), optional("/api/v1/mcp"),
    optional("/api/v1/mcp/principals"), optional("/api/v1/mcp/upstreams"), optional("/api/v1/mcp/audit"),
  ]);
  const credentials = credP?.credentials || [];
  const drive = driveP?.drive || {};
  const oauth = oauthP?.oauth || {};
  const mcp = mcpP?.mcp || {};
  const principals = principalsP?.principals || [];
  const upstreams = upstreamsP?.upstreams || [];
  const audit = auditP?.audit || [];
  document.getElementById("mcp-mini").textContent = `MCP: ${mcp.endpoint || "UNKNOWN"}`;
  main.innerHTML = pageHead("Connections & Credentials", "User-owned OAuth, SecretRef và Unified MCP Hub. Raw credential không có GET API và token Agent chỉ hiển thị một lần.") + `
    <section class="panel hub-hero"><div><div class="eyebrow">UNIFIED MCP HUB</div><div class="endpoint">${esc(mcp.endpoint || "UNKNOWN")}</div></div><div>${badge(mcp.endpoint ? "READY" : "UNKNOWN")}</div><div><div class="muted">Principals</div><strong>${esc(mcp.principal_count ?? principals.length)}</strong></div><div><div class="muted">Upstreams</div><strong>${esc(upstreams.length)}</strong></div><div><div class="muted">Protocol</div><code>${esc(mcp.protocol_version || "UNKNOWN")}</code></div></section>
    <section class="grid connections" style="margin-top:14px">
      <article class="panel"><h2 class="panel-title">CREDENTIAL MANAGER</h2>${credentials.length ? `<div class="list">${credentials.map((c)=>`<div class="list-row"><div><strong>${esc(c.name)}</strong><div class="secondary">${esc(c.provider)} · r${esc(c.active_revision)} · ${esc((c.consumer_scopes||[]).join(", ") || "no scopes")}</div></div><div class="row-actions">${badge(c.status)}<button class="button" type="button" data-credential-test="${esc(c.secret_id)}">Test</button><button class="button button-danger" type="button" data-credential-disable="${esc(c.secret_id)}">Disable</button></div></div>`).join("")}</div>` : empty("Chưa có SecretRef")}
        <form id="credential-form" class="detail-section form-grid"><label>Tên<input id="credential-name" required></label><label>Provider<input id="credential-provider" required></label><label>Consumer scopes<input id="credential-scopes" placeholder="agy-gen, mcp-hub"></label><label>Secret (one-way)<input id="credential-secret" type="password" autocomplete="off" required></label><button class="button" type="submit">Add credential</button></form></article>
      <article class="panel"><h2 class="panel-title">GOOGLE DRIVE GUIDED SETUP</h2>${kv([["Binding", badge(drive.state)],["Account", esc(drive.account_email || "Chưa xác thực")],["Root", `<code>${esc(drive.root_folder_id || "UNKNOWN")}</code>`],["OAuth", badge(oauth.state)]])}<div class="oauth-steps">1. Start OAuth → 2. mở verification URI → 3. nhập user code ở Google → 4. Poll → 5. GenOS tự bootstrap/read/write/update verify → READY.</div>${oauth.verification_uri ? `<div class="detail-section"><a class="button button-primary" target="_blank" rel="noopener noreferrer" href="${esc(oauth.verification_uri)}">Mở Google verification</a><div class="secret-once">User code: ${esc(oauth.user_code || "UNKNOWN")}</div></div>` : ""}<div class="row-actions detail-section"><button class="button button-primary" type="button" data-action="drive-oauth-start">Start OAuth</button><button class="button" type="button" data-action="drive-oauth-poll">Poll</button><button class="button" type="button" data-action="drive-reconnect">Reconnect</button><button class="button button-danger" type="button" data-action="drive-disconnect">Disconnect</button></div></article>
      <article class="panel"><h2 class="panel-title">MCP PRINCIPALS / GRANTS</h2>${principals.length ? principals.map((p)=>`<div class="list-row"><div><strong>${esc(p.name || p.principal_id)}</strong><div class="secondary">${esc((p.scopes||[]).join(", ") || "deny by default")}</div></div><div class="row-actions">${badge(p.status)}<button class="button" type="button" data-principal-rotate="${esc(p.principal_id)}">Rotate</button><button class="button button-danger" type="button" data-principal-revoke="${esc(p.principal_id)}">Revoke</button></div></div>`).join("") : empty("Chưa có external Agent principal")}<form id="principal-form" class="detail-section form-grid"><label>Tên Agent/client<input id="principal-name" required></label><label>Scopes<input id="principal-scopes" placeholder="genos.status, github.*"></label><button class="button" type="submit">Tạo principal</button></form></article>
    </section>
    <section class="grid two" style="margin-top:14px"><article class="panel"><h2 class="panel-title">UPSTREAM MCP REGISTRY</h2>${upstreams.length ? upstreams.map((u)=>listRow(`${u.namespace || "upstream"} · ${u.name || ""}`, u.endpoint || "", badge(u.status || u.state))).join("") : empty("Chưa đăng ký upstream MCP")}<form id="upstream-form" class="detail-section form-grid"><label>Namespace<input id="upstream-namespace" required placeholder="github"></label><label>Tên<input id="upstream-name" required></label><label>Endpoint<input id="upstream-endpoint" required placeholder="https://…"></label><label>SecretRef ID (optional)<input id="upstream-secret-id"></label><button class="button" type="submit">Register upstream</button></form></article><article class="panel"><h2 class="panel-title">SANITIZED INVOCATION AUDIT</h2>${audit.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Time</th><th>Principal</th><th>Tool</th><th>Decision</th></tr></thead><tbody>${audit.slice(0,40).map((a)=>`<tr><td>${esc(fmtTime(a.started_at || a.created_at))}</td><td>${esc(a.principal_id || "UNKNOWN")}</td><td>${esc(a.tool || a.tool_name || a.namespace || "UNKNOWN")}</td><td>${badge(a.decision || a.state)}</td></tr>`).join("")}</tbody></table></div>` : empty("Chưa có invocation audit")}</article></section>`;
  document.getElementById("credential-form").addEventListener("submit", onCredentialAdd);
  document.getElementById("principal-form").addEventListener("submit", onPrincipalAdd);
  document.getElementById("upstream-form").addEventListener("submit", onUpstreamAdd);
}
async function onCredentialAdd(event) {
  event.preventDefault(); const scopes = document.getElementById("credential-scopes").value.split(",").map((s)=>s.trim()).filter(Boolean);
  await api("/api/v1/credentials", {method:"POST", body:{name:document.getElementById("credential-name").value.trim(),provider:document.getElementById("credential-provider").value.trim(),secret:document.getElementById("credential-secret").value,consumer_scopes:scopes}});
  document.getElementById("credential-secret").value=""; toastMsg("Credential đã lưu qua SecretProvider/SecretRef"); await renderConnections();
}
async function onPrincipalAdd(event) {
  event.preventDefault(); const scopes = document.getElementById("principal-scopes").value.split(",").map((s)=>s.trim()).filter(Boolean);
  const result = await api("/api/v1/mcp/principals", {method:"POST", body:{name:document.getElementById("principal-name").value.trim(),scopes}});
  showOneTimeToken(result.mcp?.token || result.mcp?.credential || result.mcp?.plain_token || result.mcp?.access_token); await renderConnections();
}
async function onUpstreamAdd(event) {
  event.preventDefault(); const secret = document.getElementById("upstream-secret-id").value.trim();
  await api("/api/v1/mcp/upstreams", {method:"POST", body:{namespace:document.getElementById("upstream-namespace").value.trim(),name:document.getElementById("upstream-name").value.trim(),endpoint:document.getElementById("upstream-endpoint").value.trim(),secret_id:secret || null}}); toastMsg("Đã đăng ký upstream MCP"); await renderConnections();
}
function showOneTimeToken(token) {
  const dialog = document.getElementById("one-time-dialog"); document.getElementById("one-time-token").textContent = token || "Token không có trong response — kiểm tra evidence/API contract."; dialog.showModal();
}

async function renderReports() {
  const [jobsP, historyP, driveP] = await Promise.all([api("/api/v1/jobs"), api("/api/v1/reports/history"), optional("/api/v1/drive")]);
  const jobs = jobsP.jobs || [];
  const reports = historyP.reports || [];
  const latest = historyP.latest || reports[0] || null;
  const drive = driveP?.drive || {};
  main.innerHTML = pageHead("Reports & Job progress", "JobRun và report history tồn tại qua refresh. Drive report là projection từ shared observability authority.", `<button class="button button-primary" data-action="report-publish">Publish System Report</button>`) + `
    <section class="grid report-top"><article class="panel"><h2 class="panel-title">LATEST SYSTEM REPORT</h2>${latest ? `${kv([["Recorded", esc(fmtTime(latest.recorded_at))],["Fingerprint", `<code>${esc(latest.fingerprint || "UNKNOWN")}</code>`],["Diff", badge(latest.diff?.state)],["Drive", badge(drive.state)]])}<div class="progress"><span style="width:100%"></span></div>` : empty("Chưa có report history")}</article><article class="panel"><h2 class="panel-title">JOB ACTIVITY</h2>${jobs.length ? jobs.slice(0,8).map((j)=>`<div class="list-row"><div><strong>${esc(j.kind || j.job_id)}</strong><div class="secondary">${esc(j.current_step || "unknown step")} · ${esc(fmtTime(j.updated_at))}</div><div class="progress"><span style="width:${Math.max(0,Math.min(100,Number(j.progress_percent)||0))}%"></span></div></div><div>${badge(j.state)}</div></div>`).join("") : empty("Chưa có durable JobRun")}</article></section>
    <section class="grid two" style="margin-top:14px"><article class="panel"><h2 class="panel-title">REPORT HISTORY / DIFF</h2>${reports.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Time</th><th>Job</th><th>Mode</th><th>Diff</th><th>Fingerprint</th></tr></thead><tbody>${reports.map((r)=>`<tr><td>${esc(fmtTime(r.recorded_at))}</td><td><code>${esc((r.job_id||"").slice(0,10))}</code></td><td>${r.manual?"MANUAL":"SCHEDULED"}</td><td>${badge(r.diff?.state)}</td><td><code>${esc((r.fingerprint||"UNKNOWN").slice(0,24))}</code></td></tr>`).join("")}</tbody></table></div>` : empty("Chưa có report history")}</article><article class="panel"><h2 class="panel-title">ALL JOBRUN</h2>${jobs.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Job</th><th>Kind</th><th>State</th><th>Progress</th><th>Step</th></tr></thead><tbody>${jobs.slice(0,100).map((j)=>`<tr><td><code>${esc((j.job_id||"").slice(0,10))}</code></td><td>${esc(j.kind || "UNKNOWN")}</td><td>${badge(j.state)}</td><td>${esc(j.progress_percent ?? "UNKNOWN")}%</td><td>${esc(j.current_step || "UNKNOWN")}</td></tr>`).join("")}</tbody></table></div>` : empty("Chưa có JobRun")}</article></section>`;
}

async function handleAction(action, target) {
  if (action === "kanban-sync") { await api("/api/v1/kanban/sync", {method:"POST"}); toastMsg("Đã chạy typed Drive Inbox sync"); return renderKanban(); }
  if (action === "new-card-focus") { document.getElementById("new-card-title")?.focus(); return; }
  if (action === "agent-auth-start") { await api("/api/v1/agents/agy-gen/auth/start", {method:"POST", body:{}}); toastMsg("Đã khởi động auth window"); return renderAgy(); }
  if (action === "agent-verify") { await api("/api/v1/agents/agy-gen/auth/verify", {method:"POST"}); toastMsg("Đã chạy direct provider/model probe"); return renderAgy(); }
  if (action === "agent-restart") { await api("/api/v1/agents/agy-gen/runtime/restart", {method:"POST"}); toastMsg("Runtime đã restart, Agent identity được giữ nguyên"); return renderAgy(); }
  if (action === "drive-oauth-start") { await api("/api/v1/drive/oauth/start", {method:"POST", body:{}}); toastMsg("OAuth session đã bắt đầu"); return renderConnections(); }
  if (action === "drive-oauth-poll") { await api("/api/v1/drive/oauth/poll", {method:"POST"}); toastMsg("Đã poll Google OAuth"); return renderConnections(); }
  if (action === "drive-reconnect") { await api("/api/v1/drive/reconnect", {method:"POST", body:{}}); toastMsg("Đã chạy reconnect state machine"); return renderConnections(); }
  if (action === "drive-disconnect") { await api("/api/v1/drive/disconnect", {method:"POST"}); toastMsg("Đã unbind Drive; remote content không bị xóa"); return renderConnections(); }
  if (action === "report-publish") { await api("/api/v1/reports/system", {method:"POST"}); toastMsg("System Report JobRun đã hoàn tất hoặc trả typed state"); return renderReports(); }
}

main.addEventListener("click", async (event) => {
  const target = event.target.closest("button,a"); if (!target) return;
  try {
    if (target.dataset.card) { app.selectedCard = target.dataset.card; return renderKanban(); }
    if (target.dataset.library) { app.selectedLibrary = target.dataset.library; return renderLibrary(); }
    if (target.dataset.action) return await handleAction(target.dataset.action, target);
    if (target.dataset.libraryActivate) { const [kind,name,revision] = target.dataset.libraryActivate.split("|"); await api("/api/v1/agents/agy-gen/library/activate", {method:"POST",body:{kind,name,revision:Number(revision)}}); toastMsg("Đã activate revision"); return renderLibrary(); }
    if (target.dataset.libraryDisable) { const [kind,name] = target.dataset.libraryDisable.split("|"); await api("/api/v1/agents/agy-gen/library/disable", {method:"POST",body:{kind,name}}); toastMsg("Đã disable library item"); return renderLibrary(); }
    if (target.dataset.credentialTest) { await api(`/api/v1/credentials/${target.dataset.credentialTest}/test`, {method:"POST"}); toastMsg("Credential test PASS/FAIL đã được cập nhật"); return renderConnections(); }
    if (target.dataset.credentialDisable) { await api(`/api/v1/credentials/${target.dataset.credentialDisable}/disable`, {method:"POST"}); toastMsg("Credential đã disable"); return renderConnections(); }
    if (target.dataset.principalRotate) { const result = await api(`/api/v1/mcp/principals/${target.dataset.principalRotate}/rotate`, {method:"POST"}); showOneTimeToken(result.mcp?.token || result.mcp?.credential || result.mcp?.plain_token); return renderConnections(); }
    if (target.dataset.principalRevoke) { await api(`/api/v1/mcp/principals/${target.dataset.principalRevoke}/revoke`, {method:"POST"}); toastMsg("Principal đã revoke"); return renderConnections(); }
  } catch (err) { toastMsg(err.message || String(err), true); }
});
main.addEventListener("submit", async (event) => {
  if (event.target.id === "card-transition-form") {
    event.preventDefault(); try { await api(`/api/v1/cards/${app.selectedCard}/transition`, {method:"POST",body:{to_state:document.getElementById("card-transition-state").value,reason:"OWNER_UI"}}); toastMsg("Card đã transition"); await renderKanban(); } catch (err) { toastMsg(err.message,true); }
  }
  if (event.target.id === "card-comment-form") {
    event.preventDefault(); try { await api(`/api/v1/cards/${app.selectedCard}/comment`, {method:"POST",body:{text:document.getElementById("card-comment-text").value}}); toastMsg("Đã thêm comment"); await renderKanban(); } catch (err) { toastMsg(err.message,true); }
  }
});

document.getElementById("nav").addEventListener("click", (event) => { const button = event.target.closest("[data-route]"); if (button) setRoute(button.dataset.route); });
document.getElementById("refresh-btn").addEventListener("click", () => render().catch(renderError));
document.getElementById("logout-btn").addEventListener("click", async () => { try { if (app.token) await api("/api/v1/auth/logout", {method:"POST"}); } catch (_err) {} clearSession(); showAuth(); });
window.addEventListener("popstate", () => setRoute(routeFromLocation(), false));

document.getElementById("show-bootstrap").addEventListener("click", () => { loginForm.classList.add("hidden"); bootstrapForm.classList.remove("hidden"); document.getElementById("bootstrap-username").focus(); });
document.getElementById("show-login").addEventListener("click", () => { bootstrapForm.classList.add("hidden"); loginForm.classList.remove("hidden"); document.getElementById("login-username").focus(); });
loginForm.addEventListener("submit", async (event) => {
  event.preventDefault(); const error = document.getElementById("auth-error"); error.textContent="";
  try {
    const payload = await api("/api/v1/auth/login", {method:"POST",auth:false,body:{username:document.getElementById("login-username").value,password:document.getElementById("login-password").value}});
    app.token = payload.session_token; app.owner = payload.owner; sessionStorage.setItem("genos.session", app.token); document.getElementById("login-password").value=""; hideAuth(); await api("/api/v1/auth/me"); setRoute(app.route,false);
  } catch (err) { error.textContent = err.message || String(err); }
});
bootstrapForm.addEventListener("submit", async (event) => {
  event.preventDefault(); const error = document.getElementById("bootstrap-error"); error.textContent="";
  try { await api("/api/v1/owner/bootstrap", {method:"POST",auth:false,body:{username:document.getElementById("bootstrap-username").value,password:document.getElementById("bootstrap-password").value}}); document.getElementById("bootstrap-password").value=""; bootstrapForm.classList.add("hidden"); loginForm.classList.remove("hidden"); toastMsg("Owner đã được bootstrap. Hãy đăng nhập."); } catch (err) { error.textContent = err.message || String(err); }
});
document.getElementById("copy-token").addEventListener("click", async () => { const value=document.getElementById("one-time-token").textContent; try { await navigator.clipboard.writeText(value); toastMsg("Đã copy one-time token"); } catch (_err) { toastMsg("Browser không cho clipboard; hãy chọn và copy thủ công", true); } });
document.getElementById("close-token").addEventListener("click", () => { document.getElementById("one-time-token").textContent=""; document.getElementById("one-time-dialog").close(); });
document.getElementById("one-time-dialog").addEventListener("close", () => { document.getElementById("one-time-token").textContent=""; });

(async function boot() {
  document.getElementById("route-title").textContent = ROUTES[app.route];
  document.querySelectorAll("#nav [data-route]").forEach((b) => b.classList.toggle("active", b.dataset.route === app.route));
  if (!app.token) { showAuth(); return; }
  try { const me = await api("/api/v1/auth/me"); app.owner = me.owner; hideAuth(); await render(); } catch (_err) { showAuth(); }
})();
