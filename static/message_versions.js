"use strict";

// ── Message versions ─────────────────────────────────────────────────────────
// A message can have several versions: the original and any rerolls the
// export kept, plus at most one edit of each (the original is never changed).
// Each version and each edit has a fixed id (message_versions.py), so edits,
// hiding, sticky notes and message links stay on the right version after a
// re-import.
//
// Under the message, right aligned, a switcher steps through the versions:
// "‹ 1 / 3 ›", or, once the message has an edit, the version's name
// ("Original", "Edited", "Response 2 Edited", "Prompt 1"…) over "2 of 3".
// Hidden versions are skipped. With the "Show hidden message placeholder"
// setting on (or the header eye showing them for this chat) the last stop lists the hidden
// ones; a message with a single version shows a small "Hidden" marker.
// Hiding never changes anything but the version hidden.
//
// Each message also gets a ⋮ menu outside its header: Create Sticky Note,
// Copy Message Link, Copy Message, Edit Message, Hide Current Message.
//
// Leans on app.js globals: state, $, messagesEl, renderMessageBody,
// createChipsEl, createArtifactChips, showConvItemMenu, apiRequest.

const MV_ICON_PATHS = {
  // Google Material Symbols: visibility_off / visibility (inlined)
  visibility_off: "m644-428-58-58q9-47-27-88t-93-32l-58-58q17-8 34.5-12t37.5-4q75 0 127.5 52.5T660-500q0 20-4 37.5T644-428Zm128 126-58-56q38-29 67.5-63.5T832-500q-50-101-143.5-160.5T480-720q-29 0-57 4t-55 12l-62-62q41-17 84-25.5t90-8.5q151 0 269 83.5T920-500q-23 59-60.5 109.5T772-302Zm20 246L624-222q-35 11-70.5 16.5T480-200q-151 0-269-83.5T40-500q21-53 53-98.5t73-81.5L56-792l56-56 736 736-56 56ZM222-624q-29 26-53 57t-41 67q50 101 143.5 160.5T480-280q20 0 39-2.5t39-5.5l-36-38q-11 3-21 4.5t-21 1.5q-75 0-127.5-52.5T300-500q0-11 1.5-21t4.5-21l-84-82Zm319 93Zm-151 75Z",
  visibility: "M607.5-372.5Q660-425 660-500t-52.5-127.5Q555-680 480-680t-127.5 52.5Q300-575 300-500t52.5 127.5Q405-320 480-320t127.5-52.5Zm-204-51Q372-455 372-500t31.5-76.5Q435-608 480-608t76.5 31.5Q588-545 588-500t-31.5 76.5Q525-392 480-392t-76.5-31.5ZM214-281.5Q94-363 40-500q54-137 174-218.5T480-800q146 0 266 81.5T920-500q-54 137-174 218.5T480-200q-146 0-266-81.5ZM480-500Zm207.5 160.5Q782-399 832-500q-50-101-144.5-160.5T480-720q-113 0-207.5 59.5T128-500q50 101 144.5 160.5T480-280q113 0 207.5-59.5Z",
};

function mvIconSvg(name, className) {
  return `<svg class="${className}" viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true"><path d="${MV_ICON_PATHS[name]}"/></svg>`;
}

// The stop after the last visible version, listing the hidden ones.
const MV_HIDDEN_PAGE = { kind: "hidden-page", id: "__hidden__" };

// ── Conversation state ────────────────────────────────────────────────────────
// state.messageEdits: version id -> edit {id, version_uuid, content}
// state.hiddenItems: ids of hidden versions and edits
// state.hiddenOverride: the header eye's choice for the open chat, true
//   (show placeholders) / false (hide them) / null (follow the setting).
//   Runtime only, back to the setting on every open; never changes the setting.
// state.pendingMessageVersion: {seq, id, index?, reply?} from a message link
function resetMessageVersionsForConversation(data) {
  state.messageEdits = new Map((data.message_edits || []).map((e) => [e.version_uuid, e]));
  state.hiddenItems = new Set(data.hidden_items || []);
  state.hiddenOverride = null;
  syncHiddenHeaderButton();
}

function mvPlaceholdersOn() {
  if (state.hiddenOverride != null) return state.hiddenOverride;
  return state.preferences.showHiddenPlaceholders !== false;
}

// ── Building a message's versions ─────────────────────────────────────────────
// Ids match message_versions.row_version_ids on the server exactly.
function mvOwnVersions(msg) {
  const seq = msg.seq;
  const sibs = Array.isArray(msg.siblings) && msg.siblings.length > 1 ? msg.siblings : null;
  if (!sibs) {
    return {
      active: 0,
      set: [{ id: msg.message_uuid || `pos:${seq}:0`, content: msg.content || "", attachments: msg.attachments || [], artifact_ids: msg.artifact_ids || [] }],
      replies: [null],
    };
  }
  const active = Math.max(0, Math.min((msg.branch_index || sibs.length) - 1, sibs.length - 1));
  const set = sibs.map((sib, i) => ({
    id: sib.uuid || (i === active ? msg.message_uuid : null) || `pos:${seq}:${i}`,
    content: sib.content || "",
    attachments: sib.attachments || [],
    artifact_ids: sib.artifact_ids || [],
  }));
  const replies = sibs.map((sib, i) => (i === active || !("asst_content" in sib) ? null : {
    id: sib.asst_uuid || `pos:${seq}:${i}:reply`,
    content: sib.asst_content || "",
    attachments: sib.asst_attachments || [],
    artifact_ids: sib.asst_artifact_ids || [],
    noResponse: !sib.asst_content,
  }));
  // One array per reply, kept, so a switch back and forth is recognised.
  return { active, set, replies: replies.map((r) => (r ? [r] : null)) };
}

// The versions of a message in order: each original, then its edit if any.
function mvItems(slot) {
  const items = [];
  slot.set.forEach((version, i) => {
    items.push({ kind: "orig", i, id: version.id });
    const edit = state.messageEdits?.get(version.id);
    if (edit) items.push({ kind: "edit", i, id: edit.id });
  });
  return items;
}

function mvLabel(slot, item) {
  if (item.kind === "hidden-page") return "Hidden";
  const edited = item.kind === "edit";
  if (slot.set.length === 1) return edited ? "Edited" : "Original";
  const word = slot.role === "user" ? "Prompt" : "Response";
  return `${word} ${item.i + 1}${edited ? " Edited" : ""}`;
}

function mvContent(slot, item) {
  const version = slot.set[item.i];
  if (item.kind === "edit") {
    const edit = state.messageEdits.get(version.id);
    return { ...version, content: edit ? edit.content : version.content, noResponse: false };
  }
  return version;
}

const mvSame = (a, b) => !!a && !!b && a.kind === b.kind && a.id === b.id;

// ── Per-message decoration (called by renderConversationMessages) ─────────────
function decorateMessageVersions({ div, msg, bodyEl, roleEl, nameEl, metaRow, artifactsMeta }) {
  if (msg.seq == null) return;
  const own = mvOwnVersions(msg);
  const slot = {
    div, msg, bodyEl, roleEl, metaRow, artifactsMeta,
    seq: msg.seq,
    role: msg.role,
    own,
    set: own.set,
    active: own.active,
    replyMode: false,
    cur: null,
    hiddenSel: null,
    placeholderOpen: false,
    // The version whose reply and continuation show (null: the one the chat
    // continued from). Only the user's own moves change it, never hiding.
    follow: null,
    followShown: false,
    editing: null,
    shownKey: null,
  };
  div._mv = slot;

  // ⋮ outside the header: before the name on the assistant's messages, after
  // it on the user's.
  const menuBtn = document.createElement("button");
  menuBtn.type = "button";
  menuBtn.className = "conv-menu-btn message-menu-btn";
  menuBtn.title = "More";
  menuBtn.setAttribute("aria-label", "Message actions");
  menuBtn.setAttribute("aria-haspopup", "true");
  menuBtn.innerHTML =
    '<svg viewBox="0 0 4 16" width="4" height="16" aria-hidden="true">' +
    '<circle cx="2" cy="2" r="1.7"/><circle cx="2" cy="8" r="1.7"/>' +
    '<circle cx="2" cy="14" r="1.7"/></svg>';
  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    openMessageMenu(slot, menuBtn);
  });
  if (msg.role === "user") roleEl.appendChild(menuBtn);
  else roleEl.insertBefore(menuBtn, nameEl);

  // "Some Responses Hidden": leftmost of the message's icons.
  const eye = document.createElement("button");
  eye.type = "button";
  eye.className = "bookmark-msg-btn message-hidden-eye";
  eye.title = "Some Responses Hidden";
  eye.setAttribute("aria-label", "Some Responses Hidden");
  eye.innerHTML = mvIconSvg("visibility_off", "bookmark-msg-icon");
  eye.hidden = true;
  eye.addEventListener("click", () => {
    slot.cur = MV_HIDDEN_PAGE;
    slot.hiddenSel = null;
    slot.followShown = true;
    renderMessageVersion(slot);
  });
  messageIconSlot(div).prepend(eye);
  slot.eye = eye;

  // Hidden versions listed near the top of the message; the single-version
  // "Hidden" marker uses the same look.
  const hiddenRow = document.createElement("div");
  hiddenRow.className = "message-hidden-links";
  hiddenRow.hidden = true;
  metaRow.after(hiddenRow);
  slot.hiddenRow = hiddenRow;

  // Footer: Restore on the left, the switcher right aligned.
  const footer = document.createElement("div");
  footer.className = "message-version-footer";
  const restore = document.createElement("button");
  restore.type = "button";
  restore.className = "message-restore-btn";
  restore.textContent = "Restore";
  restore.hidden = true;
  restore.addEventListener("click", () => restoreDisplayed(slot));
  const nav = document.createElement("div");
  nav.className = "branch-nav";
  const prev = document.createElement("button");
  prev.type = "button";
  prev.className = "branch-btn";
  prev.textContent = "<";
  prev.title = "Previous version";
  prev.setAttribute("aria-label", "Previous version");
  const text = document.createElement("div");
  text.className = "branch-nav-text";
  const label = document.createElement("span");
  label.className = "branch-label";
  const counter = document.createElement("span");
  counter.className = "branch-counter";
  counter.setAttribute("aria-live", "polite");
  text.append(label, counter);
  const next = document.createElement("button");
  next.type = "button";
  next.className = "branch-btn";
  next.textContent = ">";
  next.title = "Next version";
  next.setAttribute("aria-label", "Next version");
  nav.append(prev, text, next);
  footer.append(restore, nav);
  div.appendChild(footer);
  Object.assign(slot, { footer, restore, nav, prev, next, navLabel: label, counter });
  prev.addEventListener("click", () => stepVersion(slot, -1));
  next.addEventListener("click", () => stepVersion(slot, 1));

  // app.js already drew the version the chat continued from; draw again only
  // when another one shows (an edit, a link to a version, a hidden one).
  const first = own.set[own.active];
  slot.shownKey = `orig:${first.id}:${first.content}`;
  applyPendingVersion(slot);
  renderMessageVersion(slot);
}

// The span that holds a message's own icons (hidden eye, sticky note),
// immediately before its bookmark icon.
function messageIconSlot(div) {
  let box = div.querySelector(":scope > .message-role > .message-icons");
  if (box) return box;
  box = document.createElement("span");
  box.className = "message-icons";
  const roleEl = div.querySelector(":scope > .message-role");
  const bm = roleEl?.querySelector(".bookmark-msg-btn:not(.message-hidden-eye):not(.sticky-note-msg-btn)");
  if (bm) bm.before(box);
  else if (div.classList.contains("user")) roleEl?.prepend(box);
  else roleEl?.appendChild(box);
  return box;
}

// After a page of messages is drawn: replies under the user's other versions,
// and what follows them, match what each user message shows.
function finishMessageVersions() {
  for (const div of messagesEl.querySelectorAll(":scope > .message.user")) {
    if (div._mv) syncReplyAndDownstream(div._mv);
  }
  const pending = state.pendingMessageVersion;
  if (pending?.reply) {
    const next = messagesEl.querySelector(`.message[data-seq="${pending.seq}"]`)?.nextElementSibling;
    if (next?._mv) {
      selectItemById(next._mv, pending.id);
      renderMessageVersion(next._mv);
    }
  }
  state.pendingMessageVersion = null;
  syncHiddenHeaderButton();
}

function applyPendingVersion(slot) {
  const pending = state.pendingMessageVersion;
  if (!pending || Number(pending.seq) !== Number(slot.seq) || !pending.id) return;
  if (pending.reply) {
    const item = mvItems(slot).find((it) => it.kind === "orig" && it.i === pending.index);
    if (item) selectItem(slot, item);
    slot.followShown = true;
    return;
  }
  if (selectItemById(slot, pending.id)) slot.followShown = true;
}

function selectItemById(slot, id) {
  const item = mvItems(slot).find((it) => it.id === id);
  if (!item) return false;
  selectItem(slot, item);
  return true;
}

// Show one item, hidden or not.
function selectItem(slot, item) {
  if (state.hiddenItems.has(item.id)) {
    slot.cur = MV_HIDDEN_PAGE;
    slot.hiddenSel = item.id;
    slot.placeholderOpen = true;
  } else {
    slot.cur = item;
  }
}

// ── Drawing the current version ───────────────────────────────────────────────
function mvWalk(slot) {
  const items = mvItems(slot);
  const hidden = items.filter((it) => state.hiddenItems.has(it.id));
  const visible = items.filter((it) => !state.hiddenItems.has(it.id));
  const placeholders = mvPlaceholdersOn();
  if (!placeholders) return { items, hidden, visible, walk: visible, mode: visible.length ? "walk" : "gone" };
  if (items.length === 1 && hidden.length === 1) return { items, hidden, visible, walk: [], mode: "placeholder" };
  return { items, hidden, visible, walk: hidden.length ? [...visible, MV_HIDDEN_PAGE] : visible, mode: "walk" };
}

// The version the chat continued from, as the user last edited it.
function defaultItem(slot, walk) {
  return walk.find((it) => it.kind === "edit" && it.i === slot.active)
    || walk.find((it) => it.kind === "orig" && it.i === slot.active)
    || walk[0]
    || null;
}

// What the message shows right now: {item, page} where page is the hidden
// list (item = the selected hidden one) or null.
function displayedItem(slot) {
  const info = mvWalk(slot);
  if (info.mode === "gone") return { info, item: null, page: null };
  if (info.mode === "placeholder") return { info, item: info.hidden[0], page: "placeholder" };
  let cur = info.walk.find((it) => mvSame(it, slot.cur)) || null;
  if (!cur) cur = defaultItem(slot, info.walk);
  slot.cur = cur;
  if (cur === MV_HIDDEN_PAGE) {
    const sel = info.hidden.find((it) => it.id === slot.hiddenSel) || info.hidden[0];
    slot.hiddenSel = sel.id;
    return { info, item: sel, page: "hidden" };
  }
  return { info, item: cur, page: null };
}

function renderMessageVersion(slot) {
  const { info, item, page } = displayedItem(slot);
  if (slot.followShown) {
    slot.followShown = false;
    if (item && page !== "placeholder") slot.follow = item.i;
  }
  slot.div.hidden = info.mode === "gone";
  if (info.mode === "gone") return;

  const hasEdit = info.items.some((it) => it.kind === "edit");
  const showContent = page !== "placeholder" || slot.placeholderOpen;

  // Hidden list / single "Hidden" marker, near the top of the message.
  slot.hiddenRow.innerHTML = "";
  if (page === "placeholder") {
    slot.hiddenRow.appendChild(hiddenLinkButton("Hidden", slot.placeholderOpen, () => {
      slot.placeholderOpen = !slot.placeholderOpen;
      renderMessageVersion(slot);
    }));
  } else if (page === "hidden") {
    for (const hiddenItem of info.hidden) {
      const selected = hiddenItem.id === item.id;
      slot.hiddenRow.appendChild(hiddenLinkButton(`${mvLabel(slot, hiddenItem)} Hidden`, selected, () => {
        slot.hiddenSel = hiddenItem.id;
        slot.followShown = true;
        renderMessageVersion(slot);
      }));
    }
  }
  slot.hiddenRow.hidden = !page;

  // Body and chips.
  const key = showContent && item ? `${item.kind}:${item.id}:${mvContent(slot, item).content}` : "";
  if (!slot.editing && key !== slot.shownKey) {
    slot.shownKey = key;
    drawVersionContent(slot, showContent ? item : null);
  }

  slot.restore.hidden = !(showContent && page) || !!slot.editing;

  // Switcher.
  const walk = info.walk;
  const pos = walk.findIndex((it) => mvSame(it, slot.cur));
  const showNav = walk.length > 1 && !slot.editing;
  slot.nav.hidden = !showNav;
  if (showNav) {
    slot.prev.hidden = pos <= 0;
    slot.next.hidden = pos >= walk.length - 1;
    const onHiddenPage = slot.cur === MV_HIDDEN_PAGE;
    slot.navLabel.hidden = !hasEdit && !onHiddenPage;
    slot.navLabel.textContent = mvLabel(slot, slot.cur);
    slot.counter.textContent = hasEdit ? `${pos + 1} of ${walk.length}` : `${pos + 1} / ${walk.length}`;
  }
  slot.footer.hidden = slot.nav.hidden && slot.restore.hidden;

  // "Some Responses Hidden" only for a message with several versions.
  slot.eye.hidden = !(mvPlaceholdersOn() && info.items.length > 1 && info.hidden.length);

  if (slot.role === "user") syncReplyAndDownstream(slot);
}

function hiddenLinkButton(text, pressed, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "message-hidden-link";
  b.textContent = text;
  b.setAttribute("aria-pressed", String(pressed));
  b.addEventListener("click", onClick);
  return b;
}

function drawVersionContent(slot, item) {
  const { div, bodyEl } = slot;
  bodyEl.hidden = !item;
  div.querySelectorAll(":scope > .file-chips, :scope > .artifact-chips").forEach((el) => el.remove());
  if (!item) return;
  const version = mvContent(slot, item);
  if (version.noResponse) {
    bodyEl.innerHTML = '<p class="branch-no-response">(Claude did not respond in this branch)</p>';
  } else {
    renderMessageBody(bodyEl, version.content);
  }
  if (version.attachments?.length) {
    const chips = createChipsEl(version.attachments);
    if (chips.children.length) {
      if (slot.role === "user") bodyEl.before(chips);
      else bodyEl.after(chips);
    }
  }
  if (slot.role === "assistant" && version.artifact_ids?.length) {
    const art = createArtifactChips(version.artifact_ids, slot.artifactsMeta);
    if (art) slot.footer.before(art);
  }
}

function stepVersion(slot, delta) {
  const { info } = displayedItem(slot);
  const pos = info.walk.findIndex((it) => mvSame(it, slot.cur));
  const next = info.walk[pos + delta];
  if (!next) return;
  slot.cur = next;
  slot.hiddenSel = null;
  slot.followShown = true;
  renderMessageVersion(slot);
}

// A user message's other versions keep their own reply, and the rest of the
// chat follows only the version the chat continued from. Both follow what the
// user chose to look at; hiding a version never changes them.
function syncReplyAndDownstream(slot) {
  if (slot.role !== "user" || slot.own.set.length < 2 || !slot.div.isConnected) return;
  const shownIdx = slot.follow ?? slot.active;
  const replyDiv = slot.div.nextElementSibling;
  const reply = replyDiv?.classList.contains("assistant") ? replyDiv._mv : null;
  if (reply) {
    const replySet = shownIdx !== slot.active ? slot.own.replies[shownIdx] : null;
    const want = replySet || reply.own.set;
    if (reply.set !== want) {
      reply.set = want;
      reply.active = replySet ? 0 : reply.own.active;
      reply.replyMode = !!replySet;
      reply.cur = null;
      reply.hiddenSel = null;
      reply.placeholderOpen = false;
      reply.shownKey = null;
      renderMessageVersion(reply);
    }
  }
  const showDownstream = shownIdx === slot.active;
  const all = Array.from(messagesEl.querySelectorAll(":scope > .message"));
  all.slice(all.indexOf(slot.div) + 2).forEach((el) => el.classList.toggle("branch-hidden", !showDownstream));
}

function refreshAllMessageVersions() {
  messagesEl?.querySelectorAll(":scope > .message").forEach((div) => {
    if (div._mv) renderMessageVersion(div._mv);
  });
  syncHiddenHeaderButton();
}

// ── Hide / Restore ────────────────────────────────────────────────────────────
async function setItemHidden(slot, item, hidden) {
  try {
    await apiRequest("/api/hidden-messages", {
      method: "POST",
      body: { conversation_id: state.activeId, item_id: item.id, hidden },
      action: hidden ? "Hide message" : "Restore message",
    });
  } catch (_) {
    return false;
  }
  if (hidden) state.hiddenItems.add(item.id);
  else state.hiddenItems.delete(item.id);
  return true;
}

async function hideDisplayed(slot) {
  const { item } = displayedItem(slot);
  if (!item || state.hiddenItems.has(item.id)) return;
  const walkBefore = mvWalk(slot).walk;
  const pos = walkBefore.findIndex((it) => mvSame(it, item));
  if (!(await setItemHidden(slot, item, true))) return;
  // Stay near where the user was: the next visible version, else the one before.
  const walk = mvWalk(slot).walk.filter((it) => it !== MV_HIDDEN_PAGE);
  slot.cur = walk[Math.min(pos, walk.length - 1)] || null;
  slot.placeholderOpen = false;
  renderMessageVersion(slot);
  syncHiddenHeaderButton();
}

async function restoreDisplayed(slot) {
  const { item } = displayedItem(slot);
  if (!item || !state.hiddenItems.has(item.id)) return;
  if (!(await setItemHidden(slot, item, false))) return;
  slot.cur = item;
  slot.placeholderOpen = false;
  renderMessageVersion(slot);
  syncHiddenHeaderButton();
}

// ── Header eye: shows or hides the hidden-message placeholders ───────────────
// It starts as the Chat Settings default (open eye: placeholders shown) and
// flips them for this chat only.
function syncHiddenHeaderButton() {
  const btn = $("hidden-messages-toggle-btn");
  if (!btn) return;
  const any = !!state.activeId && (state.hiddenItems?.size || 0) > 0;
  btn.hidden = !any;
  const on = mvPlaceholdersOn();
  btn.setAttribute("aria-pressed", String(on));
  btn.title = on ? "Hide hidden message placeholders" : "Show hidden message placeholders";
  btn.setAttribute("aria-label", btn.title);
  btn.innerHTML = mvIconSvg(on ? "visibility" : "visibility_off", "bookmark-nav-icon");
}

$("hidden-messages-toggle-btn")?.addEventListener("click", () => {
  state.hiddenOverride = !mvPlaceholdersOn();
  refreshAllMessageVersions();
});

// ── ⋮ menu ────────────────────────────────────────────────────────────────────
function openMessageMenu(slot, anchor) {
  const { item } = displayedItem(slot);
  const items = [
    { label: "Create Sticky Note", fn: () => window.stickyNotes?.create(slot.seq) },
    { label: "Copy Message Link", fn: () => copyMessageLink(slot, item) },
    { label: "Copy Message", fn: () => copyMessageText(slot, item) },
    { label: "Edit Message", fn: () => beginMessageEdit(slot) },
  ];
  if (item && !state.hiddenItems.has(item.id)) {
    items.push({ label: "Hide Current Message", fn: () => hideDisplayed(slot) });
  }
  showConvItemMenu({ id: `message:${slot.seq}` }, anchor, items);
}

function messageLinkFor(slot, item) {
  const id = item && !String(item.id).startsWith("pos:") ? item.id : slot.msg.message_uuid || null;
  return window.InternalLinks.messageLink(state.activeId, slot.seq, id);
}

function copyMessageLink(slot, item) {
  window.scratchPads?.copyLink(messageLinkFor(slot, item));
}

async function copyMessageText(slot, item) {
  const text = item ? mvContent(slot, item).content || "" : "";
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch (_) {
    const field = document.createElement("textarea");
    field.value = text;
    field.className = "sr-only";
    document.body.appendChild(field);
    field.select();
    try {
      copied = document.execCommand("copy");
    } catch (__) {
      copied = false;
    }
    field.remove();
  }
  window.scratchPads?.toast(copied ? "Message copied" : "Could not copy the message.");
}

// ── Edit Message ──────────────────────────────────────────────────────────────
// A plain text box in place of the message, the size the message takes up,
// with Cancel and Save under it. Nothing is saved until Save.
function beginMessageEdit(slot) {
  if (slot.editing) {
    slot.editing.textarea.focus();
    return;
  }
  const { item } = displayedItem(slot);
  if (!item) return;
  if (slot.bodyEl.hidden) {
    // A closed "Hidden" marker: show the message being edited.
    slot.placeholderOpen = true;
    renderMessageVersion(slot);
  }
  const version = slot.set[item.i];
  const edit = state.messageEdits.get(version.id);
  const startText = edit ? edit.content : version.content;
  const width = slot.bodyEl.offsetWidth;
  const height = slot.bodyEl.offsetHeight;

  const box = document.createElement("div");
  box.className = "message-edit";
  const textarea = document.createElement("textarea");
  textarea.className = "summary-textarea message-edit-textarea";
  textarea.setAttribute("aria-label", "Edit message");
  textarea.value = startText;
  // Measured: the same space the message takes up.
  textarea.style.setProperty("--message-edit-width", `${width}px`);
  textarea.style.setProperty("--message-edit-height", `${height}px`);
  const actions = document.createElement("div");
  actions.className = "message-edit-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "summary-btn";
  cancel.textContent = "Cancel";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "summary-btn summary-btn-primary";
  save.textContent = "Save";
  actions.append(cancel, save);
  box.append(textarea, actions);

  slot.bodyEl.hidden = true;
  slot.bodyEl.after(box);
  slot.editing = { box, textarea, version };
  renderMessageVersion(slot);
  textarea.focus();

  const syncSave = () => { save.disabled = !textarea.value.trim(); };
  syncSave();
  textarea.addEventListener("input", syncSave);
  cancel.addEventListener("click", () => endMessageEdit(slot));
  const doSave = async () => {
    const content = textarea.value;
    if (!content.trim()) return false;
    save.disabled = true;
    let res;
    try {
      res = await apiRequest("/api/message-edits", {
        method: "POST",
        body: { conversation_id: state.activeId, version_uuid: version.id, content },
        action: "Save message edit",
      });
    } catch (_) {
      syncSave();
      return false;
    }
    if (!res?.edit) {
      syncSave();
      return false;
    }
    state.messageEdits.set(version.id, res.edit);
    slot.cur = { kind: "edit", i: slot.set.indexOf(version), id: res.edit.id };
    slot.followShown = true;
    endMessageEdit(slot);
    return true;
  };
  slot.editing.save = doSave;
  save.addEventListener("click", doSave);
}

function messageEditIsDirty(slot) {
  const editing = slot.editing;
  if (!editing) return false;
  const edit = state.messageEdits.get(editing.version.id);
  return editing.textarea.value !== (edit ? edit.content : editing.version.content);
}

function openMessageEdits() {
  return [...(messagesEl?.querySelectorAll(":scope > .message") || [])]
    .map((div) => div._mv)
    .filter((slot) => slot && messageEditIsDirty(slot));
}

// Before the chat's messages are replaced: an unsaved edit asks first (Save
// or Cancel), the same prompt as Summaries. Resolves false on Cancel.
async function messageEditsNavigationGuard() {
  for (const slot of openMessageEdits()) {
    slot.div.scrollIntoView({ block: "center", behavior: preferredScrollBehavior() });
    const result = await openSummaryUnsavedModal();
    if (result === "cancel") return false;
    if (!(await slot.editing.save())) return false;
  }
  return true;
}

window.addEventListener("beforeunload", (event) => {
  if (openMessageEdits().length) {
    event.preventDefault();
    event.returnValue = "";
  }
});

function endMessageEdit(slot) {
  if (!slot.editing) return;
  slot.editing.box.remove();
  slot.editing = null;
  slot.shownKey = null;
  renderMessageVersion(slot);
}

window.messageVersions = {
  reset: resetMessageVersionsForConversation,
  refresh: refreshAllMessageVersions,
  iconSlot: messageIconSlot,
};
