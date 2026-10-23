"""Where the app keeps its files.

Every path is built from the app's own folder, never from the folder the app
was started in, so a desktop shortcut or an editor's Run button finds the same
files as `python3 app.py` run from here (AUDIT.md #20).
"""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "history.db"
SOURCE_DIR = APP_DIR / "source"
LOG_DIR = APP_DIR / "logs"


def userdata_path(db_path) -> Path:
    """userdata.db, the user's own data, always beside history.db."""
    return Path(db_path).parent / "userdata.db"
