/* Chat history normalization stays off the UI thread; output is sanitized HTML. */
const escapeHtml = (value = "") => String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

function valueOf(raw) {
  if (typeof raw !== "string") return raw;
  try { return JSON.parse(raw); } catch (_) { return raw; }
}

function timestamp(event) {
  const raw = event?.ts ?? event?.created_at;
  const numeric = Number(raw);
  if (Number.isFinite(numeric)) return numeric < 1e12 ? numeric * 1000 : numeric;
  const parsed = Date.parse(String(raw || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function eventText(raw, labels) {
  const payload = valueOf(raw);
  if (typeof payload === "string") return payload;
  if (!payload || typeof payload !== "object") return String(payload ?? "");
  if (["userMessage", "browserMessage"].includes(payload.type)) {
    const text = payload.text || (payload.content || []).map(item => item.text || "").join("\n");
    if (/^\s*<codex_internal_context\b[^>]*\bsource=["']goal["']/i.test(text)) return "";
    return text;
  }
  if (payload.type === "agentMessage") return payload.text || "";
  if (payload.type === "agentMessageStarted") return "";
  if (payload.type === "agent_delta") return payload.delta || "";
  if (payload.type === "reasoning") return payload.text || payload.summary || "";
  if (payload.type === "goal_updated") return "";
  if (payload.type === "fileChange") return payload.text || labels.fileChanged;
  if (payload.type === "contextCompaction") return labels.contextCompressed;
  if (payload.type === "externalTurnStarted") return labels.terminalTurnStarted;
  if (payload.type === "turn_completed") return labels.terminalTurnCompleted;
  if (payload.type === "turn_aborted") return labels.terminalTurnAborted;
  if (["slashCommand", "commandResult"].includes(payload.type)) return payload.text || "";
  if (payload.type === "codex") return payload.method ? `Codex · ${payload.method}` : labels.codexActivity;
  return payload.delta || payload.text || JSON.stringify(payload);
}

function isUser(event) {
  const payload = valueOf(event.payload);
  return ["userMessage", "browserMessage", "slashCommand"].includes(payload?.type) || event.stream === "user";
}

function isAssistant(event) {
  const payload = valueOf(event.payload);
  return ["agentMessage", "agent_delta", "commandResult"].includes(payload?.type) || event.stream === "assistant";
}

function userDedupeBody(text) {
  return String(text || "")
    .replace(/<image\b[^>]*>[\s\S]*?<\/image>/gi, "")
    .replace(/<audio\b[^>]*>[\s\S]*?<\/audio>/gi, "")
    .replace(/<video\b[^>]*>[\s\S]*?<\/video>/gi, "")
    .replace(/\[\[codex-input:[^\]]+\]\]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function mergeEvents(events, messages) {
  const visible = (messages || []).filter(message => ["running", "steering", "steered", "sent"].includes(message.status));
  const merged = [];
  const seenUserKeys = new Set();
  const seenUserBodies = new Map();
  for (const event of events || []) {
    const payload = valueOf(event.payload);
    if (payload?.type === "userMessage") {
      const body = userDedupeBody(eventText(payload, {}));
      if (body && (seenUserBodies.get(body) || 0) > 0) continue;
      const key = String(payload.client_message_id || body || payload.item_id || "").trim();
      if (key && seenUserKeys.has(key)) continue;
      if (key) seenUserKeys.add(key);
      if (body) seenUserBodies.set(body, (seenUserBodies.get(body) || 0) + 1);
    }
    merged.push(event);
  }
  for (const message of visible) {
    const id = String(message.id || "").trim();
    const body = userDedupeBody(message.body || "");
    if (id && seenUserKeys.has(id)) {
      continue;
    }
    if (body && (seenUserBodies.get(body) || 0) > 0) {
      seenUserBodies.set(body, (seenUserBodies.get(body) || 0) - 1);
      continue;
    }
    merged.push({
      id: `browser-${message.id}`,
      session_id: message.session_id || `browser-${message.id}`,
      ts: message.created_at || new Date().toISOString(),
      stream: "user",
      payload: { type: "browserMessage", text: message.body || "", status: message.status, error: message.error || "", client_message_id: message.id },
    });
  }
  return merged.map((event, index) => ({ event, index })).sort((a, b) => timestamp(a.event) - timestamp(b.event) || a.index - b.index).map(item => item.event);
}

function markdown(text) {
  const files = [];
  const clean = String(text || "")
    .replace(/\[\[codex-input:(localImage|localAudio|mention):([^\]]+)\]\]/g, (_, kind, path) => { files.push({ kind, path: decodeURIComponent(path) }); return ""; })
    .replace(/\[\[codex-file:([^\]]+)\]\]/g, (_, path) => { files.push({ kind: "legacy", path: decodeURIComponent(path) }); return ""; })
    .replace(/(?:Uploaded to workspace|已上传到工作区)\s*[:：]\s*([^\n\r，。]+?\.[a-z0-9]{1,12})(?=\s|[，。]|$)/gi, (_, path) => { files.push({ kind: "legacy", path: path.trim() }); return ""; })
    .replace(/\[(?:Attachments?|附件)\s*[:：]\s*[^\]]+\]/gi, "")
    .trim();
  const body = escapeHtml(clean)
    .replace(/```(?:[a-zA-Z0-9_+-]+)?\n?([\s\S]*?)```/g, '<pre class="code-block">$1</pre>')
    .replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/^###\s+(.+)$/gm, "<h4>$1</h4>")
    .replace(/^[-*]\s+(.+)$/gm, '<span class="markdown-bullet">$1</span>')
    .replace(/\n/g, "<br>");
  const uniqueFiles = [...new Map(files.map(file => [file.path, file])).values()];
  return body + uniqueFiles.map(file => {
    const path = file.path;
    const safePath = escapeHtml(path);
    const kind = file.kind === "legacy" ? (/\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(path) ? "localImage" : "mention") : file.kind;
    if (kind === "localImage") return `<div class="chat-file-attachment chat-image-attachment" data-chat-kind="${kind}" data-chat-file="${safePath}"><span class="chat-file-preview"></span></div>`;
    const name = escapeHtml(path.split("/").pop() || path);
    const mediaKind = kind === "localAudio" || /\.(mp3|wav|m4a|aac|flac|ogg|opus)$/i.test(path) ? "audio" : /\.(mp4|webm|mov|m4v|ogv)$/i.test(path) ? "video" : /\.pdf$/i.test(path) ? "pdf" : "file";
    const icon = { audio: "AUD", video: "VID", pdf: "PDF", file: "FILE" }[mediaKind];
    return `<div class="chat-file-attachment chat-${mediaKind}-attachment" data-chat-kind="${kind}" data-chat-media="${mediaKind}" data-chat-file="${safePath}"><span class="chat-file-icon">${icon}</span><span class="chat-file-copy"><strong>${name}</strong><small>${safePath}</small></span><span class="chat-file-preview" hidden></span></div>`;
  }).join("");
}

function buildBlocks(events, rawActivity, labels) {
  const blocks = []; const assistantItems = new Map(); const userItems = new Map(); let current = null;
  for (const event of events) {
    if (["metrics", "history"].includes(event.stream)) continue;
    const text = eventText(event.payload, labels).trim();
    if (!text) continue;
    const role = isUser(event) ? "user" : isAssistant(event) ? "assistant" : "activity";
    const payload = valueOf(event.payload);
    if (role === "activity") {
      if (!rawActivity && ["app-server", "stdout", "stderr"].includes(event.stream)) continue;
      let group = blocks[blocks.length - 1];
      if (!group || group.role !== "activities") { group = { role: "activities", items: [] }; blocks.push(group); }
      group.items.push(text); current = null; continue;
    }
    const commandBlock = ["slashCommand", "commandResult"].includes(payload?.type);
    const itemId = role === "user" ? (payload?.client_message_id || payload?.item_id || "") : (payload?.item_id || "");
    const itemMap = role === "assistant" ? assistantItems : userItems;
    if (itemId && itemMap.has(itemId)) {
      current = itemMap.get(itemId);
      if (payload.type === "agentMessage") { current.text = text; current.streaming = false; }
      else if (payload.type === "agent_delta") { current.text += text; current.streaming = true; }
      else current.text = text;
      continue;
    }
    if (!current || current.role !== role || role === "user" || commandBlock || current.commandBlock) {
      current = { role, text: "", session: event.session_id, commandBlock, itemId, delivery: payload?.type === "browserMessage" ? payload.status : "", error: payload?.error || "", streaming: payload?.type === "agent_delta", origin: event.stream === "rollout" ? "terminal" : "web", time: event.ts || event.created_at || "" };
      blocks.push(current); if (itemId) itemMap.set(itemId, current);
    }
    current.text += `${current.text ? "\n" : ""}${text}`;
  }
  for (const block of blocks) if (block.role !== "activities") block.html = markdown(block.text);
  return blocks;
}

self.onmessage = event => {
  const { requestId, events, messages, rawActivity, labels } = event.data;
  try {
    const merged = mergeEvents(events, messages);
    self.postMessage({ requestId, blocks: buildBlocks(merged, rawActivity, labels || {}) });
  } catch (error) {
    self.postMessage({ requestId, error: String(error?.message || error) });
  }
};
