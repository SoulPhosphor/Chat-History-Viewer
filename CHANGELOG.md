# Changelog

The version number lives in `version.py`. Each release adds an entry here,
newest first.

## 1.10.0 — 2026-10-28

Bookmark fixes, message versions, editing, hiding and sticky notes.

- **Bookmarks:** the header bookmark icon no longer shows when a chat has no
  bookmarks. A new bookmark is named in a field with a Save button in line
  with its icon, instead of a popup.
- **Import:** "Review deleted conversations that have new messages" is a toggle
  (off by default).
- **Every reroll has a fixed id.** Each saved version of a message keeps the
  export's own id. Chats imported before this get the ids from the backups in
  `source/` at start-up; no re-import needed.
- **Message ⋮ menu:** Create Sticky Note, Copy Message Link, Copy Message, Edit
  Message, Hide Current Message. Links can point at one version or one edit.
- **Edit Message:** one edit per version, saved only with Save. The original is
  always kept, and a re-import never replaces an edit.
- **Version switcher** under each message, right aligned, with `<` and `>`.
  Once a message has an edit it names the version (Original, Edited, Response 2
  Edited, Prompt 1…).
- **Hide Current Message:** hidden versions are skipped and listed at the end,
  with Restore. New Chat Settings toggle "Show hidden message placeholder" (on
  by default), and a header eye that shows the placeholders for the open chat.
- **Sticky Notes:** one note per message, with its own fixed id, title and rich
  text (links included). Header icon, bar and panel follow the bookmark rules.
  Advanced Search has a Sticky Notes text location and "Must Have Sticky Notes".
- Database: userdata change 5 adds `message_edits`, `hidden_messages` and
  `sticky_notes` (backed up automatically before it runs).

## 1.9.0 — 2026-10-20

Repair session 10 from `AUDIT.md` (Phase 6: #32, #38, #17, #19, #36, #37).

- **Media Hub loads in pages and follows the Claude/ChatGPT toggle.** Load more
  media as needed. Category counts say how many items have been loaded; unlinked
  images on disk stay available on either side.
- **Long conversations load in pages.** Previous/Next messages keep every message
  available. Search, bookmarks and internal links open the page containing their
  target, including attachments, artifacts, retries and math.
- **External Markdown images wait for your click.** Each image has a Load external
  image button. Loading it contacts its website, without sending the app page as
  a referrer. Local exported images continue to display normally.
- **Polish and reliability:** the legacy message-ID repair no longer rechecks old
  exports whenever a new file arrives; search treats `%`, `_` and backslashes
  literally; broken dates stay unknown; accented file names match across Unicode
  forms; Windows restarts wait for the previous process to release its port.
  Size-limit messages use readable field names, and failed thumbnails and link
  previews show a message.
- No database schema changes. The real-export verification and one-time rebuild
  remain pending as separate user tasks.

## 1.8.0 — 2026-10-19

Repair session 9 from `AUDIT.md` (Phase 5 part 2: #33).

- **Imports run in the background.** The import screen shows what is happening,
  how many chats have been checked or processed, and a Cancel button. You can
  leave the screen or reopen the app and reconnect to the job.
- **Cancel keeps the chats already saved.** Complete chats are saved in small
  batches. The screen says what was kept; an unfinished export can be imported
  again to finish, without creating duplicate chats.
- **One job runs at a time across copies of the app.** A second import gets a
  clear message asking you to wait. Another copy of the app can open while an
  import runs, and notes and other saved choices remain available.
- **Review imports and rebuilds use the same progress and cancellation.** The
  existing import screen now has a Rebuild from exports button. A rebuild keeps
  your saved choices and makes a database copy first. Cancelling a rebuild leaves
  the chat history incomplete until you rebuild again; saved user choices stay.
- **Nothing is re-imported by this update.** Checking your real exports and the
  one planned re-import are the next step.
- **Going back:** this release does not change either database's layout
  (history change 6, user-data change 4). Going back to 1.7.0 does **not** need
  a database copy. Stop any running job before going back. To undo a partial
  rebuild rather than finish it, restore the database copies made before it.

## 1.7.0 — 2026-10-19

Repair session 8 from `AUDIT.md` (Phase 5 part 1: #4, #12, #8, #31, #24).

- **Long Claude chats are no longer cut off.** The importer used to stop
  after 300 turns, and after 100 steps in one tool-using reply. There is no
  limit now. Chats that were cut off come back whole at the one re-import
  planned after the next session; nothing is re-imported now.
- **Every attachment is kept.** A file Claude listed without any text used
  to vanish. It now shows as a missing-file chip and in the Attachment
  Report, so you can add the file yourself.
- **The Import Audit lists what was left out.** It now shows how much of
  each kind of content the app leaves out on purpose (such as Claude's
  thinking and tool use), and any kind of content it doesn't know yet, which
  would mean the export format has changed. Files imported before this
  version show these counts after the re-import.
- **An import can no longer half-write a chat.** Each chat is written
  completely or not at all; a chat that fails is left as it was, recorded in
  the Error Log, and the rest carry on.
- **Only one import runs at a time**, also across two copies of the app. A
  second click says an import is already running instead of starting
  another.
- **Big exports need far less memory.** Exports are read one chat at a time
  instead of all at once. A 38 MB test export now builds with about 30 MB of
  memory instead of about 350 MB, and a little faster.
- **Free disk space is checked first.** Building or importing stops before
  changing anything when the drive doesn't have about three times the
  export's size free, and says how much is needed.
- **Upload asks before replacing a file.** If a file with the same name is
  already saved (two chats can each have an `image.png`), the Attachment
  Report asks before replacing it.
- **Going back to 1.6.0 needs the database copy.** This version changes
  `history.db`, and 1.6.0 will not start with the changed file. To go back,
  undo the update, then stop the app and copy the newest
  `history-…-before-db-change.db` from `backups/` over `history.db`.
  `userdata.db` is not changed. Imports done since the update are lost
  (import them again).

## 1.6.0 — 2026-10-19

Repair session 7 from `AUDIT.md` (Phase 4 part 2: #5, #11, #34, #19, #39).

- **Artifacts with the same name in different chats no longer replace each
  other.** An artifact now belongs to its chat. Artifacts that were already
  lost this way come back at the one re-import planned after the importer
  fixes; nothing is re-imported now.
- Tags and mood tags each have an id that never changes, memories have an id
  that stays the same after a rebuild, and Claude models use the same id on
  every install. **Reload Model Data** now matches models by that id, so a
  model you renamed keeps its name and is not added a second time.
- **Notes and Summaries are protected like Scratch Pads.** If another window
  saved them after you opened them, your save is refused with a message
  instead of quietly replacing the other change. Your text stays on screen
  to copy.
- Saving screen settings can no longer change what the app records about
  your export (which side it came from).
- **Safety Snapshots now cover everything you added**: also mood tags,
  bookmarks, notes, summaries, Scratch Pads, related links and model tags.
  An older snapshot restores only what it holds and leaves the rest alone.
- **Rebuilding now keeps the Compare bar and the chats waiting for review**
  on the import screen. Both are moved into `userdata.db` automatically the
  first time this version starts.
- **Going back to 1.5.0 needs the database copies.** This version changes
  `history.db` and `userdata.db`, and 1.5.0 will not start with the changed
  files. To go back, undo the update, then stop the app and copy the newest
  `history-…-before-db-change.db` and `userdata-…-before-db-change.db` from
  `backups/` over `history.db` and `userdata.db`. Changes made since the
  update are lost.

## 1.5.0 — 2026-10-19

Repair session 6 from `AUDIT.md` (Phase 4 part 1, #6 and #7).

- Your chat renames, Archive and Recycle Bin, pinned chats and screen
  settings now live in `userdata.db` with the rest of your own data. They are
  moved there automatically the first time this version starts.
- Rebuilding is safe again. If you delete `history.db` and start the app, it
  is built from every export in `source/`, including the ones brought in with
  Import New Chats, and chats you deleted permanently stay deleted. Your
  renames, Archive, Recycle Bin, pins and settings are kept. The Compare bar
  and chats waiting for review on the import screen are not kept yet.
- Deleting a chat permanently now also removes its summaries, its place in
  unfinished bulk label runs, and any copy waiting for review. All the places
  a chat's data is kept are listed in one file (`conversation_tables.py`).
- **Going back to 1.4.0 needs the database copy.** This version changes
  `history.db`, and 1.4.0 will not start with the changed file. To go back,
  undo the update, then stop the app and copy the newest
  `history-…-before-db-change.db` from `backups/` over `history.db`.
  Renames, archiving, deleting and pins done since the update are lost.

## 1.4.0 — 2026-10-19

Repair session 5 from `AUDIT.md` (Phase 3, one set of rules for every
query, #25 and #13).

- Quick search and the **Attachment Report** now follow the same rules as
  the sidebar and Advanced Search: chats in the Recycle Bin are left out,
  only the side you are looking at (Claude or ChatGPT) is listed, and a chat
  you renamed shows, and can be found by, its new name.
- The Attachment Report reloads when you switch between Claude and ChatGPT.
- Behind the scenes, what "not in the Recycle Bin", "this side" and "the
  chat's title" mean is now written in one place (`query_rules.py`) and
  every list, search and report uses it. Nothing else should look
  different.
- No database changes: going back to 1.3.0 needs no database copy.

## 1.3.0 — 2026-10-18

Repair session 4 from `AUDIT.md` (Phase 2, the updater, #16).

- **Check for Updates** (About page) now looks at the newest published
  release on GitHub, not the `main` branch, so only released versions reach
  the app.
- Checking no longer installs anything. It shows the installed version, the
  new version, its CHANGELOG entries, how many files change, and any app
  files that were edited on this computer. Nothing changes until you press
  **Install version …**, and only the version you were shown is installed.
- Before an update replaces files, the old ones are kept in `update-backup/`
  in the app folder (only the last update is kept). **Go back to version …**
  on the About page puts them back; if the new version will not start, run
  `python updater.py --restore`.
- The About page shows the installed version. Update failures are plain
  sentences, and network or disk problems go to the error log.
- `/api/update` is replaced by `/api/update/status`, `/api/update/check`,
  `/api/update/apply` and `/api/update/restore` (see `ENDPOINTS.md`).

## 1.2.0 — 2026-10-18

Repair session 3 from `AUDIT.md` (Phase 2, browser side).

- Every server call now goes through one helper (`static/api.js`). When the
  server says something failed, the screen says so in plain words and the
  change is not shown as saved; the technical detail goes to the error log
  (#23). Server messages that showed raw Python errors (restore, permanent
  delete, review import, import audit, Word/file previews) are now plain
  sentences.
- New **Error Log** page on the ⓘ menu, above About: the 10 most recent
  errors (repeats grouped, with count and first/last time), a Copy button
  for each and a Clear log button. Failed server calls, uncaught JavaScript
  errors and unhandled promise rejections in the browser are written to the
  same log, without any chat text (#35).
- Text between two `$` signs is escaped before it goes back on the page, so
  chat text like `$5 for <img …> and $10` can no longer add HTML or run
  code. Maths still renders. A `\begin…\end` block inside `$$…$$` now
  renders too (#2).
- KaTeX is now kept inside the app (`static/vendor/katex/`, updated from
  0.16.11 to 0.19.0), so maths works offline and no code loads from the
  internet (#15).
- The app page has a Content-Security-Policy: it only runs the app's own
  scripts and styles (#1). The Custom GPT header rule that was added from
  JavaScript moved into `style.css` for this.

## 1.1.0 — 2026-10-18

Repair session 2 from `AUDIT.md` (Phase 2, server side: the front door).

- `ENDPOINTS.md` lists every endpoint, what it takes and what it returns (#27).
- The server only answers requests addressed to `127.0.0.1` or `localhost`,
  which blocks DNS rebinding (#3).
- Changes (POST, PATCH, DELETE) are only accepted from the app's own page:
  JSON bodies (multipart for uploads), the app's own `Origin`, never
  `Sec-Fetch-Site: cross-site`. Files from `source/` are shown in the browser
  only for safe types (images, PDF, plain text, audio, video); anything else,
  such as HTML or SVG, is sent as a download, and nothing is type-sniffed (#1).
- Every failure gets a JSON reply: `400` for bad input (broken JSON, a body
  that isn't an object, a bad size header, `?limit=abc`), `500` with a plain
  message for anything unexpected, logged to `logs/errors.json`. No more
  dropped connections (#9).
- Size limits: 20 MB per request, 100 MB per upload (`413`). Text fields over
  their limit (label names 60, tags 40, titles, notes and summaries 200,000
  characters, and so on, see `request_limits.py`) now get a clear `400`
  instead of being cut short (#10). Imported chat titles are not limited;
  a title the user types when renaming a chat is (1,000 characters).
- Unknown `/api/` addresses and bad numbers in a request also get JSON
  errors, and a refused request's body is read first so the browser gets the
  error instead of a reset connection.

## 1.0.0 — 2026-10-18

First numbered version. Repair session 1 from `AUDIT.md` (Phase 0 and
Phase 1: the safety net).

- Every path is built from the app's own folder, so starting the app from
  another folder no longer makes a second database (#20).
- Python 3.12 or newer is required; older versions get a clear message at
  start-up. Added `requirements.txt` (#14).
- Tests and `scripts/check_styles.py` run on GitHub on every push, with new
  tests that record how labels, bulk labels and snapshots behave today (#26).
- Errors are written to `logs/errors.json`, without chat text, grouped by
  repeat, at most 200 groups (#22).
- Both databases are copied into `backups/` on start-up and before imports,
  bulk changes, restores and database changes; the last 5 of each are kept
  (#21).
- Each database records a `schema_version`; database changes are numbered
  steps and a failed step is reported instead of hidden (#18).
- Removed the unused `claude.json` (#28) and the README's advice to rebuild
  the index (#6).
