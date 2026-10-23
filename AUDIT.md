# App Audit and Repair Plan — before adding API access

Date: 2026-10-18. Covers every Python file, the main front-end code
(`static/app.js` and the editor/link modules), and the database layout.

**How this was checked.** I read the code, then ran tests against a throwaway
copy of the app with fake data. Each finding says how sure I am:

- **Confirmed** — I reproduced it.
- **From the code** — clear from reading the code, but not run.
- **Needs a real export** — depends on what real Claude / ChatGPT export files
  contain, which I could not check here.

All 161 existing tests pass, and `scripts/check_styles.py` passes. None of the
problems below are covered by a test today.

**What "API access" means here.** The code already has fields waiting for it
("Notes to AI", "Notes from AI", summaries marked as written by "ai"), so I
assumed the next step is the app calling the Claude API to read chats and
write notes and summaries. Most of this report also applies if "API" means
letting other programs read the app's data.

**How to use this file.** Part A is the repair plan: work through the phases in
order and tick items off. Part B holds the full details of each finding,
grouped by the same phases. Finding numbers (#1, #2, …) are labels, not the
order of work; they stay fixed so references keep working.

---

**Status (session 11, 2026-10-21): the repair plan is finished.** Every
finding from #1 to #39 is fixed (sessions 1–10), and #40 was fixed in
session 11. The only items left are the user's own Phase 5 steps: checking
real exports and re-importing once. The Phase 7 plan in Part B is a plan
only; no API work has started.

---

# Session instructions (read first)

The repair work is split into sessions. To start one, copy the prompt from
[**Next session prompt**](#next-session-prompt) at the bottom of this file into
a new session.

## Rules for every session

1. **Read first:** this file, `CLAUDE.md` and `STYLE_GUIDE.md`. Then read only
   the code the session's findings point to. Part B gives file and line
   references, so there is no need to re-read the whole app.
2. **Do only this session's items.** If something else is found, add it to
   Part B as a new numbered finding, give it a place in the plan, and leave it
   unfixed.
3. **Keep the plan's order.** If an item turns out to need something from a
   later session, stop and say so in the pull request rather than working
   around it.
4. **Never touch the user's real data.** Test on throwaway copies with made-up
   data. Database changes go through the change system from #18 once it exists.
5. **Test before finishing:** run `python3 -m unittest discover -s tests` and
   `python scripts/check_styles.py`, plus any new tests for this session's
   fixes. Report failures with their output; don't hide them.
6. **Update this file before finishing:**
   - tick the finished boxes in Part A;
   - mark the session's row in the session table below as done;
   - add anything learned that later sessions need (a changed file name, a new
     helper to use, a step that moved);
   - replace the [Next session prompt](#next-session-prompt) at the bottom with
     the prompt for the following session, in the same format.
   - **paste that full prompt into your final chat reply** as a text block,
     so the user can copy it without opening this file. Saying "the prompt
     is at the bottom of AUDIT.md" is not enough.
7. **One pull request per session**, holding the fixes and this file's updates
   together. Once it is merged, the bottom of this file is ready for the next
   session.
8. **Ask Codex to review only substantial code changes.** Codex reviews cost
   the user money. Post `@codex review` only on a pull request with a
   substantial code change (new features, database changes, real bug fixes).
   Documentation, `.gitignore` and other small changes get no Codex review;
   merge them once CI passes. (User's rule, session 11.) When the review
   arrives, make the reasonable changes it asks for (anything that does not go
   against these rules, `CLAUDE.md` or the session's scope), push them to the
   same branch, and reply on each review comment you don't follow, saying
   why. Do this every code session without being asked.
   **Then merge the pull request yourself.** Once the review (if any) is
   handled and the tests (CI) pass on the latest commit, merge it into `main`. Don't
   leave it waiting for the user. If something blocks the merge (a failing
   check you can't fix, a conflict, a question only the user can answer),
   say what it is instead.
9. **If the session runs low on room**, finish the item in progress, update
   this file to say exactly where work stopped, and write the next prompt so it
   continues from there.

## Session table

| Session | Work | Plan items | Status |
|---|---|---|---|
| 1 | Phase 0 + Phase 1: preparation and safety net | #28, #29, #30, #14, #20, #26, #22, #21, #18 (#6: README advice only) | Done (user still does the manual backup and git email) |
| 2 | Phase 2, server side | #27, #3, #1 (not the security header yet), #9, #10 | Done |
| 3 | Phase 2, browser side | #23, #35, #2, #15, then the security header from #1 | Done |
| 4 | Phase 2, the updater | #16 | Done (user publishes release v1.3.0 after merging) |
| 5 | Phase 3: one set of query rules | #25, #13 | Done (user publishes release v1.4.0 after merging) |
| 6 | Phase 4, part 1: move user data, one purge list | #6, #7 | Done (user publishes release v1.5.0 after merging) |
| 7 | Phase 4, part 2: remaining database changes | #5, #11, #34, #19 (data items), #39 | Done (user publishes release v1.6.0 after merging) |
| 8 | Phase 5, part 1: importer fixes | #4, #12, #8, #31, #24 | Done (user publishes release v1.7.0 after merging) |
| 9 | Phase 5, part 2: background jobs | #33 | Done (user publishes release v1.8.0 after merging) |
| — | **User:** check with real exports, then re-import once | Phase 5 last two items | Not started |
| 10 | Phase 6: speed (optional before the API) | #32, #38, #17, #19 (polish items), #36, #37 | Done (version 1.9.0) |
| 11 | Phase 7 planning: API plan and open decisions | API checklist, Phase 7 plan | Done (planning only; no code, no release) |
| 12 | Phase 7 step 1: AI foundation (no product decisions) | Phase 7 plan, step 1 | Not started |
| 13+ | Phase 7 steps 2–4: first AI task, many chats, request log | Phase 7 plan, steps 2–4 | Waiting on the user's answers (D1–D11) |

**User-only step after session 4:** the updater now installs published
GitHub releases only. After session 4's pull request is merged, publish a
release on GitHub with the tag `v1.3.0` on the merged `main` commit (GitHub →
Releases → Draft a new release). Do the same with each later session's
version (`v` plus the number in `version.py`); until a release exists, Check
for Updates says no released version was found.

**User-only steps in session 1:** the manual backup of `history.db`,
`userdata.db` and `source/` (Phase 0) has to be done by you on your own
machine, before merging anything from session 1.

---

# Part A — Repair plan

## The rules behind this order

Each step below sits after everything it depends on. The main dependencies:

1. **Back up before changing anything.** Several fixes change the database. A
   copy made first means a mistake costs minutes, not data.
2. **The safety net comes before repairs.** Tests that run on every push, an
   error log and automatic backups mean each later fix is checked and
   recoverable.
3. **Build the database change system (#18) before any change to the
   database.** Moving tables (#6), the artifact key (#5), tag IDs (#11) and note
   versions (#34) all change the database. Doing them without numbered,
   recorded steps means redoing them later.
4. **Write the endpoint list (#27) before the security checks (#1).** You can't
   protect endpoints you haven't listed.
5. **Bring KaTeX into the app (#15) before the Content-Security-Policy in #1.**
   That policy would block KaTeX from the internet and break math.
6. **The server's error replies (#9) come before the browser's error handling
   (#23) and the Error Log page (#35).** The browser can only show and log a
   failure the server reports clearly.
7. **Put the shared query rules (#25) in one place before moving tables (#6).**
   Once the rules live in one place, moving a table means changing one helper,
   not about 50 queries.
8. **Fix rebuild (#6) before rebuilding anything.** Rebuild currently wipes
   renames and archive state. The artifact fix (#5) needs a rebuild, so #6
   must be done first.
9. **Finish every importer fix before re-importing — then re-import once.**
   #4, #5 and #12 all need a fresh import to recover data. Doing them one at a
   time means three re-imports.
10. **Build one background-job system (#33) and use it for both imports and AI
    runs.** Building it twice is the backwards step to avoid.

## Phase 0 — Before touching any code

No code changes yet; nothing here can break anything.

- [ ] **Make a manual backup** of `history.db`, `userdata.db` and the whole
      `source/` folder. Keep it outside the app folder. *(User step.)*
- [x] **Stop using "rebuild the index".** Remove that advice from the README
      troubleshooting list until #6 is fixed. (#6) *(Session 6 fixed #6; the
      README explains the safe rebuild again.)*
- [x] **Choose the minimum Python version.** Recommended: 3.12+. Every later
      fix and the API SDK depend on this choice. (#14)
- [x] **Start a version number and `CHANGELOG.md`**, so each phase can be
      released and identified. (#30)
- [x] **Remove the leftover `claude.json`.** (#28)
- [ ] **Set git to your GitHub "noreply" email** for future commits. (#29)
      *(User step.)*

## Phase 1 — Safety net

Everything after this is tested, logged and backed up automatically.

- [x] **Build every path from the app's folder.** Comes first because backups,
      logs and tests all need to find the right files. (#20)
- [x] **Python version check at start-up, plus `requirements.txt`.** (#14)
- [x] **Run tests automatically on every push**, with GitHub Actions running
      the tests and `scripts/check_styles.py`. Add tests that record how labels,
      bulk labels and snapshots behave today, so later phases can't change
      them by accident. (#26)
- [x] **Error log file**, with every field in the #22 table, the privacy
      rule, repeats grouped, and the 200-group limit. (#22)
- [x] **Automatic backups of both databases**: on start-up and before imports,
      bulk changes, restores and database changes. Keep the last few copies.
      (#21)
- [x] **Database change system.** A `schema_version` in each database, numbered
      change steps, and failures reported instead of hidden. Phase 4 depends on
      this. (#18)

### Session 1 notes for later sessions

What exists now and must be used, not rebuilt:

- **Paths:** `app_paths.py` (`APP_DIR`, `DB_PATH`, `SOURCE_DIR`, `LOG_DIR`).
  Any new file location is built from `APP_DIR`. Older `Path(__file__).parent`
  uses in `server.py` were left as they were (they already point at the app
  folder).
- **Version:** `version.py` (`__version__`, now `1.0.0`) and `CHANGELOG.md`.
  Each session adds a CHANGELOG entry and raises the version.
- **Database changes (#18):** `schema_changes.py`. Each database has a
  `schema_version` table. The steps live in `server.py` as
  `HISTORY_SCHEMA_STEPS` and `USERDATA_SCHEMA_STEPS` (both at 1: step 1 holds
  every column and table rebuild that existed before). **Phase 4 adds its
  changes as new steps at the end of these lists**, using
  `schema_changes.add_column()` for columns. A failed step raises
  `SchemaChangeError`; the app logs it and stops at start-up. A database
  newer than the app is refused. Indexes and `CREATE TABLE IF NOT EXISTS`
  still run on every start, as before.
- **Backups (#21):** `backups.py`. `backup_all(db_path, reason)` copies both
  databases into `backups/` beside them, keeping 5 of each. Already called on
  start-up, before each pending database change, and before these POSTs
  (`Handler._BACKUP_BEFORE`): import-new, review-import, bulk-apply,
  bulk-runs/continue, snapshot restore, recycle-bin restore and purge. If the
  copy fails, the change is refused with a `500`. Add new bulk endpoints and
  AI runs to that list.
- **Error log (#22):** `errorlog.py`, file `logs/errors.json`. Use
  `errorlog.record_exception(exc, action=..., ids=..., shape=...)` for server
  errors and `errorlog.record(where="browser", ..., browser={...})` for
  browser errors (#35 sends them; the function and its fields are ready).
  `request_shape()`, `ids_in_payload()` and `describe_path()` apply the
  privacy rule; never pass raw text to the log. Every `do_*` method is
  wrapped by `_log_errors`, which logs and then **re-raises**, so the
  dropped connection from #9 is unchanged: #9 replaces the re-raise with
  the JSON reply. `_read_json_body` keeps the parsed body on
  `self._logged_body` for the log. The ~80 places that catch an error and
  carry on silently are **not** logged yet; #9 should route them through
  `errorlog.record_exception` as it touches them.
- **Tests:** `.github/workflows/tests.yml` runs the tests and the style check
  on Python 3.12 and 3.13 on every push. `tests/test_label_behaviour.py`
  records today's label, bulk-label and snapshot behaviour;
  `tests/test_safety_net.py` covers this session's pieces.

### Session 2 notes for later sessions

What exists now and must be used, not rebuilt:

- **Endpoint list:** `ENDPOINTS.md`. Every new endpoint goes in it, in the
  same change. The README's API section points to it.
- **Front door:** `Handler._front_door_problem()` in `server.py` runs before
  every `do_*` method (through `_log_errors`), which also drains any body a
  route didn't read (`_discard_unread_body`) and turns `send_error` on
  `/api/` addresses into the JSON shape. It checks the `Host` (#3) and,
  for POST/PATCH/DELETE, `Sec-Fetch-Site`, `Origin` and the content type (#1).
  A change with a body must be `application/json`; only `/api/upload-file`
  takes `multipart/form-data`. New browser code must send
  `Content-Type: application/json` with every body (`apiModelState` in
  `static/app.js` was fixed to do so). `_authorized_update_request` still
  guards `/api/update` on top of this.
- **Error replies (#9):** `_log_errors` no longer re-raises. `ApiError` gets
  its own status and message (logged only when 500 or above); anything else
  is logged and gets `500` with `_SERVER_ERROR_MESSAGE`. Every error reply is
  `{"error": "<plain message>"}`. Session 3's browser helper (#23) can rely on
  that shape. `_read_json_body` raises `400` for broken JSON or a non-object;
  `_qs_int` gives `400` for a bad number in the query string. The ~80 places
  that catch an error and carry on silently are still **not** logged; this
  session did not touch them. Route each through
  `errorlog.record_exception` when a later session edits it.
- **Size limits (#10):** `request_limits.py`. `MAX_BODY` 20 MB,
  `MAX_UPLOAD` 100 MB (`413`). `FIELD_LIMITS` are checked on every JSON body
  by field name, so only add a field there if it never carries imported text
  (`title` is left out on purpose: Compare tabs send imported chat titles;
  a typed rename is checked against `CONVERSATION_TITLE_MAX`);
  `check_text()` is used where a module used to cut silently (label names 60,
  tags 40, GPT names and folder names 120, snapshot names 120, Scratch Pad
  titles 200). AI-written notes and summaries get the same 200,000-character
  limit as the user's own (`NOTE_MAX`). The browser does not yet show these
  errors well; that is #23.
- **User files (#1):** `Handler._send_user_data()` serves `/source/`,
  `/chatfiles/` and `/files/`. Only `_INLINE_TYPES` may show in the browser;
  everything else is a download, always with `nosniff`.
- **Tests:** `tests/test_front_door.py` covers this session on a throwaway
  database with a real server.

### Session 3 notes for later sessions

What exists now and must be used, not rebuilt:

- **Server calls (#23):** `static/api.js`, loaded first. Every browser call
  uses `apiRequest(url, {method, body, signal, keepalive, headers, action,
  notify, log, allow, expect})`; no other file calls `fetch()` (a test
  checks this). An object `body` is sent as JSON with the right
  Content-Type; `FormData` is sent as is. On failure it shows the server's
  plain `{"error"}` message (or a plain fallback) in the shared notice
  (`showAppNotice()`, `#app-notice`), sends the detail to the log, and
  throws an `ApiRequestError` (`userMessage`, `status`, `reported`), so the
  code after the call (the "saved" state) never runs. `notify: false` when
  the screen shows the message itself; `expect: [404, 409]` for statuses a
  screen handles as a known outcome; `log: false` only for background
  polling. `apiModelState` and `spRequest` are now thin names for it.
  `initApp` carries on when one start-up load fails.
- **Plain messages:** server errors that showed `{e}` now log the error and
  say a sentence; `_busy_or_failed()` in `server.py` turns a locked database
  into "another change, such as an import, is running".
- **Error Log (#35):** `GET /api/error-log` (10 newest groups),
  `DELETE /api/error-log` (empties the file), `POST /api/error-log/browser`
  (browser errors; `errorlog.record_browser()` keeps only the address
  pattern of a failed call and parses the stack with
  `errorlog.browser_frames()`). The page is `openErrorLog()` in `app.js`
  (special view `error_log`); everything is drawn with `textContent`.
  `window` `error` and `unhandledrejection` handlers in `api.js` send the
  rest; errors `apiRequest` already reported are skipped.
- **Markdown (#2):** stashed math is put back escaped (`esc(raw(...))` at the
  end of `md`). `escHtml` now escapes `"` too.
- **KaTeX (#15):** `static/vendor/katex/` (0.19.0, `.woff2` fonts only, see its
  README). Served by `Handler._serve_vendor_file`.
- **Content-Security-Policy (#1):** `CONTENT_SECURITY_POLICY` in `server.py`,
  sent with the app page. Scripts, styles, fonts and connections are the
  app's own only; no `<style>` elements or `style=""` attributes from
  JavaScript (the Custom GPT rule moved to `style.css`). Images may still
  come from `https:` (see #37).
- **Tests:** `tests/test_browser_side.py`.

### Session 4 notes for later sessions

What exists now and must be used, not rebuilt:

- **Releases (#16):** `updater.py` only installs the newest published GitHub
  release (`/releases/latest`; drafts and pre-releases are skipped). Its
  tag must be the version number with or without a leading `v` (`v1.3.0`),
  and the release's own `version.py` must say the same number, or the
  install is refused.
  Each session's version in `version.py` therefore becomes a release the
  user publishes after merging (see the session table). The CHANGELOG entry
  is what the About page shows: keep the heading format
  `## X.Y.Z — date` so `updater.changelog_entries()` finds it, and write the
  entry for the user (plain words).
- **Flow:** `POST /api/update/check` changes nothing and returns the version,
  the CHANGELOG entries between the installed and the new version, file
  counts and the app files edited on this computer (compared with the
  installed version's release). `POST /api/update/apply {version, commit}`
  installs that release only if it is still the newest and its tag still
  points at the commit that was checked. If an update stops partway and
  can't be undone, the old files stay in `.update-backup.new/` (or the older
  backup in `.update-backup.old/`) and further updates are refused until
  that folder is moved away. Apply and restore
  hold `.update.lock` in the app folder (an operating-system lock, freed if
  the app stops), so two copies of the app can't update at once. All update POSTs need the
  `X-CHV-Update: 1` header (`_authorized_update_request`). Failures raise
  `updater.UpdateError(message, status)`; `Handler._api_update` turns them
  into `{"error"}` replies and logs network and disk failures.
- **Undo:** the files an update replaces are copied to `update-backup/`
  (`files/` plus `manifest.json` with the versions and the replaced and
  added files); only the last update is kept. `POST /api/update/restore` or
  `python updater.py --restore` puts them back, removes the files the update
  added, and deletes the backup. This is app files only: a database change
  delivered by an update is undone from `backups/` (#21), which
  `schema_changes` fills before each pending change. A later database step
  should therefore stay readable by the previous version where it can, or the
  CHANGELOG entry should say that going back needs the database copy too.
- **Never managed by the updater:** `source/`, `backups/`, `logs/`,
  `update-backup/`, `*.db` / `*.sqlite*`, `.env`, `.env.local`, `.update.lock`, `.git`,
  `__pycache__`. Add any new runtime folder to `_PROTECTED_ROOT_DIRS`.
- **Not done:** files a release deletes are left in place (as before).
- **Tests:** `tests/test_updater.py` (a fake GitHub, throwaway folders, and the
  routes on a real server).

### Session 5 notes for later sessions

What exists now and must be used, not rebuilt:

- **Query rules (#25):** `query_rules.py`. Every conversation query takes
  its pieces from there instead of writing the SQL out:
  `META_JOIN` / `meta_join(on, meta)` (the join to `conversation_meta`),
  `DISPLAY_TITLE` / `display_title(conv, meta)` (rename, else imported
  title), `NOT_DELETED`, `IN_RECYCLE_BIN`, `archived()`, `not_archived()`,
  and for the side: `provider_or_none(value)`, `provider_from_qs(qs)`,
  `is_provider(p)`, `and_provider(p)` (empty when no side is given) and
  `providers_in(list)`. The default aliases are `c` (conversations) and `cm`
  (conversation_meta). New lists, searches, reports and the API use these.
- **Session 6 (#6) moves the table by changing `meta_join()` and the
  column pieces in that one file.** What still names `conversation_meta`
  directly, and must move with it: the writes (`server.py` rename / archive /
  delete in `_api_conversation_update` and folder moves, restore and purge in the
  Recycle Bin code, `_ensure_runtime_schema`), `snapshots.py`,
  `gizmos.py`'s folder move, `advanced_search._attach_metadata` (reads
  archived/deleted), `build_db.py` (creates the table and reads `deleted`
  during import) and the `deleted` / `archived` columns read in
  `_api_detail` and `scratchpads.py`. `grep -n conversation_meta *.py`
  lists them.
- **Sidebar list:** `_api_conversations` now builds one query for every view;
  only the search part (`search_sql`) differs. An index search that fails
  falls back to titles and tags (it caught any error before; now only
  database errors).
- **Quick search and the Attachment Report (#13)** take `provider`; without
  it they cover both sides, still never the Recycle Bin. The report reloads
  when the side changes. `/api/search` is not called by the current screens
  (`apiSearch` in `app.js` is unused); it is ready for the API.
- **Not done:** the Media Hub (`/api/gallery`) still shows both sides; that
  is new finding #38, placed with #32.
- **Tests:** `tests/test_query_rules.py` (recorded sidebar, pinned, folder,
  bookmark, Advanced Search and Media Hub behaviour, plus the #13 fixes).

### Session 6 notes for later sessions

What exists now and must be used, not rebuilt:

- **User state lives in userdata.db (#6):** `conversation_meta`,
  `pinned_conversations` and `ui_preferences` are created in
  `_ensure_userdata_schema` and always named `udb.<table>` in SQL (every
  connection from `open_db()` has userdata.db attached as `udb`).
  `query_rules.meta_join()` joins `udb.conversation_meta`. history.db change
  2 (`_history_change_2_move_user_state`, table list in `MOVED_TO_USERDATA`)
  copied old rows across and dropped history.db's copies.
  `_ensure_runtime_schema` now runs `_ensure_userdata_schema` first and
  attaches userdata.db, and backs up **both** files before a history.db
  change. `dataset_format` is written by `build_db.build` into
  `udb.ui_preferences`. Path helper: `app_paths.userdata_path(db_path)`.
- **Schema versions now:** history.db 2, userdata.db 1. Version 1.4.0 refuses
  the new history.db; the CHANGELOG says how to go back.
- **Rebuild (#6):** `server.rebuild_history(db_path, source_dir)` builds from
  `conversations.json` (or the first recognised export when it is gone), drops
  chats that have a `udb.deleted_records` row, then runs
  `import_new_backups(..., skip_binned=False)` over the rest of `source/`,
  so a chat in the Recycle Bin also gets its newest copy (and stays in the
  Recycle Bin). A normal import still skips chats in the Recycle Bin. `app.py` calls it
  whenever `history.db` is missing (`server.has_rebuild_source()` decides
  whether there is anything to build from). The re-import in Phase 5 should
  use this, not `build_db.build` alone.
- **One purge list (#7):** `conversation_tables.py`. `IMPORTED_TABLES` (what
  an import writes), `HISTORY_TABLES` and `USERDATA_TABLES`, each entry
  `(table, conversation column, provider column or None)`.
  `delete_conversation_rows()` is what `_purge_conversation` uses (it also
  takes the chat out of unfinished bulk-label runs);
  `delete_imported_rows()` is what the rebuild uses. `IMPORTED_TABLES`
  includes `import_sources`, so the import audit forgets a deleted chat too. **Every new table that
  holds a conversation's rows (tag ids, note versions, AI data) is added to
  this file.** A missing table is skipped; other errors now roll the purge
  back instead of being ignored.
- **Checked:** recent and saved Advanced Searches hold search words and
  folder choices, not chat ids, so purge leaves them alone.
- **Not done:** Compare tabs (`workspace_tabs`) and chats waiting for review
  (`pending_reimport`) are still in history.db and are lost when it is
  deleted. New finding #39, placed in session 7.
- **Tests:** `tests/test_user_data_move.py` (moving an old-layout database,
  rebuild keeps state and reads every backup, rebuild without
  `conversations.json`, purge list and bulk runs). `test_notes.py` now
  checks the purge list instead of a line of `server.py`.

### Session 7 notes for later sessions

What exists now and must be used, not rebuilt:

- **Schema versions now:** history.db 5, userdata.db 4. Version 1.5.0
  refuses both; the CHANGELOG says how to go back.
  - history change 3 moved `workspace_tabs` (Compare bar) and
    `pending_reimport` (review list) into userdata.db (#39). Always write
    `udb.workspace_tabs` / `udb.pending_reimport`. `build_db.SCHEMA` no longer
    creates or drops them, and both are in `conversation_tables.USERDATA_TABLES`.
  - history change 4: `artifacts` has `PRIMARY KEY (conv_id, id)` (#5).
    `build_db` keeps one row per (chat, name). `/api/artifact/<id>` needs
    `?conv=<conv_id>`; the browser's `openArtifactTab(convId, id, title)`
    passes it. Conversation detail's artifact metadata now carries `conv_id`.
  - history change 5: `memories.memory_id` = `build_db.memory_id(account,
    text)`, the same after a rebuild (#11).
  - userdata change 2: `udb.tags (id, kind 'tag'|'mood', name)`, and a
    `tag_id` column on `conversation_tags` / `conversation_mood_tags`.
    Every insert must set it: use `tag_ids.tag_id(conn, kind, name)`, or
    `tag_ids.fill_missing(conn)` after a bulk write. Rows keep the text, so
    searches are unchanged. There is no tag rename yet, and the API does not
    return tag ids yet (Phase 7 decides).
  - userdata change 3: `version` on `conversation_notes` and
    `conversation_summaries` (#34). GET returns it; PATCH takes
    `base_version` and answers 409 for an out-of-date copy (`_base_version`,
    `_check_version` in server.py). A PATCH without `base_version` is not
    checked: AI runs in Phase 7 must send it.
  - userdata change 4: Claude models use the `id` in `claude_models.json`
    (re-keyed once by name). Reload Model Data matches by id only (#11).
    A new model in the file needs a new `id`; never change a shipped one.
- **Preferences (#19):** `INTERNAL_PREF_KEYS` in server.py (`dataset_format`)
  are hidden from GET and ignored by PATCH /api/preferences.
- **Snapshots (#19):** extended rather than replaced. Snapshot format 2 adds
  `snapshots.TABLE_CATEGORIES` (mood tags, bookmarks, notes, summaries,
  related links, model tags, Scratch Pads), each saved as whole table rows.
  Restoring raises saved versions past every earlier one. A format 1
  snapshot restores only what it holds; the restore dialog lists only those.
  A new table of user data should join `TABLE_CATEGORIES`.
- **Tests:** `tests/test_phase4_part2.py`.

### Session 8 notes for later sessions

What exists now and must be used, not rebuilt:

- **Schema versions now:** history.db 6, userdata.db 4. Version 1.6.0
  refuses the new history.db; the CHANGELOG says how to go back.
  history change 6 adds `dropped_types` and `unknown_types` (JSON
  `{content type: count}`) to `imported_backups`.
- **Reading exports (#31):** never `json.load` an export. Use
  `build_db.iter_export(path)` (one item at a time; `NotAList` for a file
  that is not a list, `ValueError` for broken JSON, possibly after some
  items), `export_provider(path)` (reads the first 5 chats),
  `scan_export(path, provider)` (first reading: the winning entry per id,
  date range, content-type counts) and `iter_records(path, provider)`
  (second reading: one importer record at a time). `file_sha256(path)`
  hashes in pieces. `build()`, `import_new_backups()`, `_rebuild_seed()`
  and `message_ids.fill_missing_from_sources()` all use them.
  `parse_backup` / `dedup_records` are still there for tests and small
  callers but hold a whole file.
- **Free space (#31):** `build_db.check_free_space(db_path, export_bytes)`
  raises `NotEnoughSpace` (needs `SPACE_FACTOR` = 3 times the export).
  `/api/import-new` answers `507`; `app.py` prints the message.
- **Savepoints (#8):** `build_db.reconcile_backup` opens a transaction if none
  is open and writes each record inside `SAVEPOINT import_chat`; an error rolls
  that chat back, is counted in `errors` and goes to the error log. The
  caller still commits. Code called inside it must not commit:
  `message_ids.reanchor_bookmarks(conn, cid, commit=False)` is how the
  bookmark step avoids it.
- **One import at a time (#8):** `import_lock.hold(db_path)` (thread lock +
  `.import.lock` file beside history.db; `ImportBusy` with a plain
  message). Used by `Handler.do_POST` for `_ONE_IMPORT_AT_A_TIME`
  (`/api/import-new`, `/api/import-new/review-import`, answered `409`
  before the backup) and by `server.rebuild_history`. The updater leaves
  `.import.lock` alone. **Session 9's job system replaces this lock**; it
  was enough for now, so nothing was stopped.
- **Content types (#12):** each importer record has `content_types`
  (`{"dropped": {...}, "unknown": {...}}`) from
  `claude_content_types` / `chatgpt_content_types`. Shown types are
  `CLAUDE_SHOWN_TYPES` and `CHATGPT_SHOWN_TYPES` / `CHATGPT_SHOWN_PARTS`;
  update those when the viewer learns a new type. `/api/import-audit`
  returns the totals as `dropped_types` / `unknown_types`; the Import
  Audit screen lists them.
- **Attachments (#12):** an attachment with no text is kept with
  `content: ""` and takes the `uuid` of the `files` entry with the same
  name.
- **Upload (#24):** `/api/upload-file` writes to `Handler.source_dir /
  "files"` (it used the app folder before) and answers `409` unless the
  form has `replace=1`. The Attachment Report asks with `openConfirm`.
- **Not done:** the Attachment Report and `/files/` still read
  `Path(__file__).parent / "source"` rather than `Handler.source_dir`
  (the same folder in the app; only tests differ).
- **Tests:** `tests/test_importer.py` (recorded behaviour first, then each
  fix).

### Session 9 notes for later sessions

- **Version 1.8.0; no schema changes.** history.db remains at 6 and userdata.db
  at 4. Job receipts use private files, so going back to 1.7.0 does not need a
  database copy. Stop a running job first.
- **One general runner:** `background_jobs.start(db_path, kind, callback)`
  returns an immediate receipt; the callback gets a `Job`. It owns its backup
  and connections. `run_sync` is the CLI equivalent. `Job.check()` cooperates
  with cancellation; `Job.progress(message, done, total)` records progress;
  `Job.batch(conn, done)` commits between complete records, every 20 records
  or about 0.25 seconds. Keep per-chat writes in `reconcile_backup` savepoints.
  AI jobs must use this runner rather than add another lock or worker.
- **Cross-copy coordination:** one OS lock, still `.import.lock` for upgrade
  compatibility. A second job is refused with a plain `409` message before its
  backup. `import_lock.py` is only a compatibility alias, not a second lock.
  Receipts/cancel requests live in `.background-jobs/` beside the databases;
  excluded from git and updater-managed files. A stopped process is shown as
  interrupted, never automatically restarted. `ENDPOINTS.md` documents the
  receipts and `/api/jobs`, `/api/jobs/<id>`, `/api/jobs/<id>/cancel`.
- **Import/review/rebuild POSTs now return `202 {job}`.** Browser code polls
  through `apiRequest`. `server.review_import` owns review batches and only
  clears a deletion/review record when the chat was successfully reconciled.
  Every worker backs up first; failed jobs report a plain message via status.
- **Import retry:** only a completed file is renamed/hash-recorded. Cancel
  commits complete chats so far; errors roll back the current batch. A retry
  reconciles prior batches without duplicating chats. The cancelled message
  explicitly says that chats already saved are kept.
- **Rebuild:** still `server.rebuild_history`, also `POST /api/rebuild` from
  the existing import screen. Validate/count the seed before clearing anything.
  Existing databases keep their schema; imported conversation rows are cleared
  using `conversation_tables` in batches. Seed tombstones are excluded before
  any chat becomes visible. Cancelled rebuilds leave partial history until
  another rebuild; user state remains. A before-rebuild database copy exists.
- **Startup:** `prepare_startup` takes the same lock for maintenance when idle.
  During a job, another app checks versions read-only, backs up, and serves
  without competing maintenance. Backend auto-restart waits until the job ends.
- **UI:** existing import button/layout/spinner/summary styles are reused;
  no new CSS rules or theme values. Progress is plain text with counts; Cancel
  and Rebuild reuse review button styling. Reopening/reloading reconnects.
- **Tests:** `tests/test_background_jobs.py` first records replay behavior, then
  checks batching, cancellation/retry, failed backup, review failure, rebuild,
  and a real second server process against throwaway databases. Existing
  session-8 HTTP tests now follow the async receipt before checking outcomes.
- **Codex review:** fixed a completion/poll race by re-reading the receipt
  after finding the OS lock free; its regression test reproduces the old
  false interruption and checks that the completed result is returned.
- **Validation:** all 333 tests passed, including the Node job-controller tests;
  `python3 scripts/check_styles.py`, `node --check static/app.js`, and
  `git diff --check` passed. A visual browser check was unavailable: Chromium
  was absent and its download returned a truncated archive. No visual-browser
  pass is claimed; existing styles are reused without any CSS changes.
- **Next:** the user's real-export check and one re-import remain undone.
  No real exports or real databases were opened or re-imported in this session.

## Phase 2 — The front door: security and input

All of these are in the same place: where requests come into the server. Do
them together, in this order.

- [x] **Write the full endpoint list:** what each takes and returns. (#27)
- [x] **Host check** (blocks DNS rebinding). (#3)
- [x] **Request checks on every POST, PATCH and DELETE**, plus serving user
      files as downloads, never as web pages. Leave the
      Content-Security-Policy for later in this phase. (#1)
- [x] **One error wrapper:** clear JSON `400` / `500` replies, never a dropped
      connection, and errors written to the log from Phase 1. (#9)
- [x] **Size limits** on requests and fields, with clear errors instead of
      silent cutting. (#10)
- [x] **Browser-side helper** that checks every server reply and shows
      failures in plain language (no codes or technical words; those go to
      the log). Needs #9. (#23)
- [x] **Error Log page** on the ⓘ menu above About: the 10 most recent error
      blocks (repeats grouped, with count and first/last time), separators, a
      Copy button under each, a Clear log button, and browser errors sent to
      the log.
      Needs #22, #9 and #23. (#35)
- [x] **Escape math text in the markdown renderer.** Add the `$5 … $10` test.
      (#2)
- [x] **Keep KaTeX inside the app** (and update it). (#15)
- [x] **Now add the Content-Security-Policy** from #1. Needs #15.
- [x] **Updater: tagged releases, show what's changing, keep an undo copy.**
      From here on, every phase's fixes reach users through it. (#16)

## Phase 3 — One set of rules for every query

- [x] **Put the shared rules in one place**: "not deleted", "this provider",
      "display title" (renamed or original). Have every list, search and report
      use them. (#25)
- [x] **Quick search and the Attachment Report** then follow the same rules.
      This mostly falls out of the step above. (#13)

## Phase 4 — Database changes

Every item is one numbered step in the change system (#18), with an automatic
backup first (#21).

- [x] **Move renames, archive, Recycle Bin, pins and preferences into
      `userdata.db`.** Make rebuild read every backup and honour permanent
      deletions. Needs Phase 3. (#6)
- [x] **One list of "tables that belong to a conversation"**, used by
      permanent delete. Add summaries to it. Every table added later, including
      AI tables, joins this list. (#7)
- [x] **Artifact key becomes (chat, artifact name).** The lost artifacts come
      back at the re-import in Phase 5. (#5)
- [x] **IDs for tags and mood tags**, and a stable ID for memories. Fix the
      model reload matching by name. (#11)
- [x] **Version check on Notes and Summaries**, like Scratch Pads. (#34)
- [x] **Smaller data items:** protect internal preference keys, and make
      snapshots cover everything (or replace them with the Phase 1 backups).
      (#19)
- [x] **Compare bar and chats waiting for review survive a rebuild.** (#39)

## Phase 5 — The importer, then one re-import

- [x] **Remove the 300-turn and 100-step limits**, or mark truncated chats.
      (#4)
- [x] **Attachments:** keep every entry, and count dropped and unknown content
      types in the import audit. (#12)
- [x] **Savepoint per chat, one import at a time.** (#8)
- [x] **Read exports one chat at a time; check free disk space first.** (#31)
- [x] **Upload asks before replacing a file with the same name.** (#24)
- [x] **Background-job system** (ID, progress, cancel, one at a time, small
      commits). Imports use it first; AI runs reuse it in Phase 7. (#33)
- [ ] **Check against a real Claude and a real ChatGPT export**, especially
      images, attachments and whether any chat hit the old 300-turn limit.
      (#12)
- [ ] **Re-import / rebuild once.** This recovers cut-off turns (#4),
      overwritten artifacts (#5) and dropped attachments (#12). Compare chat and
      message counts before and after.

## Phase 6 — Speed (optional before the API)

- [x] **Media Hub loads in pages.** (#32) Give it the side the sidebar
      shows at the same time. (#38)
- [x] **Long chats load in pages.** Use the same paging shape the API will use.
      (#17)
- [x] **Remaining small items:** start-up re-scan, `?limit=` caps, search
      wildcards, timestamps parsed as 0, Unicode file names, Windows restart.
      (#19)
- [x] **Plain words in size-limit messages**, and the quiet catches in the
      browser. (#36)
- [x] **Markdown images from the internet:** decide whether to keep loading
      them. (#37)

## Phase 7 — The API

Everything it relies on is now in place: security, error handling, size
limits, backups, versioned notes, one-list purge, the background-job system and
reliable IDs. Follow the [API checklist](#phase-7--the-api-checklist) and the
[Phase 7 plan](#phase-7-plan-session-11) in Part B.

- [x] **`.env` kept out of git.** (#40, session 11)
- [x] **Plan the API work and list the user's decisions.** (Session 11,
      planning only; no code and no release.)
- [ ] **User answers the open questions** (D1–D11 in the Phase 7 plan).
- [ ] **Foundation, no product decisions needed:** key reading, optional
      SDK, run records and provenance tables (userdata change 5), context
      builder, token estimator, metadata-only request log, AI job kind, all
      tested with a fake client. (Session 12, plan step 1)
- [ ] **First AI task on one chat**, with estimate, confirmation, progress,
      cancel and version-checked save. Needs D1–D7. (Session 13, plan step 2)
- [ ] **Many chats in one run**, optionally through the Message Batches API.
      Needs D5 and D8. (Session 14, plan step 3)
- [ ] **Visible request log screen.** Needs D9. (Session 14 or 15, plan step 4)

---

# Part B — Findings in detail

## Summary

| # | Problem | Severity | Phase |
|---|---------|----------|-------|
| 1 | Any website you visit can write files into the app and run code in it | Critical | 2 |
| 2 | Chat text with `$` signs can inject live HTML/JavaScript | Critical | 2 |
| 3 | Any website can read all your chats through a "DNS rebinding" trick | Critical | 2 |
| 4 | Long Claude chats are silently cut off at 300 turns | High | 5 |
| 5 | Artifacts with the same name in different chats overwrite each other | High | 4 |
| 6 | "Rebuild the index" wipes renames, archive, Recycle Bin, pins and settings, and drops every imported backup | High | 0 (stop using it), 4 (fixed in session 6) |
| 7 | Permanent delete leaves the chat's summaries behind | Medium | 4 |
| 8 | Import can half-write a chat if one record fails | Medium | 5 |
| 9 | Malformed requests crash the request with no reply | Medium | 2 |
| 10 | No size limits on anything sent to the server | Medium | 2 |
| 11 | Some IDs are not stable or not unique | Medium | 4 |
| 12 | Attachments: many are dropped or never shown | Medium | 5 |
| 13 | Quick search shows chats from the Recycle Bin and from the other provider | Low | 3 |
| 14 | The app says "Python 3.9+" but crashes on 3.9; versions are out of date | Low | 0, 1 |
| 15 | KaTeX loads from the internet without an integrity check | Low | 2 |
| 16 | The updater installs whatever is on `main`, with no check or undo | Low | 2 |
| 17 | Very long chats are slow to open | Low | 6 |
| 18 | Database changes have no version number; failures are hidden | Low → needed first | 1 |
| 19 | Smaller issues | Low | 4, 6 |
| 20 | The app only works when started from its own folder | Medium | 1 |
| 21 | No automatic backups of your own data | High | 1 |
| 22 | Errors leave no trace | Medium | 1 |
| 23 | The screen can say "saved" when it wasn't | Medium | 2 |
| 24 | Uploading a file silently replaces one with the same name | Low | 5 |
| 25 | Copy-pasted query code | Medium | 3 |
| 26 | Large parts have no tests; tests never run automatically | Medium | 1 |
| 27 | No single list of the app's endpoints | Medium | 2 |
| 28 | Leftover `claude.json` | Low | 0 |
| 29 | Personal email addresses in public commit history | Low | 0 |
| 30 | No version number or changelog | Low | 0 |
| 31 | Memory and disk grow faster than the export | Medium | 5 |
| 32 | The Media Hub loads everything at once | Low | 6 |
| 33 | Import blocks other changes and other copies of the app | Medium | 5 |
| 34 | Notes and Summaries: the last save wins, silently | High (for the API) | 4 |
| 35 | Error Log page (requested) | — | 2 |
| 36 | Some on-screen messages still use field names; some failures are only logged | Low | 6 |
| 37 | Markdown images still load from the internet | Low | 6 |
| 38 | The Media Hub shows both Claude and ChatGPT chats | Low | 6 |
| 39 | A rebuild still loses the Compare bar and chats waiting for review | Low | 4 |
| 40 | `.env` (where the API key will live) is not in `.gitignore` | Medium (for the API) | 7 (fixed in session 11) |
| 41 | Tests run on Node 20, which reached end of life in April 2026 | Low | 7 |

### Checked and fine

- **No private data or keys in git history.** I searched all 205 commits: no
  export files, databases, attachments, `.env` files or API keys were ever
  committed. `.gitignore` covers them.
- **No SQL injection.** Every value goes into the database through `?`
  placeholders. The few values written straight into SQL come from fixed lists.
- **No unsafe file paths** in upload or file serving (no `../` tricks).
- **Long single messages** render quickly (see #17).
- **At 3,000 chats**, the sidebar, opening a chat, quick search and Advanced
  Search all answer in under 0.2 s (see Phase 5 and 6 findings).

---

## Phase 0 findings — before touching code

### 28. Leftover and confusing files

`claude.json` (506 lines) is not used by the app; only a code comment mentions
it. It sits next to `claude_models.json`, which is used. Delete it or move it
to a `docs/` folder.

### 29. Personal email addresses in public commit history

One commit is authored with your university email, and one with another
contributor's Gmail. If the repository is public, those addresses are public.
Rewriting history to remove them is possible but disruptive. The easy step is
setting git to your GitHub "noreply" address for future commits.

### 30. No version number or changelog

Nothing says which version you are running, so bug reports, the updater (#16)
and "did this fix reach me?" all depend on guesswork. Add a version number and
a short `CHANGELOG.md`.

---

## Phase 1 findings — safety net

### 20. The app only works when started from its own folder — Confirmed

`app.py` looks for `history.db` and `source/` in whatever folder you start it
from, not the app's folder. I started it from another folder, next to a working
`history.db`, and got "source/conversations.json not found". A desktop
shortcut, an editor's Run button or a different terminal folder can trigger
this. Worse: if a `conversations.json` happens to be in that folder, the app
builds a **second, separate** database there. Some parts (uploads, `/files/`)
already use the app's own folder, so one running app can mix the two.

**Fix:** build every path from the app's folder (`Path(__file__).parent`), as
those parts already do.

### 14. "2024 code" check: Python and library versions — Confirmed / From the code

- README and `app.py` say **Python 3.9+**. `build_db.py` uses `str | None`
  type hints without `from __future__ import annotations`, which **crashes on
  import under Python 3.9**. The real minimum today is 3.10.
- Python 3.9 reached end of life in October 2025, and **3.10 reaches end of
  life this month (October 2026)**. Current versions are 3.12–3.14.
- The current official Anthropic Python SDK (1.x) needs Python 3.10 or newer.
- This environment runs Python 3.13 and SQLite 3.45.

**Fix:** state **Python 3.12+** in the README and `app.py`, add a start-up
check that prints a clear message on older versions, and test on that version.
When the API is added, the "no third-party dependencies" promise has to
change: the `anthropic` package will be needed. Add a `requirements.txt` (or
`pyproject.toml`) with pinned versions.

Otherwise the Python code uses current, standard library features; I found
nothing deprecated. The HTTP server is Python's built-in `http.server`.
Python's own docs say it only does basic security checks, which is acceptable
for a local-only app once #1–#3 are fixed.

### 26. Large parts have no tests, and tests never run automatically — From the code

Labels, bulk labels, snapshots, Custom GPTs and the import of real export
shapes have no tests. There is no automatic test run on GitHub (no `.github/`
workflows), so a change can break something and nobody finds out until it is
used.

**Fix:** a GitHub Actions workflow that runs the tests and
`scripts/check_styles.py` on every push. Add tests for each fix as it is made:
the security checks (#1–#3), the renderer (#2), the 300-turn limit (#4),
artifact keys (#5), purge cleanup (#7).

### 22. Errors leave no trace — From the code

The server's request log is switched off (`log_message` does nothing,
`server.py:1389`). There are about 80 places that catch an error and carry on
silently, and no log file. When something goes wrong, there is nothing to look
at afterwards.

**Fix:** keep the silence for normal requests, but write every error to an
error log file in `logs/`. Every error must be recorded with enough
information to fix it without asking anyone to reproduce it. Each entry holds:

| Field | Example | Why |
|-------|---------|-----|
| **Error ID** (a new UUID) | `3f9c…` | So an error can be named exactly when reporting it |
| **When**, with time zone | `2026-10-18 14:02:11 -04:00` | Order of events; matching with other logs |
| **Where** | `server` or `browser` | Which half of the app failed |
| **What** — error type and message | `sqlite3.OperationalError: database is locked` | The failure itself |
| **Full traceback** (server) or **stack trace** (browser) | file, line and function of every step | Shows exactly which code failed |
| **What was being done** | `PATCH /api/conversation/<id>`, status `500`; in the browser, the screen and action ("Rename conversation") | Connects the failure to what you did |
| **IDs involved** | conversation, message, label, pad… IDs | Find the exact record |
| **Request shape** | field names and their sizes, e.g. `title: 42 chars` | What was sent, without its private text |
| **App version** (#30) and **database versions** (#18) | `1.4.0`, history v7 / userdata v12 | Rules out "already fixed" or "old database" |
| **Python, SQLite and operating system versions** | `Python 3.13.2, SQLite 3.45, Windows 11 23H2` | Many bugs are system-specific |
| **Browser name and version, window size** (browser errors) | `Firefox 142, 1440×900` | Many display bugs are browser-specific |

**Privacy rule:** the log never contains chat text, note or summary text,
search words or the API key — only IDs, sizes and names. You will be pasting
these entries into chats and bug reports.

**Repeats are grouped.** The same error happening again doesn't make a new
entry; it adds to the existing one. "The same error" means the same place
(server or browser), the same kind of error, the same line of code and the
same action. It ignores what changes each time: the time, the IDs, and the
numbers in the message. Each group keeps:
- how many times it has happened;
- when it **first** happened and when it **last** happened;
- the full details of its **5 most recent** occurrences (the IDs and sizes
  can differ each time).

**Size:** the file keeps at most 200 groups. When it is full, the group whose
last occurrence is oldest drops off, so the file can never fill the disk, and
one error repeating thousands of times can't push the others out.

Errors that happen in the browser are sent to the server and written to the
same log (#35). Later, log every AI call too: time, model, token counts,
success or failure.

### 21. No automatic backups of your own data — From the code

`userdata.db` holds everything you created: labels, tags, notes, summaries,
bookmarks, Scratch Pads, folders. It is the only copy. Nothing backs it up, and
nothing checks it for damage. Snapshots cover only part of it (#19). Once the
API writes AI notes in bulk, one bad run could overwrite many notes with no way
back.

**Fix:** an automatic copy (SQLite's built-in backup) on start-up and before
imports, bulk changes, restores, database changes and AI runs, keeping the last
few copies.

### 18. Database changes have no version number

Database changes are done by "try to add the column, ignore any error". A real
failure (disk full, locked file) is hidden the same way as "column already
exists". There is no schema version to say what state a database is in.

**Fix:** a `schema_version` row in each database and numbered change steps
that report failures. Phase 4 makes several database changes, so this comes
first.

---

## Phase 2 findings — the front door: security and input

### 27. No single list of the app's endpoints — From the code

The README's API list names about 40 endpoints. The server has roughly twice
that (labels, bulk labels, snapshots, notes, summaries, bookmarks, Advanced
Search, related chats, Recycle Bin, updates…). Write down every endpoint, what
it takes and what it returns. The security checks in #1 and the API both need
this list.

### 3. Any website can read your chats via DNS rebinding — Confirmed

The server answers whatever `Host` name the request uses. I sent
`Host: attacker.example:5174` and got the full conversation list back. A
malicious site can point its own domain name at `127.0.0.1` and then read every
endpoint as if it were its own: all chats, notes, and (after the API is added)
anything stored there, such as settings or keys.

**Fix:** reject any request whose `Host` is not `127.0.0.1:<port>` or
`localhost:<port>`. This is a few lines and also blocks most of #1.

### 1. Any website can write files into the app and run code in it — Confirmed

The server checks who is calling on exactly one endpoint (`/api/update`). Every
other endpoint accepts requests from any website open in your browser.

A browser lets any web page send some kinds of POST to `127.0.0.1` without
asking: plain-text bodies and file-upload forms. The server reads the body as
JSON whatever its type (`server.py:1620`, `_read_json_body`), so that is enough.

What I ran against a test copy (requests marked as coming from another site):

1. `POST /api/scratchpads` as plain text from `Origin: http://evil.example` →
   a Scratch Pad was created.
2. `POST /api/upload-file` from the same origin → saved `evil.html` into
   `source/files/`.
3. `GET /source/files/evil.html` → served back as `text/html`, shown in the
   browser, on the app's own address.

Step 3 is the dangerous one. A page on the app's own address can do anything
the app can: read every chat, permanently empty the Recycle Bin, even start the
updater (it passes the "same site" check). `/source/...` serves any file under
`source/` shown in the browser (`server.py:1841`), and so does `/chatfiles/...`
(`server.py:1862`).

The same trick also lets any website, on its own:
- permanently delete everything in the Recycle Bin (`POST /api/recycle-bin/purge {"all": true}`),
- start an import, change labels, add bookmarks, restore an old snapshot, and so on.

Newer Chrome versions ask before a site can reach local addresses, which helps.
Firefox and Safari do not.

**Fix:**
- One check at the top of every POST, PATCH and DELETE: reject the request
  unless `Content-Type` is `application/json` (or multipart only for upload),
  `Origin` is the app's own address when present, and `Sec-Fetch-Site` is not
  `cross-site`. The helper already exists (`_authorized_update_request`);
  make it apply to everything.
- Serve user files and export files as downloads
  (`Content-Disposition: attachment`) plus `X-Content-Type-Options: nosniff`, or
  only allow a short list of safe types (images, PDF, plain text) to show in the
  browser. Never show `.html`, `.svg` or `.xml` from `source/` in the browser.
- Add a `Content-Security-Policy` header to the app page. **Do this after #15**,
  or the policy will block KaTeX loading from the internet and break math.

**Why the security fixes go before the API:** an API feature will add an API
key, cost money per call, and write data. Today any website can trigger actions
in the app. An endpoint like "summarise all chats" would let a stranger spend
your money, and a stored key would be readable.

### 9. Malformed requests crash with no reply — Confirmed

All of these dropped the connection without any reply (the browser sees a
network error), and printed a Python traceback:

- `Content-Length: abc`
- a JSON body that is a list (`[1]`) instead of an object
- `?limit=abc` on `/api/conversations`

Any uncaught error does the same. The front end is the only caller today, so
this rarely shows, but an API invites more varied callers.

**Fix:** one wrapper around every handler: bad input → `400` with a JSON
message, unexpected error → `500` with a JSON message, and the error logged
(#22). The message in the reply is the plain-language one the user will see
(see #23); the technical detail goes only to the log. Make `_read_json_body` reject anything that isn't an object.

### 10. No size limits — From the code

The server reads whatever `Content-Length` says into memory: uploads, notes,
summaries, Scratch Pads, preference values. Some fields are silently cut
(tags at 40 characters, label names at 60, folder names not at all) with no
message.

**Fix:** a request size cap (for example 20 MB, higher for uploads), clear
`413` errors, and documented field limits that return an error instead of
silently cutting. Decide the size limits for AI-written notes and summaries
now. (Import memory use is #31.)

### 23. The screen can say "saved" when it wasn't — From the code

Only 6 of the 76 places the browser talks to the server check whether the
server reported a failure. For example, renaming a chat (`static/app.js:1318`)
shows the new title whether or not the server saved it. Notes do check
properly.

**Fix:** one shared helper for server calls that checks for failure and shows
a message, used everywhere. Do this after #9, so every failure arrives as a
clear JSON error.

**Messages shown to the user are plain language.** Every error message on
screen:
- says in everyday words what didn't happen, and what to do next if there is
  anything to do;
- is short, ideally one sentence;
- contains no error codes, tracebacks, file paths, database terms or
  programming words. Those go only to the error log (#22) and the Error Log
  page (#35).

| Instead of | Show |
|------------|------|
| `HTTP 500` / `Malformed response` | "Something went wrong. Your change wasn't saved. Please try again." |
| `restore failed: database is locked` | "The restore couldn't finish because an import is running. Try again when it's done." |
| `Failed to read DOCX: <python error>` | "This Word file couldn't be opened." |
| `413 Payload Too Large` | "This is too large to save. The limit is 20 MB." |

Some server messages today pass the raw Python error straight to the screen
(for example `restore failed: {e}`, `permanent delete failed: {e}`,
`Failed to read DOCX: {e}`, `audit unavailable: {e}` in `server.py`). Replace
them with plain messages and send the technical detail to the log.

### 35. Error Log page — Requested

A new page that shows the error log from #22.

**Where:** a new **Error Log** item on the ⓘ menu, placed directly above
**About** (the menu today: Import New Chats, Settings, Custom GPTs,
Attachment Report, Import Audit, Add Claude Models, Labels, About).

**What it shows:**
- The **10 most recent error blocks**, most recent first. One block is one
  group from #22, however many times that error happened, so a repeating
  error takes one slot and leaves the other nine for different errors. Older
  blocks stay in the log file but are not shown.
- At the top of each block: the **date and time it last happened**, then
  **how many times** it has happened if more than once (for example "Happened
  7 times"), and **when it first happened**. The first time helps find the
  error that caused the others: the earliest one is usually the cause.
- Then every field from the #22 table for the most recent occurrence, followed
  by the IDs of the other recent occurrences when they differ.
- A **visible separator** between errors, so it is obvious where each one
  starts and stops.
- A **Copy** button under each block. It copies the whole block (count,
  first and last time, and every field of its recent occurrences) as plain
  text ready to paste into a chat or bug report. Screen readers announce when
  it has been copied.
- A **Clear log** button at the top of the page. It asks first ("Clear all
  errors? This can't be undone.") using the app's existing confirmation
  dialog, then empties the whole log file, not only the 10 shown, so cleared
  errors don't come back.
- When there are no errors: "No errors recorded."

**What it captures from the browser:** failed server calls (via the #23
helper), plus any uncaught JavaScript error or unhandled promise rejection.
Each is sent to the server and written to the log, following the same privacy
rule as #22.

**Appearance:** follows `STYLE_GUIDE.md`. Every colour, size, spacing and
weight (including the separator and the Copy button) is a variable in
`static/theme.css`, used through `var(--…)`. Rules go in an existing
component stylesheet, with no inline styles in HTML or JavaScript. It reuses
the existing screen, menu and button patterns and the keyboard/focus
behaviour in `static/accessibility.js`. Run `python scripts/check_styles.py`
afterwards.

**Why it is in session 3:**
- it needs the server log from #22 (session 1);
- the endpoint that receives browser errors needs the front-door checks
  (#1, #3, #9, #10; session 2), or any website could fill the log;
- failed calls are only caught once the #23 helper exists;
- error text can contain anything, so it is shown as plain text, never as
  HTML (the lesson of #2).

### 2. Chat text with `$` signs can inject HTML/JavaScript — Confirmed

The markdown renderer sets aside anything between `$...$` (for math), then puts
it back **without escaping it** (`static/app.js:508` and `:538`). Normal text is
enough to trigger it:

```
I paid $5 for <img src=x onerror=alert(1)> and $10 later
```

comes out as a live `<img onerror=…>` tag. Two dollar amounts on one line with
HTML between them is common in coding chats. Often this just breaks the page
layout; with the wrong text it runs code (with the same reach as #1).

This matters more once the API exists: AI-written text will go through the
same renderer, and conversation content (which may include text pasted from
anywhere) will be sent to the AI and come back.

**Fix:** escape the stashed math text before putting it back (KaTeX reads
the text content, so escaping does not break math). Add a test with the
example above.

Smaller related points: markdown images (`![](https://…)`) load from the
internet, which tells that server when you open the chat; some `escHtml(...)`
calls are used inside HTML attributes, but `escHtml` does not escape `"`.

### 15. KaTeX loads from the internet — From the code

`static/index.html` loads KaTeX 0.16.11 (2024) from `cdn.jsdelivr.net` with no
`integrity=` hash. If that CDN were compromised, it could run code inside the
app with access to every chat. Math also does not render offline, which goes
against "fully local".

**Fix:** keep a copy in `static/`, and update to the current KaTeX release.
This must come before the Content-Security-Policy in #1.

### 16. The updater — From the code

`/api/update` downloads whatever is on the `main` branch of the GitHub repo and
replaces the app's files. It checks that the download matches GitHub, but:

- anyone who can push to `main` (or a compromised GitHub account) can ship code
  that runs on every install, and after the API it would have access to the key;
- there is no version number, changelog or "what's changing" step;
- local edits to app files are overwritten without warning;
- the backup copy of the old files is deleted when the update finishes, so
  there is no undo if the new version won't start.

**Fix:** update from tagged releases rather than `main` (needs #30), show the
version and changes before applying, and keep the last backup in a folder the
user can restore from. Later phases include database changes delivered through
the updater, so an undo copy matters from here on.

---

## Phase 3 findings — one set of rules for every query

### 25. Copy-pasted code — From the code

The main conversation-list query is written out 5 times inside one function
(`_api_conversations`), with small differences. When copies drift, a fix lands
in one and not the others; #13 is this kind of drift. `static/app.js` is 6,600
lines with 242 top-level functions, and `server.py` is 4,600 lines. That isn't
wrong in itself, but it makes changes riskier.

**Fix:** put the shared rules (what "not deleted", "this provider", "display
title" mean) in one place and have every query use them. Do this before #6:
moving tables then means changing one helper instead of every query.

### 13. Quick search ignores the Recycle Bin and the provider toggle — From the code

`/api/search` (`server.py:2380`), used by the sidebar search, does not filter
out deleted chats or chats from the other side (Claude vs ChatGPT), and it
matches the original title, not your renamed one. The Attachment Report also
lists deleted chats. Once #25 is done, these use the shared rules and agree
with the sidebar, Advanced Search and the future API.

---

## Phase 4 findings — database changes

### 6. "Rebuild the index" wipes user data — From the code

The README tells users to run `rm -f history.db && python3 app.py` to rebuild,
including as a troubleshooting step. The rebuild:

- **Deletes** (`build_db.py:840` onward): custom chat titles (renames), Archive
  flags, Recycle Bin flags, loose pins, UI preferences, Compare tabs, the
  import-history table, and chats waiting for review.
- **Reads only `source/conversations.json`.** Every backup brought in with
  "Import New Chats" is renamed to `Provider-conversations-DATE.json`, so the
  rebuild skips them all. Any ChatGPT chats in a Claude-first install vanish
  until you import again.
- **Brings back permanently deleted chats** that are still in
  `conversations.json`, because the build doesn't check `deleted_records`.

Your tags, labels, folders, notes, bookmarks and Scratch Pads survive (they are
in `userdata.db`), but the renames and archive/delete state they sit next to
do not.

**Fix:** move `conversation_meta`, `pinned_conversations` and `ui_preferences`
into `userdata.db` (where the README already says user data lives). Make the
rebuild re-import every recognised backup in `source/` and honour
`deleted_records`. Until then, don't rebuild (Phase 0).

### 7. Permanent delete leaves summaries behind — From the code

`_purge_conversation` (`server.py:1329`) removes tags, labels, notes, bookmarks
and links, but not `conversation_summaries`. If that chat is re-imported later,
its old summary comes back. Rows in `pending_reimport`, saved bulk-label runs
and recent searches that point at the deleted chat are also left.

**Fix:** keep one list of "tables that belong to a conversation", use it for
permanent delete, and add summaries to it. Every table added later (tag IDs,
note versions, AI data) joins this list.

### 5. Artifacts with the same name overwrite each other — Confirmed

**Fixed in session 7.**

Claude names artifacts with short labels like `snake-game` or `solution`. The
`artifacts` table uses that label alone as its unique key (`build_db.py:954`),
and the build keeps only the last one seen across **all** chats
(`build_db.py:1140`). I imported two chats that both had a `snake-game`
artifact: only one was kept, and the other chat's artifact was gone.

Incremental imports do the same: `INSERT OR REPLACE` takes the row from the
other chat, and a later update of that other chat deletes it.
`/api/artifact/<id>` can't say which chat's artifact it means.

**Fix:** make the key `(conv_id, id)` and look artifacts up by both. The lost
artifacts come back at the single re-import at the end of Phase 5. That
re-import is only safe once #6 is fixed.

### 11. IDs — what is stable and what isn't

You asked that everything have a solid ID that never changes. Here is where
each thing stands:

| Thing | ID used | Changes on rename? | Survives rebuild? | Problem |
|-------|---------|--------------------|-------------------|---------|
| Conversation | Export's own UUID | No ✅ | Yes ✅ | Chats with no UUID get a hash of their whole content, so **any** change makes a new ID (rare) |
| Conversation identity | `(id, provider)` in import logic | — | — | The table key is `id` alone; a matching ID from the other provider is skipped, never stored |
| Message | Export's own message id ✅ | — | Yes ✅ | Falls back to position when missing. The internal row number (`messages.id`) changes on every re-import — never expose it in an API |
| Artifact | (chat, Claude's short label) ✅ | — | Yes ✅ | Fixed in session 7 (#5). Artifacts lost before come back at the Phase 5 re-import |
| Label | UUID ✅ | No ✅ | Yes ✅ | — |
| Folder | UUID ✅ | No ✅ | Yes ✅ | — |
| Bookmark | UUID ✅ | No ✅ | Yes ✅ | — |
| Scratch Pad | UUID ✅ | No ✅ | Yes ✅ | — |
| Saved search, snapshot | UUID ✅ | No ✅ | Yes ✅ | — |
| Claude model | The id in `claude_models.json` ✅ (UUID for models added by hand) | No ✅ | Yes ✅ | Fixed in session 7: same id on every install; "Reload Model Data" matches by id |
| Tag / Mood tag | UUID in `udb.tags` ✅ | No ✅ | Yes ✅ | Fixed in session 7. Rows still keep the text; there is no rename yet |
| Custom title, notes, summary | Keyed by conversation ID ✅ | No ✅ | Notes/summaries yes; **custom titles no** (#6) | — |
| Memory | `memory_id`, made from its text ✅ | — | Yes ✅ | Fixed in session 7. The row number still changes; never expose it |
| Project, project doc | Export UUID ✅ | — | Yes | Only loaded at first build, never by "Import New Chats" |
| Custom GPT name | Export's gizmo id ✅ | No ✅ | Yes ✅ | — |

**Fix in this phase:** artifacts (#5), tag and mood-tag IDs, a stable memory
ID, and model reload matching by ID. (Done in session 7.) In Phase 7, give every AI-written item
(each AI note, each AI summary version, each AI run) its own UUID from the
start.

### 34. Notes and Summaries: the last save wins, silently — From the code

**Fixed in session 7.**

Scratch Pads check which version you were editing and refuse a save from an
out-of-date copy. Notes and Summaries don't: if two browser tabs edit the same
chat's notes, the second save quietly erases the first. Once the AI writes
"Notes from AI" or summaries while you have the same chat open, the same thing
can happen between you and the AI.

**Fix:** give Notes and Summaries the same version check Scratch Pads already
have, and have AI runs use it too.

### 19. Smaller issues

Phase 4 (data), both fixed in session 7 (preference keys protected;
snapshots extended):
- `PATCH /api/preferences` can overwrite internal keys such as
  `dataset_format`.
- Snapshots don't cover mood tags, bookmarks, notes, summaries, Scratch Pads,
  related links or model tags (the README says snapshots are not a full
  backup). Either extend them or rely on the full backups from #21.

Phase 6 (polish):
- On every start, if any message has no id, the app re-reads and parses
  **every** backup in `source/` twice before it starts answering. Adding a
  backup re-triggers this. Slow with many large backups.
- `?limit=` has no upper bound on `/api/conversations` and `/api/search`.
- In-chat search treats `%` and `_` in the search text as wildcards.
- Timestamps that fail to parse become `0` (shown as 1970).
- File names with accents can fail to match on macOS (different Unicode
  forms).
- Auto-restart uses `os.execv`, which behaves differently on Windows.

### 39. A rebuild still loses the Compare bar and chats waiting for review — From the code

**Fixed in session 7.**

Found in session 6. After #6, deleting `history.db` keeps renames, Archive,
Recycle Bin, pins and settings, but two smaller things still live in
`history.db` and are lost: the chats on the Compare bar (`workspace_tabs`)
and previously deleted chats held aside for review on the import screen
(`pending_reimport`). The held-aside chats stay deleted (their deletion
records are in `userdata.db`), but the review list is empty, and their
backup is marked as imported, so they can't be offered again.

**Fix:** move both tables into `userdata.db` the same way as #6 (a numbered
step that copies and drops), and keep them in `conversation_tables.py`.

---

## Phase 5 findings — the importer

### 4. Long Claude chats are cut off at 300 turns — Confirmed

`build_db.py:362` stops reading a Claude conversation after 300
question/answer turns (`for _ in range(300)`). I built a 450-turn chat: only
300 turns were saved, and the import still marked it **"normal"**. Nothing
tells you text is missing. There is a second limit of 100 assistant steps in a
row (`build_db.py:425`, `:441`), which long tool-using replies can hit.

This is the "extra long" edge case you suspected, but for long chats, not
long single messages (those are fine, see #17).

**Fix:** remove the fixed limits (the loop already stops on repeats via the
`seen` set), or set them far higher and mark the chat `truncated` in the
import audit when they are reached. The missing turns come back at the single
re-import at the end of this phase.

**Fixed in session 8:** both limits are gone, so nothing is cut off and no
"truncated" mark is needed. The assistant chain stops on a repeated message
(`build_db._assistant_chain`), so a loop in an export still ends.

### 12. Attachments — how they work today

You said you weren't sure how attachments are handled. In short:

**Claude exports**
- A file whose text Claude extracted (PDFs, docs, pasted text) is shown as a
  chip, and its text is searchable. ✅
- Images and other files are listed by name and file id. **The export does not
  contain the files themselves**, so they show only if you put a matching file
  in `source/files/` (the Attachment Report helps with this).
- An entry in `attachments` with **no** extracted text is **dropped
  completely** — no chip, no record (`build_db.py:205`, `if extracted:`). It
  only shows if the same file also appears in the message's `files` list.
- Only `text` content blocks are kept. Thinking, tool use, tool results, web
  search results and citations are dropped, and so is any **new block type**
  Claude adds in future, with nothing in the import audit to say so.

**ChatGPT exports**
- Images point to `/source/<file-id>`. ChatGPT export files are usually named
  `file-<id>-<original name>`, which may not match the lookup, so images may
  not show. **Needs a real export.**
- Other uploaded files (PDFs, spreadsheets) are **not imported at all**:
  ChatGPT messages always get `attachments: None` (`build_db.py:744`).

**Fix:**
- Keep every attachment entry, even without extracted text, so it shows as a
  "missing file" chip and in the Attachment Report.
- Count dropped and unknown content types in the import audit, so a format
  change in a future export is visible rather than silent.
- Check image and file lookup against a real ChatGPT export.
- For the API (Phase 7): decide what an attachment means to the AI. Extracted
  text can be sent as text; images and PDFs can be sent as image or document
  inputs only if the file is actually on disk.

**Fixed in session 8:** every attachment entry is kept and the counts are in
the import audit. The real-export check and the ChatGPT file import are
still open (the user's step after session 9, and Phase 7).

### 8. Import can half-write a chat — From the code

`reconcile_backup` (`build_db.py`) deletes a chat's messages and then inserts
the new ones. If anything fails in between, the error is caught and counted,
the loop moves on, and the half-written state is **committed** at the end. The
chat can end up with no messages, or no search entry.

Also, two imports started at once (a double click) both run; there is no lock.

**Fix:** wrap each chat in a `SAVEPOINT` and roll back to it on error. Allow
only one import at a time (the job system in #33 provides this). The API will
need the same pattern for any multi-step write.

**Fixed in session 8.** A simple lock was enough for one import at a time; #33
still adds progress, cancel and small commits.

### 31. Memory and disk grow faster than the export — Confirmed

I generated a fake Claude history of 3,000 chats (5 to 80 turns each, with
attachments) and ran the app against it.

| What | Result |
|------|--------|
| Export file | 161 MB |
| First build | 18 s, **662 MB of memory** |
| `history.db` | **435 MB** (2.7× the export; mostly the search index) |
| Start-up | ~2 s |
| Sidebar list, open a chat, quick search, Advanced Search | all under 0.2 s ✅ |
| Attachment Report | 0.4 s, 0.7 MB |
| **Media Hub** | **4.3 s, 19 MB sent to the browser** (#32) |
| **Re-importing the same 3,000 chats (newer dates)** | **about 1½ minutes, in one request, no progress shown** (#33) |

Building needs about 4× the export's size in memory, and the database is about
2.7× the export's size on disk. A 1 GB ChatGPT export would need about 4 GB of
memory, and the app gives no warning before running out.

**Fix:** read the export as a stream (one chat at a time) instead of loading it
whole, and check free disk space before building.

**Fixed in session 8.** A made-up 38 MB export (3,000 chats) now builds with
29 MB of memory instead of 347 MB, in 4.2 s instead of 6.8 s.

### 24. Uploading a file silently replaces one with the same name — From the code

`/api/upload-file` overwrites an existing file in `source/files/` without
asking. Two different chats can each have an attachment called `image.png`.

**Fixed in session 8:** the server answers `409` unless the upload says
`replace`, and the Attachment Report asks first.

### 33. Import blocks other changes and other copies of the app — Confirmed / From the code

An import holds a lock on `history.db` until it finishes. While it runs:
- Changes stored in `userdata.db` (labels, notes, tags) still work.
  Confirmed.
- Renames, archive, delete and pins (stored in `history.db` until #6 moves
  them) wait 10 seconds, then fail with no reply (#9). From the code.
- A second copy of the app **could not start at all**. It crashed with
  "database is locked". Confirmed.

**Fix:** one background-job system — each job has an ID, progress and cancel,
runs one at a time, and commits in small batches instead of one huge
transaction. Imports use it first; AI runs reuse it in Phase 7.


**Fixed in session 9.** One shared background runner handles new imports,
review imports and rebuilds with progress, cancellation and small commits.
A second app starts during an import; a second job gets a plain refusal.

---

## Phase 6 findings — speed

### 32. The Media Hub loads everything at once — Confirmed

**Fixed in session 10.** See the session 10 notes for paging and privacy behavior.

It reads every message in the database and sends one response with every
image, attachment and artifact. At 3,000 chats that is 4 seconds and 19 MB; at
30,000 chats expect roughly 40 seconds and 200 MB. Load it in pages, like the
sidebar does.

### 17. Long messages and long chats — Confirmed

**Fixed in session 10.** See the session 10 notes for paging and privacy behavior.

Long **single messages** are fine. I timed the markdown renderer on a 1.3 MB
single line of JSON (0.15 s), a 2.3 MB log paste (0.4 s), and lines designed
to trip up its patterns. None hung.

Long **chats** are heavier: opening a chat sends every message in one response
and draws them all at once, with KaTeX scanning each one. A chat with thousands
of messages will be slow. Load long chats in pages, using the same paging
shape the API will use.

For the API, the bigger issue is size: a long chat may be more text than one
AI request can take. The current Claude models accept up to 1M tokens of input
(Claude Haiku 4.5: 200K), but cost scales with length. The API code should
count tokens first (the API has a token-counting endpoint) and either split
long chats or tell the user — never cut them off silently (see #4).

### 36. Leftover technical words and quiet failures on screen — From the code

**Fixed in session 10.** See the session 10 notes for paging and privacy behavior.

Found in session 3. Two small gaps left after #23:

- The size-limit messages from #10 name the field as the code does
  ("title is too long (at most 1,000 characters).", "condensed_summary is too
  long …") and a few checks say "`<field>` must be text" (`server.py`,
  `request_limits.too_long`). They should use everyday names ("The title is
  too long …").
- Some screens still catch a failure and carry on quietly on purpose (for
  example the Media Hub thumbnails, link previews in Scratch Pads, the model
  strip). `apiRequest` logs each one and, unless the screen shows its own
  message, shows the notice; a handful of background loads pass
  `notify: false` and show nothing. Review them once the API work shows which
  matter.

### 37. Markdown images load from the internet — From the code

**Fixed in session 10.** See the session 10 notes for paging and privacy behavior.

Already noted under #2. The Content-Security-Policy added in session 3 still
allows `https:` images (`img-src`), because chat text can contain markdown
images and blocking them would change what chats show. Opening such a chat
tells that server you opened it. Decide whether to block them (one word in
`CONTENT_SECURITY_POLICY`) or show a "load images" button.

### 38. The Media Hub shows both sides — From the code

**Fixed in session 10.** See the session 10 notes for paging and privacy behavior.

Found in session 5. `/api/gallery` leaves out the Recycle Bin and shows
renamed titles (it uses the #25 rules), but it takes no `provider`, so the
Media Hub lists Claude and ChatGPT chats together while every other list
follows the sidebar toggle. Fix with #32: when the Media Hub loads in pages,
pass the side and add `and_provider()` to its two queries. The unlinked
images from `source/` belong to no chat and stay as they are.

---

## Phase 7 — the API checklist

Things to decide or build in from the start, so they don't become the next
round of surprises:

1. **Where the API key lives.** Not in `userdata.db` (it is meant to be copied
   between machines) and not in preferences (any page can read them through
   `GET /api/preferences`). Use the `ANTHROPIC_API_KEY` environment variable or
   a `.env` file (the updater already protects `.env`), and never send the key
   to the browser.
2. **Long-running work uses the background-job system from #33.** Summarising
   many chats takes minutes. For large batches the Message Batches API costs
   half as much.
3. **Cost control.** Count tokens before sending, show an estimate, set a
   maximum per run, and log each call's token usage (#22).
4. **Treat AI output as untrusted text.** It goes through the same markdown
   renderer as chat text (fixed in #2). Chat content sent to the AI can contain
   instructions ("ignore the above…"); the AI should only write to the fields
   you allow (for example "Notes from AI"), never act on the chats directly.
5. **Every AI-written item gets a UUID and a record of what made it**: model,
   date, which chat and message IDs it read, and which run produced it. The
   `summary_author` field ("personal", "ai", "collaborative") is a start. New
   AI tables join the conversation-table list from #7 and use the version
   check from #34.
6. **Don't hard-code old models.** Current Claude models (October 2026) include
   `claude-opus-5-5`, `claude-sonnet-5-5` and `claude-haiku-5-5`. Keep the
   model name in one setting, and use the Models API to list what is available
   instead of copying names into code. (`claude_models.json` is a different
   list: the claude.ai models a chat was written with. Don't mix the two.)
7. **Use the official `anthropic` Python SDK** (Python 3.10+; pinned in
   `requirements.txt` from #14). It handles retries for rate limits and server
   errors. Check `stop_reason` on every reply (`max_tokens` means the answer
   was cut off; `refusal` means it declined), and handle each case in the UI.
   *Session 11 check (2026-10-21):* PyPI has `anthropic` 2.0.0, released
   that same day, and 1.17.0 before it. Session 12 reads the 2.0.0 changelog
   and picks the pin; see the Phase 7 plan.
8. **Privacy.** Sending chats to the API sends private data off the machine.
   Make it explicit, per run, which chats are sent.
9. **Backups before AI runs** (#21), so a bad run can be undone.

---

## Phase 7 plan (session 11)

Written in session 11 (2026-10-21). Planning only: no API calls, no code, no
release. The real-export check and the one re-import from Phase 5 are still
the user's own pending steps; this plan does not depend on them for coding.
One caution: run the one re-import **before the first real AI run on real
chats**, not after. AI results will point at message IDs, and the re-import
is when cut-off turns (#4) come back. The rebuild keeps everything in
`userdata.db`, so AI results made earlier are not lost, but results made
from chats that were still cut off would describe incomplete chats.

### What "API access" means in this plan

The app (the Python server, never the browser) calls the Claude API to read
chats the user picks and write text back into fields the user allows. The
browser never sees the key and never talks to the provider. The AI never
acts on chats (no deleting, renaming, labelling or moving); it only returns
text that the app saves into an allowed field.

### What already exists and gets reused

| Need | Already built | How the API work uses it |
|---|---|---|
| Long work with progress and Cancel | `background_jobs.start(db, kind, callback)`, `Job.progress`, `Job.check`, `Job.batch`, `/api/jobs/<id>`, `/api/jobs/<id>/cancel` | An AI run is a job of kind `ai`. Progress counts chats. Cancel stops between chats. No second lock or worker. |
| One job at a time | The OS lock in `background_jobs.hold` | An AI run blocks imports while it runs, and the other way round. Message Batches can take up to 24 hours, so a batch run must **not** hold the lock while it waits (see step 3). |
| Backups | `backups.backup_all(db, reason)` | Every AI job backs up first, like imports (`reason="ai-run"`). |
| Database changes | `schema_changes.apply_steps` with `USERDATA_SCHEMA_STEPS` | New tables are userdata change 5, appended, with the automatic backup. history.db stays at 6. |
| Permanent delete covers new tables | `conversation_tables.USERDATA_TABLES` | Every new AI table with a `conversation_id` joins this list. |
| No silent overwrite | `version` on notes and summaries, `_base_version`, `_check_version` (409) | The run reads the field's version when it starts and saves with that `base_version`. If the user edited the field during the run, the save is refused and the result is kept as unsaved (see D4). |
| Who wrote a summary | `condensed_author` / `summary_author` = `personal`, `ai`, `collaborative` | AI saves use author `ai`; the existing colours and "collaborative" logic stay as they are. |
| Fields waiting for the AI | "Notes to the AI", "Notes from the AI" (sidebar and full Notes editor) | Notes to the AI can be part of what is sent; Notes from the AI is a natural place for results. |
| Stable IDs | Conversation IDs, message `message_uuid`, `message_ids.locate()` / reanchoring | Provenance records which message UUIDs were read, so a later re-import can still find them. |
| Picking many chats | The bulk tool's criteria, `/api/labels/bulk-preview` and `bulk-preview-list` (Preview filters the list), Advanced Search results | Reuse the same criteria and Preview to choose the chats for a run. |
| Safe logs | `errorlog.py` privacy rule: IDs, sizes, names, never chat text or keys | The request log follows the same rule by default (see D9). |
| Failure messages | `apiRequest()`, `showAppNotice()` | All new screens use them; no new error styles. |
| Rendering AI text | The markdown renderer with the #2 math escape | AI text is untrusted and goes through the same renderer as chat text. |
| Long chats | Chat detail is paged (session 10) | The context builder reads every message from the database directly; it never relies on one detail page. |
| Key protection | The updater already leaves `.env` and `.env.local` alone | The key lives in the environment or `.env`. |

### Provider facts checked on 2026-10-21

From Anthropic's pricing page and PyPI. **Reference for the user's decisions
only.** The app never contains these prices or model names (see "Nothing
hard-coded"); they will go out of date. Prices are US dollars per million
tokens, standard (not batch) rates.

| Model | ID | Input | Output | Batch input / output | Notes |
|---|---|---|---|---|---|
| Claude Opus 5.5 | `claude-opus-5-5` | $4 | $20 | $2 / $10 | 1M-token context |
| Claude Sonnet 5.5 | `claude-sonnet-5-5` | $2 | $10 | $1 / $5 | 1M-token context |
| Claude Haiku 5.5 | `claude-haiku-5-5` | $0.10 | $0.50 | $0.05 / $0.25 | Prompts over 100,000 tokens cost $0.50 / $2.50 (batch $0.25 / $1.25) |

- Message Batches: half price, results can take up to 24 hours, no fast mode.
- Token counting (`messages.count_tokens`) gives the input size before sending.
- Models API (`models.list()`) lists available model IDs, context size
  (`max_input_tokens`) and output cap (`max_tokens`).
- Stop reasons to handle: `end_turn` (normal), `max_tokens` (cut off),
  `refusal` (declined; `stop_details` says why), `pause_turn`, `stop_sequence`.
- On Opus 5.5 and Sonnet 5.5 thinking cannot be switched off; cost is
  controlled with `output_config.effort` (Opus 5.5 defaults to `medium`).
  Older request shapes such as `budget_tokens` are rejected.
- Python SDK: `anthropic` 2.0.0 was released on 2026-10-21 (needs Python
  3.10+; this app needs 3.12, so fine). 1.17.0 is the last 1.x. Session 12
  reads the 2.0.0 changelog and pins one exact version. It is an
  **optional** dependency: the app must still start and work without it, with
  AI features shown as unavailable.

### How a run works (the technical shape; no product choices in it)

1. The user picks chats and a task, and the screen shows what will be sent
   (chat count, message count, attachments counted by kind).
2. **Estimate:** the server builds the exact request text and asks the token
   counter for its size, then shows the input tokens and a maximum output
   (and a price only as D11 decides). Nothing is sent to the model
   yet. (Counting sends the text to the provider; see D6.)
3. **Confirm:** the user confirms. The server starts an `ai` job; the receipt
   is shown with the existing progress/Cancel controls.
4. **Job:** back up → for each chat: `job.check()` → build context from
   message rows by ID → call the model → check `stop_reason` → save the result
   with `base_version` in its own small transaction → record token usage →
   `job.progress()`. One failed chat is recorded and the run continues (or
   stops; see D7).
5. **Finish:** the job result lists saved, refused (version conflict), cut off,
   declined and failed chats, and the total tokens.

### New data (userdata change 5, planned; names may change in session 12)

- `ai_runs`: run ID (the job ID), task, provider, model, settings used,
  created/finished time, status, estimated and actual input/output/cache
  tokens, provider batch ID if any. Never chat text.
- `ai_outputs`: UUID, run ID, conversation ID, target field, the field's
  version before and after, status (`saved`, `conflict`, `cut_off`,
  `declined`, `failed`), the result text (it is user data now, like a note),
  the first and last message UUID read and the message count, model,
  created time. Joins `conversation_tables.USERDATA_TABLES`.
- AI prompt templates, if D3 says they are editable, are stored like other
  user data in `userdata.db`, never in `ui_preferences` (any page can read
  those).
- No key, no token, no provider account ID anywhere in either database.

### Nothing hard-coded (user's rule, session 11)

The user asked that nothing in the API work be hard-coded. In practice:

The user's own words: don't type in Anthropic's prices or the models.

- **Models** come only from the Models API (`models.list()`: ID, display
  name, context size, output cap, capabilities such as batch and effort).
  The user picks one; the pick is a setting. No model name or ID is typed
  into the app's code or data files (tests use made-up names with a fake
  client). If no key is set, there is simply no model list yet.
- **Prices** are not typed in anywhere. Anthropic has no API that returns
  prices (checked 2026-10-21: the Models API has none), so the app counts
  **tokens**, which the API does report exactly. How to show money is D11.
- **Instructions sent to the AI** (per task) live in a data file or in
  `userdata.db` if D3 makes them editable. No prompt text in code.
- **Limits and defaults** (per-run token limit, maximum output, effort per
  task, how many log entries to keep) are named settings with one default
  each, kept in one place.
- **Screen values** follow CLAUDE.md and STYLE_GUIDE.md: colours, sizes and
  weights are CSS variables in `theme.css`; screen text stays in the markup.
- Tests check that the code reads these from their one source.

### Keys and privacy, fixed rules (not open questions)

- The key is read on the server, at call time, from `ANTHROPIC_API_KEY` or
  `.env` in the app folder. It is never written to a database, preferences, a
  receipt, the error log, the request log, or any reply to the browser. The
  browser only ever learns "a key is set: yes/no".
- `.env` stays out of git (`.gitignore`, fixed in session 11 as #40) and is
  already protected from the updater.
- The SDK's own debug logging (`ANTHROPIC_LOG`) stays off by default, since
  it can print request bodies.
- Job receipts hold counts and IDs only, never prompts or results (the
  rule already stated in `background_jobs.py`).
- Chat text in a prompt is marked as quoted material; the instructions say
  to treat it as data. The model gets no tools, so it cannot act on anything.

### Decisions the user still needs to make

These are the choices the repository and earlier instructions don't answer.
Each has a suggestion; nothing here is decided until the user answers.

- **D1. Provider(s).** Claude (Anthropic) only, or also OpenAI for the
  ChatGPT side? *Suggestion:* Claude only for now, with a `provider` column
  so another can be added later. Adding OpenAI later needs its own
  documentation check.
- **D2. Model choice.** One default model in Settings, with an optional
  per-run change? Which default: Opus 5.5 (best, $4/$20), Sonnet 5.5
  ($2/$10) or Haiku 5.5 (cheapest, $0.10/$0.50)? Should the effort level
  (low/medium/high) be shown to the user or fixed per task?
  *Suggestion:* a Settings dropdown filled from the Models API, a per-run
  dropdown on the confirm dialog, effort fixed per task and not shown.
- **D3. Which tasks come first, and who writes the instructions.** Options:
  (a) write the condensed summary, (b) write the full summary, (c) write
  "Notes from the AI", (d) answer a free question about one chat, (e)
  answer a question across several chats. Are the instructions built into
  the app, editable in Settings, or typed each run? Is "Notes to the AI"
  always sent with its chat?
  *Suggestion:* start with (a)–(c); built-in instructions the user can edit
  in Settings; "Notes to the AI" sent when not empty.
- **D4. Where results appear and how they are saved.** Save straight into
  the field (backed up and version-checked), or show a draft to accept or
  discard first? If the field already has text: replace, add below, or
  ask? Where do answers to a free question (d/e) go: a Scratch Pad, Notes
  from the AI, or only on screen?
  *Suggestion:* show a draft in the existing summary/notes editors with
  Accept and Discard; never replace existing text without asking; free
  answers go to a new Scratch Pad.
- **D5. Which chats can be sent, and from where.** The open chat only? Chats
  picked with the bulk tool's criteria and Preview? Advanced Search
  results? The Compare tabs? Are archived and Recycle Bin chats ever
  included?
  *Suggestion:* the open chat (a button on the chat's ⋮ menu or its
  Notes/Summary editor) and bulk-tool criteria with Preview; Recycle Bin
  never; archived only if the criteria say so.
- **D6. What is sent from each chat.** All messages, or only the current
  branch (edits create branches)? Attachments: extracted text, images and
  PDFs on disk, artifacts? Hidden/system messages? Can a single run be
  told "no attachments"? Is the token count (which sends the text to the
  provider without running the model) acceptable before confirming?
  *Suggestion:* the visible branch; extracted attachment text and artifacts
  yes; images and PDFs off unless ticked for that run; token counting yes.
- **D7. Cost limits and confirmation.** A maximum per run in tokens (or in
  dollars, if D11 allows prices)? Always confirm, or only above an amount? A monthly total shown
  anywhere? If one chat fails, stop the run or skip it? What happens to a
  chat larger than the model's context (refuse it, or split it)?
  *Suggestion:* always confirm with the estimate; a per-run token limit in
  Settings (the run stops before going over); skip failed chats and list
  them; refuse chats too large and say so, never cut them silently.
- **D8. Message Batches for many chats.** Offer "cheaper but can take up to
  a day" as an option when several chats are picked? *Suggestion:* yes,
  as a choice on the confirm dialog, off by default.
- **D9. The visible request log.** Where: a page on the ⓘ menu beside the
  Error Log? What it shows: time, task, model, chat count, tokens,
  stop reason, provider request ID, status (all safe). Should it also be
  able to show the exact text sent and received? That text is private chat
  data, so it would break the error log's privacy rule unless it is kept
  separately. *Suggestion:* metadata only by default, plus an opt-in "keep
  full request text" switch that stores it in a local folder beside the
  databases (excluded from git), keeps the last N runs, and has a Clear
  button.
- **D11. Showing cost without typed-in prices.** The app will not contain
  prices. Choose one: (a) show tokens only, with a link to Anthropic's
  pricing page; (b) Settings has an empty price box for each model you use,
  which **you** fill in from the pricing page, and dollars are shown only
  when filled; (c) the app reads the pricing page from the internet (it
  breaks whenever that page changes; not recommended). *Suggestion:* (a)
  now, (b) only if you want dollar figures.
- **D10. Entering the key.** Only by editing `.env` / the environment, or
  also a Settings box that writes `.env` on the server (and never shows the
  key again)? *Suggestion:* a Settings box that writes `.env`, shows only
  "key set" and has a Remove button; editing `.env` by hand keeps working.

### Implementation steps (one session each unless noted)

1. **Foundation (session 12, no decisions needed).** Follow "Nothing
   hard-coded" above. Pin the SDK as an
   optional dependency and handle "not installed". Key reading from the
   environment / `.env`, `GET /api/ai/status` returning only
   `{sdk_installed, key_set}`. Userdata change 5 (`ai_runs`, `ai_outputs`),
   added to `conversation_tables`. A context builder that reads one chat's
   messages by ID from the database (all pages, visible branch and full
   tree both available) and reports counts. A token estimator (token
   counting through the client, so the fake client in tests); no prices. A metadata-only request log module
   following `errorlog.py`'s privacy rule. An `ai` job callback that runs on
   a **fake client** in tests: backup, version-checked save, cancel between
   chats, usage recorded, each `stop_reason` handled. Also move the test
   workflow off Node 20 (#41). No real API call, no
   new screens, no CSS. ENDPOINTS.md updated for the status endpoint. Plus
   a test that the key never appears in any reply, receipt, or log.
2. **First real task on one chat (needs D1–D7).** The real client behind the
   same interface; token count, estimate and confirm dialog (existing
   `.modal` classes), the run's progress with the existing job controls,
   results shown and saved as decided in D4. Settings rows for model, key
   (D10) and limits, using `.settings-*` classes.
3. **Many chats (needs D5, D8).** Chat picking with the bulk tool's criteria
   and Preview. If batches are chosen: one short job submits the batch and
   records its ID, the lock is released, and a later short job (on startup,
   or a "Check results" button) collects finished results and saves them.
4. **Request log screen (needs D9).** Reuse the Error Log page's layout and
   classes where they fit; add `--ai-log-*` variables only as aliases of
   existing values.

Each implementation session follows the normal rules: tests first,
`check_styles.py`, CHANGELOG and version bump for code changes, one pull
request, `@codex review` (code changes only, rule 8), merge when green. STYLE_GUIDE.md: no new colours
or sizes in rules; any new component variables alias existing ones in
`theme.css`.

---

## Phase 7 findings

### 40. `.env` is not in `.gitignore` — From the code

Found in session 11. The API checklist puts the key in `ANTHROPIC_API_KEY`
or a `.env` file, and the updater already leaves `.env` and `.env.local`
alone, but `.gitignore` lists neither. A user who creates `.env` in the app
folder and runs `git add .` would commit the key to a public repository.
**Fixed in session 11** (at the user's request): `.env` and `.env.local`
are now in `.gitignore`.

### 41. Tests run on an old Node version — From the code

Found in session 11. `.github/workflows/tests.yml` sets up Node 20 for the
browser-code tests. Node 20 stopped getting security fixes in April 2026.
Nothing is broken today, and it only affects the automatic tests, not the
app. **Fix in session 12:** move the workflow to a supported Node version
(check which ones are current) and make sure the Node tests still pass.

---

## Not covered by this audit

- Running the app on Windows or macOS (I tested on Linux).
- Real Claude and ChatGPT export files (I used made-up ones). This is a step
  in Phase 5.
- Accessibility and visual design. `STYLE_REVIEW.md` covers some of this.

---

### Session 10 notes for later sessions

- Phase 6 is complete without the separate Phase 5 real-export verification or
  one-time rebuild. Both Phase 5 checkboxes and the user-step row remain pending.
- Version is 1.9.0; schemas stay history 6 and userdata 4. No migration needed.
- Conversation detail now returns bounded pages (`offset`, `limit`, `total`,
  `next_offset`); `target_seq` finds a message's page. Do not assume a detail
  response contains an entire long chat when implementing AI context selection.
  Page boundaries keep a user and its immediate response together. The server
  also returns `previous_offset`; clients must use it instead of subtracting
  the limit. Edges are canonical, including on pages expanded by one message.
- `renderConversationMessages()` is the shared renderer; `showConversationPage()`
  and `ensureConversationMessage()` serve page navigation and target jumps.
  Search and bookmarks use them; Scratch Pad links already open by target seq.
- Gallery pages bound attachment/artifact entries, use the shared provider rule,
  and retain globally unlinked images. Provider switches reload an open gallery
  and invalidate old Load more controls before fetching the new side. Counts in the browser are explicitly
  labelled loaded items. New API parameters/response metadata are in ENDPOINTS.md.
- Markdown external images render inert buttons, then load per click with
  `referrerPolicy = "no-referrer"`. HTTPS stays in the image CSP for this explicit
  action. Local source/files/chatfiles image paths still render automatically.
- Legacy ID repair caches individual file signatures per BACKFILL_VERSION.
  Only new or changed backups are parsed; unchanged exports are never rescanned
  merely because another file arrived. Exact-revision, provider and text matching
  remain, with no duplicate backup pass. Temporary
  read failures are logged and retried on the next startup, not marked complete.
- Unknown imported dates are null, not fabricated epoch dates. Filename lookup
  normalizes NFC in Python and JavaScript. Windows spawns a replacement process
  which waits for the old port to close before binding.
- Synthetic regression coverage: tests/test_phase6.py and tests/phase6_ui.cjs.
  Validation: all 349 tests passed; the style check and changed-JavaScript syntax
  checks passed. Native Windows/macOS execution and real exports were not used.

---

### Session 11 notes for later sessions

- Planning only. No code, no schema change, no version bump, no release.
  Version stays 1.9.0; schemas stay history 6 and userdata 4.
- The plan is the [Phase 7 plan](#phase-7-plan-session-11) in Part B: what is
  reused, provider facts checked on 2026-10-21, the run shape, planned
  userdata change 5, fixed key/privacy rules, decisions D1–D11, and steps 1–4.
- Session 12 (step 1) needs no product decisions. Steps 2–4 wait for the
  user's answers to D1–D11. Never fill a decision with the suggestion
  without the user saying so.
- New finding #40: `.env` was not in `.gitignore`. Fixed in this session at the
  user's request. New finding #41 (tests on Node 20) goes into session 12.
- Rule 8 changed at the user's request: Codex reviews only pull requests
  that change code. Documentation-only pull requests merge once CI passes.
- The user asked that nothing be hard-coded. See "Nothing hard-coded" in the
  Phase 7 plan; every implementation session follows it.
- `anthropic` 2.0.0 came out on 2026-10-21, the day of this session. Read its
  changelog before choosing between it and 1.17.0; pin one exact version.
- Order note: session 10 ran before the user's Phase 5 real-export check and
  re-import. That is harmless for Phase 6 and for building Phase 7, but the
  re-import should happen before the first real AI run on real chats (see
  the Phase 7 plan).

---

# Next session prompt

**Next: session 12 — Phase 7 step 1, the AI foundation.**

The real-export checks and one-time rebuild remain separate, pending user tasks.
They are not prerequisites for this session. Decisions D1–D11 in the Phase 7
plan are still open unless the user's message answers them.

```text
Read AUDIT.md, starting with "Session instructions (read first)", and follow
its rules. Sessions 1–11 are complete (session 11 was planning only). Start
session 12: Phase 7 step 1, the AI foundation. Read CLAUDE.md, STYLE_GUIDE.md,
the Phase 7 checklist, the "Phase 7 plan (session 11)" and finding #41.

If my message answers any of decisions D1–D11, record the answers in the
Phase 7 plan first. Do not treat a suggestion as a decision I made. This
session builds only step 1, which needs no decisions.

The real-export verification and one-time rebuild from Phase 5 remain pending
as separate user tasks. Do not mark them complete. Do not request personal
exports, local file paths, manual testing, or access to my computer. Do not
touch private data, and make no real API call: tests use a fake client.

Nothing may be hard-coded: follow "Nothing hard-coded" in the Phase 7 plan
(no Anthropic prices or model names typed in anywhere; models come from the
Models API; AI instructions, limits and screen values each come from one
data file, setting or CSS variable).

Step 1: fix #41 first (move the test workflow off Node 20). Read the anthropic
2.0.0 changelog and pin one exact SDK version as an optional dependency; the
app must start and work without it. Read the key only on the server, from
ANTHROPIC_API_KEY or .env, and add GET /api/ai/status returning only
{sdk_installed, key_set}. Add userdata change 5 (ai_runs, ai_outputs) as a
numbered step with the automatic backup, and add the new tables to
conversation_tables. Build the context builder (one chat's messages by ID
from the database, all pages, visible branch and full tree, with counts),
a token estimator (no prices and no model names typed in anywhere; models
come only from the Models API), a metadata-only request log that
follows errorlog.py's privacy rule, and an "ai" background-job callback
using background_jobs.start, backups, base_version saves, cancel between
chats, usage records and every stop_reason. Add tests proving the key never
appears in a reply, receipt, preference, database or log. No new screens or
CSS. Update ENDPOINTS.md.

Current schema versions are history 6 and userdata 4. Run the full tests and
python scripts/check_styles.py. Bump the version and CHANGELOG for the code
change. Update AUDIT.md (tick step 1, session table, session 12 notes, next
prompt). Use one pull request. It changes code, so comment @codex review,
address reasonable findings, and merge only after Codex has reviewed and CI
passes on the latest commit. Paste the full next-session prompt in a writing block in the final
response.
```
