// Read-only projection of the Codex timeline into a compact decision tree.
const explorationStatusMeta = {
  planned: { label: "未进行", symbol: "○" },
  active: { label: "进行中", symbol: "●" },
  completed: { label: "完成", symbol: "✓" },
  failed: { label: "失败", symbol: "!" },
  rolledback: { label: "已回退", symbol: "↶" },
  abandoned: { label: "已转向", symbol: "↗" },
};
const explorationZoomBounds = { min: .35, max: 1.8 };
let explorationZoom = 1;
let explorationDrag = null;
let suppressExplorationClick = false;

function clampExplorationZoom(value) {
  return Math.min(explorationZoomBounds.max, Math.max(explorationZoomBounds.min, value));
}

function updateExplorationZoom(anchorX, anchorY, nextZoom) {
  const viewport = $("#exploration-map-viewport");
  const canvas = $("#exploration-map-canvas");
  const world = canvas?.querySelector(".exploration-map-world");
  if (!viewport || !canvas || !world) return;
  const oldZoom = explorationZoom;
  const zoom = clampExplorationZoom(nextZoom);
  if (Math.abs(zoom - oldZoom) < .001) return;
  const pointerX = Number.isFinite(anchorX) ? anchorX : viewport.clientWidth / 2;
  const pointerY = Number.isFinite(anchorY) ? anchorY : viewport.clientHeight / 2;
  const worldX = (viewport.scrollLeft + pointerX) / oldZoom;
  const worldY = (viewport.scrollTop + pointerY) / oldZoom;
  explorationZoom = zoom;
  const width = Number(world.dataset.width) || world.offsetWidth;
  const height = Number(world.dataset.height) || world.offsetHeight;
  canvas.style.width = `${width * zoom}px`;
  canvas.style.height = `${height * zoom}px`;
  world.style.transform = `scale(${zoom})`;
  viewport.scrollLeft = worldX * zoom - pointerX;
  viewport.scrollTop = worldY * zoom - pointerY;
  const label = $("#exploration-zoom-reset");
  if (label) label.textContent = `${Math.round(zoom * 100)}%`;
}

function explorationNodeById(id) {
  return (state.explorationNodes || []).find(node => node.id === id) || null;
}

function explorationCurrentNode() {
  const nodes = state.explorationNodes || [];
  return [...nodes].reverse().find(node => node.status === "active") || nodes[nodes.length - 1] || null;
}

function explorationLayout(nodes) {
  const ids = new Set(nodes.map(node => node.id));
  const children = new Map(nodes.map(node => [node.id, []]));
  const roots = [];
  for (const node of nodes) {
    if (node.parentId && ids.has(node.parentId) && node.parentId !== node.id) children.get(node.parentId).push(node);
    else roots.push(node);
  }
  const positions = new Map();
  const visiting = new Set();
  let nextRow = 0;
  const place = (node, depth) => {
    if (positions.has(node.id) || visiting.has(node.id)) return positions.get(node.id)?.row ?? nextRow++;
    visiting.add(node.id);
    const branch = children.get(node.id) || [];
    const rows = branch.map(child => place(child, depth + 1));
    const row = rows.length ? rows.reduce((sum, value) => sum + value, 0) / rows.length : nextRow++;
    positions.set(node.id, { depth, row });
    visiting.delete(node.id);
    return row;
  };
  roots.forEach(root => place(root, 0));
  nodes.forEach(node => { if (!positions.has(node.id)) place(node, 0); });
  nodes.forEach((node, column) => { positions.get(node.id).column = column; });
  const maxRow = Math.max(0, ...[...positions.values()].map(position => position.row));
  return { positions, maxRow, width: Math.max(760, 90 + nodes.length * 244), height: Math.max(430, 80 + (maxRow + 1) * 126) };
}

function explorationEventKey(event) {
  if (event?.id != null && event.id !== "") return `${event.stream || "event"}:${event.id}`;
  const payload = typeof event?.payload === "string" ? event.payload : JSON.stringify(event?.payload || {});
  return `${event?.stream || "event"}:${event?.session_id || ""}:${event?.ts || event?.created_at || ""}:${payload}`;
}

async function loadFullExplorationHistory(force = false) {
  if (!state.selectedId || state.explorationLoading || (state.explorationHistoryComplete && !force)) return;
  const taskId = state.selectedId;
  const requestId = ++state.explorationRequestId;
  state.explorationLoading = true;
  state.explorationLoadError = "";
  renderExplorationMap();
  try {
    let page = await api(`/tasks/${encodeURIComponent(taskId)}/timeline?limit=500`);
    let events = [...(page.items || [])];
    state.explorationLoadedEventCount = events.length;
    let cursor = String(page.next_cursor || "");
    let hasMore = Boolean(page.has_more && cursor);
    const seenCursors = new Set();
    while (hasMore && state.selectedId === taskId && requestId === state.explorationRequestId) {
      if (seenCursors.has(cursor)) throw new Error("完整会话游标没有继续前进");
      seenCursors.add(cursor);
      page = await api(`/tasks/${encodeURIComponent(taskId)}/timeline?limit=500&before=${encodeURIComponent(cursor)}`);
      events = [...(page.items || []), ...events];
      state.explorationLoadedEventCount = events.length;
      const nextCursor = String(page.next_cursor || "");
      hasMore = Boolean(page.has_more && nextCursor);
      cursor = nextCursor;
      renderExplorationMap();
      await new Promise(resolve => setTimeout(resolve, 0));
    }
    if (state.selectedId !== taskId || requestId !== state.explorationRequestId) return;
    const combined = [...events, ...(state.explorationEvents || [])];
    const unique = new Map();
    combined.forEach(event => unique.set(explorationEventKey(event), event));
    state.explorationEvents = [...unique.values()];
    state.explorationLoadedEventCount = state.explorationEvents.length;
    state.explorationHistoryComplete = !hasMore;
    state.explorationRevision += 1;
    state.explorationNeedsSync = true;
    scheduleRenderChat();
  } catch (error) {
    if (state.selectedId === taskId && requestId === state.explorationRequestId) state.explorationLoadError = error.message || String(error);
  } finally {
    if (state.selectedId === taskId && requestId === state.explorationRequestId) {
      state.explorationLoading = false;
      renderExplorationMap();
    }
  }
}

function explorationEdgePath(parent, child) {
  const startX = parent.x + 194;
  const startY = parent.y + 43;
  const endX = child.x;
  const endY = child.y + 43;
  const curve = Math.max(24, (endX - startX) * .48);
  return `M ${startX} ${startY} C ${startX + curve} ${startY}, ${endX - curve} ${endY}, ${endX} ${endY}`;
}

function renderExplorationDetail(node) {
  const detail = $("#exploration-map-detail");
  if (!detail) return;
  if (!node) {
    detail.innerHTML = `<div class="exploration-detail-empty"><span class="exploration-map-symbol large" aria-hidden="true"><i></i><i></i><i></i></span><strong>选择一个关键节点</strong><p>查看它为什么出现、相关修改以及执行证据。</p></div>`;
    return;
  }
  const meta = explorationStatusMeta[node.status] || explorationStatusMeta.planned;
  const evidence = (node.evidence || []).map(value => `<li>${esc(value)}</li>`).join("");
  const files = (node.files || []).map(value => `<li><code>${esc(value)}</code></li>`).join("");
  const commands = (node.commands || []).map(value => `<li><code>${esc(value)}</code></li>`).join("");
  const typeLabel = node.kind === "plan" ? "计划方向" : node.kind === "decision" ? "关键结论" : node.kind === "steering" ? "方向调整" : node.kind === "rollback" ? "回退决策" : "用户目标";
  detail.innerHTML = `<div class="exploration-detail-head"><span class="exploration-node-state ${esc(node.status)}">${meta.symbol} ${meta.label}</span><small>${esc(typeLabel)}</small></div><h3>${esc(node.title)}</h3>${node.summary ? `<p class="exploration-detail-summary">${esc(node.summary)}</p>` : ""}<dl class="exploration-detail-facts"><div><dt>时间</dt><dd>${node.time ? esc(shortDate(node.time)) : "当前会话"}</dd></div><div><dt>关键度</dt><dd>${Number(node.score) || 0} / 10</dd></div><div><dt>执行异常</dt><dd>${Number(node.failures) || 0}</dd></div></dl>${evidence ? `<section><h4>方向证据</h4><ul>${evidence}</ul></section>` : ""}${files ? `<section><h4>相关文件</h4><ul>${files}</ul></section>` : ""}${commands ? `<details><summary>核心命令 · ${(node.commands || []).length}</summary><ul>${commands}</ul></details>` : ""}<div class="exploration-detail-actions"><button type="button" class="primary" data-exploration-jump="${esc(node.id)}">在对话中查看</button></div>`;
}

function revealExplorationNode(id, smooth = false) {
  const viewport = $("#exploration-map-viewport");
  const node = viewport?.querySelector(`[data-exploration-node="${CSS.escape(id)}"]`);
  if (!viewport || !node) return;
  viewport.scrollTo({
    left: (node.offsetLeft + node.offsetWidth / 2) * explorationZoom - viewport.clientWidth / 2,
    top: (node.offsetTop + node.offsetHeight / 2) * explorationZoom - viewport.clientHeight / 2,
    behavior: smooth ? "smooth" : "auto",
  });
}

function renderExplorationMap(options = {}) {
  const nodes = state.explorationNodes || [];
  const count = $("#exploration-map-count");
  if (count) count.textContent = String(nodes.length);
  const openButton = $("#exploration-map-open");
  if (openButton) openButton.classList.toggle("has-active", nodes.some(node => node.status === "active"));
  if (!state.explorationOpen) return;

  if (options.liveUpdate && $("#exploration-follow-current")?.checked) state.explorationSelectedNodeId = explorationCurrentNode()?.id || "";

  const canvas = $("#exploration-map-canvas");
  const loadOlder = $("#exploration-load-older");
  const historyStatus = $("#exploration-history-status");
  if (loadOlder) {
    loadOlder.hidden = !state.explorationLoadError;
    loadOlder.disabled = state.explorationLoading;
  }
  if (historyStatus) {
    historyStatus.className = state.explorationLoadError ? "error" : state.explorationLoading ? "loading" : state.explorationHistoryComplete ? "complete" : "";
    historyStatus.textContent = state.explorationLoadError ? `全量加载失败 · ${state.explorationLoadError}` : state.explorationLoading ? `正在加载完整会话 · ${state.explorationLoadedEventCount} 条事件` : state.explorationHistoryComplete ? `全量对话 · ${state.explorationEvents.length} 条事件` : `最近路径 · ${state.explorationEvents.length} 条事件`;
  }
  if (!nodes.length) {
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.innerHTML = `<div class="exploration-empty"><span class="exploration-map-symbol large" aria-hidden="true"><i></i><i></i><i></i></span><strong>还没有形成关键路径</strong><p>出现明确目标、计划步骤或方向调整后，地图会自动生长。</p></div>`;
    renderExplorationDetail(null);
    return;
  }

  const layout = explorationLayout(nodes);
  const viewport = $("#exploration-map-viewport");
  const viewportHeight = viewport?.clientHeight || 0;
  const viewportWidth = viewport?.clientWidth || 0;
  const verticalOffset = Math.max(40, (viewportHeight - 86) / 2);
  const horizontalOffset = Math.max(48, (viewportWidth - 194) / 2);
  layout.width = Math.max(layout.width, horizontalOffset * 2 + Math.max(0, nodes.length - 1) * 244 + 194);
  layout.height = Math.max(layout.height, verticalOffset * 2 + layout.maxRow * 126 + 86);
  const coordinates = new Map();
  for (const node of nodes) {
    const position = layout.positions.get(node.id);
    coordinates.set(node.id, { x: horizontalOffset + position.column * 244, y: verticalOffset + position.row * 126 });
  }
  const edges = nodes.map(node => {
    const parent = coordinates.get(node.parentId);
    const child = coordinates.get(node.id);
    if (!parent || !child) return "";
    return `<path class="exploration-edge ${esc(node.status)}" d="${explorationEdgePath(parent, child)}" />`;
  }).join("");
  const cards = nodes.map((node, index) => {
    const point = coordinates.get(node.id);
    const meta = explorationStatusMeta[node.status] || explorationStatusMeta.planned;
    const selected = node.id === state.explorationSelectedNodeId;
    const time = node.time ? `<span class="exploration-node-time" style="left:${point.x}px;top:${Math.max(8, point.y - 22)}px">${esc(shortDate(node.time))}</span>` : "";
    return `${time}<button type="button" class="exploration-node ${esc(node.status)}${selected ? " selected" : ""}" style="left:${point.x}px;top:${point.y}px" data-exploration-node="${esc(node.id)}" aria-pressed="${selected}"><span class="exploration-node-index">${String(index + 1).padStart(2, "0")}</span><span class="exploration-node-copy"><strong>${esc(node.title)}</strong><small>${meta.symbol} ${meta.label}${node.files?.length ? ` · ${node.files.length} 文件` : ""}</small></span></button>`;
  }).join("");
  canvas.style.width = `${layout.width * explorationZoom}px`;
  canvas.style.height = `${layout.height * explorationZoom}px`;
  canvas.innerHTML = `<div class="exploration-map-world" data-width="${layout.width}" data-height="${layout.height}" style="width:${layout.width}px;height:${layout.height}px;transform:scale(${explorationZoom})"><svg class="exploration-edges" width="${layout.width}" height="${layout.height}" aria-hidden="true">${edges}</svg>${cards}</div>`;
  const zoomLabel = $("#exploration-zoom-reset");
  if (zoomLabel) zoomLabel.textContent = `${Math.round(explorationZoom * 100)}%`;

  let selected = explorationNodeById(state.explorationSelectedNodeId);
  if (!selected) {
    selected = explorationCurrentNode();
    state.explorationSelectedNodeId = selected?.id || "";
  }
  renderExplorationDetail(selected);
  if (options.reveal || ($("#exploration-follow-current")?.checked && options.liveUpdate)) requestAnimationFrame(() => revealExplorationNode(selected?.id || "", Boolean(options.liveUpdate)));
}

function openExplorationMap() {
  state.explorationOpen = true;
  state.explorationSelectedNodeId = explorationCurrentNode()?.id || "";
  const map = $("#exploration-map");
  map.classList.add("open");
  map.setAttribute("aria-hidden", "false");
  document.body.classList.add("exploration-map-visible");
  renderExplorationMap({ reveal: true });
  if (state.explorationNeedsSync) scheduleRenderChat();
  loadFullExplorationHistory();
}

function closeExplorationMap() {
  state.explorationOpen = false;
  const map = $("#exploration-map");
  map.classList.remove("open");
  map.setAttribute("aria-hidden", "true");
  document.body.classList.remove("exploration-map-visible");
}

function jumpToExplorationNode(id) {
  const node = explorationNodeById(id);
  if (!node || !Number.isInteger(node.blockIndex)) return;
  closeExplorationMap();
  const windowSize = 90;
  state.chatVirtualStart = Math.max(0, Math.min(node.blockIndex - 12, Math.max(0, state.chatBlocks.length - windowSize)));
  paintVirtualChat(false);
  requestAnimationFrame(() => {
    const target = $("#chat-log")?.querySelector(`[data-chat-block-index="${node.blockIndex}"]`);
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
    target?.classList.add("exploration-chat-target");
    setTimeout(() => target?.classList.remove("exploration-chat-target"), 1800);
  });
}

$("#exploration-map-open")?.addEventListener("click", openExplorationMap);
$("#exploration-map-close")?.addEventListener("click", closeExplorationMap);
$("#exploration-map")?.addEventListener("click", event => { if (event.target === event.currentTarget) closeExplorationMap(); });
$("#exploration-map-viewport")?.addEventListener("wheel", event => {
  if (!state.explorationOpen || !state.explorationNodes?.length) return;
  event.preventDefault();
  const rect = event.currentTarget.getBoundingClientRect();
  const intensity = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? .045 : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? .35 : .0015;
  updateExplorationZoom(event.clientX - rect.left, event.clientY - rect.top, explorationZoom * Math.exp(-event.deltaY * intensity));
}, { passive: false });
$("#exploration-map-viewport")?.addEventListener("pointerdown", event => {
  if (event.button !== 0 || !state.explorationOpen) return;
  const viewport = event.currentTarget;
  explorationDrag = { id: event.pointerId, x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop, moved: false };
  viewport.setPointerCapture(event.pointerId);
});
$("#exploration-map-viewport")?.addEventListener("pointermove", event => {
  const drag = explorationDrag;
  if (!drag || drag.id !== event.pointerId) return;
  const dx = event.clientX - drag.x;
  const dy = event.clientY - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) < 4) return;
  drag.moved = true;
  event.currentTarget.classList.add("dragging");
  event.currentTarget.scrollLeft = drag.left - dx;
  event.currentTarget.scrollTop = drag.top - dy;
  event.preventDefault();
});
const finishExplorationDrag = event => {
  if (!explorationDrag || explorationDrag.id !== event.pointerId) return;
  suppressExplorationClick = explorationDrag.moved;
  if (suppressExplorationClick) setTimeout(() => { suppressExplorationClick = false; }, 0);
  explorationDrag = null;
  event.currentTarget.classList.remove("dragging");
  if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
};
$("#exploration-map-viewport")?.addEventListener("pointerup", finishExplorationDrag);
$("#exploration-map-viewport")?.addEventListener("pointercancel", finishExplorationDrag);
$("#exploration-map-viewport")?.addEventListener("click", event => {
  if (!suppressExplorationClick) return;
  suppressExplorationClick = false;
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);
$("#exploration-zoom-out")?.addEventListener("click", () => updateExplorationZoom(null, null, explorationZoom - .1));
$("#exploration-zoom-reset")?.addEventListener("click", () => updateExplorationZoom(null, null, 1));
$("#exploration-zoom-in")?.addEventListener("click", () => updateExplorationZoom(null, null, explorationZoom + .1));
$("#exploration-map-canvas")?.addEventListener("click", event => {
  const button = event.target.closest("[data-exploration-node]");
  if (!button) return;
  state.explorationSelectedNodeId = button.dataset.explorationNode;
  const current = explorationCurrentNode();
  if (current && current.id !== state.explorationSelectedNodeId) $("#exploration-follow-current").checked = false;
  renderExplorationMap();
});
$("#exploration-map-detail")?.addEventListener("click", event => {
  const button = event.target.closest("[data-exploration-jump]");
  if (button) jumpToExplorationNode(button.dataset.explorationJump);
});
$("#exploration-load-older")?.addEventListener("click", async () => {
  await loadFullExplorationHistory(true);
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && state.explorationOpen) { event.preventDefault(); closeExplorationMap(); }
});
