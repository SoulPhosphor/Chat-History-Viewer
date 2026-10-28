"use strict";

// ── Sticky Notes ──────────────────────────────────────────────────────────────
// One note per message, shown inline under the message: the comment bank icon,
// the note's title (its own field, "Untitled", "Untitled 1"…) and the text in
// the shared visual editor (summary.js), links included. The formatting bar
// shows only while the note is being edited. Each note keeps a fixed id that
// never changes when its title or text does.
//
// A message with a note shows the comment bank icon left of its bookmark icon
// and the note's title on the date line, after any bookmark title (click it to
// rename). The header icon lists the notes with the bookmark rules: 1 → jump
// straight to it, 2–4 → a bar beneath the chat header, 5+ → a panel over the
// sidebar. Going to a note always opens it. Nothing about which notes are open
// is remembered across sessions.
//
// Leans on globals from app.js, summary.js and message_versions.js.

const STICKY_ICON_PATH =
  "M500-520v-280H160v525l46-45h594v-480H700v280l-100-60-100 60ZM80-80v-720q0-33 23.5-56.5T160-880h640q33 0 56.5 23.5T880-800v480q0 33-23.5 56.5T800-240H240L80-80Zm80-720v480-480Z";

function stickyIconSvg(className) {
  return `<svg class="${className}" viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true"><path d="${STICKY_ICON_PATH}"/></svg>`;
}

state.stickyNotes = state.stickyNotes || [];

// Which survey view is open: null | "bar" | "panel". Runtime only.
let _stickyView = null;
// Notes open under their message in this visit (ids). Runtime only.
const _openStickyNotes = new Set();
// Open editors by note id: {note, ed, section, saved}.
const _stickyEditors = new Map();

function stickyNoteById(id) {
  return state.stickyNotes.find((n) => n.id === id) || null;
}
function stickyNoteForSeq(seq) {
  return state.stickyNotes.find((n) => Number(n.seq) === Number(seq)) || null;
}
function orderedStickyNotes() {
  return [...state.stickyNotes].sort((a, b) => Number(a.seq) - Number(b.seq));
}
function stickyLabel(note) {
  return (note.title || "").trim() || "Untitled";
}
function updateLocalStickyNote(note) {
  const i = state.stickyNotes.findIndex((n) => n.id === note.id);
  if (i >= 0) state.stickyNotes[i] = note;
  else state.stickyNotes.push(note);
}
function stickyMessageEl(seq) {
  return messagesEl?.querySelector(`:scope > .message[data-seq="${seq}"]`) || null;
}

// ── API ───────────────────────────────────────────────────────────────────────
function apiCreateStickyNote(seq) {
  return apiRequest("/api/sticky-notes", {
    method: "POST",
    body: { conversation_id: state.activeId, seq },
    action: "Create sticky note",
  });
}
function apiUpdateStickyNote(id, body) {
  return apiRequest(`/api/sticky-notes/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
    action: "Save sticky note",
  });
}

// ── Per-message decoration (called by renderConversationMessages) ─────────────
function decorateMessageStickyNote({ div, seq }) {
  syncMessageStickyDom(seq, div);
  const note = stickyNoteForSeq(seq);
  if (note && _openStickyNotes.has(note.id)) openStickyNoteSection(note, { scroll: false });
}

// The icon left of the bookmark, and the title on the date line.
function syncMessageStickyDom(seq, div = stickyMessageEl(seq)) {
  if (!div) return;
  const note = stickyNoteForSeq(seq);
  let btn = div.querySelector(":scope > .message-role .sticky-note-msg-btn");
  let titleEl = div.querySelector(":scope > .message-meta-row .sticky-note-meta-title");
  if (!note) {
    btn?.remove();
    titleEl?.remove();
    return;
  }
  if (!btn) {
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "bookmark-msg-btn active sticky-note-msg-btn";
    btn.innerHTML = stickyIconSvg("bookmark-msg-icon");
    btn.addEventListener("click", () => {
      const current = stickyNoteForSeq(seq);
      if (!current) return;
      if (_stickyEditors.has(current.id)) closeStickyNoteSection(current.id);
      else openStickyNoteSection(current);
    });
    window.messageVersions.iconSlot(div).appendChild(btn);
  }
  btn.title = "Sticky Note";
  btn.setAttribute("aria-label", `Sticky note ${stickyLabel(note)}`);
  btn.setAttribute("aria-expanded", String(_stickyEditors.has(note.id)));

  const metaRow = div.querySelector(":scope > .message-meta-row");
  if (!titleEl && metaRow) {
    titleEl = document.createElement("span");
    titleEl.className = "bookmark-title has-name sticky-note-meta-title";
    makeKeyboardAction(titleEl, "Rename sticky note");
    titleEl.addEventListener("click", (e) => {
      if (titleEl.querySelector("input")) return;
      const current = stickyNoteForSeq(seq);
      if (!current) return;
      e.stopPropagation();
      beginStickyMetaRename(titleEl, current);
    });
    metaRow.appendChild(titleEl);
  }
  if (titleEl && !titleEl.querySelector("input")) renderStickyMetaTitle(titleEl, note);
}

function renderStickyMetaTitle(titleEl, note) {
  titleEl.textContent = stickyLabel(note);
  titleEl.setAttribute("role", "button");
  titleEl.tabIndex = 0;
  titleEl.dataset.noteId = note.id;
  titleEl.title = stickyLabel(note);
  titleEl.setAttribute("aria-label", `Rename sticky note ${stickyLabel(note)}`);
}

// Rename on the date line, the same way a bookmark title renames.
function beginStickyMetaRename(titleEl, note) {
  const input = document.createElement("input");
  input.type = "text";
  input.className = "bookmark-title-input";
  input.setAttribute("aria-label", "Sticky note title");
  input.value = note.title || "";
  titleEl.textContent = "";
  titleEl.removeAttribute("role");
  titleEl.removeAttribute("tabindex");
  titleEl.classList.add("editing");
  titleEl.appendChild(input);
  input.focus();
  input.select();
  input.addEventListener("click", (e) => e.stopPropagation());
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const retainFocus = document.activeElement === input;
    titleEl.classList.remove("editing");
    if (save) await saveStickyTitle(note.id, input.value);
    const current = stickyNoteById(note.id) || note;
    renderStickyMetaTitle(titleEl, current);
    if (retainFocus) titleEl.focus();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      finish(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(true));
}

// Titles save at once (Enter or leaving the field). An empty title keeps the
// one it had.
async function saveStickyTitle(id, value) {
  const note = stickyNoteById(id);
  const title = String(value || "").replace(/\s+/g, " ").trim();
  if (!note || !title || title === note.title) return;
  let res;
  try {
    res = await apiUpdateStickyNote(id, { title });
  } catch (_) {
    return;
  }
  if (!res?.note) return;
  const open = _stickyEditors.get(id);
  // Keep the text version the open editor is based on.
  updateLocalStickyNote({ ...note, title: res.note.title, updated_at: res.note.updated_at });
  if (open) open.note = stickyNoteById(id);
  stickyNoteChanged(id);
}

// Redraw everything that shows a note's title.
function stickyNoteChanged(id) {
  const note = stickyNoteById(id);
  if (!note) return;
  syncMessageStickyDom(note.seq);
  const open = _stickyEditors.get(id);
  if (open && document.activeElement !== open.titleInput) open.titleInput.value = note.title;
  refreshStickySurfaces();
}

// ── The note under its message ────────────────────────────────────────────────
const STICKY_TOOLBAR_HTML = `
  <select class="summary-format-select" data-format="normal" aria-label="Text style">
    <option value="normal">Normal</option>
    <option value="heading1">Heading 1</option>
    <option value="heading2">Heading 2</option>
    <option value="heading3">Heading 3</option>
  </select>
  <button type="button" class="summary-tool-btn" data-format="bold" title="Bold" aria-label="Bold"><strong>B</strong></button>
  <button type="button" class="summary-tool-btn" data-format="italic" title="Italic" aria-label="Italic"><em>I</em></button>
  <button type="button" class="summary-tool-btn" data-format="underline" title="Underline" aria-label="Underline"><u>U</u></button>
  <button type="button" class="summary-tool-btn" data-format="strike" title="Strikethrough" aria-label="Strikethrough"><s>S</s></button>
  <div class="summary-highlight-wrap">
    <button type="button" class="summary-tool-btn sticky-highlight-button" title="Highlight" aria-label="Highlight">▰</button>
    <div class="summary-highlight-menu" hidden>
      <div class="summary-palette" aria-label="Highlight palette"></div>
      <label class="summary-palette-custom">Custom <input type="color" class="summary-palette-picker" aria-label="Choose custom highlight color"></label>
      <button type="button" class="summary-palette-remove">Remove highlight</button>
    </div>
  </div>
  <button type="button" class="summary-tool-btn" data-format="bullet" title="Bulleted list" aria-label="Bulleted list">•≡</button>
  <button type="button" class="summary-tool-btn" data-format="numbered" title="Numbered list" aria-label="Numbered list">1≡</button>
  <button type="button" class="summary-tool-btn scratchpad-link-tool" data-format="link" title="Insert link" aria-label="Insert link" aria-haspopup="dialog" aria-expanded="false">
    <svg viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true"><path d="M440-280H280q-83 0-141.5-58.5T80-480q0-83 58.5-141.5T280-680h160v80H280q-50 0-85 35t-35 85q0 50 35 85t85 35h160v80ZM320-440v-80h320v80H320Zm200 160v-80h160q50 0 85-35t35-85q0-50-35-85t-85-35H520v-80h160q83 0 141.5 58.5T880-480q0 83-58.5 141.5T680-280H520Z"/></svg>
  </button>`;

function openStickyNoteSection(note, { scroll = true, focusTitle = false } = {}) {
  const div = stickyMessageEl(note.seq);
  if (!div) return null;
  _openStickyNotes.add(note.id);
  let open = _stickyEditors.get(note.id);
  if (open && !open.section.isConnected) {
    _stickyEditors.delete(note.id);
    open = null;
  }
  if (!open) open = buildStickyNoteSection(note, div);
  syncMessageStickyDom(note.seq, div);
  if (scroll) {
    open.section.scrollIntoView({ block: "nearest", behavior: preferredScrollBehavior() });
  }
  if (focusTitle) {
    open.titleInput.focus();
    open.titleInput.select();
  }
  return open;
}

function buildStickyNoteSection(note, div) {
  const section = document.createElement("section");
  section.className = "sticky-note";
  section.dataset.noteId = note.id;
  section.setAttribute("aria-label", "Sticky note");

  const head = document.createElement("div");
  head.className = "sticky-note-head";
  head.insertAdjacentHTML("beforeend", stickyIconSvg("sticky-note-icon"));
  const titleInput = document.createElement("input");
  titleInput.type = "text";
  titleInput.className = "scratchpad-title-input sticky-note-title";
  titleInput.maxLength = 200;
  titleInput.setAttribute("aria-label", "Sticky note title");
  titleInput.value = note.title;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "bookmark-panel-close sticky-note-close";
  close.title = "Close sticky note";
  close.setAttribute("aria-label", "Close sticky note");
  close.textContent = "✕";
  head.append(titleInput, close);

  const editorEl = document.createElement("div");
  editorEl.className = "summary-editor sticky-note-editor";
  const toolbar = document.createElement("div");
  toolbar.className = "summary-toolbar";
  toolbar.setAttribute("role", "toolbar");
  toolbar.setAttribute("aria-label", "Sticky note formatting");
  toolbar.innerHTML = STICKY_TOOLBAR_HTML;
  const visual = document.createElement("div");
  visual.className = "summary-visual sticky-note-visual";
  visual.contentEditable = "true";
  visual.setAttribute("role", "textbox");
  visual.setAttribute("aria-multiline", "true");
  visual.setAttribute("aria-label", "Sticky note");
  const input = document.createElement("textarea");
  input.className = "summary-textarea";
  input.setAttribute("aria-label", "Sticky note Markdown");
  input.hidden = true;
  input.value = note.body || "";
  const actions = document.createElement("div");
  actions.className = "sticky-note-actions";
  const status = document.createElement("span");
  status.className = "scratchpad-status sticky-note-status";
  status.setAttribute("role", "status");
  const revert = document.createElement("button");
  revert.type = "button";
  revert.className = "summary-btn";
  revert.textContent = "Revert";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "summary-btn summary-btn-primary";
  save.textContent = "Save";
  actions.append(status, revert, save);
  editorEl.append(toolbar, visual, input, actions);
  section.append(head, editorEl);
  div.appendChild(section);

  const open = { note, section, titleInput, toolbar, actions, status, revert, save, input, visual, saved: note.body || "", saving: null };
  const linkButton = toolbar.querySelector('[data-format="link"]');
  open.ed = createVisualEditor({
    visual,
    input,
    toolbar,
    highlightMenu: toolbar.querySelector(".summary-highlight-menu"),
    highlightButton: toolbar.querySelector(".sticky-highlight-button"),
    onChange: () => syncStickyEditor(open),
    isOpen: () => section.isConnected,
    onLinkCommand: () => window.scratchPads?.openLinkPopup(linkButton, {
      editor: open.ed,
      button: linkButton,
      markdown: () => input.value,
      focus: () => visual.focus(),
      insert: (range, label, target) => stickyInsertLink(open, range, label, target),
    }),
  });
  attachVisualEditor(open.ed);
  setEditorMode(open.ed, "visual");
  refreshPaletteControls(open.ed);
  _stickyEditors.set(note.id, open);

  // The formatting bar and Save show while the note is being edited (focus
  // in its text) or has unsaved changes.
  const editing = () => section.contains(document.activeElement) && document.activeElement !== titleInput
    && document.activeElement !== close;
  open.syncEditing = () => {
    const active = editing() || !toolbar.querySelector(".summary-highlight-menu").hidden;
    toolbar.hidden = !active;
    actions.hidden = !active && !stickyIsDirty(open) && !status.textContent;
  };
  section.addEventListener("focusin", () => open.syncEditing());
  section.addEventListener("focusout", () => setTimeout(() => open.syncEditing(), 0));
  open.syncEditing();
  syncStickyEditor(open);

  // Ctrl / Cmd + click follows a link, as in Scratch Pads.
  visual.addEventListener("click", (event) => {
    const leaf = event.target.closest(".visual-link");
    if (leaf && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      window.scratchPads?.follow(leaf.dataset.href);
    }
  });

  titleInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      titleInput.blur();
    } else if (e.key === "Escape") {
      e.preventDefault();
      titleInput.value = stickyNoteById(note.id)?.title || note.title;
      titleInput.blur();
    }
  });
  titleInput.addEventListener("blur", async () => {
    await saveStickyTitle(note.id, titleInput.value);
    titleInput.value = stickyNoteById(note.id)?.title || titleInput.value;
  });
  close.addEventListener("click", () => closeStickyNoteSection(note.id));
  revert.addEventListener("click", () => {
    clearEditorHistory(open.ed);
    input.value = open.saved;
    renderVisual(open.ed);
    status.textContent = "";
    syncStickyEditor(open);
  });
  save.addEventListener("click", () => saveStickyBody(open));
  return open;
}

function stickyInsertLink(open, range, label, target) {
  _editor = open.ed;
  open.visual.focus();
  const source = open.input.value;
  const start = Math.max(0, Math.min(range?.start ?? source.length, source.length));
  const end = Math.max(start, Math.min(range?.end ?? start, source.length));
  const result = window.SummaryEditorCore.insertLink(source, start, end, window.InternalLinks.cleanLabel(label), target);
  commitVisualSource(result.source, result.caret);
}

function stickyIsDirty(open) {
  return open.input.value !== open.saved;
}

function syncStickyEditor(open) {
  const dirty = stickyIsDirty(open);
  open.save.disabled = !dirty || !!open.saving;
  open.revert.disabled = !dirty || !!open.saving;
  if (dirty && !open.saving) open.status.textContent = "Unsaved changes";
  else if (!dirty && open.status.textContent === "Unsaved changes") open.status.textContent = "";
  open.syncEditing?.();
}

async function saveStickyBody(open) {
  if (open.saving) return open.saving;
  open.saving = (async () => {
    const text = open.input.value;
    open.status.textContent = "Saving…";
    syncStickyEditor(open);
    try {
      const res = await apiUpdateStickyNote(open.note.id, { body: text, base_version: open.note.version });
      if (!res?.note) throw new Error("no note");
      open.saved = text;
      open.note = res.note;
      updateLocalStickyNote(res.note);
      open.status.textContent = stickyIsDirty(open) ? "Unsaved changes" : "Saved";
      return true;
    } catch (error) {
      open.status.textContent = error?.status === 409
        ? "This sticky note was changed in another window. Your text is still here; copy it, then reopen the chat."
        : "Could not save. Your changes are still here; try Save again.";
      return false;
    } finally {
      open.saving = null;
      syncStickyEditor(open);
    }
  })();
  return open.saving;
}

// Close the note. Unsaved text asks first (the same prompt as Scratch Pads).
async function closeStickyNoteSection(id) {
  const open = _stickyEditors.get(id);
  if (open && stickyIsDirty(open)) {
    const result = await openSummaryUnsavedModal();
    if (result === "cancel") return;
    if (!(await saveStickyBody(open))) return;
  }
  _openStickyNotes.delete(id);
  _stickyEditors.delete(id);
  if (open) {
    const restore = open.section.contains(document.activeElement);
    open.section.remove();
    const note = stickyNoteById(id);
    if (note) {
      syncMessageStickyDom(note.seq);
      if (restore) stickyMessageEl(note.seq)?.querySelector(".sticky-note-msg-btn")?.focus();
    }
  }
}

// Before the chat's messages are replaced (another chat, another page or
// screen): unsaved note text asks first, with the same prompt as closing.
// Resolves false when the user cancels.
async function stickyNotesNavigationGuard() {
  for (const open of [..._stickyEditors.values()]) {
    if (!open.section.isConnected || !stickyIsDirty(open)) continue;
    open.section.scrollIntoView({ block: "center", behavior: preferredScrollBehavior() });
    const result = await openSummaryUnsavedModal();
    if (result === "cancel") return false;
    if (!(await saveStickyBody(open))) return false;
  }
  return true;
}

function stickyNotesHaveUnsavedChanges() {
  return [..._stickyEditors.values()].some((open) => open.section.isConnected && stickyIsDirty(open));
}

window.addEventListener("beforeunload", (event) => {
  if (stickyNotesHaveUnsavedChanges()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

// ── Create (⋮ → Create Sticky Note) ──────────────────────────────────────────
// Opens the note under the message with the cursor in its title. A message
// that already has its note opens that one.
async function createStickyNote(seq) {
  if (!state.activeId) return;
  let note = stickyNoteForSeq(seq);
  if (!note) {
    let res;
    try {
      res = await apiCreateStickyNote(seq);
    } catch (_) {
      return;
    }
    if (!res?.note) return;
    note = res.note;
    updateLocalStickyNote(note);
    refreshStickySurfaces();
  }
  openStickyNoteSection(note, { focusTitle: true });
}

// ── Going to a note ───────────────────────────────────────────────────────────
async function goToStickyNote(note) {
  if (!note) return;
  if (typeof notesNavigationGuard === "function") {
    const canLeaveNotes = await notesNavigationGuard({ endVisit: false });
    if (!canLeaveNotes) return;
  }
  if (messagesEl && messagesEl.hidden && typeof closeSummaryPanel === "function") closeSummaryPanel();
  if (!(await ensureConversationMessage(note.seq))) return;
  const open = openStickyNoteSection(note, { scroll: false });
  if (!open) return;
  open.section.scrollIntoView({ block: "center", behavior: preferredScrollBehavior() });
}

// ── Survey surfaces (header button, bar, panel) — the bookmark rules ─────────
function closeStickyViews() {
  _stickyView = null;
  if ($("sticky-bar")) $("sticky-bar").hidden = true;
  if ($("sticky-panel")) $("sticky-panel").hidden = true;
}

function renderStickyBar() {
  const links = $("sticky-bar-links");
  if (!links) return;
  links.innerHTML = "";
  for (const note of orderedStickyNotes()) {
    const a = document.createElement("button");
    a.type = "button";
    a.className = "bookmark-bar-link";
    a.textContent = stickyLabel(note);
    a.title = stickyLabel(note);
    a.addEventListener("click", () => goToStickyNote(note));
    links.appendChild(a);
  }
}

function renderStickyPanel() {
  const list = $("sticky-panel-list");
  if (!list) return;
  list.innerHTML = "";
  for (const note of orderedStickyNotes()) {
    const row = document.createElement("div");
    row.className = "bookmark-panel-row";
    const a = document.createElement("button");
    a.type = "button";
    a.className = "bookmark-panel-item";
    a.textContent = stickyLabel(note);
    a.title = stickyLabel(note);
    a.addEventListener("click", () => goToStickyNote(note));
    row.appendChild(a);
    list.appendChild(row);
  }
}

function refreshStickySurfaces() {
  const btn = $("sticky-notes-toggle-btn");
  const count = state.stickyNotes.length;
  if (btn) btn.hidden = count === 0 || !state.activeId;
  if (_stickyView === "bar") {
    if (count < 2 || count > 4) closeStickyViews();
    else renderStickyBar();
  } else if (_stickyView === "panel") {
    if (count < 5) closeStickyViews();
    else renderStickyPanel();
  }
}

function onStickyToggleClick() {
  const count = state.stickyNotes.length;
  if (count === 0) return;
  if (count === 1) {
    closeStickyViews();
    goToStickyNote(orderedStickyNotes()[0]);
    return;
  }
  if (count <= 4) {
    if (_stickyView === "bar") return closeStickyViews();
    $("sticky-panel").hidden = true;
    _stickyView = "bar";
    renderStickyBar();
    $("sticky-bar").hidden = false;
    return;
  }
  if (_stickyView === "panel") return closeStickyViews();
  $("sticky-bar").hidden = true;
  _stickyView = "panel";
  renderStickyPanel();
  const panel = $("sticky-panel");
  if (typeof raiseSidebarOverlay === "function") raiseSidebarOverlay(panel);
  panel.hidden = false;
}

// ── Conversation lifecycle ────────────────────────────────────────────────────
function resetStickyNotesForConversation(notes) {
  closeStickyViews();
  _openStickyNotes.clear();
  _stickyEditors.clear();
  state.stickyNotes = Array.isArray(notes) ? notes.slice() : [];
  refreshStickySurfaces();
}

$("sticky-notes-toggle-btn")?.addEventListener("click", onStickyToggleClick);
$("sticky-bar-close")?.addEventListener("click", closeStickyViews);
$("sticky-panel-close")?.addEventListener("click", closeStickyViews);

window.stickyNotes = {
  create: createStickyNote,
  goTo: goToStickyNote,
};
