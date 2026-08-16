// DOM event wiring and application bootstrap. Feature logic lives in focused modules.
document.addEventListener("click", async event => {
  const button = event.target.closest("button"); if (!button) return;
  try {
    if (button.id === "install-codex") {
      button.disabled = true; button.textContent = "安装中…";
      const result = await api("/codex/install", { method: "POST" });
      toast(`Codex 已安装：${result.codex_bin}`);
      return refresh(false);
    }
    if (button.id === "new-task" || button.id === "empty-new-task") return createQuickSession();
    if (button.id === "open-terminal") return openTerminal();
    if (button.id === "terminal-close") return closeTerminal();
    if (button.id === "terminal-reconnect") { if (!state.selectedId) return toast("请先选择一个会话"); const windowNode = $("#terminal-window"); windowNode.classList.add("open"); windowNode.setAttribute("aria-hidden", "false"); const preserveHistory = terminalTaskId === state.selectedId; if (terminalSocket) { terminalSocket.onclose = null; terminalSocket.close(); terminalSocket = null; } return connectTerminal(state.selectedId, preserveHistory); }
    if (button.id === "connection-status") return openPanel("server");
    if (button.id === "update-codex") {
      button.disabled = true; button.textContent = uiLabel("updatingCodex");
      const result = await api("/codex/update", { method: "POST" });
      toast(`Codex 已更新：${result.codex_bin || "latest"}`);
      return openPanel("server");
    }
    if (button.dataset.trashRestore) { await api(`/trash/${encodeURIComponent(button.dataset.trashRestore)}/restore`, { method: "POST" }); await refresh(false); await renderTrashPanel(); return toast("会话已恢复"); }
    if (button.dataset.trashDelete && await appConfirm(uiLabel("permanentDeleteConfirm"), { danger: true })) { await api(`/trash/${encodeURIComponent(button.dataset.trashDelete)}`, { method: "DELETE" }); await renderTrashPanel(); return toast("会话已永久删除"); }
    if (button.id === "sync-native") { await api("/native/sync", { method: "POST" }); await refresh(false); return toast("本机 Codex 已同步"); }
    if (button.dataset.composerAttachmentRemove !== undefined) {
      pendingAttachments.splice(Number(button.dataset.composerAttachmentRemove), 1);
      renderComposerAttachments(); renderGoalBar(); return;
    }
    if (button.dataset.sessionId) return selectSession(button.dataset.sessionId);
    if (button.dataset.queueClear !== undefined) {
      if (!state.selectedId || !await appConfirm(uiLabel("clearQueueConfirm"), { danger: true })) return;
      const result = await api(`/tasks/${state.selectedId}/messages`, { method: "DELETE" });
      for (const messageId of result.message_ids || []) removeTaskMessage(messageId);
      if (state.editingQueuedId && (result.message_ids || []).includes(state.editingQueuedId)) {
        state.editingQueuedId = null; $("#message-input").value = ""; $("#composer-context").textContent = "";
      }
      renderQueuedMessages(); renderGoalBar(); scheduleRenderChat(); return toast("排队消息已清空");
    }
    if (button.dataset.queueEdit) {
      const message = state.selectedMessages.find(item => item.id === button.dataset.queueEdit);
      if (!message) return;
      state.editingQueuedId = message.id;
      const input = $("#message-input"); input.value = message.body || ""; resizeComposerInput(input); $("#composer-context").textContent = "正在编辑排队消息"; renderQueuedMessages(); renderGoalBar(); input.focus(); return;
    }
    if (button.dataset.queueDispatch) {
      const message = state.selectedMessages.find(item => item.id === button.dataset.queueDispatch);
      if (!message) return;
      button.disabled = true; button.textContent = "…";
      try {
        const result = await api(`/tasks/${state.selectedId}/messages/${button.dataset.queueDispatch}/dispatch`, { method: "POST" });
        upsertTaskMessage(result); renderQueuedMessages(); scheduleRenderChat();
        toast(result.status === "steered" ? "已插入当前 Codex turn" : "已立即执行");
      } catch (error) { button.disabled = false; button.textContent = "▶"; toast(error.message); }
      return;
    }
    if (button.dataset.queueDelete) {
      await api(`/tasks/${state.selectedId}/messages/${button.dataset.queueDelete}`, { method: "DELETE" });
      removeTaskMessage(button.dataset.queueDelete);
      if (state.editingQueuedId === button.dataset.queueDelete) { state.editingQueuedId = null; $("#message-input").value = ""; $("#composer-context").textContent = ""; }
      renderQueuedMessages(); renderGoalBar(); scheduleRenderChat(); return toast("排队消息已删除");
    }
    if (button.dataset.filter) { state.filter = button.dataset.filter; $$(".filter").forEach(node => node.classList.toggle("active", node === button)); return renderSessionList(); }
    if (button.dataset.panel) return openPanel(button.dataset.panel);
    if (button.id === "close-panel") return closePanel();
    if (button.id === "close-drawer" || button.id === "cancel") return closeDrawer();
    if (button.id === "mobile-sessions") return $(".session-sidebar").classList.toggle("open");
    if (button.id === "close-inspector") { state.inspectorClosed = true; setInspectorOpen(false); return; }
    if (button.dataset.workspaceChange !== undefined) return openWorkspacePicker();
    if (button.dataset.workspaceUpload !== undefined) {
      const input = $("#workspace-upload-input"); input.dataset.path = state.workspacePath; input.click(); return;
    }
    if (button.dataset.workspaceDownload !== undefined) return downloadWorkspaceFile(button.dataset.workspaceDownload);
    if (button.dataset.workspaceEdit !== undefined) return openWorkspaceFileEditor(button.dataset.workspaceEdit);
    if (button.dataset.copyThread) { await copyText(button.dataset.copyThread); return toast("Thread ID 已复制"); }
    if (button.dataset.sshUse !== undefined) { state.activeSSHHost = button.dataset.sshUse; if (state.activeSSHHost) localStorage.setItem("codex-dashboard-ssh-host", state.activeSSHHost); else localStorage.removeItem("codex-dashboard-ssh-host"); drawSSHPanel(); return toast(state.activeSSHHost ? `新会话将使用 ${state.activeSSHHost}` : "新会话将使用本机"); }
    if (button.dataset.sshConnect) return connectSSHHost(button.dataset.sshConnect);
    if (button.dataset.sshInstall) { button.disabled = true; button.textContent = "安装中…"; const row = await api(`/ssh/install-codex?host=${encodeURIComponent(button.dataset.sshInstall)}`, { method: "POST" }); updateSSHHost(row); drawSSHPanel(); return toast("Codex 已安装"); }
    if (button.dataset.sshDisconnect) { await api(`/ssh/disconnect?host=${encodeURIComponent(button.dataset.sshDisconnect)}`, { method: "POST" }); updateSSHHost({ ...state.sshHosts.find(item => item.alias === button.dataset.sshDisconnect), connected: false, status: "disconnected" }); if (state.activeSSHHost === button.dataset.sshDisconnect) { state.activeSSHHost = ""; localStorage.removeItem("codex-dashboard-ssh-host"); } drawSSHPanel(); return toast("SSH 已断开"); }
    if (button.dataset.pickerBrowse !== undefined) return loadWorkspacePicker(button.dataset.pickerBrowse);
    if (button.id === "workspace-picker-select") return selectWorkspaceDirectory();
    if (button.dataset.browse !== undefined) return loadWorkspace(button.dataset.browse);
    if (button.id === "conversation-rename") return openRename();
    if (button.id === "conversation-fork") return runOperation("fork");
    if (button.id === "conversation-more") {
      const inspector = $("#inspector");
      const shouldOpen = inspector.classList.contains("closed") || !inspector.classList.contains("open");
      state.inspectorClosed = !shouldOpen; setInspectorOpen(shouldOpen); return;
    }
    if (button.dataset.threadAction) return runOperation(button.dataset.threadAction);
    if (button.id === "new-memory") return memoryForm();
    if (button.dataset.memoryView) return memoryView(button.dataset.memoryView);
    if (button.dataset.memoryEdit) return memoryForm(button.dataset.memoryEdit);
    if (button.dataset.generatedMemory) return generatedMemoryView(button.dataset.generatedMemory);
    if (button.dataset.memoryDelete && await appConfirm(uiLabel("deleteMemoryConfirm"), { danger: true })) { await api(`/memories/${button.dataset.memoryDelete.split("/").map(encodeURIComponent).join("/")}`, { method: "DELETE" }); await refresh(false); return openPanel("memories"); }
    if (button.id === "reset-generated-memories" && await appConfirm(uiLabel("rebuildMemoryConfirm"), { danger: true })) { await api("/memories/reset", { method: "POST", body: JSON.stringify({ confirm: true }) }); return toast("生成记忆已重置"); }
    if (button.id === "new-provider") return providerForm();
    if (button.dataset.providerUse) {
      if (!state.selectedId || !state.selectedTask) return toast("请先选择一个会话");
      if (button.disabled) return;
      button.disabled = true;
      const provider = state.providers.find(item => item.id === button.dataset.providerUse);
      try {
        const activeTurn = ["running", "retrying", "queued"].includes(state.selectedTask.status);
        const task = await api(`/tasks/${state.selectedId}`, { method: "PATCH", body: JSON.stringify({ provider_id: button.dataset.providerUse, model: "" }) });
        state.selectedTask = { ...state.selectedTask, ...task };
        mergeTask(task); renderConversation(); refreshOpenPanel();
        toast(activeTurn ? `已选择 ${provider?.name || "Provider"}，当前 turn 完成后生效` : `当前会话已切换到 ${provider?.name || "Provider"}`);
      } catch (error) { toast(`Provider 切换失败：${error.message}`); }
      finally { button.disabled = false; }
      return;
    }
    if (button.dataset.providerEdit) return providerForm(state.providers.find(item => item.id === button.dataset.providerEdit));
    if (button.dataset.providerCheck) return checkOneProvider(button.dataset.providerCheck);
    if (button.dataset.providerDelete && await appConfirm(uiLabel("deleteProviderConfirm"), { danger: true })) { await api(`/providers/${button.dataset.providerDelete}`, { method: "DELETE" }); await refresh(false); return openPanel("server"); }
    if (button.id === "sync-providers") { await api("/providers/sync-native", { method: "POST" }); await refresh(false); return openPanel("server"); }
    if (button.id === "check-providers") return checkAllProviders();
    if (button.id === "new-skill") return skillForm();
    if (button.dataset.skillEdit) return skillForm(state.skills.find(item => item.id === button.dataset.skillEdit));
    if (button.dataset.skillDelete && await appConfirm(uiLabel("deleteSkillConfirm"), { danger: true })) { await api(`/skills/${button.dataset.skillDelete}`, { method: "DELETE" }); await refresh(false); return openPanel("skills"); }
  } catch (error) { toast(error.message); }
});
$("#language-select").onchange = event => {
  applyLanguage(event.target.value);
  renderSessionList(); renderSidebarStats();
  if (state.selectedTask) renderConversation();
  renderConnectionStatus();
  refreshOpenPanel();
};
$("#terminal-window").addEventListener("click", event => { if (event.target.id === "terminal-window") closeTerminal(); });
$("#panel").addEventListener("click", event => { if (event.target === event.currentTarget) closePanel(); });
$("#drawer").addEventListener("click", event => { if (event.target === event.currentTarget) closeDrawer(); });
async function openRename() { const name = await appPrompt({ title: uiLabel("renameSessionTitle"), label: uiLabel("sessionName"), value: state.selectedTask?.name || "", confirmLabel: uiLabel("save"), required: true }); if (!name?.trim()) return; await api(`/tasks/${state.selectedId}/operation`, { method: "POST", body: JSON.stringify({ operation: "rename", args: [name.trim()] }) }); await refresh(); toast("会话已重命名"); }
function switchSession(step) {
  const tasks = sortTasks(state.tasks);
  if (!tasks.length) return toast("还没有可切换的会话");
  const current = tasks.findIndex(task => task.id === state.selectedId);
  const nextIndex = current < 0 ? (step > 0 ? 0 : tasks.length - 1) : (current + step + tasks.length) % tasks.length;
  const next = tasks[nextIndex];
  if (next?.id && next.id !== state.selectedId) selectSession(next.id);
}
function switchToTopSession() {
  const top = sortTasks(state.tasks)[0];
  if (!top) return toast("还没有可切换的会话");
  if (top.id === state.selectedId) { renderSessionList(); scrollSessionIntoView(top.id); return; }
  selectSession(top.id);
}
function toggleTerminalShortcut() {
  if (!state.selectedId) return toast("请先选择一个会话");
  if ($("#terminal-window")?.classList.contains("open")) closeTerminal();
  else openTerminal();
}
async function runOperation(operation) {
  if (!state.selectedId) return;
  if (["archive", "delete"].includes(operation)) {
    const message = uiLabel(operation === "delete" ? "trashSessionConfirm" : "archiveSessionConfirm");
    if (!await appConfirm(message, { danger: operation === "delete" })) return;
  }
  const result = await api(`/tasks/${state.selectedId}/operation`, { method: "POST", body: JSON.stringify({ operation, args: [] }) });
  if (result.memory_mode && state.selectedTask) {
    // Reflect the acknowledged mode immediately while the authoritative
    // refresh also updates the other browser clients.
    state.selectedTask = { ...state.selectedTask, memory_mode: result.memory_mode };
    mergeTask(state.selectedTask);
    renderConversation();
  }
  if (result.deleted || result.trashed) {
    if (terminalTaskId === state.selectedId) destroyTerminal();
    state.selectedId = null;
    showEmptyConversation();
    await refresh(false);
  } else {
    // A command can change fields that are only present in the full task
    // payload (memory mode, archive state, renamed title). Refresh the
    // selected conversation as well as the sidebar, otherwise the inspector
    // keeps rendering the previous snapshot.
    await refresh(true);
    if (result.task?.id && result.task.id !== state.selectedId) await selectSession(result.task.id);
  }
  const message = operation === "fork" ? "会话已复制" : result.trashed ? "会话已移到回收站" : operation === "unarchive" ? "会话已取消归档" : operation === "memory-enable" ? "记忆已打开" : operation === "memory-disable" ? "记忆已关闭" : "操作已完成";
  toast(message);
}

async function submitMessage(mode = "codex") {
  if (!state.selectedId) return;
  const taskId = state.selectedId;
  const input = $("#message-input");
  let message = input.value.trim();
  const activeTurn = ["running", "retrying", "queued"].includes(state.selectedTask?.status);
  if (!message && !pendingAttachments.length) {
    if (mode === "codex" && activeTurn && !state.editingQueuedId) {
      await changeSelectedRun("stop");
    }
    return;
  }
  if (state.editingQueuedId) {
    const messageId = state.editingQueuedId;
    try {
      const updated = await api(`/tasks/${taskId}/messages/${messageId}`, { method: "PATCH", body: JSON.stringify({ message }) });
      upsertTaskMessage(updated); state.editingQueuedId = null; input.value = ""; input.style.height = ""; $("#composer-context").textContent = ""; renderQueuedMessages(); renderGoalBar(); scheduleRenderChat(); input.focus(); toast("排队消息已更新");
    } catch (error) { toast(error.message); }
    return;
  }
  if (mode === "codex" && message.startsWith("/") && !pendingAttachments.length) {
    input.value = ""; input.style.height = ""; $("#command-palette").hidden = true;
    try { await submitSlashCommand(message); } catch (error) { input.value = message; renderCommandPalette(); toast(error.message); }
    return;
  }
  if (pendingAttachments.length) {
    message += pendingAttachments.map((file, index) => {
      const prefix = message || index ? "\n\n" : "";
      if (file.binary) return `${prefix}[${uiLabel("attachments")}：${file.name}]\n${uiLabel("binaryAttachment", { path: file.workspacePath || file.name })}\n[[codex-file:${encodeURIComponent(file.workspacePath || file.name)}]]`;
      return `${prefix}[${uiLabel("attachments")}：${file.name}]\n\n\`\`\`\n${file.content}\n\`\`\``;
    }).join("");
  }
  // Enter never interrupts an active turn. The server owns the durable FIFO
  // and dispatches this message as a new turn once the current owner exits.
  const clientMessageId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  const delivery = mode === "queue" || activeTurn ? "queue" : "auto";
  const optimisticStatus = delivery === "queue" ? "queued" : "sending";
  const optimistic = { id: clientMessageId, body: message, status: optimisticStatus, created_at: new Date().toISOString() };
  upsertTaskMessage(optimistic); state.historyIndex = -1; input.value = ""; resizeComposerInput(input); pendingAttachments = []; $("#composer-context").textContent = ""; $("#attach-file").value = ""; $("#command-palette").hidden = true; renderGoalBar(); scheduleRenderChat(); input.focus();
  try {
    const delivered = await api(`/tasks/${taskId}/messages`, { method: "POST", body: JSON.stringify({ message, client_message_id: clientMessageId, delivery }) });
    if (state.selectedId === taskId) { upsertTaskMessage(delivered); renderQueuedMessages(); refreshComposerHistory(); renderGoalBar(); scheduleRenderChat(); }
    if (activeTurn && delivered.status === "queued") toast(uiLabel("queuedAfterTurn"));
  } catch (error) {
    if (state.selectedId === taskId) { upsertTaskMessage({ id: clientMessageId, status: "failed", error: error.message }); renderGoalBar(); scheduleRenderChat(); }
    toast(error.message);
  }
}
$("#message-form").onsubmit = async event => { event.preventDefault(); await submitMessage("codex"); };

$("#goal-edit").onclick = () => {
  const task = state.selectedTask; if (!task) return;
  $("#goal-input").value = task.goal || "";
  $("#goal-clear").hidden = !task.goal;
  $("#goal-text").hidden = true; $("#goal-edit-form").hidden = false;
  $("#goal-input").focus();
};
$("#goal-edit-cancel").onclick = () => { $("#goal-edit-form").hidden = true; $("#goal-text").hidden = false; };
$("#goal-clear").onclick = async () => {
  if (!state.selectedId || !state.selectedTask?.goal || !await appConfirm(uiLabel("clearGoalConfirm"), { danger: true })) return;
  try {
    const task = await api(`/tasks/${state.selectedId}/goal`, { method: "PUT", body: JSON.stringify({ objective: "" }) });
    state.selectedTask = { ...state.selectedTask, ...task }; mergeTask(task); $("#goal-edit-form").hidden = true; $("#goal-text").hidden = false; renderConversation(); toast("Goal 已清空");
  } catch (error) { toast(error.message); }
};
$("#goal-edit-form").onsubmit = async event => {
  event.preventDefault(); if (!state.selectedId) return;
  try {
    const task = await api(`/tasks/${state.selectedId}/goal`, { method: "PUT", body: JSON.stringify({ objective: $("#goal-input").value.trim() }) });
    state.selectedTask = { ...state.selectedTask, ...task }; mergeTask(task); $("#goal-edit-form").hidden = true; $("#goal-text").hidden = false; renderConversation(); toast(task.goal ? "Goal 已更新" : "Goal 已清除");
  } catch (error) { toast(error.message); }
};
async function changeSelectedRun(action, announce = true) {
  if (!state.selectedId || !state.selectedTask) return null;
  try {
    const task = await api(`/tasks/${state.selectedId}/${action}`, { method: "POST" });
    state.selectedTask = { ...state.selectedTask, ...task }; mergeTask(task); renderConversation();
    if (announce) toast(action === "stop" ? "Codex 已停止，输入内容仍保留" : "Codex 已继续运行");
    return task;
  } catch (error) {
    if (announce) toast(error.message);
    return null;
  }
}
async function toggleGoalRun() {
  const task = state.selectedTask;
  const goal = String(task?.goal || "").trim();
  if (!task || !state.selectedId || !goal) return toast("请先设置 Goal");
  const goalRunning = ["running", "retrying", "queued"].includes(task.status) && task.goal_status !== "paused";
  if (goalRunning) {
    const stopped = await changeSelectedRun("stop", false);
    if (!stopped || ["running", "retrying", "queued"].includes(stopped.status)) return toast("当前 Codex 未能暂停");
  }
  try {
    const updated = await api(`/tasks/${state.selectedId}/goal`, { method: "PUT", body: JSON.stringify({ status: goalRunning ? "paused" : "active" }) });
    state.selectedTask = { ...state.selectedTask, ...updated }; mergeTask(updated); renderConversation();
    if (!goalRunning) {
      const resumed = await changeSelectedRun("resume", false);
      if (!resumed) return toast("Goal 已继续，但恢复 Codex 失败");
      toast("Goal 已继续，正在 resume");
    } else {
      toast("Goal 已暂停");
    }
  } catch (error) { toast(error.message); }
}
$("#goal-run-toggle").onclick = toggleGoalRun;
$("#goal-retry-toggle").onclick = async () => {
  if (!state.selectedId || !state.selectedTask) return;
  if (!String(state.selectedTask.goal || "").trim()) return toast("请先设置 Goal");
  try {
    const task = await api(`/tasks/${state.selectedId}`, { method: "PATCH", body: JSON.stringify({ retry_forever: !state.selectedTask.retry_forever }) });
    state.selectedTask = { ...state.selectedTask, ...task }; mergeTask(task); renderConversation(); toast(task.retry_forever ? "Goal 自动续跑已开启" : "Goal 自动续跑已关闭");
  } catch (error) { toast(error.message); }
};
function resizeComposerInput(input = $("#message-input")) {
  if (!input) return;
  input.style.height = "30px";
  const height = Math.min(Math.max(input.scrollHeight, 30), 120);
  input.style.height = `${height}px`;
  input.style.overflowY = input.scrollHeight > 120 ? "auto" : "hidden";
}
function renderComposerAttachments() {
  const context = $("#composer-context");
  if (!context) return;
  context.classList.toggle("attachments", Boolean(pendingAttachments.length));
  if (!pendingAttachments.length) { context.textContent = ""; return; }
  context.innerHTML = pendingAttachments.map((file, index) => `<span class="composer-attachment" title="${esc(file.name)}"><span aria-hidden="true">▤</span><strong>${esc(file.name)}</strong><small>${formatBytes(file.size)}</small><button type="button" data-composer-attachment-remove="${index}" title="${uiLabel("delete")}">×</button></span>`).join("");
}
function attachmentUploadName(name) {
  const value = String(name || "attachment").replace(/[^\w.()\-\u4e00-\u9fff ]/g, "_").trim() || "attachment";
  const dot = value.lastIndexOf(".");
  const stem = dot > 0 ? value.slice(0, dot) : value;
  const ext = dot > 0 ? value.slice(dot) : "";
  return `${stem}-${Date.now().toString(36)}${ext}`;
}
async function attachComposerFiles(fileList) {
  const files = [...(fileList || [])].filter(Boolean);
  if (!files.length) return;
  const added = [];
  let inlineChars = pendingAttachments.reduce((total, item) => total + (item.binary ? 0 : String(item.content || "").length), 0);
  for (const file of files) {
    const name = file.name || "attachment";
    try {
      const sample = new Uint8Array(await file.slice(0, Math.min(file.size || 0, 8192)).arrayBuffer());
      const declaredText = String(file.type || "").startsWith("text/") || /\.(txt|md|markdown|json|ya?ml|xml|csv|log|py|js|ts|tsx?|jsx|css|html?|sh|toml|ini|cfg|conf)$/i.test(name);
      const magic = String.fromCharCode(...sample.slice(0, 8));
      const binarySignature = magic.startsWith("\x89PNG\r\n\x1a\n") || magic.startsWith("\xff\xd8\xff") || magic.startsWith("GIF8") || magic.startsWith("%PDF") || magic.startsWith("PK\x03\x04") || magic.startsWith("\x7fELF");
      let text = "";
      let textFile = declaredText && !binarySignature;
      if (textFile) {
        try { text = await file.text(); new TextDecoder("utf-8", { fatal: true }).decode(sample); }
        catch (_) { textFile = false; }
      } else {
        textFile = !binarySignature && !sample.includes(0) && (() => { try { new TextDecoder("utf-8", { fatal: true }).decode(sample); return true; } catch (_) { return false; } })();
        if (textFile) text = await file.text();
      }
      if (textFile && inlineChars + text.length <= 12000) {
        added.push({ name, size: file.size || 0, content: text, binary: false });
        inlineChars += text.length;
        continue;
      }
      if (!state.selectedId) throw new Error("请先选择一个会话");
      // Pasted/copied files get a unique name, so an attachment never opens
      // the workspace overwrite confirmation dialog unexpectedly.
      const uploadName = attachmentUploadName(name);
      const uploadFile = new File([file], uploadName, { type: file.type || "application/octet-stream", lastModified: file.lastModified });
      const result = await uploadWorkspaceFile(state.selectedId, state.workspacePath || "", uploadFile);
      if (result?.skipped) continue;
      const workspacePath = result?.entry?.path || `${state.workspacePath ? `${state.workspacePath}/` : ""}${name}`;
      added.push({ name, size: file.size || 0, content: "", binary: true, workspacePath });
    } catch (error) {
      toast(error?.message ? `${uiLabel("fileUploadFailed", { name })}：${error.message}` : uiLabel("fileReadFailed", { name }));
    }
  }
  if (!added.length) return;
  pendingAttachments.push(...added);
  renderComposerAttachments();
  renderGoalBar();
  toast(uiLabel("filesReady", { count: added.length }));
  $("#message-input").focus();
}
$("#message-input").addEventListener("input", event => { resizeComposerInput(event.target); state.commandIndex = 0; state.historyIndex = -1; renderCommandPalette(); renderGoalBar(); });
$("#message-input").addEventListener("keydown", async event => {
  if (event.isComposing || event.keyCode === 229) return;
  const palette = $("#command-palette");
  if (!palette.hidden) {
    const matches = commandMatches(event.target.value);
    if (event.key === "ArrowDown") { event.preventDefault(); state.commandIndex = (state.commandIndex + 1) % matches.length; renderCommandPalette(); return; }
    if (event.key === "ArrowUp") { event.preventDefault(); state.commandIndex = (state.commandIndex - 1 + matches.length) % matches.length; renderCommandPalette(); return; }
    if (event.key === "Tab") { event.preventDefault(); completeCommand(); return; }
    if (event.key === "Escape") { event.preventDefault(); palette.hidden = true; return; }
    if (event.key === "Enter" && !event.shiftKey && matches.length && !event.target.value.trim().includes(" ")) {
      const command = selectedCommand(); const value = event.target.value.trim().slice(1).toLowerCase();
      const exact = command && (value === command.name || (command.aliases || []).includes(value));
      if (!exact) { event.preventDefault(); completeCommand(command); return; }
    }
  }
  if (event.key === "Escape" && state.editingQueuedId) {
    event.preventDefault(); event.stopPropagation(); state.editingQueuedId = null; event.target.value = ""; resizeComposerInput(event.target); $("#composer-context").textContent = ""; renderQueuedMessages(); renderGoalBar(); return;
  }
  if (event.key === "Enter" && event.shiftKey) { event.preventDefault(); return; }
  if (event.key === "Escape" && ["running", "retrying", "queued"].includes(state.selectedTask?.status)) {
    event.preventDefault(); event.stopPropagation(); await changeSelectedRun("stop"); return;
  }
  if (event.key === "ArrowUp" && !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey && (!event.target.value || state.historyIndex >= 0)) {
    if (!state.composerHistory.length) return;
    event.preventDefault(); state.historyIndex = state.historyIndex < 0 ? state.composerHistory.length - 1 : Math.max(0, state.historyIndex - 1); event.target.value = state.composerHistory[state.historyIndex]; resizeComposerInput(event.target); return;
  }
  if (event.key === "ArrowDown" && !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey && state.historyIndex >= 0) {
    event.preventDefault(); state.historyIndex += 1; if (state.historyIndex >= state.composerHistory.length) { state.historyIndex = -1; event.target.value = ""; } else event.target.value = state.composerHistory[state.historyIndex]; resizeComposerInput(event.target); return;
  }
  if (event.key === "Enter" && event.altKey && !event.ctrlKey) { event.preventDefault(); await submitMessage("queue"); return; }
  if (event.key === "Enter" && !event.shiftKey && !event.altKey && !event.ctrlKey && !event.metaKey) { event.preventDefault(); $("#message-form").requestSubmit(); }
});
$("#session-search").addEventListener("input", event => { state.query = event.target.value.trim(); renderSessionList(); });
$("#attach-button").onclick = () => $("#attach-file").click();
$("#theme-toggle").onclick = toggleTheme;
$("#attach-file").multiple = true;
$("#attach-file").onchange = async event => { await attachComposerFiles(event.target.files); event.target.value = ""; };
document.addEventListener("click", event => {
  if (event.target.closest(".message.user .message-avatar")) $("#user-avatar-file")?.click();
});
$("#user-avatar-file").onchange = event => {
  const file = event.target.files?.[0];
  if (!file) return;
  if (!String(file.type || "").startsWith("image/")) { toast("请选择图片文件"); event.target.value = ""; return; }
  const reader = new FileReader();
  reader.onload = () => {
    state.userAvatar = String(reader.result || "");
    try { localStorage.setItem("codex-dashboard-user-avatar", state.userAvatar); } catch (_) { toast("图片过大，无法保存头像"); return; }
    renderChat();
    toast("头像已更新");
  };
  reader.onerror = () => toast("头像读取失败");
  reader.readAsDataURL(file);
  event.target.value = "";
};
$("#message-input").addEventListener("paste", event => {
  const files = [...(event.clipboardData?.files || [])];
  if (!files.length) return;
  event.preventDefault();
  attachComposerFiles(files);
});
const composer = $("#message-form");
for (const eventName of ["dragenter", "dragover"]) {
  composer.addEventListener(eventName, event => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault(); event.stopPropagation(); composer.classList.add("dragging");
  });
}
composer.addEventListener("dragleave", event => {
  if (event.relatedTarget && composer.contains(event.relatedTarget)) return;
  composer.classList.remove("dragging");
});
composer.addEventListener("drop", event => {
  if (!event.dataTransfer?.files.length) return;
  event.preventDefault(); event.stopPropagation(); composer.classList.remove("dragging");
  attachComposerFiles(event.dataTransfer.files);
});
$("#workspace-upload-input").onchange = event => uploadWorkspaceFiles(event.target.files || [], event.target.dataset.path || "");
document.addEventListener("dragover", event => {
  const section = event.target.closest("#workspace-section[data-entry-kind=\"directory\"]");
  if (!section || !event.dataTransfer?.types.includes("Files")) return;
  event.preventDefault(); section.classList.add("dragging");
});
document.addEventListener("dragleave", event => { event.target.closest("#workspace-section")?.classList.remove("dragging"); });
document.addEventListener("drop", event => {
  const section = event.target.closest("#workspace-section[data-entry-kind=\"directory\"]");
  if (!section || !event.dataTransfer?.files.length) return;
  event.preventDefault(); section.classList.remove("dragging");
  uploadWorkspaceFiles(event.dataTransfer.files, section.dataset.path || "");
});
$("#permission-toggle").onclick = async () => { if (!state.selectedTask) return; try { const task = await api(`/tasks/${state.selectedId}`, { method: "PATCH", body: JSON.stringify({ yolo: !state.selectedTask.yolo }) }); state.selectedTask = { ...state.selectedTask, ...task }; renderConversation(); toast(task.yolo ? "YOLO 已开启" : "已切换为受控模式"); } catch (error) { toast(error.message); } };
$("#command-button").onclick = () => { const input = $("#message-input"); if (!input.value.startsWith("/")) input.value = "/"; input.focus(); state.commandIndex = 0; renderCommandPalette(); };
document.addEventListener("click", event => { const option = event.target.closest("[data-command-index]"); if (!option) return; const command = commandMatches($("#message-input").value)[Number(option.dataset.commandIndex)]; completeCommand(command); });
// Capture the physical backquote key before xterm can turn it into terminal input.
document.addEventListener("keydown", event => {
  if (event.code !== "Backquote" || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || event.repeat) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  toggleTerminalShortcut();
}, true);
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") { event.preventDefault(); $("#message-input").focus(); return; }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n" && !event.shiftKey) { event.preventDefault(); createQuickSession(); }
  const target = event.target;
  const editing = target instanceof HTMLElement && target.closest("input, textarea, select, [contenteditable=\"true\"]");
  if (event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey && !editing) {
    if (event.code === "KeyN") { event.preventDefault(); switchSession(1); return; }
    if (event.code === "KeyP") { event.preventDefault(); switchSession(-1); return; }
    if (event.code === "KeyC") { event.preventDefault(); switchToTopSession(); return; }
  }
  if (event.key === "Escape" && ["running", "retrying", "queued"].includes(state.selectedTask?.status) && !$("#panel").classList.contains("open") && !$("#drawer").classList.contains("open") && !$("#terminal-window").classList.contains("open")) { event.preventDefault(); changeSelectedRun("stop"); }
});
const mascotDanceSequence = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight"];
let mascotDanceIndex = 0;
document.addEventListener("keydown", event => {
  const target = event.target;
  const editing = target instanceof HTMLElement && target.closest("input, textarea, select, [contenteditable=\"true\"], .ace_editor");
  const terminalOpen = $("#terminal-window")?.classList.contains("open");
  if (editing || terminalOpen || event.repeat || event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) { mascotDanceIndex = 0; return; }
  if (!mascotDanceSequence.includes(event.key)) { mascotDanceIndex = 0; return; }
  mascotDanceIndex = event.key === mascotDanceSequence[mascotDanceIndex] ? mascotDanceIndex + 1 : (event.key === mascotDanceSequence[0] ? 1 : 0);
  if (mascotDanceIndex !== mascotDanceSequence.length) return;
  mascotDanceIndex = 0;
  triggerMascotDance();
});
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { connectOverviewSocket(); if (state.selectedId && !socketPaused && (!socket || socket.readyState === WebSocket.CLOSED)) connectSocket(state.selectedId, true); if (terminalTaskId && terminalShouldReconnect && !terminalSocket) connectTerminal(terminalTaskId, true); reconcileSelectedTaskStatus(); } });
window.addEventListener("offline", renderConnectionStatus);
window.addEventListener("online", () => {
  setRealtimeChannel("overview", "reconnecting");
  if (state.selectedId && !socketPaused) setRealtimeChannel("task", "reconnecting");
  if (terminalTaskId && terminalShouldReconnect) setRealtimeChannel("terminal", "reconnecting");
  if (!overviewSocket || overviewSocket.readyState === WebSocket.CLOSED) connectOverviewSocket();
  if (state.selectedId && !socketPaused && (!socket || socket.readyState === WebSocket.CLOSED)) connectSocket(state.selectedId, true);
  if (terminalTaskId && terminalShouldReconnect && !terminalSocket) connectTerminal(terminalTaskId, true);
  heartbeatRealtime();
});
setTheme(localStorage.getItem("codex-dashboard-theme") || "dark");
setInterval(updateTurnElapsed, 1000);
setInterval(reconcileSelectedTaskStatus, 5000);
setInterval(heartbeatRealtime, 10_000);
renderConnectionStatus();
showEmptyConversation(); refresh(false);
