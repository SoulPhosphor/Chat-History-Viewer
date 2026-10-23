# Claude History Viewer

A fully local web app for browsing and searching your own Claude conversation history.

Core runtime has **no required third-party dependencies**: Python 3.12+ and SQLite only.

This public starter contains only the code and placeholder folders needed to run the app. Add your own export data before launching it.

---

## Setup

1. **Prepare your export**
  Go to Claude > Settings > Privacy > Export Data.

  Unzip the archive and place `conversations.json` inside `source/`:

  ```text
  claude-history/
  ├── source/
  │   └── conversations.json   <- your Claude export
  ├── app.py
  └── ...
   ```

1. **Run**

   ```bash
   python3 app.py
   ```

  On first run, the app builds a local SQLite full-text search index (`history.db`).
  For large exports this may take 20-40 seconds; subsequent starts are fast.

1. The browser opens automatically at **[http://127.0.0.1:5174](http://127.0.0.1:5174)** (once the server is actually listening — no more racing a blank tab).

If you want to keep the starter completely clean, leave the placeholder `source/README.md` in place and add only your own export files.

### Updating the app from the About page

Open **About** from the ⓘ menu and press **Check for Updates**. The app looks
at the newest published release on GitHub (never unreleased work on `main`)
and shows the version, what changes in it, and any app files you edited
yourself. Nothing is installed until you press **Install version …**.

Before files are replaced, the old ones are kept in the `update-backup/`
folder inside the app folder (only the last update is kept). To undo the
update, press **Go back to version …** on the About page, or, if the new
version will not start, run:

```bash
python updater.py --restore
```

Going back restores the app's files only. Copies of your databases are in
`backups/`.

### Updating the app by hand (auto-reload)

You do **not** need to stop and restart to pick up new code. While the app is
running, just replace the files:

- Replace any backend file (`server.py`, `build_db.py`, `labels.py`,
  `bulk_labels.py`, `snapshots.py`, `api_common.py`) and the running server
  re-launches itself within a second or two; the open browser tab reloads on
  its own.
- Replace anything in `static/` (`index.html`, `theme.css`, component CSS, `app.js` or the
  feature modules) and the tab reloads on its own — no server restart needed.

Your database is never touched by this — new tables are created automatically on
startup, so you never have to delete or rebuild `history.db` just to get a new
feature. If you leave an old copy running and start another, the new one won't
fight it for the port — it comes up cleanly on the next free port and opens the
browser there.

---

## Features

- **Fast, meaningful search** - FTS5 + Porter stemming across conversation text and attachment text, so relevant threads surface quickly.
- **Import New Chats** (ⓘ menu)
  - Drop newly-exported Claude or ChatGPT backups into `source/` and click **Import Conversations**
  - Progress and **Cancel** are shown on this screen. Complete chats are saved in small batches; cancelling keeps chats already saved. Run the import again to finish an incomplete file without duplicating chats.
  - Only one background job runs at once, including across two copies of the app. Another copy can open while it runs, check its progress, or cancel it.
  - **Rebuild from exports** uses the same progress and cancellation. It makes a database copy first and keeps saved user choices. Cancelling a rebuild leaves incomplete history until you rebuild again.
  - Each file is identified as a Claude or ChatGPT export from its contents (not its filename); only recognized backups are imported
  - Imported files are renamed to `Provider-conversations-YYYY-MM-DD.json` (the date is the latest message in the backup); identical re-drops are detected by hash and skipped, and same-date collisions get a `-1`, `-2`, … suffix
  - Conversation identity is the original UUID plus its provider, so overlapping backups reconcile in place instead of duplicating, a deleted conversation is never re-added, and renaming a chat never changes its identity
  - An import-history table below the button lists every backup with First / Last chat dates, total chats, and import date; filter by provider and sort by any column
- **ChatGPT / Claude toggle** - A toggle at the top of the sidebar switches the whole view between your ChatGPT and Claude conversations; conversations **and** folders are scoped to the selected side, and the app reopens on the side you last used.
- **Conversation views that match your workflow** - Recent / Pinned / Archived / Deleted / All, with smooth pagination for large exports.
- **Pinning with manual ordering** - Keep long-lived threads at the top in the order you choose.
- **Compare bar** (the top strip)
  - Holds up to **4 conversations you explicitly add** via the **Compare** action (chat row ⋮ menu or the in-chat ⋮ menu) — opening a chat never adds one
  - Compare items are a fast pinned-conversation type and are the **one place providers may mix**: a Claude and a ChatGPT chat can sit side by side even though the sidebar, folders and pins are per-side
  - Each tab is outlined in its provider's colour; the active tab fills with that colour
  - Bottom-left quick buttons open **transient views** (they do not add to the bar)
- **Settings** (ⓘ menu) - **Compare Tab Model Identifier**: choose the outline colour for ChatGPT and Claude Compare tabs (defaults: royal blue / green)
- **Rich message rendering**
  - Markdown: headings, bold/italic, lists, blockquotes, horizontal rules, strikethrough
  - Fenced code blocks with language labels and one-click **copy button**
  - Tables with horizontal scroll for wide content blocks
  - Math via KaTeX (`$...$`, `$$...$$`, `\\(...\\)`, `\\[...\\]`), rendered by a copy of KaTeX kept in the app (works offline)
  - Inline code, links, images
- **Media Hub (`/api/gallery`)**
  - Grouped by conversation to preserve context
  - Three sections: conversation images / attachments / artifacts
  - One-click jump-back to the source conversation or message position
- **Attachment Report**
  - Detects unresolved attachments (no extracted content and no local file)
  - Supports one-click upload into `source/files/` for targeted repair
- **Claude model tagging** (Claude exports only)
  - Tag each conversation with the model(s) it was written with, from the header line
  - Only models that claude.ai actually offered during the conversation are listed
  - **Add Claude Models** (ⓘ menu) edits that availability list — add a model, change dates, delete a row
  - Two black icons flag a tagging that no longer matches those dates:
    a caution sign when a tagged model was never available during the conversation,
    a report icon when the tagged models don't cover it end to end
  - Either can be dismissed per conversation, and returns if the tagging goes wrong again
  - **Unverified Models** joins the sidebar filter while any conversation is flagged
- **Scratch Pads** (briefcase menu)
  - Your own documents for notes and links to conversations or single messages. They belong to no conversation and to neither provider, so the same pads show on the Claude and ChatGPT sides
  - The Scratch Pads sidebar (new, search by title, rename and delete from each pad's ⋮) takes the place of the conversation list; closing it brings the list back exactly as it was
  - The editor is the Summary editor (Visual by default, Markdown optional, Save / Revert, unsaved-changes warning) with an **Insert Link** button: search conversation titles and named bookmarks, paste an internal link or a web address, or open **Advanced Search** to pick a chat (**Link Chat**) or a single message (**Link Message**)
  - Selected text becomes the link's text. Click a link to Open, Change or Remove it (Ctrl/Cmd-click opens it); **Back to …** in the chat title bar returns to the pad, unsaved edits intact
  - **Internal links** name their target by stable ids, so they survive renames and rebuilds: `chv://conversation/<id>` for a chat, `chv://conversation/<id>/message/<seq>/<message id>` for one message
  - Each message keeps its own id from the export, and that id is what message links and bookmarks go by. They still find the right message after a re-import where an earlier message was edited or regenerated, and if their message is gone they say so instead of opening whatever message is now in its place. Position is only used for chats whose export has no message ids. Databases built before this get the ids filled in from the backups in `source/` the next time the app starts
  - The link button in the chat title bar, and the ⋮ beside each bookmark in Global Bookmarks and the bookmark panel, offer **Copy Internal Link** and **Add to Scratch Pad** (adds the link to the end of any pad, or a new one, without opening it)
- **Memories / Projects panels**
  - Quick side views for workspace-level memory and project browsing
- **Artifact viewer**
  - Open by artifact id
  - Includes a visible source-conversation return link
- **Keyboard navigation** - `/` to focus search, `Esc` to clear search, plus list navigation shortcuts

---

## Optional dependency

DOCX text preview in `/api/file-content` uses `python-docx` when available:

```bash
pip install python-docx
```

If it is not installed, only DOCX preview is unavailable; all other features continue to work.

## Included data layout

The starter keeps the directory structure expected by the app, but it does not ship with private data. Add only your own files in these locations:

- `source/conversations.json` - required Claude export
- `source/files/` - optional local attachments and uploaded replacements
- `source/projects/` - optional project export data
- `source/memories.json` - optional memory data, if your export includes it
- `source/users.json` - optional user metadata, if your export includes it

---

## Where model data is stored

Model availability, per-conversation model tags, and dismissed warnings all live
in `userdata.db`, a sibling of `history.db`. `claude_models.json` seeds that
database the first time the app runs and is never read again, so edits made on
the **Add Claude Models** screen are permanent and are not overwritten by the
shipped file.

Scratch Pads live in `userdata.db` too.

`userdata.db` survives `history.db` being rebuilt or deleted, and carries this
data between browsers and machines — copy it alongside your export and your
tags, dates, and dismissals come with it.

---

## New exports

Bring a new export in with **Import New Chats** in the app.

To rebuild the search index (for example if `history.db` is damaged), stop
the app, delete `history.db` and start the app again. Keep `userdata.db` and
the files in `source/`. The rebuild reads every export in `source/`, including
the ones brought in with Import New Chats, and keeps your renames, Archive,
Recycle Bin, pins, settings, the Compare bar and chats waiting for review on
the import screen, which live in `userdata.db`. Chats you deleted permanently
stay deleted.

---

## Backups and the error log

- **Backups.** Copies of `history.db` and `userdata.db` go into `backups/` on
  every start and before imports, bulk label changes, restores and database
  changes. The newest 5 copies of each are kept. To go back to one, stop the
  app and copy it over the matching database file.
- **Error log.** Errors are written to `logs/errors.json`. It holds IDs, sizes,
  code locations and versions, never chat, note or summary text or search
  words, so entries can be pasted into a bug report.
- **Version.** The app's version is in `version.py`; changes are listed in
  [CHANGELOG.md](CHANGELOG.md).

---

## Workspace cleanup (local)

```bash
find . -type d -name '__pycache__' -prune -exec rm -rf {} +
find . -type f -name '*.pyc' -delete
rm -rf .venv
find . -name '.DS_Store' -delete
```

---

## Quick troubleshooting

1. **`source/conversations.json` not found**
  Ensure the export file exists at `source/conversations.json`.

1. **First launch is slow**
  Expected behavior on large exports while `history.db` is being built.

1. **Attachment cannot be displayed**
  Open Attachment Report and upload the matching file into `source/files/`.

1. **DOCX preview unavailable**
  Install `python-docx` (`pip install python-docx`) or download/open the file externally.

1. **Something went wrong**
  Look in `logs/errors.json` and include the entry's `error_id` and details
  when reporting it.

---

## File overview

| File                | Purpose                                                            |
| ------------------- | ------------------------------------------------------------------ |
| `app.py`            | Entry point - builds index if needed, starts server, opens browser |
| `claude_models.json`| Starting list of Claude models and the dates they were offered     |
| `build_db.py`       | Parses `conversations.json` -> SQLite + FTS5 index                 |
| `server.py`         | stdlib HTTP server: routing, rendering, JSON APIs                  |
| `api_common.py`     | `ApiError` - how core logic signals an HTTP error status           |
| `labels.py`         | Label definitions and conversation label assignment                |
| `bulk_labels.py`    | Conditional bulk labeling: criteria, preview, apply, saved runs    |
| `snapshots.py`      | Safety Snapshots (Labels screen) of everything you added (labels, folders, tags, mood tags, titles, archive/delete, pins, bookmarks, notes, summaries, Scratch Pads, related links, model tags): capture, list, restore. Imported chats are not in them |
| `message_ids.py`    | Stable message ids: lookup, bookmark re-anchoring, filling older databases |
| `scratchpads.py`    | Scratch Pad storage, link suggestions and link checks              |
| `app_paths.py`      | Where the app's files live, built from the app's own folder       |
| `version.py`        | The app's version number (see `CHANGELOG.md`)                     |
| `schema_changes.py` | Numbered database changes and the `schema_version` record         |
| `tag_ids.py`        | Stable ids for tags and mood tags                                  |
| `background_jobs.py` | Shared job IDs, progress, cancellation and one-at-a-time worker |
| `backups.py`        | Automatic copies of both databases into `backups/`                |
| `errorlog.py`       | The error log, `logs/errors.json`                                 |
| `requirements.txt`  | Python packages (none required; optional ones listed)             |
| `static/index.html` | App shell                                                          |
| `static/theme.css`  | Central appearance variables; see [STYLE_GUIDE.md](STYLE_GUIDE.md) |
| `static/style.css`  | UI styles                                                          |
| `static/accessibility.js` | Shared dialog, menu, keyboard, resize, and motion behaviour |
| `scripts/check_styles.py` | Dependency-free central-style guard |
| `STYLE_REVIEW.md` | Audit results and retained visual/contrast findings |
| `static/app.js`     | Frontend state, rendering, tabs, quick views, interactions         |
| `static/labels_screen.js` | Labels screen: settings and label definitions                |
| `static/bulk_labels.js`   | Bulk-label tool UI and Unfinished Label Runs                 |
| `static/snapshots.js`     | Safety Snapshots UI and the restore-category dialog          |
| `static/summary.js`       | Visual Markdown editor engine (Summary and Scratch Pads)     |
| `static/internal_links.js`| The internal link format (`chv://…`)                         |
| `static/scratchpads.js`   | Scratch Pads workspace, Insert Link, Add to Scratch Pad      |
| `static/scratchpads.css`  | Scratch Pads and link-control styles                         |

---

## API

The full list, with what each endpoint takes and returns and the rules every
request must follow, is in [ENDPOINTS.md](ENDPOINTS.md). The server only
answers at `127.0.0.1` or `localhost`, and only accepts changes from the app's
own page. Main endpoints consumed by the frontend:

```text
GET    /api/conversations?q=<query>&limit=50&offset=0&view=recent
GET    /api/conversation/<id>
GET    /api/search?q=<query>
GET    /api/search-in-conversation?conv_id=<id>&q=<query>
GET    /api/gallery
GET    /api/attachment-report
GET    /api/imported-backups?provider=all|claude|chatgpt
POST   /api/import-new
GET    /api/memories
GET    /api/projects
GET    /api/project/<id>
GET    /api/artifact/<id>
GET    /api/claude-models
GET    /api/conversation-models?conv_id=<id>
GET    /api/preferences
GET    /api/pinned
GET    /api/tabs
GET    /api/files-manifest
GET    /api/file-content?name=<filename>
GET    /files/<filename>
GET    /chatfiles/<filename>
GET    /source/<file_id>

POST   /api/upload-file
POST   /api/pinned
POST   /api/pinned/reorder
POST   /api/claude-models
POST   /api/conversation-models
POST   /api/conversation-models/dismiss
POST   /api/tabs
PATCH  /api/preferences
PATCH  /api/conversation/<id>
PATCH  /api/tabs/<id>
PATCH  /api/claude-models/<period_id>
DELETE /api/pinned/<conversation_id>
DELETE /api/tabs/<id>
DELETE /api/claude-models/<period_id>
DELETE /api/conversation-models/<conv_id>/<model_id>

GET    /api/scratchpads?q=<title search>
GET    /api/scratchpads/<id>
POST   /api/scratchpads                      {title?, content?}
PATCH  /api/scratchpads/<id>                 {title?, content?}
POST   /api/scratchpads/<id>/append          {markdown}
DELETE /api/scratchpads/<id>
GET    /api/link-suggestions?q=<text>
GET    /api/link-resolve?conversation=<id>&seq=<n>
```

---

## Claude export format notes

The parser in `build_db.py` targets Claude export structures centered on:

- conversation metadata (`uuid`, `name`, `created_at`, `updated_at`)
- `chat_messages`
- message `content` blocks (primarily text content)
- attachments and artifact tool operations

Non-display/system-style blocks (for example `thinking`, `tool_use`, `tool_result`) are filtered from normal rendering/indexing paths where appropriate.

### Paged conversations, Media Hub, and external images

Long chats use **Previous messages** and **Next messages**. Search, bookmarks and
internal message links jump to the right page. All imported text remains stored;
pagination does not shorten the conversation. The Media Hub follows the sidebar's
Claude/ChatGPT selection and uses **Load more media**. Its category counts show
loaded items. Unlinked images on disk are available on either side.

Markdown images hosted on another website wait for **Load external image**.
Each click permits just that image, for that rendered view, and contacts its host
without sending the app address as a referrer. Local export images display as
before. Failed images can be retried; private chats are not sent through a proxy.

Session 10 does not perform the separate real-export checks or the one-time
rebuild from Phase 5. Those tasks remain pending; they do not block Phase 6.
