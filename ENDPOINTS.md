# Endpoints

Every address the server answers, what it takes and what it returns
(AUDIT.md #27). The routing lives in `server.py` (`do_GET`, `do_POST`,
`do_PATCH`, `do_DELETE`). Keep this file in step with it.

## Rules for every request

- **Host.** Only `127.0.0.1:<port>` or `localhost:<port>` is answered. Any
  other `Host` gets `403` (#3).
- **Changes (POST, PATCH, DELETE).** A request with a body must send
  `Content-Type: application/json` (`multipart/form-data` only for
  `/api/upload-file`), else `415`. `Origin`, when sent, must be the app's own
  address, and `Sec-Fetch-Site` must not be `cross-site`, else `403` (#1).
- **Bodies** must be a JSON object. Anything else gets `400` (#9).
- **Size.** Bodies over 20 MB (100 MB for `/api/upload-file`) get `413`.
  Text fields over their limit get `400` naming the field; a preference value
  over 100,000 bytes gets `413` (#10). Limits are
  in `request_limits.py`.
- A refused request's unread body (up to 256 MB) is read and dropped, so the
  caller gets the error reply rather than a reset connection.
- **Errors** come back as JSON `{"error": "<plain message>"}` with `400`,
  `403`, `404`, `409`, `413`, `415`, `500` or `507`, including unknown `/api/`
addresses (`404`). Details go only to
  `logs/errors.json` (#9).
- `<id>` parts of a path are URL-encoded.
- Endpoints marked **backup** copy both databases first (`backups.py`); if
  the copy fails nothing changes and the reply is `500`. For background jobs,
  the failure is recorded in the job instead (the admission reply is `202`).
- Endpoints marked **one at a time** rewrite many chats. Only one of them (or
  a rebuild) runs at once, in this or another copy of the app
  (`background_jobs.py`, #33); a second one gets `409` before any backup is made.

## Pages and files (GET)

| Path | Returns |
|---|---|
| `/`, `/index.html` | The app page, with a `Content-Security-Policy` header: only the app's own scripts, styles, fonts and connections (`CONTENT_SECURITY_POLICY` in `server.py`, #1). |
| `/app.js`, `/style.css`, `/static/<name>.js|.css` | Front-end files (flat names only). |
| `/static/vendor/katex/<path>` | The app's copy of KaTeX (#15): `.js`, `.css` and `.woff2` only, never outside that folder. |
| `/files/<name>` | A file from `source/files/` (name match ignores case and space/underscore). Always a download. |
| `/chatfiles/<name>` | A file from `source/chat_files/`. Shown in the browser only for safe types (images, PDF, plain text); everything else is a download. |
| `/source/<id or relative path>` | A file under `source/`. Same safe-type rule as `/chatfiles/`. `409` if a bare id matches several files. |

All three file routes send `X-Content-Type-Options: nosniff`.

## Reading (GET)

| Path | Query | Returns |
|---|---|---|
| `/api/version` | — | `{started_at, static_sig, running_sig, disk_sig, stale, updating}` (`updating`: an update is being checked, installed or undone; the page waits before reloading) |
| `/api/update/status` | — | `{current_version, backup}`: the installed version and the kept update backup (`null` if none). No network (#16) |
| `/api/error-log` | — | `{groups: [...]}`: the 10 error groups that happened most recently, newest first, from `logs/errors.json` (#35) |
| `/api/conversations` | `q, limit, offset, view, provider, pinned_first` | `{conversations: [...], total, ...}` |
| `/api/conversation/<id>` | `limit, offset, target_seq` | `{conversation, messages, tags, mood_tags, labels, bookmarks, models, ...}` |
| `/api/search` | `q, limit, provider` | `{results: [...]}` (never the Recycle Bin; one side when `provider` is given; renamed titles) |
| `/api/search-in-conversation` | `conv_id, q` | `{matches: [...]}` |
| `/api/gallery` | `provider, limit, offset` | `{sections: [...], unlinked_images, unlinked_total, offset, limit, next_offset}` (Media Hub) |
| `/api/attachment-report` | `provider` | `{missing: [...], ...}` (never the Recycle Bin; one side when `provider` is given; renamed titles) |
| `/api/import-audit` | `status` | `{status, total, rows}`; without `status` the counts, plus `dropped_types` and `unknown_types` (`{content type: count}` over every imported file) |
| `/api/imported-backups` | `provider` | `{backups: [...]}` |
| `/api/jobs` | — | `{job}` (latest receipt, or `null`); shared across copies and page reloads. |
| `/api/jobs/<id>` | — | `{job}`, or `404` with a plain error if absent. |
| `/api/import-new/review` | — | `{review: [...]}` |
| `/api/folders` | `provider` | `{folders: [...]}` |
| `/api/memories` | — | `{memories: [{id, memory_id, content}]}`. `memory_id` stays the same after a rebuild; `id` does not |
| `/api/projects` | — | `{projects: [...]}` |
| `/api/project/<id>` | — | `{project}` |
| `/api/artifact/<id>?conv=<conv_id>` | `conv` (required: the same name can be in several chats) | the artifact `{id, conv_id, title, content, ...}`; 400 without `conv`, 404 if that chat has no such artifact |
| `/api/claude-models` | — | `{models: [...], format}` |
| `/api/conversation-models` | `conv_id` | the chat's model state |
| `/api/tags`, `/api/mood-tags` | — | `{tags: [...]}` |
| `/api/gizmos` | — | `{gizmos: [...]}` |
| `/api/gizmo-conversations` | `gizmo_id` | `{conversations: [...]}` |
| `/api/labels` | — | `{labels: [...]}` with counts |
| `/api/conversation-labels/<conv_id>` | — | the chat's labels |
| `/api/labels/bulk-history` | — | `{history: [...], retention}` |
| `/api/labels/bulk-runs` | — | `{runs: [...]}` (unfinished bulk runs) |
| `/api/snapshots` | — | `{snapshots: [...]}` |
| `/api/summaries/<conv_id>` | — | `{conversation_id, summary, condensed_summary, author, version, ...}` |
| `/api/notes/<conv_id>` | — | `{conversation_id, user_notes, notes_to_ai, notes_from_ai, version, ...}` |
| `/api/bookmarks/<conv_id>` | — | `{conversation_id, bookmarks: [...]}` |
| `/api/global-bookmarks` | `q, scope, sort, provider, limit, offset` | `{conversations: [...], total}` |
| `/api/preferences` | — | `{preferences: {key: value}}`, without keys the app keeps for itself (`dataset_format`) |
| `/api/pinned` | `provider` | `{pinned: [...]}` |
| `/api/tabs` | — | `{tabs: [...]}` |
| `/api/files-manifest` | — | `{files: [names]}` |
| `/api/file-content` | `name` | `{name, kind, text or url}` |
| `/api/advanced-search/options` | — | filter choices (models, folders, labels, ...) |
| `/api/advanced-search/history` | — | `{recent: [...], saved: [...]}` |
| `/api/related/<conv_id>` | — | `{related: [...]}` |
| `/api/scratchpads` | `q` | `{pads: [...]}` |
| `/api/scratchpads/<id>` | — | `{pad}` |
| `/api/link-suggestions` | `q` | `{suggestions: [...]}` |
| `/api/link-resolve` | `conversation, seq, message` | `{ok, conversation_id, seq, ...}` |

## Changing (POST)

| Path | Body | Returns |
|---|---|---|
| `/api/upload-file` | multipart: `file`, `name`, `replace?` (`1`) | `{ok, saved}`; `409` when a file with that name is already in `source/files/` and `replace` is not `1` |
| `/api/error-log/browser` | `{error_type, message, stack, action, request: {method, url}, status, browser: {name, version, window, screen}}` (all optional) | `{ok}`. Writes a browser error to the log; the request is kept only as its address pattern (#35). |
| `/api/pinned` | `{conversation_id}` | `{ok}` |
| `/api/pinned/reorder` | `{ids: [...]}` | `{ok}` |
| `/api/tabs` | `{id, tab_type, conversation_id?, artifact_id?, title?, pinned?, sort_index?, last_active_at?, provider?}` | `{ok}` |
| `/api/folders` | `{name, provider}` | `{ok, id}` |
| `/api/folder-items` | `{folder_id, conversation_id}` | `{ok}` |
| `/api/claude-models` | `{name, start_date, end_date?}` | `{ok, models}` |
| `/api/claude-models/reload` | — | `{models}` |
| `/api/conversation-models` | `{conv_id, model_id}` | model state |
| `/api/conversation-models/dismiss` | `{conv_id, kind}` | model state |
| `/api/tags`, `/api/mood-tags` | `{conv_id, tag}` | `{tags}` |
| `/api/gizmos/<id>/folder` | `{folder_id?, folder_name?}` | `{ok, ...}` |
| `/api/labels` | `{name?, color}` | the new label |
| `/api/labels/reorder` | `{order: [ids]}` | `{labels}` |
| `/api/conversation-labels` | `{conv_id, label_id? / label_ids?, action?}` | the chat's labels |
| `/api/labels/bulk-preview-list` | `{criteria, limit?, offset?}` | conversation list page |
| `/api/labels/bulk-preview` | criteria | `{count, ...}` |
| `/api/labels/bulk-apply` **backup** | criteria + action | `{ok, run, ...}` |
| `/api/labels/bulk-history/retention` | `{retention}` | `{ok, retention}` |
| `/api/labels/bulk-runs/continue` **backup** | `{id, batch_size?}` | run state |
| `/api/snapshots` | `{name?}` | the new snapshot |
| `/api/snapshots/<id>/restore` **backup** | `{categories?: [...]}` | `{ok, restored}` |
| `/api/import-new` **backup** **one at a time** | `{deleted_mode?, review_new_messages?}` | `202 {job}`; starts a background import. Disk/backup failures appear in job status; earlier saved batches stay. |
| `/api/import-new/review-import` **backup** **one at a time** | `{import_ids? / import_all?, dismiss_ids? / dismiss_all?}` | `202 {job}`; result `{ok, imported, dismissed, errors}`. Failed chats stay in review. |
| `/api/rebuild` **backup** **one at a time** | `{}` | `202 {job}`; rebuild through `server.rebuild_history`. Cancellation leaves partial history and keeps user state. |
| `/api/jobs/<id>/cancel` | `{}` | `{job}`; requests cancellation between complete chats, including from another copy of the app. Already finished jobs are unchanged. |
| `/api/update/check` | `{}` plus header `X-CHV-Update: 1` | `{status: "no_update" / "available", current_version, latest_version, commit, changes: [{version, title, text}], files_changed, files_added, local_edits, backup}`. Reads the newest GitHub release; changes nothing (#16) |
| `/api/update/apply` | `{version, commit}` (from the check) plus header `X-CHV-Update: 1` | `{status: "updated", version, previous_version, backup}`. Installs that release only if it is still the newest and its tag still points at that commit (`409` otherwise); the old files go to `update-backup/` (#16) |
| `/api/update/restore` | `{}` plus header `X-CHV-Update: 1` | `{status: "restored", version, backup: null}`. Puts back the files in `update-backup/` (`404` if there is none) (#16) |
| `/api/recycle-bin/restore` **backup** | `{ids?: [...], all?}` | `{ok, restored}` |
| `/api/recycle-bin/purge` **backup** | `{ids?: [...], all?}` | `{ok, purged}` |
| `/api/bookmarks` | `{conversation_id, seq, name?, source?}` | `{ok, bookmark}` |
| `/api/advanced-search` | criteria | `{results, total, ...}` |
| `/api/advanced-search/browse` | `{kind, id, provider, sort?, offset?, limit?, exclude_ids?}` | `{results, total, ...}` |
| `/api/advanced-search/saved` | `{name, criteria}` | `{ok, saved}` |
| `/api/related` | `{a, b}` | `{ok}` |
| `/api/scratchpads` | `{title?, content?}` | `{pad}` |
| `/api/scratchpads/<id>/append` | `{markdown}` | `{pad}` |

## Changing (PATCH)

| Path | Body | Returns |
|---|---|---|
| `/api/preferences` | `{preferences: {key: value}}` | `{ok}`. Keys the app keeps for itself (`dataset_format`) are ignored |
| `/api/tabs/<id>` | tab fields | `{ok}` |
| `/api/folder-items/<id>` | `{pinned}` | `{ok}` |
| `/api/folders/<id>` | `{name}` | `{ok}` |
| `/api/conversation/<id>` | `{title?, archived?, deleted?}` (title at most 1,000 characters) | `{ok}` |
| `/api/claude-models/<period_id>` | `{name?, start_date?, end_date?}` | `{ok, models}` |
| `/api/gizmos/<id>` | `{display_name}` | `{ok, ...}` |
| `/api/labels/<id>` | `{name?, color?}` | the label |
| `/api/summaries/<conv_id>` | `{summary?, condensed_summary?, author?, base_version?}` | `{ok, version, ...}`; 409 if `base_version` is not the saved version |
| `/api/notes/<conv_id>` | `{user_notes?, notes_to_ai?, notes_from_ai?, base_version?}` | `{ok, version, ...}`; 409 if `base_version` is not the saved version |
| `/api/bookmarks/<id>` | `{name}` | `{ok}` |
| `/api/scratchpads/<id>` | `{title?, content?, base_version?}` | `{pad}` (`409` if `base_version` is stale) |

## Changing (DELETE)

No body.

| Path | Query | Returns |
|---|---|---|
| `/api/pinned/<conv_id>` | — | `{ok}` |
| `/api/tabs/<id>` | — | `{ok}` |
| `/api/folder-items/<id>` | — | `{ok}` |
| `/api/claude-models/<period_id>` | — | `{ok, models}` |
| `/api/conversation-models/<conv_id>/<model_id>` | — | model state |
| `/api/tags/<conv_id>/<tag>`, `/api/mood-tags/<conv_id>/<tag>` | — | `{tags}` |
| `/api/labels/bulk-runs/<id>` | — | `{ok}` |
| `/api/labels/<id>` | — | `{ok}` |
| `/api/snapshots/<id>` | — | `{ok}` |
| `/api/bookmarks/<id>` | — | `{ok}` |
| `/api/related` | `a, b` | `{ok}` |
| `/api/advanced-search/saved/<id>` | — | `{ok}` |
| `/api/scratchpads/<id>` | — | `{ok}` |
| `/api/error-log` | — | `{ok}`. Empties the whole error log (#35). |

## Background job receipts

`job` has `id` (UUID), `kind` (`import`, `review-import`, `rebuild`; extensible),
`status` (`running`, `completed`, `cancelled`, `failed`, `interrupted`),
`message` (plain text), `progress`, `total` (null while counting), `started_at`,
`finished_at`, and `result` (the operation's summary when available). Progress
counts the current phase/file; a new file or phase starts at zero. A failed job
also has `error` and `error_status`; polling itself still succeeds with `200`.

Admission takes the shared OS lock **before** making any backup, returns `202`
immediately, and holds it through the final receipt. A second admission gets
`409 {"error": "..."}`. Every job backs up inside its worker, opens its own
connections, and releases the lock on success, cancellation or error. Job
receipts and cancellation requests are kept in `.background-jobs/` beside the
databases; they are private runtime files excluded from git and updates.

Only finished exports are hash-recorded/renamed. Cancelled or interrupted
imports can be retried: chats already committed reconcile by their stable IDs.
Cancellation saves the last complete chats; a chat savepoint is never committed
partway. Failed jobs roll back the unfinished batch and keep earlier batches.
An abruptly stopped process is shown as interrupted, with no automatic replay.

### Paged reading (session 10)

`/api/conversations` and `/api/search` cap `limit` at 500 (minimum 1).
Conversation detail defaults to 100 messages, caps the requested limit at 200,
and returns `offset`, `limit`, `total`, `previous_offset` and `next_offset`
(`null` at the corresponding end).
A page may include one extra message to keep a user message and its immediate
response together. Page edges use fixed nominal boundaries shifted past a pair,
so the returned Previous and Next offsets are inverses even after an expansion.
An input offset selects its containing page; the response reports the actual
first row offset. Saved offsets beyond a shorter re-import return its last page. `target_seq` selects the page containing that position,
using its actual row offset even when sequence numbers have gaps. The existing
conversation metadata, bookmarks, tags, model state and artifact metadata remain.

Gallery defaults to 100, maximum 200. Each page reads up to that many attachment
entries and artifact entries, plus that many unlinked files. Ordering includes
conversation IDs and message/entry positions to make ties stable. Pass the
returned `next_offset` until it is `null`; category counts describe that page's
items. The browser accumulates pages and labels its counts as loaded items.
Provider filtering applies to conversation media. Unlinked classification checks
attachment names across both providers and every page, so linked files never
become unlinked merely because their chat is outside the current page or side.

External Markdown images have no image source until their individual button is
clicked. The existing HTTPS image CSP permission supports that explicit load;
there is no server proxy and no saved permission. Browser `no-referrer` suppresses
the app address; the image host still receives the image request after consent.
