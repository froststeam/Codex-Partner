// Shared Ace adapter for every editable text document in Codex Partner.
function textEditorMarkup(id) {
  return `<section class="editor-workspace" data-editor-workspace><header class="editor-toolbar"><div class="editor-toolbar-group"><button type="button" data-editor-action="undo" title="${esc(uiLabel("undo"))}" aria-label="${esc(uiLabel("undo"))}">↶</button><button type="button" data-editor-action="redo" title="${esc(uiLabel("redo"))}" aria-label="${esc(uiLabel("redo"))}">↷</button></div><div class="editor-toolbar-group" data-markdown-tools hidden><button type="button" data-editor-action="bold" title="${esc(uiLabel("bold"))}"><strong>B</strong></button><button type="button" data-editor-action="italic" title="${esc(uiLabel("italic"))}"><em>I</em></button><button type="button" data-editor-action="heading" title="${esc(uiLabel("heading"))}">H</button><button type="button" data-editor-action="link" title="${esc(uiLabel("link"))}">↗</button><button type="button" data-editor-action="code" title="${esc(uiLabel("inlineCode"))}">&lt;/&gt;</button><button type="button" data-editor-action="list" title="${esc(uiLabel("bulletList"))}">≡</button></div><button type="button" class="editor-wrap-button" data-editor-action="wrap" title="${esc(uiLabel("wrapLines"))}" aria-pressed="false">↵</button><div class="editor-view-switch" data-markdown-tools hidden><button type="button" class="active" data-editor-view="edit">${uiLabel("editView")}</button><button type="button" data-editor-view="split">${uiLabel("splitView")}</button><button type="button" data-editor-view="preview">${uiLabel("preview")}</button></div><span class="editor-toolbar-spacer"></span><span class="editor-mode" data-editor-mode></span><span class="editor-cursor" data-editor-cursor></span></header><div class="editor-stage" data-editor-stage data-view="edit"><div id="${esc(id)}" class="unified-editor"></div><div class="editor-preview memory-viewer" data-editor-preview hidden></div></div></section>`;
}

function detectEditorMode(filename = "", content = "") {
  const modelist = window.ace?.require("ace/ext/modelist");
  const detected = modelist?.getModeForPath(String(filename || ""))?.mode || "ace/mode/text";
  if (detected !== "ace/mode/text") return detected;

  const value = String(content || "");
  const trimmed = value.trimStart();
  if (/^[\[{]/.test(trimmed)) {
    try { JSON.parse(trimmed); return "ace/mode/json"; } catch (_) {}
  }
  if (/^#!.*\bpython\b/m.test(value) || /^\s*(?:def|class|from|import)\s+[\w.]/m.test(value)) return "ace/mode/python";
  if (/^#!.*\b(?:ba|z|k)?sh\b/m.test(value)) return "ace/mode/sh";
  if (/^\s*(?:<!doctype\s+html|<html\b)/i.test(value)) return "ace/mode/html";
  if (/^\s*<\?xml\b/i.test(value)) return "ace/mode/xml";
  if (/^(?:---\s*$|[\w.-]+:\s*\S)/m.test(value)) return "ace/mode/yaml";
  return "ace/mode/text";
}

function replaceEditorSelection(editor, prefix, suffix = "", placeholder = "") {
  const range = editor.getSelectionRange();
  const selected = editor.session.getTextRange(range);
  editor.session.replace(range, `${prefix}${selected || placeholder}${suffix}`);
  editor.focus();
}

function applyMarkdownAction(editor, action) {
  if (action === "bold") return replaceEditorSelection(editor, "**", "**", uiLabel("bold"));
  if (action === "italic") return replaceEditorSelection(editor, "_", "_", uiLabel("italic"));
  if (action === "heading") return replaceEditorSelection(editor, "## ", "", uiLabel("heading"));
  if (action === "link") return replaceEditorSelection(editor, "[", "](https://)", uiLabel("link"));
  if (action === "code") return replaceEditorSelection(editor, "`", "`", "code");
  if (action === "list") {
    const range = editor.getSelectionRange();
    const selected = editor.session.getTextRange(range) || uiLabel("bulletList");
    editor.session.replace(range, selected.split("\n").map(line => `- ${line}`).join("\n"));
    editor.focus();
  }
}

function mountTextEditor(selector, options = {}) {
  if (!window.ace) throw Error(uiLabel("editorUnavailable"));
  const element = typeof selector === "string" ? $(selector) : selector;
  if (!element) throw Error(uiLabel("editorUnavailable"));

  window.ace.config.set("basePath", "/vendor/ace");
  const editor = window.ace.edit(element);
  const mode = detectEditorMode(options.filename, options.value);
  editor.setTheme("ace/theme/tomorrow_night_eighties");
  editor.session.setMode(mode);
  editor.session.setUseWorker(false);
  editor.session.setUseWrapMode(["ace/mode/markdown", "ace/mode/text"].includes(mode));
  editor.setOptions({
    fontSize: "13px",
    readOnly: Boolean(options.readOnly),
    showPrintMargin: false,
    showFoldWidgets: true,
    tabSize: 2,
    useSoftTabs: true,
    wrapBehavioursEnabled: true,
  });
  editor.setValue(String(options.value || ""), -1);
  if (options.placeholder) editor.setOption("placeholder", options.placeholder);
  editor.renderer.setScrollMargin(10, 10);
  editor.container.dataset.mode = mode.replace("ace/mode/", "");
  const workspace = editor.container.closest("[data-editor-workspace]");
  const cursor = workspace?.querySelector("[data-editor-cursor]");
  const modeLabel = workspace?.querySelector("[data-editor-mode]");
  const wrapButton = workspace?.querySelector('[data-editor-action="wrap"]');
  const markdown = mode === "ace/mode/markdown";
  if (modeLabel) modeLabel.textContent = mode.replace("ace/mode/", "").replaceAll("_", " ").toUpperCase();
  workspace?.querySelectorAll("[data-markdown-tools]").forEach(node => { node.hidden = !markdown; });
  if (wrapButton) {
    wrapButton.classList.toggle("active", editor.session.getUseWrapMode());
    wrapButton.setAttribute("aria-pressed", String(editor.session.getUseWrapMode()));
  }
  const updateCursor = () => {
    const position = editor.getCursorPosition();
    if (cursor) cursor.textContent = uiLabel("lineColumn", { line: position.row + 1, column: position.column + 1 });
  };
  editor.selection.on("changeCursor", updateCursor);
  updateCursor();
  if (options.readOnly) {
    workspace?.querySelectorAll('[data-editor-action]:not([data-editor-action="wrap"])').forEach(button => { button.disabled = true; });
  }

  let previewViewer = null;
  const renderPreview = () => {
    if (!markdown || !workspace) return;
    const preview = workspace.querySelector("[data-editor-preview]");
    if (!previewViewer) {
      previewViewer = window.toastui.Editor.factory({
        el: preview, viewer: true, initialValue: editor.getValue(), usageStatistics: false,
        ...(document.documentElement.dataset.theme === "light" ? {} : { theme: "dark" }),
      });
      markdownComponents.push(previewViewer);
    } else previewViewer.setMarkdown(editor.getValue());
  };
  let previewTimer = null;
  editor.session.on("change", () => {
    if (workspace?.querySelector("[data-editor-stage]")?.dataset.view === "edit") return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(renderPreview, 120);
  });
  workspace?.addEventListener("click", event => {
    const actionButton = event.target.closest("[data-editor-action]");
    if (actionButton) {
      const action = actionButton.dataset.editorAction;
      if (action === "undo") editor.undo();
      else if (action === "redo") editor.redo();
      else if (action === "wrap") {
        const enabled = !editor.session.getUseWrapMode();
        editor.session.setUseWrapMode(enabled);
        actionButton.classList.toggle("active", enabled);
        actionButton.setAttribute("aria-pressed", String(enabled));
      } else if (markdown) applyMarkdownAction(editor, action);
      editor.focus();
      return;
    }
    const viewButton = event.target.closest("[data-editor-view]");
    if (!viewButton) return;
    const view = viewButton.dataset.editorView;
    workspace.querySelector("[data-editor-stage]").dataset.view = view;
    workspace.querySelectorAll("[data-editor-view]").forEach(button => button.classList.toggle("active", button === viewButton));
    const preview = workspace.querySelector("[data-editor-preview]");
    preview.hidden = view === "edit";
    if (view !== "edit") renderPreview();
    requestAnimationFrame(() => editor.resize());
  });
  editor.commands.addCommand({
    name: "saveDocument",
    bindKey: { win: "Ctrl-S", mac: "Command-S" },
    exec: () => editor.container.closest("form")?.requestSubmit(),
  });
  markdownComponents.push(editor);
  requestAnimationFrame(() => { editor.resize(); if (!options.readOnly) editor.focus(); });
  return editor;
}
