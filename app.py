#!/usr/bin/env python3
"""
Claude History Viewer
=====================
Place your exported conversations.json in a source/ directory, then run:

    python3 app.py

Requires Python 3.12+. No third-party packages needed.
"""
import sys

# Checked before anything else is imported, so an older Python gets this
# message instead of a confusing error from code it can't read.
MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "Claude History Viewer needs Python %d.%d or newer; this is Python %d.%d.\n"
        "Install a current Python from https://www.python.org/downloads/ and "
        "run the app with it.\n" % (MIN_PYTHON + tuple(sys.version_info[:2]))
    )
    sys.exit(1)

import os
import threading
import webbrowser

import app_paths

# Built from the app's own folder, so the app finds the same files whichever
# folder it is started from.
DB_PATH = app_paths.DB_PATH
SOURCE  = app_paths.SOURCE_DIR / "conversations.json"
# Preferred port. CHV_PORT lets an auto-restart keep whatever port it ended up
# on (e.g. a fallback port chosen because another copy was still open), so the
# already-open browser tab reconnects to the same URL after the reload.
PORT    = int(os.environ.get("CHV_PORT", "5174"))


def main() -> None:
    # Once history.db exists, new backups come in through "Import New Chats".
    # A missing history.db is built from every export in source/: the seed
    # conversations.json (or, without it, the first export found) plus every
    # backup brought in with Import New Chats. Renames, Archive, Recycle Bin,
    # pins and everything else the user added live in userdata.db and are
    # kept; permanently deleted chats stay deleted (AUDIT.md #6).
    import server
    if not DB_PATH.exists():
        if not server.has_rebuild_source(app_paths.SOURCE_DIR):
            print(f"Error: {SOURCE} not found.")
            print("Export your Claude history and place conversations.json in source/")
            sys.exit(1)
        if app_paths.userdata_path(DB_PATH).exists():
            print("Rebuilding the search index from every export in source/ …")
        else:
            print("First run — building search index (may take ~30 s for large exports)…")
        import build_db, background_jobs
        try:
            summary = server.rebuild_history(DB_PATH, app_paths.SOURCE_DIR)
        except background_jobs.Cancelled:
            print("Rebuild cancelled. Chats already saved are kept. "
                  "Open Import New Chats to finish the rebuild.")
            summary = {"files": []}
        except (build_db.NotEnoughSpace, background_jobs.JobBusy) as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        extra = len(summary["files"])
        if extra:
            print(f"Also brought in {extra} more export(s) from source/.")


    print(f"Starting Claude History (preferred port {PORT}) …")

    def on_ready(actual_port: int) -> None:
        # Called once the server socket is bound and listening, so opening the
        # browser can no longer race a not-yet-started server. actual_port may
        # differ from PORT if another copy was still holding the preferred port.
        url = f"http://127.0.0.1:{actual_port}"
        print(f"✓ Ready — {url}  (leave this window open; Ctrl-C to quit)")
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    server.serve(port=PORT, db_path=DB_PATH, source_dir=app_paths.SOURCE_DIR, on_ready=on_ready)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # A start-up failure (a database change that failed, a backup that
        # could not be written, …) goes to the error log before the app stops.
        import errorlog
        entry = errorlog.record_exception(exc, action="start-up")
        print(f"\nThe app could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        if entry:
            print(f"Details are in {errorlog.LOG_FILE} (error id {entry['error_id']}).",
                  file=sys.stderr)
        sys.exit(1)
