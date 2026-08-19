// Session list, conversation rendering, workspace browser, and live terminal.
const chatFileObjectUrls = new Map();
const chatThumbnailObjectUrls = new Map();
const chatThumbnailLoads = new Map();
const chatWorker = typeof Worker === "function" ? new Worker("/chat-worker.js?v=20260818-full-exploration-graph") : null;
let chatBuildRequestId = 0;
let chatBuildInFlight = false;
let chatBuildQueued = false;
let chatHistoryLoadPromise = null;
let mediaViewerMarkdown = null;
function renderSidebarStats() {
  const running = sortTasks(state.tasks.filter(task => ["running", "retrying"].includes(task.status)));
  const queued = sortTasks(state.tasks.filter(task => task.status === "queued"));
  const live = [...running, ...queued];
  $("#sidebar-stats").textContent = `${state.tasks.length} ${t("sessions")} · ${running.length} ${t("statusRunning")}`;
  const summary = $("#running-summary");
  if (!summary) return;
  summary.hidden = !live.length;
  if (!live.length) { summary.innerHTML = ""; return; }
  const title = running.length ? `${t("statusRunning")} ${running.length} ${t("sessions")}${queued.length ? ` · ${t("statusQueued")} ${queued.length}` : ""}` : `${t("statusQueued")} ${queued.length} ${t("sessions")}`;
  summary.innerHTML = `<div class="running-summary-head"><span class="live-dot"></span><strong>${title}</strong></div><div class="running-summary-list">${live.slice(0, 4).map(task => `<button data-session-id="${esc(task.id)}"><span>${esc(task.name || "Codex 会话")}</span><small>${esc(statusLabel(task.status))}${task.provider_id ? ` · ${esc(state.providers.find(provider => provider.id === task.provider_id)?.name || task.provider_id)}` : ""}</small></button>`).join("")}</div>${live.length > 4 ? `<small class="running-summary-more">还有 ${live.length - 4} 个活动会话</small>` : ""}`;
}
function renderSessionList() {
  const list = $("#task-list"); const tasks = sortTasks(state.tasks.filter(taskMatches));
  if (!tasks.length) { list.innerHTML = `<div class="empty">${state.query ? "没有匹配的会话" : "还没有 Codex 会话"}</div>`; return; }
  list.innerHTML = tasks.map(task => { const active = ["running", "retrying"].includes(task.status); const selected = task.id === state.selectedId; const name = task.name || t("sessions"); const stateText = statusLabel(task.status); return `<button class="session-card ${selected ? "selected" : ""} ${active ? "running" : "paused"}" data-session-id="${esc(task.id)}" title="${esc(name)} · ${esc(stateText)}" aria-label="${esc(name)} · ${esc(stateText)}" aria-current="${selected ? "true" : "false"}"><span class="session-card-body"><strong title="${esc(name)}">${esc(name)}</strong><time class="session-card-time">${esc(shortDate(task.updated_at))}</time></span></button>`; }).join("");
}

let pointerSelectedSessionId = "";
const sessionList = $("#task-list");
sessionList?.addEventListener("pointerdown", event => {
  if (event.pointerType !== "mouse" || event.button !== 0) return;
  const card = event.target.closest(".session-card[data-session-id]");
  if (!card || !sessionList.contains(card)) return;
  pointerSelectedSessionId = card.dataset.sessionId || "";
  event.preventDefault();
  void selectSession(pointerSelectedSessionId).catch(error => toast(error.message));
});
sessionList?.addEventListener("click", event => {
  const card = event.target.closest(".session-card[data-session-id]");
  if (!card || !sessionList.contains(card)) return;
  const id = card.dataset.sessionId || "";
  event.preventDefault();
  event.stopPropagation();
  if (id && id !== pointerSelectedSessionId) void selectSession(id).catch(error => toast(error.message));
  pointerSelectedSessionId = "";
});
function scrollSessionIntoView(id) {
  const card = $$(".session-card").find(node => node.dataset.sessionId === id);
  card?.scrollIntoView({ block: "nearest" });
}
function stopQueueSync() {
  clearInterval(queueSyncTimer);
  queueSyncTimer = null;
  queueSyncInFlight = false;
}
async function syncQueuedMessages() {
  if (queueSyncInFlight || !state.selectedId) return;
  const taskId = state.selectedId;
  queueSyncInFlight = true;
  try {
    const previous = JSON.stringify(state.selectedMessages.map(message => [message.id, message.status, message.body, message.error, message.session_id]));
    const messages = await api(`/tasks/${taskId}/messages`);
    if (state.selectedId !== taskId) return;
    replaceTaskMessages(messages);
    renderQueuedMessages();
    const current = JSON.stringify(state.selectedMessages.map(message => [message.id, message.status, message.body, message.error, message.session_id]));
    if (current !== previous) scheduleRenderChat();
  } catch (_) {
    // The websocket remains the primary channel; the next interval retries.
  } finally {
    queueSyncInFlight = false;
  }
}
function showEmptyConversation() { if (terminalTaskId) destroyTerminal(); stopQueueSync(); state.sessionAbortController?.abort(); state.sessionAbortController = null; state.editingQueuedId = null; state.pendingApprovals = []; state.inspectorClosed = true; state.explorationNodes = []; state.explorationEdges = []; state.explorationEvents = []; state.explorationPrecomputed = false; state.explorationRequestId += 1; if (typeof closeExplorationMap === "function") closeExplorationMap(); $("#empty-conversation").hidden = false; $("#conversation-view").hidden = true; $("#goal-bar").hidden = true; $("#queued-messages").hidden = true; $("#approval-center").hidden = true; setInspectorOpen(false); }
function resetWorkspaceBrowser() {
  state.workspacePath = "";
  state.workspaceFile = null;
  state.workspaceRequestId += 1;
  $("#workspace-section")?.remove();
  $(".workspace-error")?.remove();
}
function clearChatSelectionForSessionSwitch() {
  window.getSelection?.()?.removeAllRanges();
  state.chatPointerSelecting = false;
  state.chatPointerDragged = false;
  state.chatSelectionActive = false;
  state.chatRenderDeferred = false;
  state.chatDeferredStickToBottom = false;
  state.deferredChatBlocks = null;
}
async function selectSession(id, openSocket = true) {
  if (state.selectedId !== id) clearChatSelectionForSessionSwitch();
  const requestId = ++state.sessionRequestId;
  state.sessionAbortController?.abort();
  const controller = new AbortController();
  state.sessionAbortController = controller;
  if (terminalTaskId && terminalTaskId !== id) closeTerminal();
  if (socket && socketTaskId !== id) { socket.onclose = null; socket.close(); socket = null; socketTaskId = ""; }
  const wasEmpty = $("#conversation-view").hidden;
  if (state.selectedId !== id) {
    resetWorkspaceBrowser(); state.titleExpanded = false; state.historyCursor = ""; state.historyHasMore = false;
    state.historyLoading = false; state.chatBlocks = []; state.chatVirtualStart = null; state.activityVisibleCounts = {}; state.activityOutputOpen = {}; state.explorationNodes = []; state.explorationEdges = []; state.explorationEvents = []; state.explorationSelectedNodeId = ""; state.explorationLoading = false; state.explorationHistoryComplete = false; state.explorationLoadedEventCount = 0; state.explorationLoadError = ""; state.explorationRequestId += 1; state.explorationRevision = 0; state.explorationNeedsSync = false; state.explorationPrecomputed = false; state.explorationMapStatus = "pending"; state.explorationProcessedEvents = 0; state.pendingApprovals = [];
    state.selectedEvents = []; state.selectedMessages = []; state.composerHistory = []; state.historyIndex = -1;
    if (typeof renderExplorationMap === "function") renderExplorationMap();
    state.runtimeMetrics = { taskId: "", ttftMs: null, tpotMs: null, estimated: true, outputTokens: 0 };
  }
  state.selectedId = id; localStorage.setItem("codex-dashboard-session", id);
  const task = state.tasks.find(item => item.id === id);
  if (task) state.selectedTask = task;
  // Reflect keyboard navigation immediately, then replace the row with the full task after loading.
  renderSessionList();
  scrollSessionIntoView(id);
  if (task) {
    $("#empty-conversation").hidden = true; $("#conversation-view").hidden = false;
    if (wasEmpty && window.innerWidth >= 861) state.inspectorClosed = false;
    setInspectorOpen(window.innerWidth >= 861 && !state.inspectorClosed);
    renderConversation();
  }
  let fullTask, timeline, messages, activityMap;
  try {
    [fullTask, timeline, messages, activityMap] = await Promise.all([
      api(`/tasks/${encodeURIComponent(id)}`, { signal: controller.signal }),
      api(`/tasks/${encodeURIComponent(id)}/timeline?limit=160`, { signal: controller.signal }),
      api(`/tasks/${encodeURIComponent(id)}/messages`, { signal: controller.signal }),
      api(`/tasks/${encodeURIComponent(id)}/activity-map`, { signal: controller.signal }),
    ]);
  } catch (error) {
    if (error.name === "AbortError") return;
    throw error;
  }
  if (requestId !== state.sessionRequestId || state.selectedId !== id) return;
  if (state.sessionAbortController === controller) state.sessionAbortController = null;
  state.selectedTask = fullTask;
  state.explorationPrecomputed = true;
  if (typeof applyActivityMapSnapshot === "function") applyActivityMapSnapshot(activityMap);
  state.selectedEvents = [...(timeline.items || []), ...(timeline.metrics || [])];
  state.explorationEvents = [];
  state.explorationNeedsSync = false;
  state.historyCursor = timeline.next_cursor || ""; state.historyHasMore = Boolean(timeline.has_more);
  replaceTaskMessages(messages);
  // A newly selected thread should open at its latest message. Consume this
  // once in renderChat instead of smooth-scrolling on every intermediate
  // update while the history/socket is loading.
  state.chatSnapToBottom = true;
  $("#empty-conversation").hidden = true; $("#conversation-view").hidden = false;
  if (wasEmpty && window.innerWidth >= 861) state.inspectorClosed = false;
  setInspectorOpen(window.innerWidth >= 861 && !state.inspectorClosed);
  renderSessionList(); renderConversation();
  if (openSocket) connectSocket(id); if (window.innerWidth <= 860) setSessionSidebarOpen(false);
  loadWorkspace("").catch(() => {});
}
function renderConversation(renderMessages = true) {
  const task = state.selectedTask; if (!task) return;
  const title = String(task.name || t("sessions"));
  const titleNode = $("#conversation-name");
  const titleToggle = $("#conversation-title-toggle");
  titleNode.textContent = title;
  const longTitle = title.length > 36;
  titleNode.title = title;
  titleToggle.hidden = true;
  titleNode.classList.toggle("expanded", false);
  titleToggle.onclick = () => { state.titleExpanded = !state.titleExpanded; renderConversation(); };
  const threadId = task.codex_session_id || task.id || "";
  const workspace = task.workspace || "";
  const location = task.ssh_host ? `${task.ssh_host} · ` : "";
  $("#conversation-subtitle").textContent = `${location}${workspace} · ${uiLabel("threadId")} ${threadId || "—"}`;
  const composerSettings = composerModelCache.get(`${task.id}:${task.provider_id || "default"}`) || {};
  renderComposerModelSelect(task, Array.isArray(composerSettings) ? composerSettings : composerSettings.models || []);
  renderComposerEffortSelect(task, Array.isArray(composerSettings) ? "" : composerSettings.reasoning_effort || task.reasoning_effort || "");
  loadComposerModels(task);
  $("#permission-toggle").classList.toggle("active", task.yolo);
  $("#permission-toggle span").textContent = task.yolo ? "YOLO" : "受控";
  if (renderMessages) state.runtimeMetrics = calculateRuntimeMetrics(task, state.selectedEvents);
  renderConnectionStatus();
  if (renderMessages) renderContextUsage();
  renderTurnProgress();
  renderGoalBar();
  renderApprovalCenter();
  refreshComposerHistory();
  renderQueuedMessages();
  if (renderMessages) renderChat();
  renderInspector();
}
const composerModelCache = new Map();
const composerModelLoads = new Map();
function normalizeModelItems(raw) {
  const items = (Array.isArray(raw) ? raw : []).map(item => {
    if (typeof item === "string") return { id: item, label: item, inputModalities: [] };
    return {
      ...item,
      id: item?.id || item?.model || item?.slug || "",
      label: item?.displayName || item?.display_name || item?.label || item?.name || item?.id || item?.model || item?.slug || "",
      inputModalities: item?.inputModalities || item?.input_modalities || [],
      isDefault: Boolean(item?.isDefault ?? item?.is_default),
    };
  }).filter(item => item.id);
  return [...new Map(items.map(item => [item.id, item])).values()];
}
function encodeModelCommandValue(value) {
  const text = String(value || "").trim();
  if (!text) return "default";
  return text;
}
async function applyModelCommand(task, updates) {
  const parts = [];
  if (Object.prototype.hasOwnProperty.call(updates, "model")) parts.push(`model=${encodeModelCommandValue(updates.model)}`);
  if (Object.prototype.hasOwnProperty.call(updates, "reasoning_effort")) parts.push(`effort=${encodeModelCommandValue(updates.reasoning_effort)}`);
  if (!parts.length) throw new Error("没有可执行的模型更新");
  const clientMessageId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  const result = await api(`/tasks/${encodeURIComponent(task.id)}/commands`, {
    method: "POST",
    body: JSON.stringify({ command: `/model ${parts.join(" ")}`, client_message_id: clientMessageId }),
  });
  if (!result?.ok) throw new Error(result?.message || "模型切换失败");
  if (result.task) {
    state.selectedTask = { ...state.selectedTask, ...result.task };
    mergeTask(result.task);
    renderConversation();
  }
  return result;
}
function selectedModelInputModalities() {
  const task = state.selectedTask;
  if (!task) return ["text", "image"];
  const cached = composerModelCache.get(`${task.id}:${task.provider_id || "default"}`) || {};
  const models = normalizeModelItems(Array.isArray(cached) ? cached : cached.models || []);
  const selected = models.find(item => item.id === task.model) || models.find(item => item.isDefault);
  const modalities = selected?.inputModalities?.map(value => String(value).toLowerCase()).filter(Boolean) || [];
  return modalities.length ? modalities : ["text", "image"];
}
function renderComposerModelSelect(task, models = []) {
  const select = $("#composer-model-select");
  if (!select) return;
  const current = task.model || "";
  const unique = normalizeModelItems(models);
  if (current && !unique.some(item => item.id === current)) unique.unshift({ id: current, label: `${current}（当前）` });
  const taskKey = `${task.id}:${task.provider_id || "default"}`;
  const options = [{ id: "", label: uiLabel("providerDefaultModel") }, ...unique];
  const signature = JSON.stringify(options.map(item => [item.id, item.label]));
  const sameTask = select.dataset.taskKey === taskKey;
  const userIsChoosing = sameTask && document.activeElement === select;
  if (select.dataset.optionsSignature !== signature) {
    if (userIsChoosing) {
      // Status patches arrive while a turn is running. Rebuilding a focused
      // native select closes its menu, so apply refreshed models after blur.
      select.dataset.pendingOptions = "1";
    } else {
      select.innerHTML = options.map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join("");
      select.dataset.optionsSignature = signature;
      delete select.dataset.pendingOptions;
    }
  }
  select.dataset.taskKey = taskKey;
  if (!userIsChoosing) select.value = current;
  if (!select.dataset.modelChangeBound) {
    select.dataset.modelChangeBound = "1";
    select.addEventListener("change", async () => {
      const activeTask = state.selectedTask;
      if (!activeTask) return;
      const previous = activeTask.model || "";
      const model = select.value;
      if (model === previous) return;
      select.disabled = true;
      try {
        await applyModelCommand(activeTask, { model });
        toast(model ? `已切换模型：${model}` : "已恢复 Provider 默认模型");
      } catch (error) { select.value = previous; toast(`模型切换失败：${error.message}`); }
      finally { select.disabled = false; }
    });
    select.addEventListener("blur", () => {
      if (!select.dataset.pendingOptions || !state.selectedTask) return;
      const cacheKey = `${state.selectedTask.id}:${state.selectedTask.provider_id || "default"}`;
      const cached = composerModelCache.get(cacheKey) || {};
      queueMicrotask(() => renderComposerModelSelect(state.selectedTask, Array.isArray(cached) ? cached : cached.models || []));
    });
  }
  select.title = unique.length ? `已加载 ${unique.length} 个模型，可直接选择` : "Provider 未返回模型列表，将使用默认模型";
}
function renderComposerEffortSelect(task, value = "") {
  const select = $("#composer-effort-select");
  if (!select) return;
  const options = [["", uiLabel("providerDefault")], ["minimal", uiLabel("minimal")], ["low", uiLabel("low")], ["medium", uiLabel("medium")], ["high", uiLabel("high")], ["xhigh", uiLabel("xhigh")], ["ultra", uiLabel("ultra")]];
  const current = task.reasoning_effort || "";
  select.innerHTML = options.map(([id, label]) => `<option value="${id}">${label}</option>`).join("");
  select.value = value || current;
  select.onchange = async () => {
    const reasoning_effort = select.value;
    if (reasoning_effort === current) return;
    select.disabled = true;
    try {
      await applyModelCommand(task, { reasoning_effort });
      toast(reasoning_effort ? `思考程度：${select.options[select.selectedIndex]?.text || reasoning_effort}` : "已恢复 Provider 默认思考程度");
    } catch (error) { select.value = current; toast(`思考程度切换失败：${error.message}`); }
    finally { select.disabled = false; }
  };
  select.title = "选择 Codex 思考程度";
}
async function loadComposerModels(task) {
  const cacheKey = `${task.id}:${task.provider_id || "default"}`;
  if (composerModelCache.has(cacheKey)) {
    const cached = composerModelCache.get(cacheKey);
    renderComposerModelSelect(task, Array.isArray(cached) ? cached : cached.models || []);
    renderComposerEffortSelect(task, Array.isArray(cached) ? task.reasoning_effort || "" : cached.reasoning_effort || task.reasoning_effort || "");
    return;
  }
  if (composerModelLoads.has(cacheKey)) return;
  const request = api(`/tasks/${encodeURIComponent(task.id)}/models`).then(result => {
    const models = normalizeModelItems(result.models);
    const settings = { models, reasoning_effort: result.reasoning_effort || "" };
    composerModelCache.set(cacheKey, settings);
    if (state.selectedId === task.id) {
      renderComposerModelSelect(state.selectedTask, models);
      renderComposerEffortSelect(state.selectedTask, settings.reasoning_effort || state.selectedTask.reasoning_effort || "");
    }
  }).catch(error => {
    if (state.selectedId === task.id) {
      const select = $("#composer-model-select");
      if (select) { select.disabled = false; select.title = `模型列表读取失败：${error.message}`; }
    }
  }).finally(() => composerModelLoads.delete(cacheKey));
  composerModelLoads.set(cacheKey, request);
}
function goalStatusLabel(value) {
  if (value === "none") return ({ zh: "未设置", en: "Unset", fr: "Non défini", ja: "未設定", ko: "미설정" }[state.language] || "Unset");
  // "active" means the Goal is enabled and unfinished, not that a
  // goal_resume turn is currently executing.
  const labels = { active: "enabled", paused: "statusStopped", blocked: "statusFailed", usageLimited: "statusRetrying", budgetLimited: "statusRetrying", complete: "statusSucceeded", none: "available" };
  return labels[value] ? t(labels[value]) : value || t("unknown");
}
function goalStatusSummary(task, goal) {
  return `${uiLabel("goalStatusPrefix")}：${goalStatusLabel(task.goal_status || (goal ? "active" : "none"))}`;
}
function goalCanAutoResume(status, retryForever = false) {
  const value = status || "none";
  if (["complete", "none"].includes(value)) return false;
  return retryForever || !["paused", "blocked"].includes(value);
}
function goalIsActive(task) {
  return Boolean(String(task?.goal || "").trim()) && (task.goal_status || "none") === "active";
}
function renderGoalBar() {
  const task = state.selectedTask;
  const bar = $("#goal-bar");
  if (!task) { bar.hidden = true; return; }
  bar.hidden = false;
  const goal = String(task.goal || "").trim();
  const activeTurn = ["running", "retrying", "queued"].includes(task.status);
  const goalActive = goalIsActive(task);
  const inputHasContent = Boolean($("#message-input")?.value.trim() || pendingAttachments.length);
  $("#goal-text").textContent = goal;
  $("#goal-text").classList.toggle("empty", !goal);
  $("#goal-task-status").textContent = goalStatusSummary(task, goal);
  $("#goal-run-toggle").classList.toggle("active", goalActive);
  $("#goal-run-toggle").disabled = !goal;
  $("#goal-run-toggle").title = !goal ? uiLabel("setGoal") : (goalActive ? uiLabel("goalPause") : uiLabel("goalStart"));
  $("#goal-run-toggle").setAttribute("aria-label", $("#goal-run-toggle").title);
  $("#goal-run-icon").textContent = goalActive ? "Ⅱ" : "▶";
  $("#goal-run-label").textContent = goalActive ? uiLabel("pause") : uiLabel("start");
  const retryToggle = $("#goal-retry-toggle");
  retryToggle.disabled = !goal;
  retryToggle.classList.toggle("active", Boolean(goal && task.retry_forever));
  retryToggle.title = goal ? (task.retry_forever ? uiLabel("retryOff") : uiLabel("retryOn")) : uiLabel("setGoal");
  retryToggle.setAttribute("aria-label", retryToggle.title);
  $("#goal-retry-label").textContent = goal && task.retry_forever ? uiLabel("on") : uiLabel("off");
  const liveState = $("#composer-live-state");
  liveState.className = `composer-live-state ${esc(task.status || "available")}`;
  $("#composer-live-state span").textContent = statusLabel(task.status);
  const stopping = !state.editingQueuedId && activeTurn && !inputHasContent;
  const sendLabel = state.editingQueuedId ? uiLabel("save") : stopping ? uiLabel("stopAction") : uiLabel("send");
  $("#send-codex-label").textContent = sendLabel;
  $("#send-codex").classList.toggle("stop-mode", stopping);
  $("#send-codex").querySelector("span:last-child").textContent = stopping ? "■" : "↑";
  $("#send-codex").title = state.editingQueuedId ? uiLabel("saveQueued") : (stopping ? uiLabel("stopCodex") : uiLabel("startCodex"));
  $("#message-input").placeholder = state.editingQueuedId ? uiLabel("editQueued") : t("composerPlaceholder");
}
function renderQueuedMessages() {
  const strip = $("#queued-messages");
  if (!strip) return;
  const queued = (state.selectedMessages || []).filter(message => ["queued", "dispatching"].includes(message.status));
  strip.hidden = false;
  if (!queued.length) {
    strip.innerHTML = `<div class="queued-head"><span><i></i>${uiLabel("queue")}</span><span class="queued-head-actions"><strong>0</strong></span></div><div class="queued-empty">${uiLabel("queueEmpty")}</div>`;
    stopQueueSync();
    return;
  }
  if (!queueSyncTimer) queueSyncTimer = setInterval(syncQueuedMessages, 1500);
  const visible = queued.slice(0, 4);
  strip.innerHTML = `<div class="queued-head"><span><i></i>${uiLabel("queue")}</span><span class="queued-head-actions"><strong>${queued.length}</strong><button type="button" class="queue-icon danger-icon" data-queue-clear title="${uiLabel("clearQueue")}">×</button></span></div>${visible.map((message, index) => { const dispatching = message.status === "dispatching"; const error = String(message.error || "").trim(); return `<div class="queued-message ${dispatching ? "dispatching" : ""} ${error ? "has-error" : ""} ${state.editingQueuedId === message.id ? "editing" : ""}"><div class="queued-message-main"><span class="queued-index">${index + 1}</span><span class="queued-body">${esc(message.body)}</span>${error ? `<small class="queued-error" title="${esc(error)}">${esc(error)}</small>` : ""}</div><div class="queued-actions"><button type="button" class="queue-icon" data-queue-dispatch="${esc(message.id)}" title="${dispatching ? uiLabel("sending") : uiLabel("dispatchNow")}" ${dispatching ? "disabled" : ""}>${dispatching ? "…" : "▶"}</button><button type="button" class="queue-icon" data-queue-edit="${esc(message.id)}" title="${uiLabel("editQueuedAction")}" ${dispatching ? "disabled" : ""}>✎</button><button type="button" class="queue-icon danger-icon" data-queue-delete="${esc(message.id)}" title="${uiLabel("deleteQueuedAction")}" ${dispatching ? "disabled" : ""}>×</button></div></div>`; }).join("")}${queued.length > visible.length ? `<div class="queued-more">${uiLabel("queueMore", { count: queued.length - visible.length })}</div>` : ""}`;
}
function approvalTitle(method) {
  if (method === "item/commandExecution/requestApproval") return uiLabel("approvalCommand");
  if (method === "item/fileChange/requestApproval") return uiLabel("approvalFile");
  if (method === "item/permissions/requestApproval") return uiLabel("approvalPermissions");
  return uiLabel("approvalQuestion");
}
function renderApprovalCenter() {
  const center = $("#approval-center");
  if (!center) return;
  const request = (state.pendingApprovals || [])[0];
  center.hidden = !request;
  if (!request) { center.innerHTML = ""; return; }
  const params = request.params || {};
  const command = params.command || params.grantRoot || "";
  const cwd = params.cwd || "";
  const reason = params.reason || "";
  const permissionText = params.permissions ? JSON.stringify(params.permissions) : "";
  const questions = Array.isArray(params.questions) ? params.questions : [];
  const detail = command ? `<div class="approval-detail"><code>${esc(command)}</code></div>` : permissionText ? `<div class="approval-detail"><code>${esc(permissionText)}</code></div>` : "";
  const context = `${cwd ? `<span>${uiLabel("approvalWorkspace")}: <code>${esc(cwd)}</code></span>` : ""}${reason ? `<span class="approval-reason">${uiLabel("approvalReason")}: ${esc(reason)}</span>` : ""}`;
  const questionFields = questions.length ? `<div class="approval-questions">${questions.map((question, index) => {
    const options = Array.isArray(question.options) ? question.options : [];
    const controls = options.length
      ? `<div class="approval-options">${options.map((option, optionIndex) => `<label title="${esc(option.description || "")}"><input type="radio" name="approval-question-${index}" value="${esc(option.label)}" ${optionIndex === 0 ? "checked" : ""}/><span>${esc(option.label)}</span></label>`).join("")}</div>`
      : `<input class="approval-answer" data-question-index="${index}" type="${question.isSecret ? "password" : "text"}" autocomplete="off"/>`;
    return `<label class="approval-question" data-question-id="${esc(question.id || `question-${index}`)}"><strong>${esc(question.header || question.question || "")}</strong>${question.header ? `<span>${esc(question.question || "")}</span>` : ""}${controls}</label>`;
  }).join("")}</div>` : "";
  const actions = questions.length
    ? `<button type="button" class="approval-action danger" data-approval-id="${esc(request.id)}" data-approval-decision="cancel">${uiLabel("approvalDeny")}</button><button type="button" class="approval-action primary" data-approval-id="${esc(request.id)}" data-approval-decision="accept">${uiLabel("approvalSubmit")}</button>`
    : `<button type="button" class="approval-action danger" data-approval-id="${esc(request.id)}" data-approval-decision="decline">${uiLabel("approvalDeny")}</button><button type="button" class="approval-action" data-approval-id="${esc(request.id)}" data-approval-decision="acceptForSession">${uiLabel("approvalSession")}</button><button type="button" class="approval-action primary" data-approval-id="${esc(request.id)}" data-approval-decision="accept">${uiLabel("approvalOnce")}</button>`;
  center.innerHTML = `<article class="approval-card${questions.length ? " has-questions" : ""}"><div class="approval-copy"><div class="approval-heading"><span>!</span><strong>${approvalTitle(request.method)}</strong><small>${uiLabel("approvalWaiting")}</small></div>${detail}<div class="approval-detail approval-context">${context}</div></div><div class="approval-actions">${actions}</div>${questionFields}</article>`;
}
function approvalAnswers() {
  const answers = {};
  $$(".approval-question", $("#approval-center")).forEach((node, index) => {
    const checked = $("input[type=radio]:checked", node);
    const text = $(".approval-answer", node);
    answers[node.dataset.questionId] = checked ? [checked.value] : text?.value ? [text.value] : [];
  });
  return answers;
}
function renderChat() {
  if (state.chatSelectionActive) { state.chatRenderDeferred = true; return; }
  const stream = $("#chat-log");
  if (!chatWorker) {
    stream.innerHTML = `<div class="chat-empty"><p>${uiLabel("workerUnavailable") || "Chat Worker unavailable"}</p></div>`;
    return;
  }
  if (chatBuildInFlight) {
    chatBuildQueued = true;
    return;
  }
  chatBuildInFlight = true;
  chatBuildQueued = false;
  const requestId = ++chatBuildRequestId;
  const taskId = state.selectedId;
  const explorationRevision = state.explorationRevision;
  const explorationSync = !state.explorationPrecomputed && state.explorationNeedsSync && (state.explorationOpen || !state.explorationNodes.length);
  chatWorker.postMessage({
    requestId,
    taskId,
    events: state.selectedEvents,
    explorationEvents: explorationSync ? state.explorationEvents : null,
    explorationRevision,
    messages: state.selectedMessages,
    rawActivity: state.rawActivity,
    taskRunning: ["running", "retrying", "queued"].includes(state.selectedTask?.status),
    taskStatus: state.selectedTask?.status || "",
    labels: {
      fileChanged: uiLabel("fileChanged"), contextCompressed: uiLabel("contextCompressed"),
      terminalTurnStarted: uiLabel("terminalTurnStarted"), terminalTurnCompleted: uiLabel("terminalTurnCompleted"),
      terminalTurnAborted: uiLabel("terminalTurnAborted"), codexActivity: uiLabel("codexActivity"),
      activityWorking: uiLabel("activityWorking"), activityPlanning: uiLabel("activityPlanning"),
      activityCommand: uiLabel("activityCommand"), activityCommandDone: uiLabel("activityCommandDone"),
      activityFile: uiLabel("activityFile"), activityFileDone: uiLabel("activityFileDone"),
      activityTool: uiLabel("activityTool"), activityToolDone: uiLabel("activityToolDone"),
      activitySearch: uiLabel("activitySearch"),
    },
  });
  chatWorker.onmessage = event => {
    if (event.data.requestId !== requestId) return;
    chatBuildInFlight = false;
    const stale = state.selectedId !== taskId;
    if (!event.data.error && !stale) {
      const blocks = event.data.blocks || [];
      if (!state.explorationPrecomputed) state.explorationNodes = event.data.explorationNodes || [];
      if (explorationSync && state.explorationRevision === explorationRevision) state.explorationNeedsSync = false;
      if (typeof renderExplorationMap === "function") renderExplorationMap({ liveUpdate: true });
      if (state.chatSelectionActive) {
        state.deferredChatBlocks = blocks;
        state.chatRenderDeferred = true;
        state.chatDeferredStickToBottom ||= state.chatSnapToBottom || chatIsNearBottom(stream);
      } else {
        state.chatBlocks = blocks;
        // Decide at paint time. Tool/file events may finish in the worker after the
        // user has already scrolled away from the bottom.
        paintVirtualChat(state.chatSnapToBottom || chatIsNearBottom(stream));
      }
    }
    const rebuild = chatBuildQueued || stale;
    chatBuildQueued = false;
    if (rebuild) scheduleRenderChat();
  };
}

function isTokenUsageProtocol(payload) {
  return payload?.type === "codex" && payload?.method === "thread/tokenUsage/updated";
}

function isHiddenProtocolNoise(payload) {
  const type = String(payload?.type || "").toLowerCase();
  if (type === "codex" && payload?.method) return !isTokenUsageProtocol(payload);
  const compact = type.replace(/[^a-z0-9]/g, "");
  return ["updated", "update", "diff", "output", "delta", "itemupdated", "itemdelta", "turnupdated", "turndiff"].includes(compact);
}

function isExplorationRelevantPayload(payload) {
  const type = String(payload?.type || "").toLowerCase();
  if (["usermessage", "browsermessage", "agentmessage", "plan", "planupdate", "filechange", "turn_completed", "turn_aborted", "externalturnstarted"].includes(type)) return true;
  if (type.includes("reason")) return /(决定|确认|根因|证明|改用|转向|放弃|不可行|关键是|结论|选择|root cause|decid|confirmed|switch(?:ing)? to|not viable)/i.test(String(payload?.text || payload?.summary || ""));
  if (type.includes("command") || type.includes("tool") || type.includes("mcp")) return String(payload?.status || "").toLowerCase() === "failed";
  return false;
}

function chatIsNearBottom(stream = $("#chat-log")) {
  return !stream || stream.scrollHeight - stream.scrollTop - stream.clientHeight < 96;
}

function captureChatViewport(stream) {
  const streamTop = stream.getBoundingClientRect().top;
  const nodes = $$("[data-chat-block-index]", stream);
  const anchor = nodes.find(node => node.getBoundingClientRect().bottom >= streamTop);
  return {
    scrollTop: stream.scrollTop,
    anchorIndex: anchor?.dataset.chatBlockIndex || "",
    anchorOffset: anchor ? anchor.getBoundingClientRect().top - streamTop : 0,
    openActivities: new Set($$("details.activity-group[open]", stream).map(node => node.dataset.chatBlockIndex)),
  };
}

function restoreChatViewport(stream, viewport, stickToBottom) {
  for (const node of $$("details.activity-group", stream)) {
    node.open = node.dataset.live === "true" || viewport.openActivities.has(node.dataset.chatBlockIndex);
  }
  if (stickToBottom) {
    stream.scrollTop = stream.scrollHeight;
    return;
  }
  const anchor = viewport.anchorIndex ? $(`[data-chat-block-index="${viewport.anchorIndex}"]`, stream) : null;
  if (anchor) {
    const streamTop = stream.getBoundingClientRect().top;
    stream.scrollTop += anchor.getBoundingClientRect().top - streamTop - viewport.anchorOffset;
    return;
  }
  stream.scrollTop = Math.min(viewport.scrollTop, Math.max(0, stream.scrollHeight - stream.clientHeight));
}

function activityCopyText(item) {
  const lines = [];
  if (item.command) lines.push(String(item.command));
  else if (item.tool) lines.push([item.tool, item.arguments].filter(Boolean).join("\n"));
  else if (item.detail) lines.push(String(item.detail));
  if (Array.isArray(item.plan)) item.plan.forEach(step => lines.push(`[${step.status || "pending"}] ${step.step || step.text || ""}`.trim()));
  if (Array.isArray(item.changes)) item.changes.forEach(change => lines.push(`${change.kind || "update"} ${change.path || change}`));
  if (item.output) lines.push(String(item.output));
  if (item.cwd) lines.push(`cwd: ${item.cwd}`);
  if (item.exitCode != null) lines.push(`exit: ${item.exitCode}`);
  return lines.filter(Boolean).join("\n");
}

function copyChatBlock(blockIndex, itemIndex = -1) {
  const block = (state.chatBlocks || [])[blockIndex];
  if (!block) return "";
  if (itemIndex >= 0 && Array.isArray(block.items)) return activityCopyText(block.items[itemIndex] || {});
  if (block.role === "activities") return (block.items || []).map(activityCopyText).filter(Boolean).join("\n\n");
  return String(block.text || "").trim();
}

function activityEventIsVisible(item, active = false) {
  const kind = String(item?.kind || "").toLowerCase();
  const tool = String(item?.tool || "").toLowerCase();
  if (kind !== "commandexecution" || !["exec", "exec_command", "functions.exec"].includes(tool)) return true;
  return active || Boolean(String(item?.command || "").trim() || String(item?.output || "").trim() || String(item?.detail || "").trim());
}

function renderActivityEvent(item, active = false, blockIndex = -1, itemIndex = -1, outputKey = "") {
  const status = String(item.status || "").toLowerCase();
  const statusText = status === "failed" ? "失败" : active ? "执行中" : "";
  const statusClass = status === "failed" ? " failed" : status === "completed" || status === "succeeded" ? " completed" : "";
  const kind = String(item.kind || "").toLowerCase();
  const command = String(item.command || "").trim();
  const output = String(item.output || "").trim();
  const args = String(item.arguments || "").trim();
  const changes = Array.isArray(item.changes) ? item.changes : [];
  const plan = Array.isArray(item.plan) ? item.plan.filter(step => step && (step.step || step.text)) : [];
  const isWait = kind.includes("wait") || /(?:^|[._])wait$/.test(String(item.tool || ""));
  const toolName = String(item.tool || "").toLowerCase();
  if (!activityEventIsVisible(item, active)) return "";
  const action = plan.length || kind.includes("plan") ? "Planned" : isWait ? "Waited" : changes.length || kind.includes("file") ? "Edited" : kind.includes("mcp") || (!command && item.tool) ? "Called" : kind.includes("search") || kind.includes("explor") ? "Explored" : kind.includes("reason") ? "Reasoned" : command ? "Ran" : item.label || uiLabel("activityWorking");
  const inlineArgs = args ? args.replace(/\s+/g, " ") : "";
  const completedPlanSteps = plan.filter(step => step.status === "completed").length;
  const subject = plan.length ? `${completedPlanSteps}/${plan.length} steps` : isWait ? "for background task" : command || (item.tool ? `${item.tool}${inlineArgs ? `(${inlineArgs})` : ""}` : String(item.detail || ""));
  const cwd = item.cwd ? `<small class="activity-cwd" title="${esc(item.cwd)}">${esc(item.cwd)}</small>` : "";
  const subjectBlock = subject ? `<code class="activity-inline-subject" title="${esc(subject)}">${esc(subject)}</code>` : "";
  const changesBlock = changes.length ? `<div class="activity-changes">${changes.slice(0, 8).map(change => `<span>${esc(change.kind || "update")} · ${esc(change.path || change)}</span>`).join("")}</div>` : "";
  const planBlock = plan.length ? `<ol class="activity-plan">${plan.map(step => { const state = String(step.status || "pending"); const mark = state === "completed" ? "✓" : state === "in_progress" ? "›" : "·"; return `<li class="${esc(state)}"><span>${mark}</span><span>${esc(step.step || step.text)}</span></li>`; }).join("")}</ol>` : "";
  let outputBlock = "";
  if (output && !isWait && !plan.length) {
    const outputLines = output.split(/\r?\n/);
    const meta = item.exitCode != null ? `exit ${item.exitCode}` : "";
    if (outputLines.length <= 9) {
      outputBlock = `<div class="activity-output"><code>${esc(output)}</code>${meta ? `<small>${esc(meta)}</small>` : ""}</div>`;
    } else {
      const hidden = outputLines.length - 7;
      const preview = [...outputLines.slice(0, 5), `… +${hidden} lines`, ...outputLines.slice(-2)].join("\n");
      const remembered = Object.prototype.hasOwnProperty.call(state.activityOutputOpen, outputKey) ? state.activityOutputOpen[outputKey] : null;
      const outputOpen = remembered === null ? status === "failed" : remembered;
      outputBlock = `<details class="activity-output activity-output-long" data-activity-output-key="${esc(outputKey)}"${outputOpen ? " open" : ""}><summary><code>${esc(preview)}</code><small>${status === "failed" ? "错误输出" : "查看完整 transcript"}${meta ? ` · ${esc(meta)}` : ""}</small></summary><code class="activity-output-full">${esc(output)}</code></details>`;
    }
  } else if (!active && (status === "completed" || status === "succeeded") && command) {
    outputBlock = `<div class="activity-output empty"><code>(no output)</code></div>`;
  }
  const copyButton = activityCopyText(item) ? `<button type="button" class="chat-copy-button activity-copy-button" data-copy-chat-block="${blockIndex}" data-copy-activity-item="${itemIndex}" title="复制这条活动" aria-label="复制这条活动">⧉</button>` : "";
  return `<div class="activity-event${active ? " current" : ""}${statusClass}"><span class="activity-event-dot" aria-hidden="true"></span><div class="activity-event-body"><div class="activity-event-head"><strong>${action}</strong>${subjectBlock}${cwd}<span class="activity-event-status">${statusText}</span>${copyButton}</div>${planBlock}${changesBlock}${outputBlock}</div></div>`;
}

function renderChatBlock(block, blockIndex, liveActivity = false) {
  const blockAttribute = ` data-chat-block-index="${blockIndex}"`;
  if (block.role === "activities") {
    const activityKey = String(block.activityKey || `activity-${blockIndex}`);
    const displayItems = block.items.filter((item, index) => activityEventIsVisible(item, liveActivity && index === block.items.length - 1));
    if (!displayItems.length) return "";
    const visibleCount = Math.max(16, Number(state.activityVisibleCounts[activityKey]) || 16);
    const items = displayItems.slice(-visibleCount);
    const latest = items[items.length - 1] || {};
    const current = liveActivity ? `<span class="activity-current">${esc(latest.text || uiLabel("activityWorking"))}</span>` : "";
    const hiddenCount = displayItems.length - items.length;
    const older = hiddenCount ? `<button type="button" class="activity-load-older" data-activity-key="${esc(activityKey)}">${uiLabel("loadEarlierActivity", { count: hiddenCount })}</button>` : "";
    return `<details class="activity-group${liveActivity ? " live" : ""}"${blockAttribute}${liveActivity ? ' data-live="true" open' : ""}><summary><span class="activity-pulse" aria-hidden="true"><i></i></span><strong>${uiLabel("activity")}</strong>${current}<small>${displayItems.length}</small></summary><div class="activity-events">${older}${items.map(item => { const itemIndex = block.items.indexOf(item); const outputKey = `activity-output:${activityKey}:${item.itemId || itemIndex}`; return renderActivityEvent(item, liveActivity && itemIndex === block.items.length - 1, blockIndex, itemIndex, outputKey); }).join("")}</div></details>`;
  }
  const deliveryLabels = { sending: uiLabel("sending"), steering: uiLabel("steering"), steered: uiLabel("steering"), queued: uiLabel("queued"), running: `${statusLabel("running")} · Codex`, failed: uiLabel("failedSend") };
  const label = block.role === "user" ? (block.commandBlock ? uiLabel("commandLabel") : "") : (block.commandBlock ? uiLabel("commandResult") : uiLabel("codexPartner"));
  return `<article class="message ${block.role}${block.commandBlock ? " command-message" : ""}${block.streaming ? " streaming" : ""}"${blockAttribute} data-item-id="${esc(block.itemId || "")}"><div class="message-avatar" role="img" aria-label="${block.role === "user" ? uiLabel("you") : uiLabel("codexPartner")}" title="${block.role === "user" ? uiLabel("you") : uiLabel("codexPartner")}">${block.role === "user" ? humanMarkup() : mascotMarkup("mascot-chat")}</div><div class="message-body copyable-chat-block"><button type="button" class="chat-copy-button" data-copy-chat-block="${blockIndex}" title="复制消息" aria-label="复制消息">⧉</button><div class="message-meta">${label ? `<strong>${label}</strong>` : ""}${block.origin === "terminal" ? `<span>${uiLabel("messageFromTerminal")}</span>` : ""}${block.time ? `<time datetime="${esc(block.time)}">${esc(shortDate(block.time))}</time>` : ""}</div><div class="message-content">${block.html || ""}${block.streaming ? `<span class="stream-cursor"></span>` : ""}</div>${deliveryLabels[block.delivery] ? `<div class="message-delivery ${esc(block.delivery)}" title="${esc(block.error)}">${esc(deliveryLabels[block.delivery])}</div>` : ""}</div></article>`;
}

function loadEarlierActivity(activityKey) {
  const block = (state.chatBlocks || []).find(item => item.role === "activities" && String(item.activityKey || "") === activityKey);
  if (!block) return;
  const displayCount = block.items.filter(item => activityEventIsVisible(item)).length;
  const visibleCount = Math.max(16, Number(state.activityVisibleCounts[activityKey]) || 16);
  state.activityVisibleCounts[activityKey] = Math.min(displayCount, visibleCount + 16);
  paintVirtualChat(false);
}

function paintVirtualChat(stickToBottom = false) {
  if (state.chatSelectionActive) { state.chatRenderDeferred = true; state.chatDeferredStickToBottom ||= stickToBottom; return; }
  const stream = $("#chat-log"); const blocks = state.chatBlocks || []; const windowSize = 90;
  const viewport = captureChatViewport(stream);
  if (!blocks.length) { stream.innerHTML = `<div class="chat-empty">${mascotMarkup("mascot-chat")}<p>${uiLabel("firstMessage")}</p></div>`; return; }
  if (state.chatVirtualStart === null || stickToBottom) state.chatVirtualStart = Math.max(0, blocks.length - windowSize);
  const start = Math.max(0, Math.min(state.chatVirtualStart, Math.max(0, blocks.length - windowSize)));
  const end = Math.min(blocks.length, start + windowSize);
  const average = Math.max(72, state.chatAverageHeight || 112);
  const topHeight = start * average; const bottomHeight = (blocks.length - end) * average;
  const olderControl = state.historyHasMore && start === 0 ? `<button type="button" class="chat-load-older" ${state.historyLoading ? "disabled" : ""}>${uiLabel("loadEarlierMessages")}</button>` : "";
  let liveActivityIndex = -1;
  if (["running", "retrying", "queued"].includes(state.selectedTask?.status)) {
    for (let index = blocks.length - 1; index >= 0; index -= 1) {
      if (blocks[index].role === "activities") { liveActivityIndex = index; break; }
    }
  }
  stream.innerHTML = `<div class="chat-virtual-spacer" style="height:${topHeight}px"></div>${olderControl}${blocks.slice(start, end).map((block, index) => renderChatBlock(block, start + index, start + index === liveActivityIndex)).join("")}<div class="chat-virtual-spacer" style="height:${bottomHeight}px"></div>`;
  const rendered = $$(".message, .activity-group", stream);
  if (rendered.length) {
    const measured = rendered.reduce((total, node) => total + node.getBoundingClientRect().height + 24, 0) / rendered.length;
    state.chatAverageHeight = Math.max(72, Math.min(360, state.chatAverageHeight * .75 + measured * .25));
  }
  $(".chat-load-older", stream)?.addEventListener("click", loadOlderTimeline);
  $$(".activity-load-older", stream).forEach(button => button.addEventListener("click", () => loadEarlierActivity(button.dataset.activityKey || "")));
  $$(".activity-output-long[data-activity-output-key]", stream).forEach(output => output.addEventListener("toggle", () => {
    state.activityOutputOpen[output.dataset.activityOutputKey] = output.open;
  }));
  hydrateChatFiles();
  if (!stream.dataset.virtualScroll) {
    stream.dataset.virtualScroll = "1";
    stream.addEventListener("scroll", () => {
      if (!state.chatBlocks.length) return;
      const latestStart = Math.max(0, state.chatBlocks.length - windowSize);
      if (chatIsNearBottom(stream)) {
        if ((state.chatVirtualStart || 0) !== latestStart) { state.chatVirtualStart = latestStart; paintVirtualChat(true); }
        return;
      }
      const target = Math.max(0, Math.floor(stream.scrollTop / Math.max(72, state.chatAverageHeight)) - 12);
      const bounded = Math.min(target, Math.max(0, state.chatBlocks.length - windowSize));
      const currentStart = state.chatVirtualStart || 0;
      if ((bounded === 0 && currentStart !== 0) || Math.abs(bounded - currentStart) >= 18) { state.chatVirtualStart = bounded; paintVirtualChat(false); }
    }, { passive: true });
  }
  restoreChatViewport(stream, viewport, stickToBottom);
  state.chatSnapToBottom = false;
}

function chatHasTextSelection() {
  const selection = window.getSelection?.();
  const stream = $("#chat-log");
  if (!selection || selection.isCollapsed || !selection.rangeCount || !stream) return false;
  const range = selection.getRangeAt(0);
  return stream.contains(range.commonAncestorContainer);
}

function releaseChatSelectionLock() {
  if (state.chatPointerSelecting || chatHasTextSelection()) return;
  state.chatSelectionActive = false;
  if (!state.chatRenderDeferred) return;
  const stickToBottom = Boolean(state.chatDeferredStickToBottom);
  state.chatRenderDeferred = false;
  state.chatDeferredStickToBottom = false;
  const deferredBlocks = state.deferredChatBlocks;
  state.deferredChatBlocks = null;
  requestAnimationFrame(() => {
    if (deferredBlocks) { state.chatBlocks = deferredBlocks; paintVirtualChat(stickToBottom); requestAnimationFrame(scheduleRenderChat); }
    else renderChat();
    if (stickToBottom) requestAnimationFrame(scrollChatToBottom);
  });
}

function installChatSelectionGuard() {
  const stream = $("#chat-log");
  if (!stream || stream.dataset.selectionGuard === "1") return;
  stream.dataset.selectionGuard = "1";
  stream.addEventListener("pointerdown", event => {
    if (event.button !== 0 || event.target.closest("button, input, textarea, select, summary")) return;
    state.chatPointerSelecting = true;
    state.chatPointerDragged = false;
    state.chatPointerStartX = event.clientX;
    state.chatPointerStartY = event.clientY;
    state.chatSelectionActive = true;
  });
  stream.addEventListener("pointermove", event => {
    if (!state.chatPointerSelecting || state.chatPointerDragged) return;
    if (Math.hypot(event.clientX - state.chatPointerStartX, event.clientY - state.chatPointerStartY) >= 5) state.chatPointerDragged = true;
  });
  stream.addEventListener("click", async event => {
    if (state.chatPointerDragged && event.target.closest("a, [data-media-src]")) {
      state.chatPointerDragged = false;
      event.preventDefault(); event.stopImmediatePropagation(); return;
    }
    state.chatPointerDragged = false;
    const button = event.target.closest("[data-copy-chat-block]");
    if (!button) return;
    event.preventDefault(); event.stopPropagation();
    const blockIndex = Number(button.dataset.copyChatBlock);
    const itemIndex = button.dataset.copyActivityItem == null ? -1 : Number(button.dataset.copyActivityItem);
    const text = copyChatBlock(blockIndex, itemIndex);
    if (!text) return toast("没有可复制的内容");
    try {
      await copyText(text);
      button.classList.add("copied"); button.textContent = "✓";
      setTimeout(() => { button.classList.remove("copied"); button.textContent = "⧉"; }, 1200);
      toast("已复制");
    } catch (error) { toast(`复制失败：${error.message}`); }
  }, true);
  document.addEventListener("pointerup", () => {
    if (!state.chatPointerSelecting) return;
    state.chatPointerSelecting = false;
    setTimeout(() => { state.chatPointerDragged = false; releaseChatSelectionLock(); }, 0);
  });
  document.addEventListener("pointercancel", () => { state.chatPointerSelecting = false; state.chatPointerDragged = false; releaseChatSelectionLock(); });
  document.addEventListener("selectionchange", () => {
    if (state.chatSelectionActive && !state.chatPointerSelecting) setTimeout(releaseChatSelectionLock, 0);
  });
}

installChatSelectionGuard();

async function loadOlderTimeline() {
  if (!state.selectedId || !state.historyHasMore || state.historyLoading) return chatHistoryLoadPromise;
  const taskId = state.selectedId; const stream = $("#chat-log"); const previousHeight = stream.scrollHeight;
  state.historyLoading = true; paintVirtualChat(false);
  chatHistoryLoadPromise = HistoryPagination.fetchEarlierTimelinePages(
    cursor => api(`/tasks/${encodeURIComponent(taskId)}/timeline?limit=160&before=${encodeURIComponent(cursor)}`),
    { cursor: state.historyCursor, hasMore: state.historyHasMore, messageTarget: 12, maxPages: 8 },
  )
    .then(page => {
      if (state.selectedId !== taskId) return;
      const existing = new Set(state.selectedEvents.map(event => `${event.stream}:${event.id}`));
      const older = (page.items || []).filter(event => !existing.has(`${event.stream}:${event.id}`));
      const metrics = state.selectedEvents.filter(event => event.stream === "metrics");
      state.selectedEvents = [...older, ...state.selectedEvents.filter(event => event.stream !== "metrics"), ...(page.metrics || metrics)];
      state.historyCursor = page.next_cursor || ""; state.historyHasMore = Boolean(page.has_more);
      state.chatVirtualStart = 0; renderChat();
      requestAnimationFrame(() => { stream.scrollTop = Math.max(0, stream.scrollHeight - previousHeight); });
    })
    .catch(error => toast(error.message))
    .finally(() => { state.historyLoading = false; chatHistoryLoadPromise = null; });
  return chatHistoryLoadPromise;
}
function appendStreamingDelta(payload) {
  if (state.chatSelectionActive) { state.chatRenderDeferred = true; return false; }
  const delta = String(payload?.delta || "");
  const itemId = String(payload?.item_id || "");
  if (!delta || !itemId) return false;
  const stream = $("#chat-log");
  const articles = $$("article.message.assistant", stream);
  const article = articles[articles.length - 1];
  if (!article || article.dataset.itemId !== itemId) return false;
  const content = $(".message-content", article);
  if (!content) return false;
  const stickToBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 96;
  const cursor = $(".stream-cursor", content);
  const fragment = document.createDocumentFragment();
  delta.split("\n").forEach((part, index) => { if (index) fragment.append(document.createElement("br")); if (part) fragment.append(document.createTextNode(part)); });
  content.insertBefore(fragment, cursor || null);
  article.classList.add("streaming");
  if (stickToBottom) stream.scrollTop = stream.scrollHeight;
  return true;
}
async function hydrateChatFiles() {
  for (const node of $$("[data-chat-file]")) {
    if (node.dataset.loaded) continue;
    node.dataset.loaded = "1";
    const path = node.dataset.chatFile;
    try {
      const name = path.toLowerCase();
      const imageMime = name.endsWith(".png") ? "image/png" : /\.jpe?g$/i.test(name) ? "image/jpeg" : name.endsWith(".gif") ? "image/gif" : name.endsWith(".webp") ? "image/webp" : name.endsWith(".bmp") ? "image/bmp" : name.endsWith(".svg") ? "image/svg+xml" : "";
      const imageByName = Boolean(imageMime);
      const taskId = state.selectedId;
      const cacheKey = `${taskId}:${path}`;
      const mediaKind = node.dataset.chatMedia || "";
      const isImage = node.dataset.chatKind === "localImage" || imageByName;
      const preview = node.querySelector(".chat-file-preview");
      const resolvedKind = chatViewerKind(path, isImage ? "image" : mediaKind);
      node.tabIndex = 0;
      node.setAttribute("role", "button");
      node.setAttribute("aria-label", `预览 ${path.split("/").pop() || path}`);
      node.onclick = event => {
        if (event.target instanceof Element && event.target.closest("audio, video")) return;
        event.preventDefault();
        openChatFileViewer({ taskId, path, kind: resolvedKind });
      };
      node.onkeydown = event => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        openChatFileViewer({ taskId, path, kind: resolvedKind });
      };
      if (isImage && preview) {
        let url = chatThumbnailObjectUrls.get(cacheKey) || "";
        if (!url) {
          let load = chatThumbnailLoads.get(cacheKey);
          if (!load) {
            load = workspaceFetch(`/tasks/${encodeURIComponent(taskId)}/workspace/thumbnail?size=640&path=${encodeURIComponent(path)}`)
              .then(async response => {
                if (!response.ok) throw new Error("thumbnail failed");
                const objectUrl = URL.createObjectURL(await response.blob());
                chatThumbnailObjectUrls.set(cacheKey, objectUrl);
                return objectUrl;
              })
              .finally(() => chatThumbnailLoads.delete(cacheKey));
            chatThumbnailLoads.set(cacheKey, load);
          }
          url = await load;
        }
        preview.hidden = false;
        preview.innerHTML = `<a class="chat-image-link" href="#" aria-label="${esc(path.split("/").pop() || path)}"><img src="${url}" alt="${esc(path.split("/").pop() || path)}" /></a>`;
      } else if (["audio", "video"].includes(mediaKind) && preview) {
        let url = chatFileObjectUrls.get(cacheKey) || "";
        if (!url) {
          const response = await workspaceFetch(`/tasks/${encodeURIComponent(taskId)}/workspace/download?path=${encodeURIComponent(path)}`);
          if (!response.ok) throw new Error("media load failed");
          url = URL.createObjectURL(await response.blob());
          chatFileObjectUrls.set(cacheKey, url);
        }
        preview.hidden = false;
        preview.classList.add("chat-media-preview");
        preview.innerHTML = mediaKind === "audio"
          ? `<audio controls preload="metadata" src="${url}"></audio>`
          : `<video controls preload="metadata" src="${url}"></video>`;
      } else {
        node.classList.add("file-only");
      }
    } catch (_) { node.classList.add("unavailable"); }
  }
}

function chatFileExtension(path) {
  const name = String(path || "").split("?")[0].split("#")[0].toLowerCase();
  const match = name.match(/\.([a-z0-9]+)$/i);
  return match ? match[1] : "";
}
function chatViewerKind(path, hint = "") {
  if (["image", "audio", "video", "pdf", "text"].includes(hint)) return hint;
  const ext = chatFileExtension(path);
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "avif"].includes(ext)) return "image";
  if (["mp3", "wav", "ogg", "oga", "m4a", "aac", "flac", "opus", "weba"].includes(ext)) return "audio";
  if (["mp4", "webm", "mov", "m4v", "ogv", "mkv"].includes(ext)) return "video";
  if (ext === "pdf") return "pdf";
  if (["txt", "md", "markdown", "json", "jsonl", "yaml", "yml", "toml", "ini", "csv", "tsv", "log", "py", "js", "ts", "tsx", "jsx", "css", "html", "xml", "sh", "rs", "go", "java", "c", "cc", "cpp", "h", "hpp", "sql"].includes(ext)) return "text";
  return "file";
}
function chatViewerLabel(kind) {
  return ({ image: "IMAGE", audio: "AUDIO", video: "VIDEO", pdf: "PDF", text: "TEXT" })[kind] || "FILE";
}
function chatDownloadUrl(taskId, path) {
  return `/api/tasks/${encodeURIComponent(taskId)}/workspace/download?path=${encodeURIComponent(path)}`;
}
async function chatFileObjectUrl(taskId, path, kind = "") {
  const cacheKey = `${taskId}:${path}`;
  let url = chatFileObjectUrls.get(cacheKey) || "";
  if (url) return url;
  const response = await workspaceFetch(`/tasks/${encodeURIComponent(taskId)}/workspace/download?path=${encodeURIComponent(path)}`);
  if (!response.ok) throw await workspaceResponseError(response);
  let blob = await response.blob();
  const ext = chatFileExtension(path);
  const fallbackType = kind === "pdf" ? "application/pdf" : kind === "image" && ext === "svg" ? "image/svg+xml" : "";
  if (fallbackType && !blob.type) blob = new Blob([blob], { type: fallbackType });
  url = URL.createObjectURL(blob);
  chatFileObjectUrls.set(cacheKey, url);
  return url;
}
function clearMediaViewerBody() {
  if (mediaViewerMarkdown) {
    try { mediaViewerMarkdown.destroy(); } catch (_) {}
    mediaViewerMarkdown = null;
  }
  const body = $("#media-viewer-body");
  if (body) body.innerHTML = "";
}
function closeChatFileViewer() {
  const viewer = $("#media-viewer");
  if (!viewer) return;
  clearMediaViewerBody();
  viewer.classList.remove("open");
  viewer.setAttribute("aria-hidden", "true");
}
$("#media-viewer-close")?.addEventListener("click", closeChatFileViewer);
$("#media-viewer")?.addEventListener("click", event => {
  if (event.target === event.currentTarget) closeChatFileViewer();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("#media-viewer")?.classList.contains("open")) {
    event.preventDefault();
    closeChatFileViewer();
  }
});
async function renderTextFileViewer(taskId, path) {
  const previewResponse = await workspaceFetch(`/tasks/${encodeURIComponent(taskId)}/workspace?path=${encodeURIComponent(path)}`);
  let content = "";
  if (previewResponse.ok) {
    const response = await previewResponse.json();
    content = response.content ?? "";
  } else if (previewResponse.status === 413) {
    const downloadResponse = await workspaceFetch(`/tasks/${encodeURIComponent(taskId)}/workspace/download?path=${encodeURIComponent(path)}`);
    if (!downloadResponse.ok) throw await workspaceResponseError(downloadResponse);
    const blob = await downloadResponse.blob();
    if (blob.size > 2_000_000) throw new Error("文本文件过大，请下载后查看");
    content = await blob.text();
  } else {
    throw await workspaceResponseError(previewResponse);
  }
  const body = $("#media-viewer-body");
  const ext = chatFileExtension(path);
  if (["md", "markdown"].includes(ext) && window.toastui?.Editor) {
    body.innerHTML = `<div id="media-viewer-markdown" class="media-viewer-markdown"></div>`;
    mediaViewerMarkdown = toastui.Editor.factory({
      el: $("#media-viewer-markdown"),
      viewer: true,
      initialValue: content,
      usageStatistics: false,
      ...(typeof markdownOptions === "function" ? markdownOptions() : {}),
    });
    return;
  }
  body.innerHTML = `<pre class="media-viewer-text"></pre>`;
  $(".media-viewer-text", body).textContent = content;
}
async function openChatFileViewer({ taskId, path, kind }) {
  const viewer = $("#media-viewer");
  const body = $("#media-viewer-body");
  if (!viewer || !body || !taskId || !path) return;
  const filename = path.split("/").filter(Boolean).pop() || path;
  clearMediaViewerBody();
  $("#media-viewer-title").textContent = filename;
  $("#media-viewer-path").textContent = path;
  $("#media-viewer-kind").textContent = chatViewerLabel(kind);
  const download = $("#media-viewer-download");
  download.href = chatDownloadUrl(taskId, path);
  download.download = filename;
  viewer.classList.add("open");
  viewer.setAttribute("aria-hidden", "false");
  body.innerHTML = `<div class="media-viewer-loading">正在加载预览…</div>`;
  try {
    if (kind === "text") {
      await renderTextFileViewer(taskId, path);
    } else if (["image", "audio", "video", "pdf"].includes(kind)) {
      const url = await chatFileObjectUrl(taskId, path, kind);
      if (kind === "image") body.innerHTML = `<img class="media-viewer-image" src="${url}" alt="${esc(filename)}" />`;
      else if (kind === "audio") body.innerHTML = `<audio class="media-viewer-audio" controls autoplay preload="metadata" src="${url}"></audio>`;
      else if (kind === "video") body.innerHTML = `<video class="media-viewer-video" controls autoplay preload="metadata" src="${url}"></video>`;
      else body.innerHTML = `<iframe class="media-viewer-frame" title="${esc(filename)}" src="${url}#view=FitH"></iframe>`;
    } else {
      body.innerHTML = `<div class="media-viewer-empty"><strong>无法内嵌预览此文件</strong><span>可以直接下载后查看。</span></div>`;
    }
  } catch (error) {
    body.innerHTML = `<div class="media-viewer-error">${esc(error.message || "预览加载失败")}</div>`;
  }
}
function localizeInspectorText(root) {
  const keys = [
    "session", "state", "thread", "sshHost", "workspacePath", "goal", "noGoal", "progress",
    "runHistory", "noRunHistory", "threadControls", "copySession", "compactContext", "moveTrash",
    "workspaceFiles", "changeDirectory", "upload", "edit", "download", "emptyDirectory", "back",
    "archive", "unarchive", "enableMemory", "disableMemory", "autoProvider",
  ];
  const labels = {
    SESSION: uiLabel("session"), 状态: uiLabel("state"), Thread: uiLabel("thread"),
    "SSH 主机": uiLabel("sshHost"), 工作目录: uiLabel("workspacePath"), GOAL: uiLabel("goal"),
    "没有设置 Goal": uiLabel("noGoal"), 进度: uiLabel("progress"), "运行记录": uiLabel("runHistory"),
    "暂无运行记录": uiLabel("noRunHistory"), "THREAD CONTROLS": uiLabel("threadControls"),
    "复制会话": uiLabel("copySession"), "压缩上下文": uiLabel("compactContext"),
    "移到回收站": uiLabel("moveTrash"), "WORKSPACE FILES": uiLabel("workspaceFiles"), "更改目录": uiLabel("changeDirectory"),
    "上传": uiLabel("upload"), "编辑": uiLabel("edit"), "下载": uiLabel("download"), "目录为空，可将文件拖到这里上传。": uiLabel("emptyDirectory"),
    none: goalStatusLabel("none"),
    "返回上级": uiLabel("back"),
  };
  // Workspace DOM is preserved across language changes. Accept every previous
  // translation as input so repeated switching never leaves mixed languages.
  for (const key of keys) {
    for (const value of Object.values(UI_LABELS[key] || {})) labels[value] = uiLabel(key);
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const value = node.nodeValue.trim();
    if (labels[value]) node.nodeValue = node.nodeValue.replace(value, labels[value]);
    else {
      const attemptPrefix = Object.values(UI_LABELS.attempt || {}).find(prefix => value.startsWith(`${prefix} `));
      if (attemptPrefix) node.nodeValue = node.nodeValue.replace(attemptPrefix, uiLabel("attempt"));
    }
  }
}
function renderInspector() {
  const task = state.selectedTask; if (!task) return;
  const content = $("#inspector-content");
  const workspaceSection = $("#workspace-section", content);
  const workspaceError = $(".workspace-error", content);
  const sessions = task.sessions || state.sessions.filter(s => s.task_id === task.id);
  const threadId = task.codex_session_id || "";
  const memoryEnabled = task.memory_mode === "enabled";
  const archiveAction = task.archived ? "unarchive" : "archive";
  const archiveLabel = task.archived ? uiLabel("unarchive") : uiLabel("archive");
  const memoryAction = memoryEnabled ? "memory-disable" : "memory-enable";
  const memoryLabel = memoryEnabled ? uiLabel("disableMemory") : uiLabel("enableMemory");
  content.innerHTML = `<section class="inspector-section"><span class="inspector-label">SESSION</span><div class="inspector-row"><span>状态</span>${status(task.status)}</div><div class="inspector-row"><span>Thread</span><button class="path-button thread-copy" data-copy-thread="${esc(threadId)}" title="${threadId ? "点击复制 Thread ID" : "Thread 尚未创建"}" ${threadId ? "" : "disabled"}>${esc(threadId || "未创建")}</button></div>${task.ssh_host ? `<div class="inspector-row"><span>SSH 主机</span><button class="path-button" data-panel="ssh">${esc(task.ssh_host)}</button></div>` : ""}<div class="inspector-row"><span>工作目录</span><button class="path-button" data-workspace-change title="重新设置工作目录">${esc(task.workspace)}</button></div></section><section class="inspector-section"><span class="inspector-label">GOAL</span><p class="inspector-goal">${esc(task.goal || "没有设置 Goal")}</p><div class="inspector-row"><span>进度</span><span>${esc(task.goal_status || "none")}</span></div></section><details class="inspector-section run-history"><summary><span>运行记录</span><small>${sessions.length}</small></summary><div class="run-history-list">${sessions.length ? sessions.map(session => `<div class="run-row"><span class="run-dot ${esc(session.status)}"></span><span><strong>尝试 ${session.attempt || 0}</strong><small>${esc(session.provider_id || "自动 Provider")} · ${esc(shortDate(session.started_at))}</small></span>${status(session.status)}</div>`).join("") : `<p class="muted">暂无运行记录</p>`}</div></details><section class="inspector-section"><span class="inspector-label">THREAD CONTROLS</span><div class="inspector-actions"><button data-thread-action="fork">复制会话</button><button data-thread-action="compact">压缩上下文</button><button data-thread-action="${archiveAction}">${archiveLabel}</button><button data-thread-action="delete" class="danger">移到回收站</button><button data-thread-action="${memoryAction}" title="当前状态：记忆${memoryEnabled ? "已开启" : "已关闭"}">${memoryLabel}</button></div></section>`;
  if (workspaceSection?.dataset.taskId === task.id) content.append(workspaceSection);
  else if (workspaceError?.dataset.taskId === task.id) content.append(workspaceError);
  localizeInspectorText(content);
  const goalProgress = $(".inspector-goal + .inspector-row span:last-child", content);
  if (goalProgress) goalProgress.textContent = goalStatusLabel(task.goal_status || (task.goal ? "active" : "none"));
}
async function loadWorkspace(path = "") {
  if (!state.selectedTask) return;
  const taskId = state.selectedTask.id;
  const requestId = ++state.workspaceRequestId;
  state.workspacePath = path;
  $(".workspace-error")?.remove();
  try {
    const data = await api(`/tasks/${taskId}/workspace?path=${encodeURIComponent(path)}`);
    if (requestId !== state.workspaceRequestId || state.selectedTask?.id !== taskId) return;
    renderWorkspace(data, taskId);
  } catch (error) {
    if (requestId !== state.workspaceRequestId || state.selectedTask?.id !== taskId) return;
    const existing = $("#workspace-section");
    if (existing) existing.remove();
    $(".workspace-error")?.remove();
    $("#inspector-content").insertAdjacentHTML("beforeend", `<p class="inspector-error workspace-error" data-task-id="${esc(taskId)}">${esc(error.message)}</p>`);
  }
}
function renderWorkspace(data, taskId = state.selectedTask?.id || "") {
  const existing = $("#workspace-section"); if (existing) existing.remove();
  $(".workspace-error")?.remove();
  const path = data.entry.path || "";
  const isDirectory = data.entry.kind === "directory";
  state.workspaceFile = isDirectory ? null : { path, name: data.entry.name, size: data.entry.size || 0, content: data.content || "", editable: data.editable === true };
  const workspaceLocked = ["running", "retrying", "queued"].includes(state.selectedTask?.status);
  const parts = path.split("/").filter(Boolean);
  const breadcrumbs = [`<button class="workspace-crumb ${parts.length ? "" : "current"}" data-browse="" title="工作区根目录">/</button>`];
  parts.forEach((part, index) => {
    const target = parts.slice(0, index + 1).join("/");
    breadcrumbs.push(`<span class="workspace-crumb-separator">/</span><button class="workspace-crumb ${index === parts.length - 1 ? "current" : ""}" data-browse="${esc(target)}">${esc(part)}</button>`);
  });
  const parent = parts.slice(0, -1).join("/");
  const toolbar = `<div class="workspace-toolbar"><button type="button" class="workspace-tool" data-workspace-change title="${workspaceLocked ? "停止当前 Codex turn 后更改工作目录" : "选择工作目录"}" ${workspaceLocked ? "disabled" : ""}><span aria-hidden="true">▰</span> 更改目录</button>${isDirectory ? `<button type="button" class="workspace-tool" data-workspace-upload title="上传文件到当前目录"><span aria-hidden="true">↑</span> 上传</button>` : `${data.editable === true ? `<button type="button" class="workspace-tool" data-workspace-edit="${esc(path)}" title="编辑当前文件"><span aria-hidden="true">✎</span> 编辑</button>` : ""}<button type="button" class="workspace-tool" data-workspace-download="${esc(path)}" title="下载当前文件"><span aria-hidden="true">↓</span> 下载</button>`}</div>`;
  const entries = data.entry.kind === "file"
    ? `<div class="file-preview"><div class="file-preview-head"><span>▣ ${esc(data.entry.name)}</span><small>${formatBytes(data.entry.size)}</small></div><pre>${esc(data.content || "")}</pre></div>`
    : `<div class="workspace-browser" id="workspace-browser">${(data.entries || []).map(entry => `<div class="file-row ${entry.kind}"><button type="button" class="file-open" data-browse="${esc(entry.path)}"><span>${entry.kind === "directory" ? "▰" : "▤"}</span><span>${esc(entry.name)}</span><small>${entry.kind === "directory" ? "目录" : formatBytes(entry.size)}</small></button>${entry.kind === "file" ? `<button type="button" class="file-download" data-workspace-download="${esc(entry.path)}" title="下载 ${esc(entry.name)}" aria-label="下载 ${esc(entry.name)}">↓</button>` : ""}</div>`).join("") || `<p class="muted">目录为空，可将文件拖到这里上传。</p>`}</div>`;
  $("#inspector-content").insertAdjacentHTML("beforeend", `<section class="inspector-section workspace-section" id="workspace-section" data-task-id="${esc(taskId)}" data-entry-kind="${esc(data.entry.kind)}" data-path="${esc(path)}"><span class="inspector-label">WORKSPACE FILES</span>${toolbar}<nav class="workspace-breadcrumbs" aria-label="工作区路径">${breadcrumbs.join("")}</nav>${path ? `<button class="file-up" data-browse="${esc(parent)}">‹ 返回上级</button>` : ""}${entries}</section>`);
  const workspace = $("#workspace-section");
  localizeInspectorText(workspace);
  workspace.querySelector("nav")?.setAttribute("aria-label", uiLabel("workspacePath"));
  workspace.querySelector("[data-workspace-change]")?.setAttribute("title", uiLabel("chooseWorkspace"));
  workspace.querySelector("[data-workspace-upload]")?.setAttribute("title", uiLabel("upload"));
  workspace.querySelector("[data-workspace-edit]")?.setAttribute("title", uiLabel("edit"));
  workspace.querySelector("[data-workspace-download]")?.setAttribute("title", uiLabel("download"));
  workspace.querySelector(".file-up")?.setAttribute("title", uiLabel("back"));
}

async function loadWorkspacePicker(path = "") {
  if (!state.selectedId) return;
  const content = $("#workspace-picker-content");
  if (content) content.innerHTML = `<p class="muted">${uiLabel("readingDirectory")}</p>`;
  try {
    const data = await api(`/tasks/${state.selectedId}/workspace-picker?path=${encodeURIComponent(path)}`);
    state.workspacePickerPath = data.path;
    const input = $("#workspace-picker-input"); if (input) input.value = data.path;
    if (!content) return;
    const roots = (data.roots || []).map(root => `<button type="button" class="workspace-root" data-picker-browse="${esc(root.path)}" title="${esc(root.path)}">${esc(root.name)}</button>`).join("");
    const entries = (data.entries || []).map(entry => `<button type="button" class="workspace-picker-row" data-picker-browse="${esc(entry.path)}"><span aria-hidden="true">▰</span><span>${esc(entry.name)}</span><small>${uiLabel("open")}</small></button>`).join("") || `<p class="muted">${uiLabel("noSubdirectories")}</p>`;
    content.innerHTML = `<div class="workspace-picker-roots">${roots}</div><div class="workspace-picker-location">${data.parent ? `<button type="button" class="workspace-picker-up" data-picker-browse="${esc(data.parent)}" title="返回上级">‹</button>` : ""}<code title="${esc(data.path)}">${esc(data.path)}</code></div><div class="workspace-picker-list">${entries}</div>`;
    const select = $("#workspace-picker-select"); if (select) select.disabled = false;
  } catch (error) {
    if (content) content.innerHTML = `<p class="inspector-error">${esc(error.message)}</p>`;
    const select = $("#workspace-picker-select"); if (select) select.disabled = true;
  }
}

function openWorkspacePicker() {
  if (!state.selectedTask) return;
  if (["running", "retrying", "queued"].includes(state.selectedTask.status)) return toast("请先停止当前 Codex turn，再更改工作目录");
  state.workspacePickerPath = state.selectedTask.workspace;
  openDrawer(`<h2>${uiLabel("chooseWorkspace")}</h2><form id="workspace-picker-form" class="workspace-picker-form"><label for="workspace-picker-input">${uiLabel("serverDirectory")}</label><div><input id="workspace-picker-input" value="${esc(state.selectedTask.workspace)}" autocomplete="off" spellcheck="false"/><button class="secondary" type="submit">${uiLabel("go")}</button></div></form><div id="workspace-picker-content" class="workspace-picker-content"><p class="muted">${uiLabel("readingDirectory")}</p></div><div class="form-actions workspace-picker-actions"><button type="button" class="secondary" id="cancel">${uiLabel("cancel")}</button><button type="button" class="primary" id="workspace-picker-select" disabled>${uiLabel("selectCurrentDirectory")}</button></div>`);
  $("#workspace-picker-form").onsubmit = event => { event.preventDefault(); loadWorkspacePicker($("#workspace-picker-input").value.trim()); };
  loadWorkspacePicker(state.workspacePickerPath);
}

async function selectWorkspaceDirectory() {
  if (!state.selectedId || !state.workspacePickerPath) return;
  const task = await api(`/tasks/${state.selectedId}`, { method: "PATCH", body: JSON.stringify({ workspace: state.workspacePickerPath }) });
  resetWorkspaceBrowser();
  state.selectedTask = { ...state.selectedTask, ...task };
  mergeTask(task);
  closeDrawer(); renderConversation(); renderSessionList();
  await loadWorkspace("");
  toast(`工作目录已切换到 ${task.workspace}`);
}

async function workspaceFetch(path, options = {}, canPrompt = true) {
  const headers = { ...(options.headers || {}) };
  const response = await fetch(`/api${path}`, { ...options, headers });
  if (response.status === 401 && canPrompt) {
    if (await requestSSHLogin()) return workspaceFetch(path, options, false);
  }
  return response;
}

async function workspaceResponseError(response) {
  return Error(await responseErrorMessage(response));
}

async function uploadWorkspaceFile(taskId, destination, file, overwrite = false) {
  const query = new URLSearchParams({ path: destination, filename: file.name, overwrite: String(overwrite) });
  const response = await workspaceFetch(`/tasks/${taskId}/workspace/upload?${query}`, { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file });
  if (response.status === 409 && !overwrite) {
    const conflict = await workspaceResponseError(response);
    if (await appConfirm(uiLabel("overwriteFileConfirm", { name: file.name }))) return uploadWorkspaceFile(taskId, destination, file, true);
    return { skipped: true, error: conflict.message };
  }
  if (!response.ok) throw await workspaceResponseError(response);
  return response.json();
}

async function uploadWorkspaceFiles(files, destination = state.workspacePath) {
  if (!state.selectedId || !files.length || state.workspaceUploading) return;
  const taskId = state.selectedId;
  state.workspaceUploading = true;
  const button = $("[data-workspace-upload]");
  if (button) { button.disabled = true; button.innerHTML = `<span aria-hidden="true">↑</span> 0/${files.length}`; }
  let uploaded = 0; let skipped = 0;
  try {
    for (const [index, file] of [...files].entries()) {
      if (button) button.innerHTML = `<span aria-hidden="true">↑</span> ${index + 1}/${files.length}`;
      const result = await uploadWorkspaceFile(taskId, destination, file);
      if (result?.skipped) skipped += 1; else uploaded += 1;
    }
    if (state.selectedId === taskId) await loadWorkspace(destination);
    toast(`已上传 ${uploaded} 个文件${skipped ? `，跳过 ${skipped} 个` : ""}`);
  } catch (error) {
    toast(`上传失败：${error.message}`);
    if (state.selectedId === taskId) await loadWorkspace(destination);
  } finally {
    state.workspaceUploading = false;
    const input = $("#workspace-upload-input"); if (input) input.value = "";
  }
}

async function downloadWorkspaceFile(path) {
  if (!state.selectedId) return;
  const response = await workspaceFetch(`/tasks/${state.selectedId}/workspace/download?path=${encodeURIComponent(path)}`);
  if (!response.ok) throw await workspaceResponseError(response);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = path.split("/").filter(Boolean).pop() || "download";
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function openWorkspaceFileEditor(path) {
  const file = state.workspaceFile;
  if (!file || file.path !== path || !file.editable) return toast(uiLabel("unsupportedFileEdit"));
  openDrawer(`<div class="drawer-heading"><div><h2>${uiLabel("editFile")}</h2><p class="drawer-meta">${esc(file.path)} · ${formatBytes(file.size)}</p></div></div><form id="workspace-file-form" class="form unified-editor-form workspace-file-form"><div class="editor-field"><span class="form-label">${uiLabel("content")}</span>${textEditorMarkup("workspace-file-editor")}</div><div class="form-actions"><button type="button" class="secondary" id="cancel">${uiLabel("cancel")}</button><button type="submit" class="primary">${uiLabel("save")} <kbd>Ctrl S</kbd></button></div></form>`, { editor: true });
  const editor = mountTextEditor("#workspace-file-editor", { filename: file.path, value: file.content });
  $("#cancel").onclick = closeDrawer;
  $("#workspace-file-form").onsubmit = async event => {
    event.preventDefault(); const restore = setFormBusy(event.target, uiLabel("saving")); const content = editor.getValue();
    try {
      const response = await workspaceFetch(`/tasks/${state.selectedId}/workspace/file?path=${encodeURIComponent(path)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }) });
      if (!response.ok) throw await workspaceResponseError(response);
      closeDrawer(); await loadWorkspace(path); toast(uiLabel("fileSaved"));
    } catch (error) { restore(); toast(uiLabel("saveFailed", { message: error.message })); }
  };
}
function commandToken(value) { return value.trim().slice(1).split(/\s+/, 1)[0].toLowerCase(); }
function commandMatches(value) {
  const token = commandToken(value); const commands = state.commands || [];
  if (!token) return commands;
  return commands.filter(command => command.name.startsWith(token) || (command.aliases || []).some(alias => alias.startsWith(token)));
}
function renderCommandPalette() {
  const input = $("#message-input"); const palette = $("#command-palette");
  if (!input || !input.value.startsWith("/") || input.value.includes("\n")) { palette.hidden = true; return; }
  const matches = commandMatches(input.value); if (!matches.length) { palette.hidden = true; return; }
  state.commandIndex = Math.max(0, Math.min(state.commandIndex, matches.length - 1));
  palette.innerHTML = matches.slice(0, 18).map((command, index) => `<button type="button" class="command-option ${index === state.commandIndex ? "active" : ""}" data-command-index="${index}" role="option" aria-selected="${index === state.commandIndex}"><span class="command-name">/${esc(command.name)} <small>${esc(command.args || "")}</small></span><span class="command-description">${esc(command.description)}${command.destructive ? " · 需要确认" : ""}</span></button>`).join("");
  palette.hidden = false;
}
function selectedCommand() { const matches = commandMatches($("#message-input").value); return matches[state.commandIndex] || matches[0]; }
function completeCommand(command = selectedCommand()) {
  if (!command) return; const input = $("#message-input"); input.value = `/${command.name}${command.args ? " " : ""}`; input.focus(); resizeComposerInput(input); renderCommandPalette();
}
async function applyCommandAction(result) {
  const action = result?.ui_action; if (!action) return;
  if (action === "new_conversation") return createQuickSession();
  if (action === "select_task" && result.task_id) { await refresh(false); await selectSession(result.task_id); $("#message-input").focus(); return; }
  if (action === "clear_selection") { state.selectedId = null; state.selectedTask = null; showEmptyConversation(); return refresh(false); }
  if (action === "refresh") return refresh(false);
  if (action === "open_panel") return openPanel(result.panel || "skills");
  if (action === "open_inspector") { state.inspectorClosed = false; setInspectorOpen(true); return; }
  if (action === "open_workspace") { state.inspectorClosed = false; setInspectorOpen(true); return loadWorkspace(state.workspacePath || ""); }
  if (action === "focus_composer") { $("#message-input").focus(); return; }
  if (action === "attach_text") { pendingAttachments = [result.attachment]; renderComposerAttachments(); $("#message-input").focus(); return; }
  if (action === "copy_last") {
    const last = [...(state.selectedEvents || [])].reverse().find(event => isAssistantEvent(event) && eventText(event.payload));
    if (!last) return toast("还没有可复制的 Codex 回复");
    try { await navigator.clipboard.writeText(eventText(last.payload)); toast("已复制最近一条回复"); } catch (_) { toast("浏览器拒绝了剪贴板访问"); }
    return;
  }
  if (action === "toggle_raw") { const value = result.value === "on" ? true : result.value === "off" ? false : !state.rawActivity; state.rawActivity = value; return renderChat(); }
  if (action === "theme") { const value = result.value === "toggle" ? (document.documentElement.dataset.theme === "wasteland" ? "dark" : "wasteland") : result.value; setTheme(value, true); return; }
  if (action === "title") { const value = result.value === "toggle" ? localStorage.getItem("codex-dashboard-title") !== "on" : result.value !== "off"; localStorage.setItem("codex-dashboard-title", value ? "on" : "off"); document.title = value && state.selectedTask ? `${state.selectedTask.name || "Codex"} · Codex Partner` : "Codex Partner"; return; }
  if (action === "vim") { localStorage.setItem("codex-dashboard-vim", result.value === "off" ? "off" : "on"); return toast("Vim 模式标记已更新"); }
  if (action === "show_keymap") return toast("Enter 发送到当前 turn · Alt+Enter 排到下一轮 · Shift+Enter 换行 · Esc 停止 · 空输入框 ↑/↓ 历史");
  if (action === "disconnect") { socketPaused = true; clearTimeout(socketReconnectTimer); if (socket) { socket.onclose = null; socket.close(); socket = null; } setRealtimeChannel("task", "paused"); return toast("当前会话的实时连接已暂停"); }
}
async function submitSlashCommand(raw) {
  if (!state.selectedId) return;
  const clientMessageId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  let result = await api(`/tasks/${state.selectedId}/commands`, { method: "POST", body: JSON.stringify({ command: raw, client_message_id: clientMessageId }) });
  if (result.requires_confirmation && await appConfirm(uiLabel("destructiveCommandConfirm", { command: raw.split(/\s+/)[0] }), { danger: true })) result = await api(`/tasks/${state.selectedId}/commands`, { method: "POST", body: JSON.stringify({ command: raw, confirmed: true, client_message_id: `${clientMessageId}-confirmed` }) });
  if (!result.ok) toast(result.message || "命令执行失败");
  await applyCommandAction(result);
  if (state.selectedId) await refreshSelectedConversation(state.selectedId);
  return result;
}
function connectOverviewSocket() {
  if (overviewSocket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(overviewSocket.readyState)) return;
  if (realtimeChannels.overview !== "reconnecting") setRealtimeChannel("overview", "connecting");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  overviewSocket = new WebSocket(`${scheme}://${location.host}/ws/overview`);
  overviewSocket.onopen = () => { noteRealtime("overview"); setRealtimeChannel("overview", "live"); };
  overviewSocket.onmessage = event => {
    noteRealtime("overview"); setRealtimeChannel("overview", "live");
    const data = JSON.parse(event.data);
    if (data.type === "task_rekeyed" && data.old_task_id && data.new_task_id && data.task) {
      const oldTaskId = data.old_task_id;
      const newTaskId = data.new_task_id;
      state.taskAliases ||= {};
      state.taskAliases[oldTaskId] = newTaskId;
      state.tasks = state.tasks.filter(task => task.id !== oldTaskId && task.id !== newTaskId);
      state.tasks.push(data.task);
      if (state.selectedId === oldTaskId) {
        state.selectedId = newTaskId;
        state.selectedTask = data.task;
        state.selectedMessages = state.selectedMessages.map(message => ({ ...message, task_id: newTaskId }));
        connectSocket(newTaskId, true);
        refreshSelectedConversation(newTaskId);
      }
      renderSessionList(); renderSidebarStats(); renderConversation();
      return;
    }
    if (data.type === "overview_snapshot") {
      applyUserProfile(data.profile, true);
      mergeOverviewSnapshot(data.tasks || []);
      renderSessionList(); renderSidebarStats();
      return;
    }
    if (data.type === "profile_updated") {
      applyUserProfile(data.profile, true);
      return;
    }
    if (data.type === "task_status" && data.task) {
      mergeTask(data.task);
      renderSessionList(); renderSidebarStats();
      if (state.selectedId === data.task.id && state.selectedTask) {
        const workspaceChanged = Boolean(data.task.workspace && data.task.workspace !== state.selectedTask.workspace);
        if (workspaceChanged) resetWorkspaceBrowser();
        state.selectedTask = { ...state.selectedTask, ...data.task };
        renderConversation();
        if (workspaceChanged) loadWorkspace("");
      }
    }
    if (data.type === "task_removed" && data.task_id) {
      state.tasks = state.tasks.filter(task => task.id !== data.task_id);
      if (state.selectedId === data.task_id) {
        state.selectedId = null; state.selectedTask = null; state.selectedMessages = [];
        if (socketTaskId === data.task_id && socket) { socket.onclose = null; socket.close(); socket = null; }
        showEmptyConversation();
      }
      renderSessionList(); renderSidebarStats();
    }
  };
  overviewSocket.onerror = () => setRealtimeChannel("overview", "reconnecting");
  overviewSocket.onclose = event => {
    overviewSocket = null;
    if (event.code === 4401) { requestSSHLogin().then(ok => { if (ok) connectOverviewSocket(); }); return; }
    setRealtimeChannel("overview", "reconnecting");
    clearTimeout(overviewReconnectTimer);
    overviewReconnectTimer = setTimeout(() => { if (document.visibilityState !== "hidden") connectOverviewSocket(); }, 1200);
  };
}
function connectSocket(id, reconnect = false) {
  clearTimeout(socketReconnectTimer);
  if (!reconnect) socketPaused = false;
  if (socket) { socket.onclose = null; socket.close(); }
  setRealtimeChannel("task", reconnect ? "reconnecting" : "connecting");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const nextSocket = new WebSocket(`${scheme}://${location.host}/ws/tasks/${id}`);
  socket = nextSocket; socketTaskId = id;
  nextSocket.onopen = () => { noteRealtime("task"); setRealtimeChannel("task", "live"); if (state.selectedId === id && reconnect) refreshSelectedConversation(id); };
  nextSocket.onmessage = async event => {
    noteRealtime("task"); setRealtimeChannel("task", "live");
    const data = JSON.parse(event.data);
    if (data.type === "snapshot" && state.selectedId === id) {
      state.selectedTask = { ...state.selectedTask, ...(data.task || {}) };
      if (data.messages) replaceTaskMessages(data.messages);
      state.pendingApprovals = data.pending_requests || [];
      renderConversation();
      return;
    }
    if ((data.type === "task_patch" || data.type === "task_status") && state.selectedId === id) {
      const patch = data.type === "task_patch" ? (data.patch || {}) : (data.task || {});
      const previousStatus = state.selectedTask?.status || "";
      state.selectedTask = { ...state.selectedTask, ...patch };
      mergeTask({ id, ...patch });
      if (patch.status && patch.status !== previousStatus) { if (!state.explorationPrecomputed) { state.explorationRevision += 1; state.explorationNeedsSync = true; } scheduleRenderChat(); }
      renderConversation(false); renderSessionList(); renderSidebarStats();
      return;
    }
    if (data.type === "workspace_changed" && state.selectedId === id) {
      if (!state.workspaceUploading) loadWorkspace(state.workspacePath || "");
      return;
    }
    if (data.type === "activity_map_ready" && state.selectedId === id) {
      loadPrecomputedActivityMap(true);
      return;
    }
    if (data.type === "activity_map_failed" && state.selectedId === id) {
      state.explorationMapStatus = "failed"; state.explorationLoadError = data.error || "活动图预计算失败"; renderExplorationMap();
      return;
    }
    if (data.type === "activity_map_patch" && state.selectedId === id) {
      const nodes = new Map((state.explorationNodes || []).map(node => [node.id, node]));
      for (const node of data.upsert_nodes || []) nodes.set(node.id, node);
      for (const nodeId of data.remove_node_ids || []) nodes.delete(nodeId);
      state.explorationNodes = [...nodes.values()].sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0));
      if (Array.isArray(data.edges)) state.explorationEdges = data.edges;
      state.explorationRevision = Number(data.revision || state.explorationRevision);
      state.explorationMapStatus = "ready";
      renderExplorationMap({ liveUpdate: true });
      return;
    }
    if (data.type === "event" && state.selectedId === id) {
      const protocolNoise = isHiddenProtocolNoise(data.payload);
      const tokenUsage = isTokenUsageProtocol(data.payload);
      if (!protocolNoise || tokenUsage) {
        const timelineEvent = { session_id: data.session_id, ts: data.ts, stream: data.stream, payload: JSON.stringify(data.payload) };
        state.selectedEvents.push(timelineEvent);
        if (!state.explorationPrecomputed && !protocolNoise && state.explorationEvents.length) {
          state.explorationEvents.push(timelineEvent); state.explorationLoadedEventCount = state.explorationEvents.length;
          if (isExplorationRelevantPayload(data.payload)) { state.explorationRevision += 1; state.explorationNeedsSync = true; }
        }
      }
      if (tokenUsage) renderContextUsage();
      if (!protocolNoise) scheduleRuntimeMetricsRefresh();
      const phase = protocolNoise ? "" : activityPhase(data.payload);
      if (phase) { livePhases.set(id, phase); renderTurnProgress(); }
      if (!protocolNoise && (String(data.payload?.type || "").toLowerCase() !== "agent_delta" || !appendStreamingDelta(data.payload))) scheduleRenderChat();
      if (!protocolNoise && !isAssistantEvent({ stream: data.stream, payload: data.payload }) && !isUserEvent({ stream: data.stream, payload: data.payload })) showActivity(data.payload);
    }
    if (data.type === "message" && state.selectedId === id) { upsertTaskMessage(data); renderQueuedMessages(); scheduleRenderChat(); }
    if (data.type === "message_removed" && state.selectedId === id) { removeTaskMessage(data.message_id); if (state.editingQueuedId === data.message_id) { state.editingQueuedId = null; $("#message-input").value = ""; } renderQueuedMessages(); scheduleRenderChat(); }
    if (data.type === "server_request" && state.selectedId === id) { if (data.request && !state.pendingApprovals.some(item => item.id === data.request.id)) state.pendingApprovals.push(data.request); livePhases.set(id, "phaseInteraction"); renderApprovalCenter(); renderTurnProgress(); }
    if (data.type === "server_request_resolved" && state.selectedId === id) { state.pendingApprovals = state.pendingApprovals.filter(item => item.id !== data.request_id); renderApprovalCenter(); }
    if ((data.type === "session" || data.type === "provider_failover") && state.selectedId === id) {
      if (data.type === "provider_failover") livePhases.set(id, "phaseProvider");
      await refreshSelectedConversation(id);
    }
  };
  nextSocket.onerror = () => { if (socket === nextSocket && !socketPaused) setRealtimeChannel("task", "reconnecting"); };
  nextSocket.onclose = event => {
    if (socket === nextSocket) socket = null;
    if (state.selectedId !== id) { setRealtimeChannel("task", "idle"); return; }
    if (event.code === 4401) { requestSSHLogin().then(ok => { if (ok && state.selectedId === id) connectSocket(id, true); }); return; }
    if (socketPaused) { setRealtimeChannel("task", "paused"); return; }
    setRealtimeChannel("task", "reconnecting");
    socketReconnectTimer = setTimeout(() => connectSocket(id, true), 900);
  };
}
function showActivity(payload) {
  const task = state.selectedTask;
  if (!task || !["running", "retrying", "queued"].includes(task.status)) return;
  const phase = activityPhase(payload);
  const detail = String(eventText(payload) || "").replace(/\s+/g, " ").trim();
  if (!phase && !detail) return;
  const phaseText = phase ? activityPhaseLabel(phase) : "";
  const generic = ["exec", "exec_command", "toolCall", "mcpToolCall"].includes(detail);
  const text = detail && !generic && detail !== phaseText ? `${phaseText}${phaseText ? " · " : ""}${detail}` : (phase || detail);
  livePhases.set(task.id, String(text).slice(0, 180));
  renderTurnProgress();
}
function terminalStatus(label, kind = "") { const node = $("#terminal-status"); node.textContent = label; node.className = `terminal-status ${kind}`; }
function ensureTerminalEmulator() {
  if (terminalEmulator) return true;
  if (!window.Terminal || !window.FitAddon || !window.Unicode11Addon || !window.WebLinksAddon) {
    terminalStatus(uiLabel("terminalModuleError"), "error");
    toast(uiLabel("terminalModuleToast"));
    return false;
  }
  terminalEmulator = new window.Terminal({
    cursorBlink: true,
    convertEol: false,
    scrollback: 10000,
    allowProposedApi: true,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: 13,
    lineHeight: 1.35,
    theme: { background: "#070a0b", foreground: "#d5e6d0", cursor: "#d6f77f", selectionBackground: "#52683e88" },
  });
  terminalFitAddon = new window.FitAddon.FitAddon();
  terminalEmulator.loadAddon(terminalFitAddon);
  terminalEmulator.loadAddon(new window.Unicode11Addon.Unicode11Addon());
  terminalEmulator.loadAddon(new window.WebLinksAddon.WebLinksAddon());
  terminalEmulator.open($("#terminal-screen"));
  terminalEmulator.onData(data => sendTerminal({ type: "input", data }));
  terminalEmulator.onResize(({ cols, rows }) => sendTerminal({ type: "resize", cols, rows }));
  return true;
}
function fitTerminal() {
  if (!terminalFitAddon || !terminalEmulator) return;
  try { terminalFitAddon.fit(); } catch (_) { /* The modal may not have layout dimensions yet. */ }
}
function sendTerminal(payload) {
  if (terminalSocket?.readyState !== WebSocket.OPEN) return false;
  terminalSocket.send(JSON.stringify(payload));
  return true;
}
function sendTerminalResize() {
  fitTerminal();
  if (!terminalSocket || terminalSocket.readyState !== WebSocket.OPEN || !terminalEmulator) return;
  sendTerminal({ type: "resize", cols: terminalEmulator.cols, rows: terminalEmulator.rows });
}
function connectTerminal(taskId, reconnect = false) {
  if (!taskId) return toast(uiLabel("selectSessionFirst"));
  if (!ensureTerminalEmulator()) return;
  clearTimeout(terminalReconnectTimer);
  if (terminalSocket) { terminalSocket.onclose = null; terminalSocket.close(); terminalSocket = null; }
  terminalTaskId = taskId;
  terminalShouldReconnect = true;
  if (reconnect) terminalEmulator.write(`\r\n[${uiLabel("terminalReconnectNotice")}]\r\n`);
  else terminalEmulator.reset();
  terminalStatus(reconnect ? uiLabel("terminalReconnecting") : uiLabel("terminalConnecting"), reconnect ? "reconnecting" : "");
  setRealtimeChannel("terminal", reconnect ? "reconnecting" : "connecting");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const nextSocket = new WebSocket(`${scheme}://${location.host}/ws/terminal/${encodeURIComponent(taskId)}`);
  terminalSocket = nextSocket;
  nextSocket.onopen = () => { noteRealtime("terminal"); terminalStatus(uiLabel("terminalConnecting")); sendTerminalResize(); terminalEmulator.focus(); };
  nextSocket.onmessage = event => {
    noteRealtime("terminal");
    const data = JSON.parse(event.data);
    if (data.type === "ready") { terminalReconnectAttempt = 0; $("#terminal-cwd").textContent = `${data.cwd} · ${data.shell}`; terminalStatus(uiLabel("terminalConnected"), "connected"); setRealtimeChannel("terminal", "live"); sendTerminalResize(); return; }
    if (data.type === "output") { terminalEmulator.write(data.data); return; }
    if (data.type === "exit") { terminalShouldReconnect = false; setRealtimeChannel("terminal", "closed"); terminalStatus(`${uiLabel("terminalClosed")}${data.code == null ? "" : ` · ${data.code}`}`, "error"); terminalEmulator.write(`\r\n[${uiLabel("terminalClosed")}]\r\n`); return; }
    if (data.type === "pong") { terminalStatus(uiLabel("terminalConnected"), "connected"); setRealtimeChannel("terminal", "live"); }
  };
  nextSocket.onerror = () => { if (terminalSocket === nextSocket) { terminalStatus(uiLabel("terminalReconnecting"), "reconnecting"); setRealtimeChannel("terminal", "reconnecting"); } };
  nextSocket.onclose = event => {
    if (terminalSocket !== nextSocket) return;
    terminalSocket = null;
    const stillOpen = $("#terminal-window").classList.contains("open") && terminalTaskId === taskId;
    if (event.code === 4401) { requestSSHLogin().then(ok => { if (ok && stillOpen) connectTerminal(taskId, true); }); return; }
    if (!terminalShouldReconnect || !stillOpen) { setRealtimeChannel("terminal", terminalShouldReconnect ? "idle" : "closed"); return; }
    terminalStatus(uiLabel("terminalReconnecting"), "reconnecting"); setRealtimeChannel("terminal", "reconnecting");
    const delay = Math.min(5000, 700 * (2 ** terminalReconnectAttempt));
    terminalReconnectAttempt += 1;
    terminalReconnectTimer = setTimeout(() => connectTerminal(taskId, true), delay);
  };
}
function openTerminal() {
  if (!state.selectedId) return toast("请先选择一个会话");
  const windowNode = $("#terminal-window");
  windowNode.classList.add("open"); windowNode.setAttribute("aria-hidden", "false");
  if (!ensureTerminalEmulator()) return;
  requestAnimationFrame(fitTerminal);
  const socketActive = terminalSocket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(terminalSocket.readyState);
  if (terminalTaskId !== state.selectedId || !socketActive) {
    const preserveHistory = terminalTaskId === state.selectedId && terminalShouldReconnect;
    connectTerminal(state.selectedId, preserveHistory);
  }
  else terminalEmulator.focus();
  if (!terminalResizeObserver) { terminalResizeObserver = new ResizeObserver(sendTerminalResize); terminalResizeObserver.observe($("#terminal-screen")); }
}
function hideTerminal() {
  $("#terminal-window").classList.remove("open");
  $("#terminal-window").setAttribute("aria-hidden", "true");
  renderConnectionStatus();
}
function closeTerminal() { hideTerminal(); }
function destroyTerminal() {
  clearTimeout(terminalReconnectTimer); terminalShouldReconnect = false; terminalReconnectAttempt = 0;
  if (terminalSocket) { terminalSocket.onclose = null; try { terminalSocket.send(JSON.stringify({ type: "close" })); } catch (_) {} terminalSocket.close(); terminalSocket = null; }
  terminalTaskId = null;
  hideTerminal();
  setRealtimeChannel("terminal", "idle"); terminalStatus(uiLabel("terminalNotConnected")); $("#terminal-cwd").textContent = uiLabel("terminalNotConnected");
}
