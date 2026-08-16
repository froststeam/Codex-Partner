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
  if (["userMessage", "browserMessage"].includes(payload.type)) return payload.text || (payload.content || []).map(item => item.text || "").join("\n");
  if (payload.type === "agentMessage") return payload.text || "";
  if (payload.type === "agentMessageStarted") return "";
  if (payload.type === "agent_delta") return payload.delta || "";
  if (payload.type === "reasoning") return payload.text || payload.summary || "";
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

function mergeEvents(events, messages) {
  const visible = (messages || []).filter(message => ["running", "steering", "steered", "sent"].includes(message.status));
  const deliveryCounts = new Map();
  for (const message of visible) {
    const body = String(message.body || "").trim();
    if (body) deliveryCounts.set(body, (deliveryCounts.get(body) || 0) + 1);
  }
  const merged = [];
  for (const event of events || []) {
    const payload = valueOf(event.payload);
    if (payload?.type === "userMessage") {
      const body = eventText(payload, {}).trim();
      if (deliveryCounts.has(body)) {
        const remaining = deliveryCounts.get(body) || 0;
        if (remaining <= 0) continue;
        deliveryCounts.set(body, remaining - 1);
      }
    }
    merged.push(event);
  }
  const nativeCounts = new Map();
  // Native thread history often omits client_message_id, so body order is the
  // stable fallback for suppressing a duplicated durable browser message.
  for (const event of merged) {
    const payload = valueOf(event.payload);
    if (payload?.type !== "userMessage") continue;
    const body = eventText(payload, {}).trim();
    if (body) nativeCounts.set(body, (nativeCounts.get(body) || 0) + 1);
  }
  for (const message of visible) {
    const body = String(message.body || "").trim();
    if (body && (nativeCounts.get(body) || 0) > 0) {
      nativeCounts.set(body, nativeCounts.get(body) - 1);
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
    .replace(/\[\[codex-file:([^\]]+)\]\]/g, (_, path) => { files.push(decodeURIComponent(path)); return ""; })
    .replace(/(?:Uploaded to workspace|已上传到工作区)\s*[:：]\s*([^\n\r，。]+?\.[a-z0-9]{1,12})(?=\s|[，。]|$)/gi, (_, path) => { files.push(path.trim()); return ""; })
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
  return body + [...new Set(files)].map(path => {
    const safePath = escapeHtml(path);
    if (/\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(path)) return `<div class="chat-file-attachment chat-image-attachment" data-chat-file="${safePath}"><span class="chat-file-preview" hidden></span></div>`;
    const name = escapeHtml(path.split("/").pop() || path);
    return `<div class="chat-file-attachment" data-chat-file="${safePath}"><span class="chat-file-icon">▤</span><span class="chat-file-copy"><strong>${name}</strong><small>${safePath}</small></span><span class="chat-file-preview" hidden></span></div>`;
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
    const itemId = payload?.item_id || "";
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
