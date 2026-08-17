(function exposeHistoryPagination(root) {
  const MESSAGE_TYPES = new Set([
    "userMessage",
    "browserMessage",
    "agentMessage",
    "slashCommand",
    "commandResult",
  ]);

  function eventPayload(event) {
    const payload = event?.payload;
    if (payload && typeof payload === "object") return payload;
    if (typeof payload !== "string") return {};
    try { return JSON.parse(payload); } catch (_) { return {}; }
  }

  function timelineMessageCount(events) {
    return (Array.isArray(events) ? events : []).reduce(
      (count, event) => count + (MESSAGE_TYPES.has(eventPayload(event).type) ? 1 : 0),
      0,
    );
  }

  async function fetchEarlierTimelinePages(loadPage, options = {}) {
    const messageTarget = Math.max(1, Number(options.messageTarget) || 12);
    const maxPages = Math.max(1, Number(options.maxPages) || 8);
    let cursor = String(options.cursor || "");
    let hasMore = options.hasMore !== false;
    let items = [];
    let metrics = [];
    let pagesLoaded = 0;
    let messageCount = 0;

    while (hasMore && pagesLoaded < maxPages && messageCount < messageTarget) {
      const page = await loadPage(cursor);
      const pageItems = Array.isArray(page?.items) ? page.items : [];
      if (!pagesLoaded && Array.isArray(page?.metrics)) metrics = page.metrics;
      items = [...pageItems, ...items];
      messageCount += timelineMessageCount(pageItems);
      pagesLoaded += 1;

      const nextCursor = String(page?.next_cursor || "");
      hasMore = Boolean(page?.has_more && nextCursor && nextCursor !== cursor);
      cursor = nextCursor;
    }

    return {
      items,
      metrics,
      next_cursor: cursor,
      has_more: hasMore,
      pages_loaded: pagesLoaded,
      message_count: messageCount,
    };
  }

  root.HistoryPagination = { timelineMessageCount, fetchEarlierTimelinePages };
})(typeof self !== "undefined" ? self : globalThis);
