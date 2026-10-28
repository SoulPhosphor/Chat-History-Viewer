"use strict";

// ── Global Bookmarks and Global Message Sticky Notes ─────────────────────────
// A provider-scoped view of every conversation that carries bookmarks. Each
// conversation shows its full title with a chevron; expanding it reveals an
// indented, undecorated list of that conversation's bookmarks. Clicking a title
// opens the conversation; clicking a bookmark opens it scrolled to that message.
//
// The list loads 25 conversations at a time and auto-loads more as the user
// scrolls (bookmarks are not counted toward the page size). A sort control
// orders conversations by the last message the user sent (newest first by
// default), a "Collapse Bookmarks" toggle hides every bookmark list at once,
// and a search box narrows by title, bookmark name, or both.
//
// Global Message Sticky Notes is the same screen for message sticky notes:
// createGlobalHub below builds both, so a change to one reaches the other.
//
// Opening a conversation from here pushes a browser-history entry, so the Back
// button returns to this table at the same scroll position.
//
// Leans on globals from app.js: `state`, `$`, `openConversation`,
// `resolveConversationSidebarEl`, `hideAllPanels`, `rememberReturnTab`,
// `ensureSpecialTab`, `returnFromSpecialView`, `renderTabs`. Loaded after app.js.

const GB_PAGE = 25;

const GB_CHEVRON_SVG =
  '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
  '<path d="M6 4l4 4-4 4" stroke="currentColor" stroke-width="1.6" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

// One screen builder for Global Bookmarks and Global Message Sticky Notes:
// the same layout, controls, paging and Back behaviour; only the list's
// items, wording and endpoint differ. cfg:
//   view, tabTitle, endpoint, action, itemsKey, navKey, ids {…},
//   text {loadError, empty, noMatch, meta(n), collapse, expand},
//   renderItem(conv, item, reload) -> element
function createGlobalHub(cfg) {
  const hs = {
    sort: "newest",
    scope: "both",
    q: "",
    collapse: false, // the Collapse toggle — default off (expanded)
    offset: 0,
    total: 0,
    loading: false,
    done: false,
    epoch: 0, // bumped on every reset so stale fetches are ignored
    items: [],
    expandedIds: new Set(),
  };

  const panel = $(cfg.ids.panel);
  const scroll = $(cfg.ids.scroll);
  const list = $(cfg.ids.list);
  const sentinel = $(cfg.ids.sentinel);
  const meta = $(cfg.ids.meta);
  const searchEl = $(cfg.ids.search);
  const scopeEl = $(cfg.ids.scope);
  const sortEl = $(cfg.ids.sort);
  const collapseEl = $(cfg.ids.collapse);

  // ── View lifecycle ──────────────────────────────────────────────────────────
  function showPanel() {
    rememberReturnTab();
    state.activeSpecialView = cfg.view;
    state.activeTabId = null;
    document
      .querySelectorAll(".conv-item.active")
      .forEach((el) => el.classList.remove("active"));
    state.activeId = null;
    if (typeof ensureSpecialTab === "function") {
      ensureSpecialTab(cfg.view, cfg.tabTitle);
    }
    hideAllPanels();
    if (panel) panel.hidden = false;
  }

  async function open(fromButton = false) {
    if (
      typeof summaryHasUnsavedChanges === "function" &&
      summaryHasUnsavedChanges()
    ) {
      const r = await openSummaryUnsavedModal();
      if (r === "cancel") return;
      if (r === "save") await saveAllUnsaved();
    }
    if (typeof closeSummaryPanel === "function") closeSummaryPanel();
    if (typeof leaveNotesForSpecialView === "function" && !(await leaveNotesForSpecialView())) return;
    if (fromButton && state.activeSpecialView === cfg.view) {
      await returnFromSpecialView();
      return;
    }
    showPanel();
    reload();
  }

  // Reload for the current provider without leaving the view (provider toggle).
  function reloadForProvider() {
    if (state.activeSpecialView !== cfg.view) return;
    reload();
  }

  // ── Loading ─────────────────────────────────────────────────────────────────
  function reset() {
    hs.epoch++;
    hs.items = [];
    hs.offset = 0;
    hs.total = 0;
    hs.done = false;
    hs.loading = false;
    hs.expandedIds.clear();
    if (list) list.innerHTML = "";
    if (scroll) scroll.scrollTop = 0;
  }

  function reload() {
    reset();
    loadMore();
  }

  function showLoading(on) {
    if (!sentinel) return;
    sentinel.innerHTML = on ? '<div class="gb-loading">Loading…</div>' : "";
  }

  function showError() {
    if (!sentinel) return;
    sentinel.innerHTML = "";
    const error = document.createElement("div");
    error.className = "gb-load-error";
    const message = document.createElement("span");
    message.textContent = cfg.text.loadError;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "gb-retry";
    retry.textContent = "Try Again";
    retry.addEventListener("click", () => loadMore());
    error.append(message, retry);
    sentinel.appendChild(error);
  }

  async function loadMore(allowAutoFill = true) {
    if (hs.loading || hs.done) return false;
    hs.loading = true;
    const epoch = hs.epoch;
    showLoading(true);

    const params = new URLSearchParams({
      provider: state.providerSide,
      sort: hs.sort,
      scope: hs.scope,
      q: hs.q,
      limit: String(GB_PAGE),
      offset: String(hs.offset),
    });

    let data;
    try {
      data = await apiRequest(`${cfg.endpoint}?${params}`, {
        action: cfg.action,
        notify: false,
      });
      if (!data || !Array.isArray(data.conversations)) {
        throw new Error("Invalid response");
      }
    } catch (_) {
      if (epoch === hs.epoch) {
        hs.loading = false;
        showError();
      }
      return false;
    }
    // A newer reset happened while this was in flight — drop the stale page.
    if (epoch !== hs.epoch) return;

    hs.total = data.total || 0;
    const convs = data.conversations || [];
    hs.offset += convs.length;
    if (convs.length < GB_PAGE || hs.offset >= hs.total) {
      hs.done = true;
    }

    for (const c of convs) {
      hs.items.push(c);
      if (!hs.collapse) hs.expandedIds.add(c.id);
      list.appendChild(renderConv(c));
    }

    if (!hs.items.length) {
      list.innerHTML = `<div class="gb-empty">${hs.q ? cfg.text.noMatch : cfg.text.empty}</div>`;
    }

    updateMeta();
    hs.loading = false;
    showLoading(false);
    if (allowAutoFill) maybeAutoFill();
    return true;
  }

  // Keep loading until the list overflows its container, so the scroll-triggered
  // observer has something to react to. Never runs while the panel is hidden
  // (its container has no height then, which would load every page at once).
  function maybeAutoFill() {
    if (hs.done || hs.loading) return;
    if (!panel || panel.hidden) return;
    if (!scroll) return;
    if (scroll.scrollHeight <= scroll.clientHeight + 4) {
      loadMore();
    }
  }

  function updateMeta() {
    if (!meta) return;
    const n = hs.total;
    meta.textContent = n === 0 ? "" : cfg.text.meta(n);
  }

  // Reload in place, keeping the scroll position (an item was deleted).
  function reloadInPlace() {
    restoreHistoryList(scroll ? scroll.scrollTop : 0, hs.offset);
  }

  // ── Rendering one conversation group ────────────────────────────────────────
  function renderConv(c) {
    const expanded = hs.expandedIds.has(c.id);

    const wrap = document.createElement("div");
    wrap.className = "gb-conv" + (expanded ? " expanded" : "");
    wrap.dataset.id = c.id;

    const head = document.createElement("div");
    head.className = "gb-conv-head";

    const titleBtn = document.createElement("button");
    titleBtn.type = "button";
    titleBtn.className = "gb-title";
    const title = c.title || "Untitled";
    titleBtn.textContent = title;
    titleBtn.title = title;
    titleBtn.addEventListener("click", () => openConversationFromHub(c.id, null));

    const chevron = document.createElement("button");
    chevron.type = "button";
    chevron.className = "gb-chevron";
    chevron.innerHTML = GB_CHEVRON_SVG;
    setChevronState(chevron, expanded);
    chevron.addEventListener("click", () => toggleConv(c.id, wrap, chevron));

    head.append(titleBtn, chevron);
    wrap.appendChild(head);

    const itemsWrap = document.createElement("div");
    itemsWrap.className = "gb-bookmarks";
    for (const item of c[cfg.itemsKey] || []) {
      itemsWrap.appendChild(cfg.renderItem(c, item, { open: openConversationFromHub, reload: reloadInPlace }));
    }
    wrap.appendChild(itemsWrap);
    return wrap;
  }

  function setChevronState(chevron, expanded) {
    chevron.setAttribute("aria-expanded", String(expanded));
    chevron.setAttribute("aria-label", expanded ? cfg.text.collapse : cfg.text.expand);
  }

  function toggleConv(id, wrap, chevron) {
    const expanded = !hs.expandedIds.has(id);
    if (expanded) hs.expandedIds.add(id);
    else hs.expandedIds.delete(id);
    wrap.classList.toggle("expanded", expanded);
    setChevronState(chevron, expanded);
  }

  // ── Navigation (Back returns to this table, same scroll position) ───────────
  // extra: what to do once the conversation is open (e.g. open a sticky note).
  async function openConversationFromHub(convId, seq, extra = null) {
    history.replaceState(
      {
        [cfg.navKey]: true,
        view: "gb-list",
        scrollTop: scroll ? scroll.scrollTop : 0,
        loadedCount: hs.offset,
      },
      "",
    );
    history.pushState({ [cfg.navKey]: true, view: "conversation", convId, seq, extra }, "");
    await openFromHistory(convId, seq, extra);
  }

  async function openFromHistory(convId, seq, extra) {
    const el =
      typeof resolveConversationSidebarEl === "function"
        ? resolveConversationSidebarEl(convId)
        : null;
    await openConversation(convId, el, seq != null ? seq : null);
    if (extra && state.activeId === convId) cfg.afterOpen?.(extra);
  }

  async function restoreHistoryList(scrollTop, loadedCount) {
    // Names and membership may have changed while the conversation was open.
    // Reload from the database instead of revealing the stale cached DOM, then
    // rebuild enough pages to restore the previous scroll position.
    reset();
    const target = Math.max(GB_PAGE, Number(loadedCount) || 0);
    while (!hs.done && hs.offset < target) {
      const loaded = await loadMore(false);
      if (!loaded) return;
    }
    if (typeof scrollTop === "number") {
      requestAnimationFrame(() => {
        if (scroll) scroll.scrollTop = scrollTop;
      });
    }
  }

  let restoringCanceledPopstate = false;
  window.addEventListener("popstate", async (e) => {
    const st = e.state;
    if (!st || !st[cfg.navKey]) return;
    if (restoringCanceledPopstate) {
      restoringCanceledPopstate = false;
      return;
    }
    if (st.view === "gb-list") {
      if (typeof leaveNotesForSpecialView === "function" && !(await leaveNotesForSpecialView())) {
        // popstate fires after the browser moves its pointer. Put it back on the
        // still-visible conversation entry when the user cancels leaving Notes.
        restoringCanceledPopstate = true;
        history.forward();
        return;
      }
      showPanel();
      if (typeof renderTabs === "function") renderTabs();
      restoreHistoryList(st.scrollTop, st.loadedCount);
    } else if (st.view === "conversation") {
      openFromHistory(st.convId, st.seq, st.extra);
    }
  });

  // ── Controls ────────────────────────────────────────────────────────────────
  let searchTimer = null;
  searchEl?.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      const v = searchEl.value.trim();
      if (v === hs.q) return;
      hs.q = v;
      reload();
    }, 220);
  });

  scopeEl?.addEventListener("change", () => {
    hs.scope = scopeEl.value;
    if (hs.q) reload();
  });

  sortEl?.addEventListener("change", () => {
    hs.sort = sortEl.value;
    reload();
  });

  collapseEl?.addEventListener("change", () => {
    hs.collapse = collapseEl.checked;
    if (hs.collapse) hs.expandedIds.clear();
    else hs.items.forEach((c) => hs.expandedIds.add(c.id));
    list?.querySelectorAll(".gb-conv").forEach((el) => {
      const exp = hs.expandedIds.has(el.dataset.id);
      el.classList.toggle("expanded", exp);
      const ch = el.querySelector(".gb-chevron");
      if (ch) setChevronState(ch, exp);
    });
  });

  // ── Auto-load on scroll ─────────────────────────────────────────────────────
  if (sentinel && "IntersectionObserver" in window) {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const en of entries) {
          if (en.isIntersecting) loadMore();
        }
      },
      { root: scroll || null, rootMargin: "300px 0px" },
    );
    observer.observe(sentinel);
  }

  return { open, reloadForProvider, panel };
}

// ── Global Bookmarks ──────────────────────────────────────────────────────────
// One bookmark: its name (opens the message) and the shared bookmark ⋮ menu
// (bookmarks.js openBookmarkMenu).
function gbRenderBookmark(c, bm, hub) {
  const row = document.createElement("div");
  row.className = "gb-bookmark-row";
  const b = document.createElement("button");
  b.type = "button";
  const paint = (current) => {
    const name = (current.name || "").trim();
    b.className = "gb-bookmark" + (name ? "" : " gb-unnamed") + (current.missing ? " gb-missing" : "");
    b.textContent = name || "Untitled bookmark";
    // Its message is no longer in the chat (re-imported without it): say so,
    // and open the chat itself rather than whatever message sits there now.
    if (current.missing) b.textContent += " (message no longer in this chat)";
    b.title = b.textContent;
  };
  paint(bm);
  b.addEventListener("click", () => hub.open(c.id, bm.missing ? null : bm.seq));
  const ctx = {
    conversationId: c.id,
    conversationTitle: c.title || "Untitled",
    bookmark: bm,
    onChanged: (next) => {
      if (next) {
        Object.assign(bm, next);
        paint(bm);
      } else {
        // Deleted: reload in place, keeping the scroll position. A chat whose
        // last bookmark went leaves the list.
        hub.reload();
      }
    },
  };
  row.append(b, bookmarkMenuButton(ctx));
  return row;
}

const globalBookmarksHub = createGlobalHub({
  view: "global_bookmarks",
  tabTitle: "Global Bookmarks",
  endpoint: "/api/global-bookmarks",
  action: "Load Global Bookmarks",
  itemsKey: "bookmarks",
  navKey: "gbNav",
  ids: {
    panel: "global-bookmarks-panel", scroll: "global-bookmarks-scroll",
    list: "global-bookmarks-list", sentinel: "gb-sentinel", meta: "global-bookmarks-meta",
    search: "gb-search", scope: "gb-search-scope", sort: "gb-sort", collapse: "gb-collapse-toggle",
  },
  text: {
    loadError: "Bookmarks could not be loaded.",
    empty: "No bookmarked conversations on this side yet.",
    noMatch: "No conversations or bookmarks match your search.",
    meta: (n) => `${n.toLocaleString()} conversation${n !== 1 ? "s" : ""} with bookmarks`,
    collapse: "Collapse bookmarks",
    expand: "Expand bookmarks",
  },
  renderItem: gbRenderBookmark,
});
const openGlobalBookmarks = (fromButton = false) => globalBookmarksHub.open(fromButton);
const gbReloadForProvider = () => globalBookmarksHub.reloadForProvider();

// ── Global Message Sticky Notes ───────────────────────────────────────────────
// The same screen for message sticky notes. A note opens its chat with the
// note open under its message (sticky_notes.js). Its ⋮ menu has the bookmark
// menu's items, acting on the note.
function gsnRenderNote(c, note, hub) {
  const row = document.createElement("div");
  row.className = "gb-bookmark-row";
  const b = document.createElement("button");
  b.type = "button";
  const paint = (current) => {
    b.className = "gb-bookmark" + (current.missing ? " gb-missing" : "");
    b.textContent = (current.title || "").trim() || "Untitled";
    if (current.missing) b.textContent += " (message no longer in this chat)";
    b.title = b.textContent;
  };
  paint(note);
  b.addEventListener("click", () => hub.open(c.id, note.missing ? null : note.seq, note.missing ? null : { noteId: note.id }));
  const actions = buildConvActions({ id: `sticky:${note.id}` }, (_, btn) => {
    const target = window.InternalLinks.messageLink(c.id, note.seq, note.message_uuid || null);
    const label = (note.title || "").trim() || `Sticky note in ${c.title || "Untitled"}`;
    showConvItemMenu({ id: `sticky:${note.id}` }, btn, [
      { label: "Add to Scratch Pad", fn: () => window.scratchPads?.addToPad(btn, { label, target }) },
      { label: "Copy Internal Link", fn: () => window.scratchPads?.copyLink(target) },
      { label: "Rename", fn: async () => {
        const next = await window.stickyNotes?.rename(c.id, note);
        if (next) { Object.assign(note, next); paint(note); }
      } },
      { label: "Delete", fn: async () => {
        if (await window.stickyNotes?.remove(c.id, note.id)) hub.reload();
      } },
    ]);
  });
  const menuBtn = actions.querySelector("button");
  menuBtn.title = "More";
  menuBtn.setAttribute("aria-label", `More actions for sticky note ${b.textContent}`);
  row.append(b, actions);
  return row;
}

const globalStickyHub = createGlobalHub({
  view: "global_sticky_notes",
  tabTitle: "Global Message Sticky Notes",
  endpoint: "/api/global-sticky-notes",
  action: "Load Global Message Sticky Notes",
  itemsKey: "sticky_notes",
  navKey: "gsnNav",
  ids: {
    panel: "global-sticky-panel", scroll: "global-sticky-scroll",
    list: "global-sticky-list", sentinel: "gsn-sentinel", meta: "global-sticky-meta",
    search: "gsn-search", scope: "gsn-search-scope", sort: "gsn-sort", collapse: "gsn-collapse-toggle",
  },
  text: {
    loadError: "Sticky notes could not be loaded.",
    empty: "No conversations with message sticky notes on this side yet.",
    noMatch: "No conversations or sticky notes match your search.",
    meta: (n) => `${n.toLocaleString()} conversation${n !== 1 ? "s" : ""} with sticky notes`,
    collapse: "Collapse sticky notes",
    expand: "Expand sticky notes",
  },
  renderItem: gsnRenderNote,
  afterOpen: ({ noteId }) => {
    const note = state.stickyNotes?.find((n) => n.id === noteId);
    if (note) window.stickyNotes?.goTo(note);
  },
});
const openGlobalStickyNotes = (fromButton = false) => globalStickyHub.open(fromButton);
const gsnReloadForProvider = () => globalStickyHub.reloadForProvider();

// ── Briefcase footer menu (Global Bookmarks / Projects / Memories / Media Hub /
//    Scratch Pads) ─
const bookmarksHubBtn = $("bookmarks-hub-btn");
const bookmarksHubMenu = $("bookmarks-hub-menu");

function positionBookmarksHubMenu() {
  const inset = themePixels("--popover-safe-inset");
  const gap = themePixels("--popover-menu-gap");
  const r = bookmarksHubBtn.getBoundingClientRect();
  bookmarksHubMenu.hidden = false;
  const mw = bookmarksHubMenu.offsetWidth;
  const mh = bookmarksHubMenu.offsetHeight;
  let left = r.left;
  if (left + mw > window.innerWidth - inset) left = window.innerWidth - mw - inset;
  if (left < inset) left = inset;
  let top = r.top - mh - gap;
  if (top < inset) top = r.bottom + gap;
  bookmarksHubMenu.style.left = `${left}px`;
  bookmarksHubMenu.style.top = `${top}px`;
}

function openBookmarksHubMenu() {
  positionBookmarksHubMenu();
  prepareActionMenu(bookmarksHubMenu, bookmarksHubBtn, closeBookmarksHubMenu);
  bookmarksHubBtn.setAttribute("aria-expanded", "true");
  document.addEventListener("mousedown", onBookmarksHubOutside, true);
  document.addEventListener("keydown", onBookmarksHubKey, true);
}

function closeBookmarksHubMenu() {
  if (!bookmarksHubMenu) return;
  restoreActionMenuFocus(bookmarksHubMenu);
  bookmarksHubMenu.hidden = true;
  bookmarksHubBtn?.setAttribute("aria-expanded", "false");
  document.removeEventListener("mousedown", onBookmarksHubOutside, true);
  document.removeEventListener("keydown", onBookmarksHubKey, true);
}

function onBookmarksHubOutside(e) {
  if (!bookmarksHubMenu.contains(e.target) && e.target !== bookmarksHubBtn) {
    closeBookmarksHubMenu();
  }
}

function onBookmarksHubKey(e) {
  if (e.key === "Escape") closeBookmarksHubMenu();
}

bookmarksHubBtn?.addEventListener("click", () => {
  if (bookmarksHubMenu.hidden) openBookmarksHubMenu();
  else closeBookmarksHubMenu();
});

bookmarksHubMenu?.querySelectorAll(".sidebar-menu-item").forEach((item) => {
  item.addEventListener("click", () => {
    const action = item.dataset.action;
    closeBookmarksHubMenu();
    if (action === "global-bookmarks") openGlobalBookmarks(true);
    else if (action === "global-sticky-notes") openGlobalStickyNotes(true);
    else if (action === "projects") openProjects(true);
    else if (action === "memories") openMemories(true);
    else if (action === "media-hub") openGallery(true);
    else if (action === "scratch-pads") openScratchPads(true);
  });
});
