// Drawers and management panels for server, providers, skills, and memories.
function openPanel(type) {
  const panel = $("#panel");
  const normalized = type === "providers" ? "server" : type;
  panel.dataset.panelType = normalized;
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
  $("#connection-status")?.classList.add("panel-open");
  if (normalized === "server") renderServerPanel(type === "providers");
  if (normalized === "memories") renderMemoryPanel();
  if (normalized === "skills") renderSkillPanel();
  if (normalized === "ssh") renderSSHPanel(true);
  if (normalized === "trash") renderTrashPanel();
}
function closePanel() { $("#panel").classList.remove("open"); $("#panel").setAttribute("aria-hidden", "true"); if (!$("#drawer")?.classList.contains("open")) $("#connection-status")?.classList.remove("panel-open"); }
function refreshOpenPanel() {
  const panel = $("#panel");
  if (!panel?.classList.contains("open")) return;
  if (panel.dataset.panelType === "server") renderServerPanel();
  if (panel.dataset.panelType === "memories") renderMemoryPanel($("#memory-search")?.value || "");
  if (panel.dataset.panelType === "skills") renderSkillPanel();
  if (panel.dataset.panelType === "trash") renderTrashPanel();
  if (panel.dataset.panelType === "ssh") renderSSHPanel(false);
}
function providerConnectionRows(current) {
  if (!state.providers.length) return `<div class="empty connection-empty">${uiLabel("noProviders")}</div>`;
  return state.providers.map(item => {
    const health = providerHealth(item);
    const selected = current?.id === item.id;
    const inUse = Number(item.in_use_count || 0) > 0;
    const latency = item.health_latency_ms == null ? "--" : `${Number(item.health_latency_ms)}ms`;
    const useButton = selected ? `<button class="provider-use current" disabled>${uiLabel("current")}</button>` : item.enabled === false ? `<button class="provider-use" disabled>${uiLabel("unavailable")}</button>` : `<button data-provider-use="${esc(item.id)}" class="provider-use">${uiLabel("use")}</button>`;
    const badge = selected ? uiLabel("current") : inUse ? t("running") : item.is_default ? uiLabel("default") : uiLabel("priority", { value: Number(item.priority || 0) });
    const defaultAddress = uiLabel("codexDefaultAddress");
    return `<article class="connection-provider-item ${selected ? "current" : ""} ${item.enabled === false ? "disabled" : ""}"><div class="connection-provider-head"><div class="connection-provider-name"><strong>${esc(item.name)}</strong><span>${badge}</span></div><span class="provider-health ${esc(health.kind)}"><i></i>${esc(health.label)}</span></div><div class="connection-provider-route"><strong>${esc(item.model || item.model_provider || uiLabel("providerDefaultModel"))}</strong><span title="${esc(item.base_url || defaultAddress)}">${esc(item.base_url || defaultAddress)}</span></div><div class="connection-provider-meta"><span>${item.enabled === false ? uiLabel("excludedFromFailover") : uiLabel("priority", { value: Number(item.priority || 0) })}</span><span>${esc(item.credential_source || uiLabel("apiKeyUnset"))}</span><span>${uiLabel("latency", { value: latency })}</span><span>${uiLabel("successFailure", { success: Number(item.success_count || 0), failure: Number(item.failure_count || 0) })}</span></div><div class="connection-provider-actions">${useButton}<button data-provider-check="${esc(item.id)}">${uiLabel("check")}</button><button data-provider-edit="${esc(item.id)}">${uiLabel("edit")}</button><button class="danger-text" data-provider-delete="${esc(item.id)}">${uiLabel("delete")}</button></div></article>`;
  }).join("");
}
async function renderCodexManagement() {
  const section = $("#codex-management");
  if (!section) return;
  try {
    const info = await api("/codex/manage");
    state.codexInfo = info;
    const plan = info.update_plan || {};
    section.innerHTML = `<div class="connection-section-head"><div><span class="panel-section-label">CODEX</span><h3>${uiLabel("codexManagement")}</h3></div><button id="update-codex" class="secondary" ${plan.supported ? "" : "disabled"}>${uiLabel("updateCodex")}</button></div><div class="codex-management-grid"><div><small>${uiLabel("codexVersion")}</small><strong>${esc(info.version || uiLabel("codexVersionUnknown"))}</strong></div><div><small>${uiLabel("codexSource")}</small><strong>${esc(info.source || "missing")}</strong></div><div class="codex-management-wide"><small>${uiLabel("codexPath")}</small><code title="${esc(info.path || "")}">${esc(info.path || uiLabel("codexMissing"))}</code></div><div class="codex-management-wide"><small>${uiLabel("codexWorkingDirectory")}</small><code title="${esc(info.working_directory || "")}">${esc(info.working_directory || "")}</code></div></div>${info.error ? `<p class="connection-inline-error">${esc(info.error)}</p>` : ""}`;
  } catch (error) {
    section.innerHTML = `<div class="connection-section-head"><div><span class="panel-section-label">CODEX</span><h3>${uiLabel("codexManagement")}</h3></div></div><p class="connection-inline-error">${esc(error.message)}</p>`;
  }
}
function renderServerPanel(focusProviders = false) {
  const info = state.serverInfo || {};
  const host = location.hostname || info.hostname || "网页服务器";
  const port = location.port || info.bind_port || 8787;
  const current = currentProvider();
  const enabled = state.providers.filter(item => item.enabled !== false);
  const usable = enabled.filter(providerUsable);
  const metrics = state.runtimeMetrics || {};
  const realtime = realtimeChannels.overview === "live" ? uiLabel("liveConnected") : realtimeChannels.overview === "reconnecting" ? uiLabel("reconnecting") : uiLabel("connecting");
  $("#panel-content").innerHTML = `<div class="connection-panel"><div class="panel-title connection-panel-title"><span class="eyebrow">CONNECTION</span><h2>${uiLabel("connectionAndProviders")}</h2><p>${uiLabel("connectionDescription")}</p></div><section class="connection-overview" aria-label="${uiLabel("webService")}"><div class="connection-server"><span class="connection-server-mark" aria-hidden="true"><i></i></span><div><strong>${esc(info.user || uiLabel("serviceUser"))}@${esc(host)}</strong><small>HTTP ${esc(host)}:${esc(port)} · ${esc(info.hostname || host)}</small></div><span class="provider-health healthy"><i></i>${realtime}</span></div><div class="connection-facts"><div><small>${uiLabel("currentProvider")}</small><strong>${esc(current?.name || uiLabel("automatic"))}</strong></div><div><small>${uiLabel("usableRoutes")}</small><strong>${usable.length} / ${enabled.length}</strong></div><div><small>TTFT</small><strong>${formatMetricMs(metrics.ttftMs)}</strong></div><div><small>TPOT</small><strong>${formatMetricMs(metrics.tpotMs)}</strong></div></div><div class="connection-server-meta"><span>${state.codexAvailable ? uiLabel("codexReady") : uiLabel("codexMissing")}</span><span>${uiLabel("runningTasks", { count: Number(info.running || 0) })}</span><span title="${esc(info.default_workspace || "")}">${esc(info.default_workspace || uiLabel("workspaceUnset"))}</span></div></section><section id="codex-management" class="codex-management"></section><section id="connection-providers" class="connection-providers"><div class="connection-section-head"><div><span class="panel-section-label">PROVIDER ROUTING</span><h3>Provider</h3><p>${uiLabel("providerRoutingDescription")}</p></div><div class="connection-section-actions"><button id="sync-providers" class="secondary">${uiLabel("sync")}</button><button id="check-providers" class="secondary">${uiLabel("checkAll")}</button><button id="new-provider" class="primary">${uiLabel("add")}</button></div></div><div class="connection-provider-list">${providerConnectionRows(current)}</div></section></div>`;
  renderCodexManagement();
  if (focusProviders) requestAnimationFrame(() => $("#connection-providers")?.scrollIntoView({ block: "start" }));
}
async function renderTrashPanel() {
  const content = $("#panel-content");
  content.innerHTML = `<div class="panel-title"><span class="eyebrow">RECYCLE BIN</span><h2>${t("trash")}</h2><p>${uiLabel("recycleBinDescription")}</p></div><div class="panel-list"><div class="empty">${uiLabel("readingTrash")}</div></div>`;
  try {
    const rows = await api("/trash");
    const list = rows.map(task => `<article class="panel-item trash-item"><div class="panel-item-head"><strong>${esc(task.name || "Codex")}</strong><span class="source-badge readonly">${uiLabel("deleted")}</span></div><small>${esc(task.trashed_at ? shortDate(task.trashed_at) : shortDate(task.updated_at))} · ${esc(task.workspace || "")}</small><p>${esc(task.goal || task.prompt || uiLabel("emptySession"))}</p><div class="panel-item-actions"><button data-trash-restore="${esc(task.id)}">${uiLabel("restore")}</button><button class="danger" data-trash-delete="${esc(task.id)}">${uiLabel("permanentDelete")}</button></div></article>`).join("");
    content.querySelector(".panel-list").innerHTML = list || `<div class="empty">${uiLabel("emptyTrash")}</div>`;
  } catch (error) { content.querySelector(".panel-list").innerHTML = `<p class="inspector-error">${esc(error.message)}</p>`; }
}
function renderMemoryPanel(query = "") {
  const needle = query.trim().toLowerCase();
  const files = state.memories.filter(item => !needle || `${item.name} ${item.preview || ""}`.toLowerCase().includes(needle));
  const generated = state.generatedMemories.filter(item => !needle || `${item.thread_id} ${item.slug || ""} ${item.preview || ""}`.toLowerCase().includes(needle));
  $("#panel-content").innerHTML = `<div class="panel-title"><span class="eyebrow">NATIVE MEMORY</span><h2>${uiLabel("codexMemory")}</h2><p>${uiLabel("memorySummary", { markdown: state.memories.length, generated: state.generatedMemories.length })}</p></div><div class="panel-toolbar"><input id="memory-search" value="${esc(query)}" placeholder="${uiLabel("searchMemory")}"/><button id="new-memory" class="primary">${uiLabel("newItem")}</button></div><span class="panel-section-label">MARKDOWN</span><div class="panel-list">${files.slice(0, 80).map(item => `<article class="panel-item"><div class="panel-item-head"><strong>${esc(item.name)}</strong><span class="source-badge">${uiLabel("editable")}</span></div><small>${formatBytes(item.size)} · ${esc(shortDate(item.updated_at))}</small><p>${esc(item.preview || "")}</p><div class="panel-item-actions"><button data-memory-view="${esc(item.name)}">${uiLabel("view")}</button><button data-memory-edit="${esc(item.name)}">${uiLabel("edit")}</button><button data-memory-delete="${esc(item.name)}">${uiLabel("delete")}</button></div></article>`).join("") || `<div class="empty">${uiLabel("noMarkdownMemories")}</div>`}</div><span class="panel-section-label">${uiLabel("generated")}</span><div class="panel-list">${generated.slice(0, 40).map(item => `<article class="panel-item"><div class="panel-item-head"><strong>${esc(item.slug || item.thread_id)}</strong><span class="source-badge readonly">${uiLabel("readOnly")}</span></div><small>${esc(shortDate(item.generated_at))} · ${uiLabel("memoryUsage", { count: item.usage_count || 0 })}</small><p>${esc(item.preview || "")}</p><div class="panel-item-actions"><button data-generated-memory="${esc(item.thread_id)}">${uiLabel("view")}</button></div></article>`).join("") || `<div class="empty">${uiLabel("noGeneratedMemories")}</div>`}</div><button id="reset-generated-memories" class="danger full-button">${uiLabel("rebuildGeneratedMemories")}</button>`;
  const search = $("#memory-search");
  search.oninput = event => { const position = event.target.selectionStart; renderMemoryPanel(event.target.value); const next = $("#memory-search"); next.focus(); next.setSelectionRange(position, position); };
}
function renderSkillPanel() {
  const installed = state.skills.filter(item => item.installed).length;
  $("#panel-content").innerHTML = `<div class="panel-title"><span class="eyebrow">INSTALLED CONTEXT</span><h2>${t("skills")}</h2><p>${uiLabel("installedSkillsSummary", { count: installed })}</p></div><button id="new-skill" class="primary full-button">${uiLabel("newPersonalSkill")}</button><div class="panel-list panel-list-spaced">${state.skills.map(item => `<article class="panel-item"><div class="panel-item-head"><strong>${esc(item.name)}</strong><span class="source-badge ${item.editable ? "" : "readonly"}">${esc(item.source || "Dashboard")}</span></div><small>${item.editable ? uiLabel("editable") : uiLabel("readOnly")}${item.path ? ` · ${esc(item.path)}` : ` · ${item.enabled ? uiLabel("enabled") : uiLabel("disabled")}`}</small><p>${esc(item.description || item.content || "")}</p><div class="panel-item-actions"><button data-skill-edit="${item.id}">${item.editable ? uiLabel("edit") : uiLabel("view")}</button>${item.deletable ? `<button data-skill-delete="${item.id}">${uiLabel("delete")}</button>` : ""}</div></article>`).join("") || `<div class="empty">${uiLabel("noSkills")}</div>`}</div>`;
}
function providerHealth(item) {
  if (item.enabled === false) return { label: uiLabel("disabled"), kind: "disabled" };
  const labels = { healthy: "healthHealthy", reachable: "healthReachable", configured: "healthConfigured", needs_key: "healthNeedsKey", auth_error: "healthAuthError", degraded: "healthDegraded", unavailable: "healthUnreachable", invalid: "healthInvalid", error: "healthError", unchecked: "healthUnchecked" };
  return { label: labels[item.health_status] ? uiLabel(labels[item.health_status]) : item.health_status || uiLabel("healthUnchecked"), kind: item.health_status || "unchecked" };
}
function renderProviderPanel() { renderServerPanel(true); }
function sshStatus(item) {
  const labels = { connected: "connected", connected_no_codex: "codexUnavailable", needs_password: "needsPassword", failed: "connectionFailed", disconnected: "notConnected" };
  return { label: labels[item.status] ? uiLabel(labels[item.status]) : item.status || uiLabel("notConnected"), kind: item.connected ? (item.codex_bin ? "healthy" : "needs_key") : item.status === "needs_password" ? "needs_key" : item.status === "failed" ? "unavailable" : "unchecked" };
}
function drawSSHPanel() {
  const content = $("#panel-content"); if (!content) return;
  const localActive = !state.activeSSHHost;
  const localRow = `<article class="panel-item ssh-item ${localActive ? "active" : ""}"><div class="panel-item-head"><strong>${uiLabel("local")}${localActive ? `<span class="provider-flag live">${uiLabel("default")}</span>` : ""}</strong><span class="provider-health healthy"><i></i>${uiLabel("readyStatus")}</span></div><small>${esc(state.defaultWorkspace || "")}</small><p>Local Codex</p><div class="panel-item-actions"><button data-ssh-use="">${uiLabel("useLocal")}</button></div></article>`;
  const rows = state.sshHosts.slice(0, 1).map(item => { const health = sshStatus(item); const active = state.activeSSHHost === item.alias; const identity = (item.identity_files || []).find(path => !path.includes("_ecdsa") && !path.includes("_ed25519")) || (item.identity_files || [])[0] || "~/.ssh/config"; return `<article class="panel-item ssh-item ${active ? "active" : ""}"><div class="panel-item-head"><strong>${esc(item.alias)}${active ? `<span class="provider-flag live">${uiLabel("default")}</span>` : ""}</strong><span class="provider-health ${esc(health.kind)}"><i></i>${esc(health.label)}</span></div><small>${esc(item.user || uiLabel("defaultUser"))}@${esc(item.hostname || item.alias)}:${item.port || 22}</small><p title="${esc(item.last_error || "")}">${esc(identity)}${item.proxy_jump ? `<br>ProxyJump ${esc(item.proxy_jump)}` : ""}${item.remote_home ? `<br>${esc(item.remote_home)}` : ""}${item.codex_bin ? `<br>${esc(item.codex_bin)}` : ""}</p><div class="panel-item-actions">${item.connected ? `<button data-ssh-use="${esc(item.alias)}">${uiLabel("use")}</button>${item.codex_bin ? "" : `<button data-ssh-install="${esc(item.alias)}">${uiLabel("installCodex")}</button>`}<button data-ssh-disconnect="${esc(item.alias)}">${uiLabel("disconnect")}</button>` : `<button data-ssh-connect="${esc(item.alias)}">${item.status === "needs_password" ? uiLabel("login") : uiLabel("retry")}</button>`}</div></article>`; }).join("") || `<div class="empty">${uiLabel("noFixedServer")}</div>`;
  const fixed = state.sshHosts[0];
  const connectForm = !fixed ? `<form id="ssh-host-form" class="form"><label>${uiLabel("serverAddress")}<input name="host" required autocomplete="off" spellcheck="false" placeholder="192.168.1.20"/></label><label>${uiLabel("sshUsername")}<input name="username" required autocomplete="username" placeholder="root"/></label><label>${uiLabel("port")}<input name="port" type="number" min="1" max="65535" value="22" required/></label><label>${uiLabel("passwordOrPassphrase")}<input name="password" type="password" autocomplete="current-password"/></label><button class="primary">${uiLabel("connectServer")}</button></form>` : "";
  content.innerHTML = `<div class="panel-title"><span class="eyebrow">FIXED CODEX SERVER</span><h2>${uiLabel("serverConnection")}</h2><p>${esc(fixed ? `${fixed.hostname || fixed.alias}:${fixed.port || 22}` : (state.sshConfig || "~/.ssh/config"))} · ~/.ssh / ssh-agent</p></div>${connectForm}<div class="panel-list">${fixed ? rows : ""}</div>`;
  if (!fixed) $("#ssh-host-form").onsubmit = async event => { event.preventDefault(); const form = new FormData(event.target); try { const row = await api("/ssh/connect", { method: "POST", body: JSON.stringify({ host: form.get("host"), username: form.get("username"), port: Number(form.get("port") || 22), password: form.get("password") || null }) }); updateSSHHost(row); state.activeSSHHost = row.alias; localStorage.setItem("codex-dashboard-ssh-host", row.alias); drawSSHPanel(); if (!row.connected && row.status === "needs_password") return openSSHPasswordForm(row); if (!row.connected) toast(row.last_error || "SSH 连接失败"); else toast("Codex 服务器已连接"); } catch (error) { toast(`SSH 连接失败：${error.message}`); } };
}
async function loadSSHHosts(probe = false) {
  const data = await api(`/ssh/hosts?probe=${probe ? "true" : "false"}`);
  state.sshHosts = data.hosts || []; state.sshConfig = data.config || "~/.ssh/config";
  renderConnectionStatus();
  if (state.activeSSHHost && !state.sshHosts.some(item => item.alias === state.activeSSHHost)) { state.activeSSHHost = ""; localStorage.removeItem("codex-dashboard-ssh-host"); }
  return data;
}
async function renderSSHPanel(probe = false) {
  $("#panel-content").innerHTML = `<div class="panel-title"><span class="eyebrow">FIXED CODEX SERVER</span><h2>${uiLabel("serverConnection")}</h2><p>${uiLabel("sshConnectionDescription")}</p></div>`;
  try { await loadSSHHosts(probe); drawSSHPanel(); } catch (error) { $("#panel-content").insertAdjacentHTML("beforeend", `<p class="inspector-error">${esc(error.message)}</p>`); }
}
function updateSSHHost(row) {
  const index = state.sshHosts.findIndex(item => item.alias === row.alias);
  if (index >= 0) state.sshHosts[index] = row; else state.sshHosts.push(row);
  renderConnectionStatus();
}
async function connectSSHHost(host, password = "") {
  if (!host) return;
  try {
    const row = await api("/ssh/connect", { method: "POST", body: JSON.stringify({ host, password: password || null }) });
    updateSSHHost(row);
    if (!row.connected) { drawSSHPanel(); if (row.status === "needs_password") return openSSHPasswordForm(row); return toast(row.last_error || "SSH 主机不可达"); }
    state.activeSSHHost = row.alias; localStorage.setItem("codex-dashboard-ssh-host", row.alias); drawSSHPanel(); toast(`${row.alias} 已连接`);
  } catch (error) { toast(`SSH 连接失败：${error.message}`); }
}
function openSSHPasswordForm(item) {
  openDrawer(`<h2>连接 Codex 服务器</h2><p class="drawer-meta">${esc(item.hostname || item.alias)}:${item.port || 22}</p><form id="ssh-password-form" class="form"><label>SSH 用户名<input name="username" value="${esc(item.user || "")}" required autocomplete="username" autofocus/></label><label>SSH 密码或私钥口令<input name="password" type="password" required autocomplete="current-password"/></label><label>端口<input name="port" type="number" min="1" max="65535" value="${item.port || 22}" required/></label><div class="form-actions"><button type="button" class="secondary" id="cancel">取消</button><button class="primary">连接</button></div></form>`);
  $("#cancel").onclick = closeDrawer;
  $("#ssh-password-form").onsubmit = async event => { event.preventDefault(); const restore = setFormBusy(event.target, "连接中…"); const form = new FormData(event.target); const password = form.get("password"); try { const row = await api("/ssh/connect", { method: "POST", body: JSON.stringify({ host: item.hostname || item.alias, username: form.get("username"), port: Number(form.get("port") || 22), password }) }); updateSSHHost(row); if (row.connected) { state.activeSSHHost = row.alias; localStorage.setItem("codex-dashboard-ssh-host", row.alias); closeDrawer(); openPanel("ssh"); } else restore(); } catch (error) { restore(); toast(`SSH 连接失败：${error.message}`); } };
}
async function checkAllProviders() {
  const button = $("#check-providers");
  if (button) { button.disabled = true; button.textContent = "检查中…"; }
  try {
    state.providers = await api("/providers/check-all", { method: "POST" });
    renderServerPanel(true);
    toast("Provider 检查完成");
  } catch (error) {
    if (button) { button.disabled = false; button.textContent = "全部检查"; }
    throw error;
  }
}
async function checkOneProvider(providerId) {
  const button = $$('[data-provider-check]').find(node => node.dataset.providerCheck === providerId);
  if (button) { button.disabled = true; button.textContent = "检查中…"; }
  try {
    const checked = await api(`/providers/${providerId}/check`, { method: "POST" });
    state.providers = state.providers.map(item => item.id === providerId ? checked : item);
    renderServerPanel(true);
    toast(`${checked.name} 检查完成`);
  } catch (error) {
    if (button) { button.disabled = false; button.textContent = "检查"; }
    throw error;
  }
}
function destroyMarkdownComponents() {
  for (const component of markdownComponents) {
    try { component.destroy(); } catch (_) {}
  }
  markdownComponents = [];
}
function markdownOptions() { return document.documentElement.dataset.theme === "light" ? {} : { theme: "dark" }; }
function openDrawer(html, options = {}) { destroyMarkdownComponents(); $("#drawer-content").innerHTML = html; $("#drawer").classList.toggle("editor-drawer", Boolean(options.editor)); $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false"); $("#connection-status")?.classList.add("panel-open"); }
function closeDrawer() { destroyMarkdownComponents(); $("#drawer").classList.remove("open", "editor-drawer"); $("#drawer").setAttribute("aria-hidden", "true"); if (!$("#panel")?.classList.contains("open")) $("#connection-status")?.classList.remove("panel-open"); }
async function createQuickSession() {
  try {
    // New sessions run on the Codex Partner service host. SSH is opt-in for
    // legacy remote sessions and must never change the webpage connection.
    const created = await api("/tasks/quick", { method: "POST", body: JSON.stringify({ workspace: "", ssh_host: "" }) });
    await refresh(false);
    await selectSession(created.id);
    $("#message-input").focus();
    toast("新会话已创建 · YOLO 已开启");
  } catch (error) { toast(error.message); }
}
function setFormBusy(form, label) {
  const submit = form.querySelector('[type="submit"], button:not([type])');
  if (!submit) return () => {};
  const previous = submit.textContent; submit.disabled = true; submit.textContent = label;
  return () => { submit.disabled = false; submit.textContent = previous; };
}
async function memoryForm(name = "") {
  openDrawer(`<h2>${name ? uiLabel("editMemory") : uiLabel("newMemory")}</h2><p class="drawer-status">${name ? uiLabel("reading") : uiLabel("createMarkdownMemory")}</p>`, { editor: true });
  try {
    const content = name ? (await api(`/memories/${name.split("/").map(encodeURIComponent).join("/")}`)).content || "" : "";
    $("#drawer-content").innerHTML = `<h2>${name ? uiLabel("editMemory") : uiLabel("newMemory")}</h2><form id="memory-form" class="form unified-editor-form"><label>${uiLabel("fileName")}<input name="name" required value="${esc(name)}" ${name ? "readonly" : ""} placeholder="project-notes.md"/></label><div class="editor-field"><span class="form-label">${uiLabel("content")}</span>${textEditorMarkup("memory-editor")}</div><div class="form-actions"><button type="button" class="secondary" id="cancel">${uiLabel("cancel")}</button><button type="submit" class="primary">${uiLabel("save")} <kbd>Ctrl S</kbd></button></div></form>`;
    const editor = mountTextEditor("#memory-editor", { filename: name || "project-notes.md", value: content, placeholder: uiLabel("memoryEditorPlaceholder") });
    $("#cancel").onclick = closeDrawer;
    $("#memory-form").onsubmit = async event => {
      event.preventDefault(); const restore = setFormBusy(event.target, uiLabel("saving")); const form = new FormData(event.target);
      let file = String(form.get("name") || "").trim(); if (!/\.(md|markdown)$/i.test(file)) file += ".md";
      try {
        await api(`/memories/${file.split("/").map(encodeURIComponent).join("/")}`, { method: "PUT", body: JSON.stringify({ name: file, content: editor.getValue() }) });
        closeDrawer(); await refresh(false); openPanel("memories"); toast(uiLabel("memorySaved"));
      } catch (error) { restore(); toast(uiLabel("saveFailed", { message: error.message })); }
    };
  } catch (error) { $("#drawer-content").insertAdjacentHTML("beforeend", `<p class="inspector-error">${esc(error.message)}</p>`); }
}
async function memoryView(name) {
  openDrawer(`<h2>${esc(name)}</h2><p class="drawer-status">${uiLabel("reading")}</p>`);
  try {
    const item = await api(`/memories/${name.split("/").map(encodeURIComponent).join("/")}`);
    $("#drawer-content").innerHTML = `<div class="drawer-heading"><div><h2>${esc(name)}</h2><p class="drawer-meta">TOAST UI Markdown Viewer</p></div><button class="primary" data-memory-edit="${esc(name)}">${uiLabel("edit")}</button></div><div id="memory-viewer" class="memory-viewer"></div>`;
    const viewer = toastui.Editor.factory({ el: $("#memory-viewer"), viewer: true, initialValue: item.content || "", usageStatistics: false, ...markdownOptions() });
    markdownComponents.push(viewer);
  } catch (error) { $("#drawer-content").insertAdjacentHTML("beforeend", `<p class="inspector-error">${esc(error.message)}</p>`); }
}
async function generatedMemoryView(threadId) {
  openDrawer(`<h2>${uiLabel("generated")}</h2><p class="drawer-status">${uiLabel("reading")}</p>`);
  try {
    const item = await api(`/memories/generated/${encodeURIComponent(threadId)}`);
    $("#drawer-content").innerHTML = `<h2>${esc(item.slug || item.thread_id)}</h2><p class="drawer-meta">${esc(item.thread_id)} · ${uiLabel("memoryUsage", { count: item.usage_count || 0 })}</p><div class="readonly-document"><h3>${uiLabel("summary")}</h3><div id="generated-summary" class="memory-viewer"></div><h3>${uiLabel("rawMemory")}</h3><div id="generated-raw" class="memory-viewer"></div></div><div class="form-actions"><button type="button" class="secondary" id="cancel">${uiLabel("close")}</button></div>`;
    const summaryViewer = toastui.Editor.factory({ el: $("#generated-summary"), viewer: true, initialValue: item.rollout_summary || "", usageStatistics: false, ...markdownOptions() });
    const rawViewer = toastui.Editor.factory({ el: $("#generated-raw"), viewer: true, initialValue: item.raw_memory || "", usageStatistics: false, ...markdownOptions() });
    markdownComponents.push(summaryViewer, rawViewer);
    $("#cancel").onclick = closeDrawer;
  } catch (error) { $("#drawer-content").insertAdjacentHTML("beforeend", `<p class="inspector-error">${esc(error.message)}</p>`); }
}
function providerNameFromUrl(value) {
  try { return new URL(value).hostname || "Custom Provider"; } catch (_) { return "Custom Provider"; }
}
function providerEndpointKey(value) {
  try {
    const url = new URL(String(value || "").trim());
    url.hostname = url.hostname.toLowerCase();
    if ((url.protocol === "https:" && url.port === "443") || (url.protocol === "http:" && url.port === "80")) url.port = "";
    url.pathname = url.pathname.replace(/\/+$/, "");
    url.hash = "";
    return url.toString().replace(/\/$/, "");
  } catch (_) { return String(value || "").trim().replace(/\/+$/, "").toLowerCase(); }
}
function providerEditForm(provider) {
  const keyHint = provider.has_saved_key ? "已保存；留空不修改" : (provider.native ? "留空则沿用 Codex 配置" : "输入 API Key");
  const currentModel = provider.model || "";
  const currentOption = currentModel ? `<option value="${esc(currentModel)}" selected>${esc(currentModel)}（当前）</option>` : "";
  openDrawer(`<h2>编辑 Provider</h2>${provider.native ? `<p class="drawer-meta">来自 Codex config.toml；网页保存的 API Key 不会被同步覆盖。</p>` : ""}<form id="provider-form" class="form"><label>名称<input name="name" required value="${esc(provider.name || "")}"/></label><label>模型<select name="model" id="provider-edit-model"><option value="">Provider 默认模型</option>${currentOption}</select><small id="provider-edit-model-status" class="field-hint">正在读取模型列表…</small></label><label>modelProvider ID<input name="model_provider" value="${esc(provider.model_provider || "")}" placeholder="config.toml 中的 provider ID"/></label><label>API Key<input name="api_key" type="password" autocomplete="new-password" placeholder="${esc(keyHint)}"/></label>${provider.has_saved_key ? `<label class="toggle danger-toggle"><input name="clear_api_key" type="checkbox"/>清除已保存的 API Key</label>` : ""}<label>Base URL<input name="base_url" type="url" value="${esc(provider.base_url || "")}" placeholder="https://api.example.com/v1"/></label><label>优先级<input name="priority" type="number" value="${provider.priority ?? 100}"/></label><label class="toggle"><input name="enabled" type="checkbox" ${provider.enabled !== false ? "checked" : ""}/>启用并参与自动切换</label><div class="form-actions"><button type="button" class="secondary" id="cancel">取消</button><button type="submit" class="primary">保存并检查</button></div></form>`);
  const modelSelect = $("#provider-edit-model");
  const modelStatus = $("#provider-edit-model-status");
  const selectedTask = state.selectedTask?.provider_id === provider.id ? state.selectedTask : null;
  if (selectedTask) {
    api(`/tasks/${encodeURIComponent(selectedTask.id)}/models`).then(result => {
      const models = normalizeModelItems(result.models);
      const options = models.map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join("");
      modelSelect.innerHTML = `<option value="">Provider 默认模型</option>${options}${currentModel && !models.some(item => item.id === currentModel) ? currentOption : ""}`;
      modelSelect.value = currentModel;
      modelStatus.textContent = models.length ? `可选模型：${models.length} 个` : "Provider 未返回模型，将使用默认模型";
    }).catch(error => { modelStatus.textContent = `模型列表读取失败：${error.message}`; });
  } else {
    modelStatus.textContent = "保存后可在会话工具栏选择该 Provider 的模型";
  }
  $("#cancel").onclick = closeDrawer;
  $("#provider-form").onsubmit = async event => {
    event.preventDefault(); const restore = setFormBusy(event.target, "保存并检查…"); const form = new FormData(event.target);
    const body = { name: form.get("name"), kind: "codex", model: form.get("model"), model_provider: form.get("model_provider"), profile: "", api_key: form.get("api_key") || null, clear_api_key: form.has("clear_api_key"), base_url: form.get("base_url"), priority: Number(form.get("priority")), enabled: form.has("enabled") };
    try {
      await api(provider.id ? `/providers/${provider.id}` : "/providers", { method: provider.id ? "PUT" : "POST", body: JSON.stringify(body) });
      closeDrawer(); await refresh(false); openPanel("server"); toast("Provider 已保存并完成检查");
    } catch (error) { restore(); toast(`保存失败：${error.message}`); }
  };
}
function providerForm(provider = {}) {
  if (provider.id) return providerEditForm(provider);
  openDrawer(`<h2>新增 Provider</h2><p class="drawer-meta">先填写连接地址和 API Key。验证成功后读取模型列表，选择默认模型再完成添加。相同连接地址不能重复添加。</p><form id="provider-form" class="form provider-create-form"><label>连接地址<input name="base_url" type="url" required autocomplete="url" placeholder="https://api.example.com/v1"/></label><label>API Key<input name="api_key" type="password" required autocomplete="new-password" placeholder="输入 API Key"/></label><p id="provider-verify-status" class="provider-verify-status">验证后选择默认模型</p><div id="provider-model-row" class="provider-model-picker" hidden><div class="provider-model-picker-head"><strong id="provider-model-count">模型列表</strong><input id="provider-model-search" type="search" autocomplete="off" placeholder="筛选模型"/></div><label class="provider-model-select-label">默认模型<select name="verified_model" id="provider-verified-model" aria-label="选择默认模型"><option value="">请先验证连接</option></select></label></div><div class="form-actions"><button type="button" class="secondary" id="cancel">取消</button><button type="button" class="secondary" id="provider-verify">验证连接</button><button type="submit" class="primary" id="provider-submit" hidden>添加 Provider</button></div></form>`);
  const form = $("#provider-form");
  const verifyButton = $("#provider-verify");
  const submitButton = $("#provider-submit");
  const modelRow = $("#provider-model-row");
  const modelSelect = $("#provider-verified-model");
  const modelSearch = $("#provider-model-search");
  const modelCount = $("#provider-model-count");
  const status = $("#provider-verify-status");
  let verifiedSignature = "";
  const signature = () => `${String(form.elements.base_url.value || "").trim()}\n${String(form.elements.api_key.value || "")}`;
  const resetVerification = () => { verifiedSignature = ""; modelRow.hidden = true; submitButton.hidden = true; verifyButton.hidden = false; status.className = "provider-verify-status"; status.textContent = "验证后选择默认模型"; };
  [form.elements.base_url, form.elements.api_key].forEach(input => input.addEventListener("input", resetVerification));
  $("#cancel").onclick = closeDrawer;
  verifyButton.onclick = async () => {
    const baseUrl = String(form.elements.base_url.value || "").trim();
    const apiKey = String(form.elements.api_key.value || "");
    if (!baseUrl || !apiKey) { status.className = "provider-verify-status error"; status.textContent = "连接地址和 API Key 都必须填写"; return; }
    const duplicate = state.providers.find(item => providerEndpointKey(item.base_url) === providerEndpointKey(baseUrl));
    if (duplicate) { status.className = "provider-verify-status error"; status.textContent = `连接已存在：${duplicate.name}`; return; }
    const previous = verifyButton.textContent; verifyButton.disabled = true; verifyButton.textContent = "验证中…";
    try {
      const result = await api("/providers/verify", { method: "POST", body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }) });
      if (!result.ok) { status.className = "provider-verify-status error"; status.textContent = `验证失败：${result.detail || result.health_status}`; return; }
      const models = Array.isArray(result.models) ? result.models.slice() : [];
      if (!models.length) { status.className = "provider-verify-status error"; status.textContent = "连接成功，但没有返回模型列表，无法完成添加"; return; }
      const normalizedModels = normalizeModelItems(models);
      const renderModels = (query = "") => {
        const needle = query.trim().toLowerCase();
        const visible = normalizedModels.filter(item => !needle || `${item.id} ${item.label}`.toLowerCase().includes(needle));
        const selected = modelSelect.value;
        modelSelect.innerHTML = visible.map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join("") || `<option value="">没有匹配的模型</option>`;
        if (visible.some(item => item.id === selected)) modelSelect.value = selected;
        modelCount.textContent = `模型列表 · ${visible.length}/${normalizedModels.length}`;
      };
      modelSelect.value = normalizedModels[0].id;
      modelSearch.oninput = event => renderModels(event.target.value);
      renderModels();
      status.className = "provider-verify-status ok"; status.textContent = `连接正常 · ${normalizedModels.length} 个模型 · ${Number(result.latency_ms || 0)}ms`;
    } catch (error) { status.className = "provider-verify-status error"; status.textContent = `验证失败：${error.message}`; }
    finally { verifyButton.disabled = false; verifyButton.textContent = previous; }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    if (!verifiedSignature || verifiedSignature !== signature()) { status.className = "provider-verify-status error"; status.textContent = "连接或 API Key 已变化，请重新验证"; return; }
    if (!form.elements.verified_model.value) { status.className = "provider-verify-status error"; status.textContent = "请选择默认模型"; return; }
    const restore = setFormBusy(form, "添加中…");
    const body = { name: providerNameFromUrl(form.elements.base_url.value), kind: "codex", model: form.elements.verified_model.value, model_provider: "", profile: "", api_key: form.elements.api_key.value, clear_api_key: false, base_url: form.elements.base_url.value, priority: 100, enabled: true };
    try { await api("/providers", { method: "POST", body: JSON.stringify(body) }); closeDrawer(); await refresh(false); openPanel("server"); toast("Provider 已添加并设为可用路由"); }
    catch (error) { restore(); status.className = "provider-verify-status error"; status.textContent = `添加失败：${error.message}`; }
  };
}
function skillForm(skill = {}) {
  const readOnly = Boolean(skill.id && !skill.editable);
  const installed = Boolean(skill.installed);
  const title = readOnly ? uiLabel("viewSkill") : skill.id ? uiLabel("editSkill") : uiLabel("newPersonalSkill");
  const initialContent = skill.content || (!skill.id ? uiLabel("skillTemplate") : "");
  openDrawer(`<h2>${title}</h2>${skill.path ? `<p class="drawer-meta">${esc(skill.source)} · ${esc(skill.path)}</p>` : ""}<form id="skill-form" class="form unified-editor-form"><div class="editor-form-fields"><label>${uiLabel("name")}<input name="name" required value="${esc(skill.name || "")}" ${readOnly ? "readonly" : ""} placeholder="my-skill"/></label><label>${uiLabel("description")}<input name="description" value="${esc(skill.description || "")}" ${readOnly ? "readonly" : ""}/></label></div><div class="editor-field"><span class="form-label">${uiLabel("content")}</span>${textEditorMarkup("skill-editor")}</div>${installed ? "" : `<label class="toggle"><input name="enabled" type="checkbox" ${skill.enabled !== false ? "checked" : ""}/>${uiLabel("enable")}</label>`}<div class="form-actions"><button type="button" class="secondary" id="cancel">${readOnly ? uiLabel("close") : uiLabel("cancel")}</button>${readOnly ? "" : `<button type="submit" class="primary">${uiLabel("save")} <kbd>Ctrl S</kbd></button>`}</div></form>`, { editor: true });
  const editor = mountTextEditor("#skill-editor", { filename: skill.path || "SKILL.md", value: initialContent, readOnly });
  $("#cancel").onclick = closeDrawer;
  if (readOnly) return;
  $("#skill-form").onsubmit = async event => {
    event.preventDefault(); const restore = setFormBusy(event.target, uiLabel("saving")); const form = new FormData(event.target);
    const body = { name: form.get("name"), description: form.get("description"), content: editor.getValue(), enabled: installed ? true : form.has("enabled") };
    try {
      await api(skill.id ? `/skills/${skill.id}` : "/skills", { method: skill.id ? "PUT" : "POST", body: JSON.stringify(body) });
      closeDrawer(); await refresh(false); openPanel("skills"); toast(uiLabel("skillSaved"));
    } catch (error) { restore(); toast(uiLabel("saveFailed", { message: error.message })); }
  };
}
