/* Chat history normalization stays off the UI thread; output is sanitized HTML. */
const escapeHtml = (value = "") => String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const explorationCache = new Map();

let chatMarkdownRenderer = null;
if (typeof importScripts === "function") {
  try {
    importScripts(
      "/vendor/markdown-it/markdown-it.min.js",
      "/vendor/katex/katex.min.js",
      "/vendor/markdown-it-texmath/texmath.js",
    );
    const markdownFactory = typeof markdownit === "function" ? markdownit : self.markdownit;
    const mathEngine = typeof katex === "object" ? katex : self.katex;
    const mathPlugin = typeof texmath === "function" ? texmath : self.texmath;
    if (typeof markdownFactory === "function") {
      chatMarkdownRenderer = markdownFactory({ html: false, breaks: true, linkify: true, typographer: false });
      const defaultLinkOpen = chatMarkdownRenderer.renderer.rules.link_open || ((tokens, index, options, env, renderer) => renderer.renderToken(tokens, index, options));
      chatMarkdownRenderer.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
        const href = String(tokens[index].attrGet("href") || "");
        if (href.startsWith("/") && !href.startsWith("//")) {
          tokens[index].attrSet("href", "#");
          tokens[index].attrSet("data-chat-file", href);
          tokens[index].attrSet("data-chat-kind", "mention");
        }
        tokens[index].attrSet("target", "_blank");
        tokens[index].attrSet("rel", "noopener noreferrer");
        return defaultLinkOpen(tokens, index, options, env, renderer);
      };
      const defaultImage = chatMarkdownRenderer.renderer.rules.image;
      chatMarkdownRenderer.renderer.rules.image = (tokens, index, options, env, renderer) => {
        tokens[index].attrSet("loading", "lazy");
        tokens[index].attrSet("referrerpolicy", "no-referrer");
        return defaultImage(tokens, index, options, env, renderer);
      };
      chatMarkdownRenderer.renderer.rules.table_open = () => '<div class="markdown-table-wrap"><table>';
      chatMarkdownRenderer.renderer.rules.table_close = () => "</table></div>";
      if (typeof mathPlugin === "function" && mathEngine) {
        chatMarkdownRenderer.use(mathPlugin, {
          engine: mathEngine,
          delimiters: ["dollars", "brackets"],
          katexOptions: { throwOnError: false, strict: "ignore", trust: false, output: "htmlAndMathml" },
        });
      }
    }
  } catch (_) {
    chatMarkdownRenderer = null;
  }
}

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

function stripCodexHiddenContext(text) {
  return String(text || "")
    .replace(/<(environment_context|codex_internal_context|skills_instructions|plugins_instructions|system_reminder|memory_context|turn_context|oai-mem-citation)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "")
    .replace(/<permissions(?:\s+instructions)?\b[^>]*>[\s\S]*?<\/permissions(?:\s+instructions)?\s*>/gi, "")
    .replace(/<(environment_context|codex_internal_context|skills_instructions|plugins_instructions|system_reminder|memory_context|turn_context|oai-mem-citation)\b[^>]*\/?\s*>/gi, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function eventText(raw, labels) {
  const payload = valueOf(raw);
  if (typeof payload === "string") return payload;
  if (!payload || typeof payload !== "object") return String(payload ?? "");
  if (["userMessage", "browserMessage"].includes(payload.type)) {
    const text = payload.text || (payload.content || []).map(item => item.text || "").join("\n");
    return stripCodexHiddenContext(text);
  }
  if (payload.type === "agentMessage") return stripCodexHiddenContext(payload.text || "");
  if (payload.type === "agentMessageStarted") return "";
  if (payload.type === "agent_delta") return payload.delta || "";
  if (payload.type === "reasoning") {
    const value = payload.text || payload.summary || payload.content || "";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(item => typeof item === "string" ? item : (item?.text || item?.summary || "")).filter(Boolean).join("\n");
    return value?.text || value?.summary || "";
  }
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

function isEmptyReasoningEnvelope(text) {
  const value = String(text || "").trim();
  if (!value.startsWith("{") || !value.endsWith("}")) return false;
  try {
    const parsed = JSON.parse(value);
    if (String(parsed?.type || "").toLowerCase() !== "reasoning") return false;
    const empty = item => item == null || item === "" || (Array.isArray(item) && item.length === 0);
    return empty(parsed.summary) && empty(parsed.content) && !String(parsed.text || "").trim();
  } catch (_) { return false; }
}

function isUser(event) {
  const payload = valueOf(event.payload);
  return ["userMessage", "browserMessage", "slashCommand"].includes(payload?.type) || event.stream === "user";
}

function isAssistant(event) {
  const payload = valueOf(event.payload);
  return ["agentMessage", "agent_delta", "commandResult"].includes(payload?.type) || event.stream === "assistant";
}

function activityItem(raw, labels, event) {
  const payload = valueOf(raw);
  const native = payload?.item && typeof payload.item === "object" ? payload.item : {};
  const type = String(payload?.type || "activity").toLowerCase();
  const normalizedType = type.replace(/[^a-z0-9]/g, "");
  const status = String(payload?.status || "").toLowerCase();
  const detail = eventText(payload, labels).trim();
  let label = labels.activityWorking || "Working";
  if (type.includes("reason") || type === "plan") label = labels.activityPlanning || label;
  else if (type.includes("command")) label = status === "completed" ? (labels.activityCommandDone || label) : (labels.activityCommand || label);
  else if (type.includes("file")) label = status === "completed" ? (labels.activityFileDone || label) : (labels.activityFile || label);
  else if (type.includes("search")) label = labels.activitySearch || label;
  else if (type.includes("tool") || type.includes("mcp")) label = status === "completed" ? (labels.activityToolDone || label) : (labels.activityTool || label);
  else if (type === "contextcompaction") label = labels.contextCompressed || label;
  const generic = detail === label || ["exec", "exec_command", "toolCall", "mcpToolCall"].includes(detail);
  const stringify = value => {
    if (value == null) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
  };
  const command = stringify(payload?.command || native.command);
  const output = stringify(payload?.output || native.aggregatedOutput || native.output || native.result || native.error);
  const cwd = stringify(payload?.cwd || native.cwd);
  const tool = stringify(payload?.tool || native.tool || native.name);
  const args = stringify(payload?.arguments || native.arguments || native.input);
  const changes = Array.isArray(payload?.changes) ? payload.changes : (Array.isArray(native.changes) ? native.changes : []);
  const plan = Array.isArray(payload?.plan) ? payload.plan : (Array.isArray(native.plan) ? native.plan : (Array.isArray(native.steps) ? native.steps : []));
  const protocolNoise = ["updated", "update", "diff", "output", "delta", "itemupdated", "itemdelta", "turnupdated", "turndiff"].includes(normalizedType)
    || isEmptyReasoningEnvelope(detail);
  const rawCodexProtocol = type === "codex" && Boolean(payload?.method);
  const reasoningText = String(eventText({ type: "reasoning", text: payload?.text, summary: native.summary, content: native.content }, labels) || "").trim();
  const emptyReasoning = type.includes("reason") && (!reasoningText || ["reasoning", "正在分析与规划", "正在处理"].includes(reasoningText));
  return {
    text: generic || !detail ? label : `${label} · ${detail}`,
    kind: type,
    label,
    detail: generic ? "" : detail,
    command,
    output,
    cwd,
    tool,
    arguments: args,
    changes,
    plan,
    hidden: protocolNoise || rawCodexProtocol || emptyReasoning,
    exitCode: payload?.exit_code ?? native.exitCode ?? null,
    status: status || (type.includes("reason") ? "started" : ""),
    type,
    itemId: String(payload?.item_id || payload?.id || ""),
    eventId: String(event?.id || ""),
    turnId: String(payload?.turn_id || payload?.turnId || native.turnId || ""),
    time: event?.ts || event?.created_at || "",
  };
}

function userDedupeBody(text) {
  return stripCodexHiddenContext(text)
    .replace(/<image\b[^>]*>[\s\S]*?<\/image>/gi, "")
    .replace(/<audio\b[^>]*>[\s\S]*?<\/audio>/gi, "")
    .replace(/<video\b[^>]*>[\s\S]*?<\/video>/gi, "")
    .replace(/\[\[codex-input:[^\]]+\]\]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function attachmentPath(value) {
  const raw = String(value || "").trim();
  let text = raw;
  try { text = decodeURIComponent(raw); } catch (_) {}
  if (/^[a-zA-Z]:[\\/]/.test(text) || text.startsWith("/")) {
    return text.split(/[\\/]/).filter(Boolean).pop() || text;
  }
  return text;
}

function mergeEvents(events, messages) {
  const historyTimes = (events || [])
    .filter(event => event.stream !== "metrics")
    .map(timestamp)
    .filter(value => Number.isFinite(value) && value > 0);
  const oldestLoadedTime = historyTimes.length ? Math.min(...historyTimes) : null;
  const visible = (messages || []).filter(message => {
    if (["running", "steering"].includes(message.status)) return true;
    if (!["steered", "sent"].includes(message.status)) return false;
    if (oldestLoadedTime === null) return true;
    const createdTime = Date.parse(message.created_at || "");
    return Number.isFinite(createdTime) && createdTime >= oldestLoadedTime;
  });
  const merged = [];
  const seenUserKeys = new Set();
  const seenUserBodies = new Map();
  for (const event of events || []) {
    if (event.stream === "metrics") {
      merged.push(event);
      continue;
    }
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
    .replace(/<(image|audio|video)\b[^>]*\bpath=["']([^"']+)["'][^>]*>[\s\S]*?<\/\1>/gi, (_, tag, path) => {
      const kind = tag.toLowerCase() === "image" ? "localImage" : tag.toLowerCase() === "audio" ? "localAudio" : "legacy";
      files.push({ kind, path: attachmentPath(path) });
      return "";
    })
    .replace(/<(?:image|audio|video)\b[^>]*>[\s\S]*?<\/(?:image|audio|video)>/gi, "")
    .replace(/\[\[codex-input:(localImage|localAudio|mention):([^\]]+)\]\]/g, (_, kind, path) => { files.push({ kind, path: attachmentPath(path) }); return ""; })
    .replace(/\[\[codex-file:([^\]]+)\]\]/g, (_, path) => { files.push({ kind: "legacy", path: attachmentPath(path) }); return ""; })
    .replace(/(?:Uploaded to workspace|已上传到工作区)\s*[:：]\s*([^\n\r，。]+?\.[a-z0-9]{1,12})(?=\s|[，。]|$)/gi, (_, path) => { files.push({ kind: "legacy", path: path.trim() }); return ""; })
    .replace(/\[(?:Attachments?|附件)\s*[:：]\s*[^\]]+\]/gi, "")
    .trim();
  const fallbackBody = () => escapeHtml(clean)
    .replace(/```(?:[a-zA-Z0-9_+-]+)?\n?([\s\S]*?)```/g, '<pre class="code-block">$1</pre>')
    .replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/^###\s+(.+)$/gm, "<h4>$1</h4>")
    .replace(/^[-*]\s+(.+)$/gm, '<span class="markdown-bullet">$1</span>')
    .replace(/\n/g, "<br>");
  const body = chatMarkdownRenderer ? chatMarkdownRenderer.render(clean) : fallbackBody();
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
  const blocks = []; const assistantItems = new Map(); const userItems = new Map(); const activityItems = new Map(); let current = null;
  for (const event of events) {
    if (["metrics", "history"].includes(event.stream)) continue;
    const text = eventText(event.payload, labels).trim();
    if (!text) continue;
    const role = isUser(event) ? "user" : isAssistant(event) ? "assistant" : "activity";
    const payload = valueOf(event.payload);
    if (role === "activity") {
      if (!rawActivity && ["app-server", "stdout", "stderr"].includes(event.stream)) continue;
      const item = activityItem(payload, labels, event);
      if (item.hidden) continue;
      if (item.itemId && activityItems.has(item.itemId)) {
        const existing = activityItems.get(item.itemId);
        for (const [key, value] of Object.entries(item)) {
          const empty = value == null || value === "" || (Array.isArray(value) && value.length === 0);
          if (!empty || !existing[key]) existing[key] = value;
        }
        current = null;
        continue;
      }
      let group = blocks[blocks.length - 1];
      if (!group || group.role !== "activities") {
        const activityKey = [event.stream || "activity", event.id || item.itemId || event.session_id || event.ts || blocks.length].join(":");
        group = { role: "activities", activityKey, items: [] };
        blocks.push(group);
      }
      group.items.push(item); if (item.itemId) activityItems.set(item.itemId, item); current = null; continue;
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
    const distinctItem = Boolean(itemId && current && itemId !== current.itemId);
    if (!current || current.role !== role || role === "user" || commandBlock || current.commandBlock || distinctItem) {
      current = { role, text: "", session: event.session_id, commandBlock, itemId, eventId: String(event.id || ""), turnId: String(payload?.turn_id || payload?.turnId || ""), delivery: payload?.type === "browserMessage" ? payload.status : "", error: payload?.error || "", streaming: payload?.type === "agent_delta", origin: event.stream === "rollout" ? "terminal" : "web", time: event.ts || event.created_at || "" };
      blocks.push(current); if (itemId) itemMap.set(itemId, current);
    }
    current.text += `${current.text ? "\n" : ""}${text}`;
  }
  return blocks.filter(block => {
    if (block.role === "activities") return true;
    block.text = stripCodexHiddenContext(block.text);
    if (!block.text) return false;
    block.html = markdown(block.text);
    return true;
  });
}

function explorationFingerprint(value) {
  const text = String(value || "").toLowerCase().replace(/<[^>]+>/g, " ").replace(/[`*_#>[\](){}]/g, " ").replace(/[^\p{L}\p{N}./_-]+/gu, " ").trim();
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function explorationTitle(value, fallback = "关键活动") {
  const clean = stripCodexHiddenContext(value)
    .replace(/<(image|audio|video)\b[^>]*>[\s\S]*?<\/\1>/gi, " ")
    .replace(/\[\[(?:codex-input|codex-file):[^\]]+\]\]/g, " ")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[`*_#>\[\]()]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!clean) return fallback;
  const first = clean.split(/(?<=[。！？!?])\s+|\n/)[0].trim();
  return first.length > 58 ? `${first.slice(0, 57)}…` : first;
}

function substantiveDirection(text) {
  const clean = explorationTitle(text, "");
  if (!clean || /^\s*\//.test(clean)) return false;
  if (/^(继续|继续吧|好|好的|可以|行|嗯|收到|重试|再试一次|实现了吧|ok|okay|yes|go on)[！!。.？?\s]*$/i.test(clean)) return false;
  let score = clean.length >= 18 ? 2 : 0;
  if (/(帮|修|改|增加|添加|删除|实现|排查|检查|优化|设计|支持|恢复|回退|提交|推送|重启|需要|不要|必须|应该|为什么|为啥|异常|失败|卡|问题|bug|please|fix|add|remove|implement|investigate|debug|optimi[sz]e|design|support|error|fail)/i.test(clean)) score += 5;
  if (/(改成|换成|重新|不是|只要|不要再|范围|相反|回退|撤销|instead|rather|revert|rollback)/i.test(clean)) score += 3;
  return score >= 5;
}

function buildExplorationTree(blocks, taskRunning = false, taskStatus = "") {
  const nodes = [];
  const byId = new Map();
  const planNodes = new Map();
  const decisionNodes = new Map();
  let latestNode = null;
  let latestDirection = null;
  let planTail = null;

  const addNode = node => {
    if (byId.has(node.id)) return byId.get(node.id);
    const complete = { summary: "", evidence: [], files: [], commands: [], failures: 0, ...node };
    nodes.push(complete);
    byId.set(complete.id, complete);
    latestNode = complete;
    return complete;
  };
  const addEvidence = (node, value) => {
    const text = explorationTitle(value, "");
    if (node && text && !node.evidence.includes(text)) node.evidence.push(text);
  };

  for (let blockIndex = 0; blockIndex < blocks.length; blockIndex += 1) {
    const block = blocks[blockIndex];
    if (block.role === "user" && !block.commandBlock && substantiveDirection(block.text)) {
      const correction = /(改成|换成|重新|不是|只要|不要再|范围|回退|撤销|instead|rather|revert|rollback)/i.test(block.text);
      const rollback = /(回退|撤销|恢复到|revert|rollback)/i.test(block.text);
      const previous = latestNode;
      if (correction && previous && ["active", "planned"].includes(previous.status)) previous.status = rollback ? "rolledback" : "abandoned";
      const parentId = correction && previous ? previous.parentId : previous?.id || null;
      const source = block.itemId || block.eventId || `${block.turnId}:${blockIndex}`;
      latestDirection = addNode({
        id: `direction-${explorationFingerprint(source || block.text)}`,
        parentId,
        kind: rollback ? "rollback" : correction ? "steering" : "direction",
        title: explorationTitle(block.text, "调整目标"),
        status: "active",
        blockIndex,
        turnId: block.turnId || "",
        time: block.time || "",
        score: correction ? 8 : 6,
      });
      planTail = null;
      continue;
    }

    if (block.role === "activities") {
      for (const item of block.items || []) {
        const decisionText = String(item.detail || "").trim();
        const isDecision = item.kind.includes("reason") && /(决定|确认|根因|证明|改用|转向|放弃|不可行|关键是|结论|选择|root cause|decid|confirmed|switch(?:ing)? to|not viable)/i.test(decisionText);
        if (isDecision) {
          const title = explorationTitle(decisionText, "形成关键结论");
          const key = explorationFingerprint(`${item.turnId || item.itemId || block.activityKey || "turn"}:decision:${title}`);
          if (!decisionNodes.has(key)) {
            const switchesDirection = /(改用|转向|放弃|不可行|switch(?:ing)? to|not viable)/i.test(decisionText);
            const previous = latestNode;
            if (switchesDirection && previous && ["active", "planned"].includes(previous.status)) previous.status = "abandoned";
            const decision = addNode({
              id: `decision-${key}`,
              parentId: switchesDirection && previous ? previous.parentId : previous?.id || null,
              kind: "decision",
              title,
              status: "completed",
              blockIndex,
              itemId: item.itemId || "",
              turnId: item.turnId || "",
              time: item.time || "",
              score: 8,
            });
            addEvidence(decision, decisionText);
            decisionNodes.set(key, decision);
          }
        }
        const plans = Array.isArray(item.plan) ? item.plan : [];
        for (let planIndex = 0; planIndex < plans.length; planIndex += 1) {
          const plan = plans[planIndex] || {};
          const title = explorationTitle(plan.step || plan.text, "计划步骤");
          if (!title) continue;
          const key = explorationFingerprint(`${item.turnId || item.itemId || block.activityKey || "turn"}:${title}`);
          let node = planNodes.get(key);
          const planStatus = String(plan.status || "pending").toLowerCase();
          const status = planStatus === "completed" ? "completed" : planStatus === "in_progress" ? "active" : planStatus === "failed" ? "failed" : "planned";
          if (!node) {
            node = addNode({
              id: `plan-${key}`,
              parentId: planTail?.id || latestNode?.id || latestDirection?.id || null,
              kind: "plan",
              title,
              status,
              blockIndex,
              itemId: item.itemId || "",
              turnId: item.turnId || "",
              time: item.time || "",
              score: 7,
            });
            planNodes.set(key, node);
            planTail = node;
          } else {
            node.status = status;
            node.blockIndex = blockIndex;
            node.time = item.time || node.time;
          }
        }

        const target = [...nodes].reverse().find(node => node.status === "active") || [...nodes].reverse().find(node => node.status === "planned") || latestNode;
        if (!target) continue;
        if (item.command && !target.commands.includes(item.command)) target.commands.push(item.command);
        for (const change of item.changes || []) {
          const path = String(change?.path || change || "").trim();
          if (path && !target.files.includes(path)) target.files.push(path);
        }
        if (item.detail && (item.kind.includes("reason") || item.kind.includes("search"))) addEvidence(target, item.detail);
        if (item.status === "failed" || (item.exitCode != null && Number(item.exitCode) !== 0)) target.failures += 1;
      }
      continue;
    }

    if (block.role === "assistant" && !block.commandBlock && latestNode) {
      const target = taskRunning ? ([...nodes].reverse().find(node => node.status === "active") || latestNode) : latestNode;
      target.summary = explorationTitle(block.text, target.summary || "");
      if (!taskRunning && target.status === "active") target.status = "completed";
    }
  }

  if (taskRunning) {
    const active = [...nodes].reverse().find(node => node.status === "active") || [...nodes].reverse().find(node => node.status === "planned") || latestNode;
    for (const node of nodes) if (node !== active && node.status === "active") node.status = "completed";
    if (active && !["failed", "rolledback", "abandoned"].includes(active.status)) active.status = "active";
  } else if (taskStatus === "failed") {
    const failed = [...nodes].reverse().find(node => !["rolledback", "abandoned"].includes(node.status));
    if (failed) failed.status = "failed";
    for (const node of nodes) if (node !== failed && node.status === "active") node.status = "completed";
  } else if (taskStatus === "stopped") {
    const stopped = [...nodes].reverse().find(node => node.status === "active");
    if (stopped) stopped.status = "planned";
    for (const node of nodes) if (node !== stopped && node.status === "active") node.status = "completed";
  } else {
    for (const node of nodes) if (node.status === "active") node.status = "completed";
  }
  return nodes.map(node => ({ ...node, evidence: node.evidence.slice(0, 4), files: node.files.slice(0, 8), commands: node.commands.slice(0, 5) }));
}

self.onmessage = event => {
  const { requestId, taskId, events, explorationEvents, explorationRevision, messages, rawActivity, labels, taskRunning, taskStatus } = event.data;
  try {
    const merged = mergeEvents(events, messages);
    const blocks = buildBlocks(merged, rawActivity, labels || {});
    let explorationNodes;
    if (Array.isArray(explorationEvents)) {
      const semanticEvents = explorationEvents.length ? mergeEvents(explorationEvents, messages) : merged;
      const semanticBlocks = semanticEvents === merged && rawActivity ? blocks : buildBlocks(semanticEvents, true, labels || {});
      explorationNodes = buildExplorationTree(semanticBlocks, taskRunning, taskStatus);
      explorationCache.set(taskId || "default", { revision: explorationRevision, nodes: explorationNodes });
    } else {
      explorationNodes = explorationCache.get(taskId || "default")?.nodes || [];
    }
    self.postMessage({ requestId, blocks, explorationNodes });
  } catch (error) {
    self.postMessage({ requestId, error: String(error?.message || error) });
  }
};
