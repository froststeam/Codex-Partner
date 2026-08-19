// Read-only projection of the Codex timeline into a compact decision tree.
const explorationStatusMeta = {
  planned: { labelKey: "explorationStatusPlanned", symbol: "○" },
  active: { labelKey: "explorationStatusActive", symbol: "●" },
  completed: { labelKey: "explorationStatusCompleted", symbol: "✓" },
  failed: { labelKey: "explorationStatusFailed", symbol: "!" },
  rolledback: { labelKey: "explorationStatusRolledBack", symbol: "↶" },
  abandoned: { labelKey: "explorationStatusAbandoned", symbol: "↗" },
};
const explorationZoomBounds = { min: .1, max: 1.8 };
let explorationZoom = 1;
let explorationDrag = null;
let suppressExplorationClick = false;
let explorationFramePending = false;

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

function explorationMeta(status) {
  const meta = explorationStatusMeta[status] || explorationStatusMeta.planned;
  return { ...meta, label: uiLabel(meta.labelKey) };
}

function explorationKindKey(kind) {
  return kind === "plan" ? "explorationKindPlan" : kind === "phase" ? "explorationKindPhase" : kind === "decision" ? "explorationKindDecision" : kind === "steering" ? "explorationKindSteering" : kind === "rollback" ? "explorationKindRollback" : "explorationKindDirection";
}

function explorationTitle(node) {
  const title = String(node?.title || "");
  return node?.kind === "phase" ? title.replace(/^执行阶段[：:]\s*/, "") : title;
}

function explorationLayout(nodes, graphEdges = []) {
  const ids = new Set(nodes.map(node => node.id));
  const nodeById = new Map(nodes.map(node => [node.id, node]));
  const parent = new Map(nodes.map(node => [node.id, node.id]));
  const find = id => {
    let root = id;
    while (parent.get(root) !== root) root = parent.get(root);
    while (parent.get(id) !== id) { const next = parent.get(id); parent.set(id, root); id = next; }
    return root;
  };
  const union = (left, right) => {
    if (!ids.has(left) || !ids.has(right)) return;
    const leftRoot = find(left); const rightRoot = find(right);
    if (leftRoot !== rightRoot) parent.set(rightRoot, leftRoot);
  };
  for (const edge of graphEdges) {
    const source = nodeById.get(edge.sourceId);
    if (edge.kind !== "branch" || source?.parentId) union(edge.sourceId, edge.targetId);
  }
  for (const node of nodes) {
    if (["phase", "plan"].includes(node.kind) && ids.has(node.parentId)) union(node.parentId, node.id);
  }
  const groups = new Map();
  nodes.forEach((node, index) => {
    const root = find(node.id);
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root).push(index);
  });
  const intervals = [...groups.entries()].map(([id, indices]) => ({ id, start: Math.min(...indices), end: Math.max(...indices) })).sort((left, right) => left.start - right.start);
  const laneEnds = [];
  const lanes = new Map();
  for (const interval of intervals) {
    let lane = laneEnds.findIndex(end => end < interval.start);
    if (lane < 0) { lane = laneEnds.length; laneEnds.push(interval.end); }
    else laneEnds[lane] = interval.end;
    lanes.set(interval.id, lane);
  }
  const positions = new Map();
  nodes.forEach((node, index) => positions.set(node.id, {
    depth: Math.floor(index / 3),
    row: (lanes.get(find(node.id)) || 0) * 3 + index % 3,
    lane: lanes.get(find(node.id)) || 0,
  }));
  const maxRow = Math.max(0, ...[...positions.values()].map(position => position.row));
  const maxDepth = Math.max(0, ...[...positions.values()].map(position => position.depth));
  return { positions, laneCount: Math.max(1, laneEnds.length), maxRow, maxDepth, width: Math.max(760, 90 + (maxDepth + 1) * 244), height: Math.max(430, 80 + (maxRow + 1) * 126) };
}

function applyActivityMapSnapshot(snapshot = {}) {
  state.explorationPrecomputed = true;
  state.explorationNodes = Array.isArray(snapshot.nodes) ? snapshot.nodes : [];
  state.explorationEdges = Array.isArray(snapshot.edges) ? snapshot.edges : [];
  state.explorationMapStatus = snapshot.status || "pending";
  state.explorationHistoryComplete = snapshot.status === "ready";
  state.explorationLoadedEventCount = Number(snapshot.event_count || 0);
  state.explorationProcessedEvents = Number(snapshot.processed_events || 0);
  state.explorationRevision = Number(snapshot.revision || 0);
  state.explorationLoadError = snapshot.error || "";
  renderExplorationMap();
}

async function loadPrecomputedActivityMap(force = false) {
  if (!state.selectedId || state.explorationLoading || (state.explorationHistoryComplete && !force)) return;
  const taskId = state.selectedId;
  const requestId = ++state.explorationRequestId;
  state.explorationLoading = true;
  state.explorationLoadError = "";
  renderExplorationMap();
  try {
    if (state.selectedId !== taskId || requestId !== state.explorationRequestId) return;
    const snapshot = await api(`/tasks/${encodeURIComponent(taskId)}/activity-map`);
    applyActivityMapSnapshot(snapshot);
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
  const junctionX = startX + Math.max(24, (endX - startX) * .45);
  return `M ${startX} ${startY} H ${junctionX} V ${endY} H ${endX}`;
}

function explorationRelations(nodeId) {
  return (state.explorationEdges || []).filter(edge => edge.sourceId === nodeId || edge.targetId === nodeId);
}

function explorationRelationLabel(kind) {
  const key = ({ contains: "explorationRelationContains", branch: "explorationRelationBranch", supports: "explorationRelationSupports", related: "explorationRelationRelated", supersedes: "explorationRelationSupersedes", fixed_by: "explorationRelationFixedBy", rollback: "explorationRelationRollback" })[kind];
  return key ? uiLabel(key) : uiLabel("explorationRelationRelated");
}

function renderExplorationDetail(node) {
  const detail = $("#exploration-map-detail");
  if (!detail) return;
  if (!node) {
    detail.innerHTML = `<div class="exploration-detail-empty"><span class="exploration-map-symbol large" aria-hidden="true"><i></i><i></i><i></i></span><strong>${esc(uiLabel("explorationSelectNode"))}</strong><p>${esc(uiLabel("explorationSelectNodeDescription"))}</p></div>`;
    return;
  }
  const meta = explorationMeta(node.status);
  const evidence = (node.evidence || []).map(value => `<li>${esc(value)}</li>`).join("");
  const files = (node.files || []).map(value => `<li><code>${esc(value)}</code></li>`).join("");
  const commands = (node.commands || []).map(value => `<li><code>${esc(value)}</code></li>`).join("");
  const typeLabel = uiLabel(explorationKindKey(node.kind));
  const relations = explorationRelations(node.id).map(edge => {
    const otherId = edge.sourceId === node.id ? edge.targetId : edge.sourceId;
    const other = explorationNodeById(otherId);
    if (!other) return "";
    const direction = edge.sourceId === node.id ? "→" : "←";
    return `<button type="button" data-exploration-select="${esc(otherId)}"><small>${direction} ${esc(explorationRelationLabel(edge.kind))}</small><span>${esc(explorationTitle(other))}</span></button>`;
  }).join("");
  detail.innerHTML = `<div class="exploration-detail-head"><span class="exploration-node-state ${esc(node.status)}">${meta.symbol} ${esc(meta.label)}</span><small>${esc(typeLabel)}</small></div><h3>${esc(explorationTitle(node))}</h3>${node.summary ? `<p class="exploration-detail-summary">${esc(node.summary)}</p>` : ""}<dl class="exploration-detail-facts"><div><dt>${esc(uiLabel("explorationTime"))}</dt><dd>${node.time ? esc(shortDate(node.time)) : esc(uiLabel("explorationCurrentSession"))}</dd></div><div><dt>${esc(uiLabel("explorationImportance"))}</dt><dd>${Number(node.score) || 0} / 10</dd></div><div><dt>${esc(uiLabel("explorationErrors"))}</dt><dd>${Number(node.failures) || 0}</dd></div></dl>${relations ? `<section class="exploration-relations"><h4>${esc(uiLabel("explorationRelations"))}</h4><div>${relations}</div></section>` : ""}${evidence ? `<section><h4>${esc(uiLabel("explorationEvidence"))}</h4><ul>${evidence}</ul></section>` : ""}${files ? `<section><h4>${esc(uiLabel("explorationFiles"))}</h4><ul>${files}</ul></section>` : ""}${commands ? `<details><summary>${esc(uiLabel("explorationCommands", { count: (node.commands || []).length }))}</summary><ul>${commands}</ul></details>` : ""}<div class="exploration-detail-actions"><button type="button" class="primary" data-exploration-jump="${esc(node.id)}">${esc(uiLabel("explorationJump"))}</button></div>`;
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

function frameExplorationNodes(ids, maxZoom = .95) {
  const viewport = $("#exploration-map-viewport");
  const canvas = $("#exploration-map-canvas");
  const world = canvas?.querySelector(".exploration-map-world");
  if (!viewport || !canvas || !world) return;
  const elements = ids.map(id => world.querySelector(`[data-exploration-node="${CSS.escape(id)}"]`)).filter(Boolean);
  if (!elements.length) return;
  const left = Math.min(...elements.map(element => element.offsetLeft));
  const top = Math.min(...elements.map(element => element.offsetTop));
  const right = Math.max(...elements.map(element => element.offsetLeft + element.offsetWidth));
  const bottom = Math.max(...elements.map(element => element.offsetTop + element.offsetHeight));
  const padding = 28;
  const zoom = clampExplorationZoom(Math.min(maxZoom, (viewport.clientWidth - padding * 2) / Math.max(1, right - left), (viewport.clientHeight - padding * 2) / Math.max(1, bottom - top)));
  explorationZoom = zoom;
  const width = Number(world.dataset.width) || world.offsetWidth;
  const height = Number(world.dataset.height) || world.offsetHeight;
  canvas.style.width = `${width * zoom}px`;
  canvas.style.height = `${height * zoom}px`;
  world.style.transform = `scale(${zoom})`;
  const label = $("#exploration-zoom-reset");
  if (label) label.textContent = `${Math.round(zoom * 100)}%`;
  viewport.scrollLeft = ((left + right) / 2) * zoom - viewport.clientWidth / 2;
  viewport.scrollTop = ((top + bottom) / 2) * zoom - viewport.clientHeight / 2;
}

function frameExplorationBranch(id) {
  const node = explorationNodeById(id);
  if (!node) return;
  const parent = explorationNodeById(node.parentId);
  if (!parent) {
    frameExplorationNodes([node.id]);
    return;
  }
  const siblings = (state.explorationNodes || []).filter(candidate => candidate.parentId === parent.id);
  frameExplorationNodes([parent.id, ...siblings.map(candidate => candidate.id)]);
}

function fitExplorationTree() {
  frameExplorationNodes((state.explorationNodes || []).map(node => node.id), 1);
}

function renderExplorationMap(options = {}) {
  const nodes = state.explorationNodes || [];
  const legend = $("#exploration-map-legend");
  if (legend) legend.textContent = uiLabel("explorationLegend");
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
    historyStatus.textContent = state.explorationLoadError ? uiLabel("explorationIndexFailed", { error: state.explorationLoadError }) : state.explorationLoading ? uiLabel("explorationIndexLoading") : state.explorationMapStatus === "building" ? uiLabel("explorationIndexBuilding", { count: state.explorationProcessedEvents }) : state.explorationHistoryComplete ? uiLabel("explorationIndexReady", { count: state.explorationLoadedEventCount }) : uiLabel("explorationIndexWaiting");
  }
  if (!nodes.length) {
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.innerHTML = `<div class="exploration-empty"><span class="exploration-map-symbol large" aria-hidden="true"><i></i><i></i><i></i></span><strong>${esc(uiLabel("explorationNoPath"))}</strong><p>${esc(uiLabel("explorationNoPathDescription"))}</p></div>`;
    renderExplorationDetail(null);
    return;
  }

  const layout = explorationLayout(nodes, state.explorationEdges || []);
  const viewport = $("#exploration-map-viewport");
  const viewportHeight = viewport?.clientHeight || 0;
  const viewportWidth = viewport?.clientWidth || 0;
  const verticalOffset = Math.max(40, (viewportHeight - 86) / 2);
  const horizontalOffset = Math.max(48, (viewportWidth - 194) / 2);
  layout.width = Math.max(layout.width, horizontalOffset * 2 + layout.maxDepth * 244 + 194);
  layout.height = Math.max(layout.height, verticalOffset * 2 + layout.maxRow * 126 + 86);
  const coordinates = new Map();
  for (const node of nodes) {
    const position = layout.positions.get(node.id);
    coordinates.set(node.id, { x: horizontalOffset + position.depth * 244, y: verticalOffset + position.row * 126 });
  }
  const selectedRelations = new Set(explorationRelations(state.explorationSelectedNodeId).map(edge => edge.id));
  const connectedIds = new Set([state.explorationSelectedNodeId]);
  for (const edge of explorationRelations(state.explorationSelectedNodeId)) { connectedIds.add(edge.sourceId); connectedIds.add(edge.targetId); }
  const graphEdges = state.explorationEdges?.length ? state.explorationEdges : nodes.filter(node => node.parentId).map(node => ({ id: `parent-${node.id}`, sourceId: node.parentId, targetId: node.id, kind: "branch" }));
  const edges = graphEdges.map(edge => {
    const parent = coordinates.get(edge.sourceId);
    const child = coordinates.get(edge.targetId);
    if (!parent || !child) return "";
    const related = ["related", "supersedes", "fixed_by"].includes(edge.kind);
    const path = related ? `M ${parent.x + 97} ${parent.y + 43} C ${parent.x + 150} ${parent.y - 48}, ${child.x + 44} ${child.y + 134}, ${child.x + 97} ${child.y + 43}` : explorationEdgePath(parent, child);
    const focus = selectedRelations.has(edge.id) ? " focused" : state.explorationSelectedNodeId ? " muted" : "";
    return `<path class="exploration-edge relation-${esc(edge.kind)}${focus}" d="${path}" />`;
  }).join("");
  const cards = nodes.map((node, index) => {
    const point = coordinates.get(node.id);
    const meta = explorationMeta(node.status);
    const selected = node.id === state.explorationSelectedNodeId;
    const time = node.time ? `<span class="exploration-node-time" style="left:${point.x}px;top:${Math.max(8, point.y - 22)}px">${esc(shortDate(node.time))}</span>` : "";
    const kindLabel = uiLabel(explorationKindKey(node.kind));
    const fileCount = node.files?.length ? ` · ${node.files.length} ${uiLabel("explorationFiles")}` : "";
    const relationClass = state.explorationSelectedNodeId && !connectedIds.has(node.id) ? " dimmed" : connectedIds.has(node.id) && !selected ? " connected" : "";
    return `${time}<button type="button" class="exploration-node kind-${esc(node.kind)} ${esc(node.status)}${selected ? " selected" : ""}${relationClass}" style="left:${point.x}px;top:${point.y}px" data-exploration-node="${esc(node.id)}" aria-pressed="${selected}"><span class="exploration-node-index"><i></i><b>${String(index + 1).padStart(2, "0")}</b></span><span class="exploration-node-copy"><strong>${esc(explorationTitle(node))}</strong><small><b>${esc(kindLabel)}</b> · ${meta.symbol} ${esc(meta.label)}${esc(fileCount)}</small></span></button>`;
  }).join("");
  canvas.style.width = `${layout.width * explorationZoom}px`;
  canvas.style.height = `${layout.height * explorationZoom}px`;
  const lanes = Array.from({ length: layout.laneCount }, (_, lane) => `<i class="exploration-galaxy-lane" style="top:${verticalOffset + lane * 378 - 32}px;width:${layout.width - horizontalOffset * 2}px;left:${horizontalOffset}px"></i>`).join("");
  canvas.innerHTML = `<div class="exploration-map-world" data-width="${layout.width}" data-height="${layout.height}" style="width:${layout.width}px;height:${layout.height}px;transform:scale(${explorationZoom})">${lanes}<svg class="exploration-edges" width="${layout.width}" height="${layout.height}" aria-hidden="true">${edges}</svg>${cards}</div>`;
  const zoomLabel = $("#exploration-zoom-reset");
  if (zoomLabel) zoomLabel.textContent = `${Math.round(explorationZoom * 100)}%`;

  let selected = explorationNodeById(state.explorationSelectedNodeId);
  if (!selected) {
    selected = explorationCurrentNode();
    state.explorationSelectedNodeId = selected?.id || "";
  }
  renderExplorationDetail(selected);
  if (options.frameBranch || explorationFramePending) {
    explorationFramePending = false;
    requestAnimationFrame(() => frameExplorationBranch(selected?.id || ""));
  }
  else if (options.reveal || ($("#exploration-follow-current")?.checked && options.liveUpdate)) requestAnimationFrame(() => revealExplorationNode(selected?.id || "", Boolean(options.liveUpdate)));
}

function openExplorationMap() {
  state.explorationOpen = true;
  explorationFramePending = true;
  state.explorationSelectedNodeId = explorationCurrentNode()?.id || "";
  const map = $("#exploration-map");
  map.classList.add("open");
  map.setAttribute("aria-hidden", "false");
  document.body.classList.add("exploration-map-visible");
  renderExplorationMap({ frameBranch: true });
  if (state.explorationNeedsSync) scheduleRenderChat();
  loadPrecomputedActivityMap();
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
});
$("#exploration-map-viewport")?.addEventListener("pointermove", event => {
  const drag = explorationDrag;
  if (!drag || drag.id !== event.pointerId) return;
  const dx = event.clientX - drag.x;
  const dy = event.clientY - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) < 4) return;
  if (!drag.moved) {
    drag.moved = true;
    event.currentTarget.setPointerCapture(event.pointerId);
  }
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
$("#exploration-map-viewport")?.addEventListener("pointerleave", () => {
  if (explorationDrag && !explorationDrag.moved) explorationDrag = null;
});
$("#exploration-map-viewport")?.addEventListener("click", event => {
  if (!suppressExplorationClick) return;
  suppressExplorationClick = false;
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);
$("#exploration-zoom-out")?.addEventListener("click", () => updateExplorationZoom(null, null, explorationZoom - .1));
$("#exploration-zoom-reset")?.addEventListener("click", () => updateExplorationZoom(null, null, 1));
$("#exploration-zoom-in")?.addEventListener("click", () => updateExplorationZoom(null, null, explorationZoom + .1));
$("#exploration-zoom-fit")?.addEventListener("click", fitExplorationTree);
$("#exploration-map-canvas")?.addEventListener("click", event => {
  const button = event.target.closest("[data-exploration-node]");
  if (!button) return;
  state.explorationSelectedNodeId = button.dataset.explorationNode;
  const current = explorationCurrentNode();
  if (current && current.id !== state.explorationSelectedNodeId) $("#exploration-follow-current").checked = false;
  renderExplorationMap();
});
$("#exploration-map-detail")?.addEventListener("click", event => {
  const select = event.target.closest("[data-exploration-select]");
  if (select) {
    state.explorationSelectedNodeId = select.dataset.explorationSelect;
    $("#exploration-follow-current").checked = false;
    renderExplorationMap({ reveal: true });
    return;
  }
  const button = event.target.closest("[data-exploration-jump]");
  if (button) jumpToExplorationNode(button.dataset.explorationJump);
});
$("#exploration-load-older")?.addEventListener("click", async () => {
  await loadPrecomputedActivityMap(true);
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && state.explorationOpen) { event.preventDefault(); closeExplorationMap(); }
});
window.addEventListener("languagechange", () => {
  if (state.explorationOpen) renderExplorationMap();
});
