"""Safe, dependency-free updates for Claude History Viewer (AUDIT.md #16).

Updates come from the newest published release on GitHub, never from the
`main` branch. Checking shows the new version and its CHANGELOG entries
before anything is changed; applying installs exactly the release that was
shown. Before files are replaced, the old copies are kept in `update-backup/`
in the app folder (only the last update is kept), and `restore_backup()` or
`python updater.py --restore` puts them back.

Only files that exist in the release are replaced. Runtime data (the source
folder, databases, backups, logs and local environment files) is never part
of an update.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


REPOSITORY = "SoulPhosphor/claude-history-viewer"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
USER_AGENT = "Claude-History-Viewer-Updater"
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
BACKUP_DIR_NAME = "update-backup"
MANIFEST_NAME = "manifest.json"
LOCK_FILE_NAME = ".update.lock"

_UPDATE_LOCK = threading.Lock()
_PROTECTED_ROOT_NAMES = {
    "history.db",
    "userdata.db",
    ".env",
    ".env.local",
    LOCK_FILE_NAME,
    ".import.lock",  # import_lock.LOCK_FILE_NAME
}
_PROTECTED_ROOT_DIRS = {"source", "backups", "logs", ".background-jobs", BACKUP_DIR_NAME}
_PROTECTED_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_PROTECTED_PARTS = {".git", "__pycache__"}
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_CHANGELOG_HEADING_RE = re.compile(r"^##\s+v?(\d+\.\d+\.\d+)\b", re.MULTILINE)


class UpdateError(Exception):
    """An update failure with a plain message for the app screen and the
    HTTP status the server should reply with."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def _app_root(app_root: Path | None) -> Path:
    return (app_root or Path(__file__).resolve().parent).resolve()


def current_version(app_root: Path | None = None) -> str:
    """The version in the installed version.py, read from disk so it is right
    straight after an update or a restore."""
    try:
        text = (_app_root(app_root) / "version.py").read_text(encoding="utf-8")
    except OSError:
        text = ""
    match = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", text)
    return match.group(1) if match else "0.0.0"


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(str(text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _request(url: str, timeout: int = 20):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _read_limited(response, limit: int = MAX_DOWNLOAD_BYTES) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = response.read(min(1024 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UpdateError("The update download was unexpectedly large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_json(url: str) -> dict:
    with _request(url) as response:
        try:
            data = json.loads(_read_limited(response).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError("GitHub returned update information that could not be read.") from exc
    if not isinstance(data, dict):
        raise UpdateError("GitHub returned update information that could not be read.")
    return data


def _git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _is_managed_file(path_text: str) -> bool:
    path = PurePosixPath(path_text)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    if any(part in _PROTECTED_PARTS for part in path.parts):
        return False
    if len(path.parts) > 1 and path.parts[0] in _PROTECTED_ROOT_DIRS:
        return False
    if len(path.parts) == 1 and (path.name in _PROTECTED_ROOT_NAMES or path.name in _PROTECTED_ROOT_DIRS):
        return False
    if path.suffix.lower() in _PROTECTED_SUFFIXES:
        return False
    return True


def _latest_release() -> dict:
    """The newest published release (GitHub leaves out drafts and
    pre-releases). Returns {tag, version, notes, published_at}."""
    try:
        data = _fetch_json(f"{API_ROOT}/releases/latest")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No released version was found on GitHub yet.", 404) from exc
        raise
    tag = str(data.get("tag_name") or "").strip()
    if not parse_version(tag):
        raise UpdateError("The newest release on GitHub has no version number this app understands.")
    return {
        "tag": tag,
        "version": tag.lstrip("v"),
        "notes": str(data.get("body") or "").strip(),
        "published_at": str(data.get("published_at") or ""),
    }


def _tag_commit(tag: str) -> str:
    commit = _fetch_json(f"{API_ROOT}/commits/{urllib.parse.quote(tag, safe='')}")
    commit_sha = str(commit.get("sha") or "").strip()
    if not commit_sha:
        raise UpdateError("GitHub returned incomplete update information.")
    return commit_sha


def _commit_tree(commit_sha: str) -> dict[str, str]:
    """{path: blob sha} for every file the updater may manage in a commit."""
    data = _fetch_json(f"{API_ROOT}/git/trees/{commit_sha}?recursive=1")
    tree = data.get("tree")
    if not isinstance(tree, list) or data.get("truncated"):
        raise UpdateError("GitHub returned incomplete update information.")
    files = {}
    for entry in tree:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        sha = str(entry.get("sha") or "")
        if entry.get("type") == "blob" and sha and _is_managed_file(path):
            files[path] = sha
    return files


def _release_tree(tag: str) -> tuple[str, dict[str, str]]:
    commit_sha = _tag_commit(tag)
    files = _commit_tree(commit_sha)
    if not files:
        raise UpdateError("No application files were found in the release.")
    return commit_sha, files


def _installed_release_files(version: str) -> dict[str, str] | None:
    """The file list of the release the user has installed, used to spot
    files edited on this computer. None when that release can't be found
    (for example a copy taken straight from `main`)."""
    for tag in (f"v{version}", version):
        try:
            return _commit_tree(_tag_commit(tag))
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 422):
                raise
    return None


def _fetch_blob(blob_sha: str) -> bytes:
    import base64
    data = _fetch_json(f"{API_ROOT}/git/blobs/{blob_sha}")
    try:
        content = base64.b64decode(str(data.get("content") or ""))
    except ValueError as exc:
        raise UpdateError("GitHub returned update information that could not be read.") from exc
    if _git_blob_sha(content) != blob_sha:
        raise UpdateError("GitHub returned update information that did not match the release.")
    return content


def _local_sha(app_root: Path, relative: str) -> str:
    try:
        return _git_blob_sha(app_root.joinpath(*PurePosixPath(relative).parts).read_bytes())
    except OSError:
        return ""


def _changed_files(app_root: Path, remote_files: dict[str, str]) -> list[str]:
    return [rel for rel, sha in remote_files.items() if _local_sha(app_root, rel) != sha]


def changelog_entries(text: str, after: str, up_to: str) -> list[dict]:
    """The CHANGELOG.md entries newer than `after`, up to and including
    `up_to`, newest first: [{version, text}]."""
    low, high = parse_version(after), parse_version(up_to)
    if not high:
        return []
    headings = list(_CHANGELOG_HEADING_RE.finditer(text))
    entries = []
    for index, match in enumerate(headings):
        number = parse_version(match.group(1))
        if not number or number > high or (low and number <= low):
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[match.start():end].strip()
        title, _, rest = body.partition("\n")
        entries.append({"version": match.group(1), "title": title.lstrip("#").strip(),
                        "text": rest.strip()})
    return entries


def backup_info(app_root: Path | None = None) -> dict | None:
    """What the kept backup holds, or None when there is none."""
    folder = _app_root(app_root) / BACKUP_DIR_NAME
    try:
        manifest = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    return {
        "from_version": str(manifest.get("from_version") or ""),
        "to_version": str(manifest.get("to_version") or ""),
        "created_at": manifest.get("created_at"),
        "replaced": len(manifest.get("replaced") or []),
        "created": len(manifest.get("created") or []),
        "folder": BACKUP_DIR_NAME,
        "partial": bool(manifest.get("partial")),
    }


def status(app_root: Path | None = None) -> dict:
    """The installed version and the kept backup. Never uses the network."""
    return {"current_version": current_version(app_root), "backup": backup_info(app_root)}


def _plain_network_error(exc: BaseException) -> UpdateError | None:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return UpdateError("GitHub's limit on update checks was reached. Try again in an hour.", 503)
        if exc.code == 404:
            return UpdateError("The update files could not be found on GitHub.", 502)
        return UpdateError("GitHub could not provide the update. Try again later.", 502)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return UpdateError("GitHub took too long to answer. Try again later.", 504)
    if isinstance(exc, ssl.SSLError):
        return UpdateError("A secure connection to GitHub could not be made.", 502)
    if isinstance(exc, urllib.error.URLError):
        return UpdateError("GitHub could not be reached. Check the internet connection.", 502)
    return None


def update_running(app_root: Path | None = None) -> bool:
    """True while an update is being checked, installed or undone, by this
    process or by another copy of the app in the same folder. The server's
    auto-restart and the page's auto-reload wait for it."""
    if _UPDATE_LOCK.locked():
        return True
    path = _app_root(app_root) / LOCK_FILE_NAME
    if not path.exists():
        return False
    try:
        handle = open(path, "a+b")
    except OSError:
        return False
    with handle:  # closing the file lets go of the lock again
        return not _lock_file(handle)


def _lock_file(handle) -> bool:
    """Take an exclusive lock on an open file without waiting. The operating
    system frees it when the process ends, so a crash never leaves it stuck."""
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _locked(root: Path, work, writes: bool = True):
    """Run `work` under the update lock, turning network failures into
    plain UpdateErrors. The lock covers this process (threads) and, when
    `work` changes files, every other copy of the app running from the same
    folder (a lock file)."""
    busy = UpdateError("An update is already running. Wait for it to finish.", 409)
    if not _UPDATE_LOCK.acquire(blocking=False):
        raise busy
    try:
        if not writes:
            return _run(work)
        try:
            handle = open(root / LOCK_FILE_NAME, "a+b")
        except OSError as exc:
            raise UpdateError("The app folder could not be changed.", 500) from exc
        with handle:
            # update_running() may be holding the lock for an instant to
            # look at it, so give it a moment before saying "busy".
            for _ in range(10):
                if _lock_file(handle):
                    break
                time.sleep(0.05)
            else:
                raise busy
            return _run(work)
    finally:
        _UPDATE_LOCK.release()


def _run(work):
    try:
        return work()
    except UpdateError:
        raise
    except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError) as exc:
        raise _plain_network_error(exc) from exc


def check_for_update(app_root: Path | None = None) -> dict:
    """Compare the installed version with the newest release and describe
    the change. Nothing on disk is changed."""
    root = _app_root(app_root)

    def work():
        installed = current_version(root)
        release = _latest_release()
        result = {
            "current_version": installed,
            "latest_version": release["version"],
            "published_at": release["published_at"],
            "backup": backup_info(root),
        }
        if parse_version(installed) and parse_version(release["version"]) <= parse_version(installed):
            result["status"] = "no_update"
            return result

        commit_sha, remote_files = _release_tree(release["tag"])
        changed = _changed_files(root, remote_files)
        new_files = [rel for rel in changed if not _local_sha(root, rel)]

        local_edits = []
        installed_files = _installed_release_files(installed)
        if installed_files is not None:
            for rel in changed:
                local = _local_sha(root, rel)
                if local and rel in installed_files and local != installed_files[rel]:
                    local_edits.append(rel)
                elif local and rel not in installed_files:
                    local_edits.append(rel)

        changes = []
        if "CHANGELOG.md" in remote_files:
            try:
                text = _fetch_blob(remote_files["CHANGELOG.md"]).decode("utf-8", "replace")
                changes = changelog_entries(text, installed, release["version"])
            except UpdateError:
                changes = []
        if not changes and release["notes"]:
            changes = [{"version": release["version"], "title": release["version"],
                        "text": release["notes"]}]

        result.update({
            "status": "available",
            "commit": commit_sha,
            "changes": changes,
            "files_changed": len(changed) - len(new_files),
            "files_added": len(new_files),
            "local_edits": sorted(local_edits),
            "local_edits_known": installed_files is not None,
        })
        return result

    return _locked(root, work, writes=False)


def _download_archive(commit_sha: str) -> zipfile.ZipFile:
    with _request(f"{API_ROOT}/zipball/{commit_sha}", timeout=45) as response:
        payload = _read_limited(response)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        if archive.testzip() is not None:
            raise zipfile.BadZipFile
        return archive
    except zipfile.BadZipFile as exc:
        raise UpdateError("The downloaded update was damaged. Try again.") from exc


def _archive_member_map(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members = {}
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if info.is_dir() or len(path.parts) < 2:
            continue
        relative = PurePosixPath(*path.parts[1:])
        if relative.is_absolute() or ".." in relative.parts:
            raise UpdateError("The downloaded update contained an unsafe file path.")
        members[relative.as_posix()] = info
    return members


def _stage_files(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    remote_files: dict[str, str],
    changed: list[str],
    stage_root: Path,
) -> None:
    for relative in changed:
        info = members.get(relative)
        if info is None:
            raise UpdateError(f"A file the update needs was missing from the download ({relative}).")
        data = archive.read(info)
        if _git_blob_sha(data) != remote_files[relative]:
            raise UpdateError("The update changed while it was downloading. Try again.")
        destination = stage_root.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


# Backend files go last so the auto-reloader cannot restart midway through.
_BACKEND = {"server.py", "updater.py", "app.py", "build_db.py"}


def _backend_last(paths: list[str]) -> list[str]:
    return sorted(paths, key=lambda p: (PurePosixPath(p).name in _BACKEND, p == "server.py", p))


def _write_manifest(folder: Path, manifest: dict) -> None:
    """Write a backup's manifest so it is always either the old or the new
    one, never cut short (a cut-short manifest would hide the backup)."""
    target = folder / MANIFEST_NAME
    incoming = folder / f".{MANIFEST_NAME}.writing"
    try:
        incoming.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(incoming, target)
    except OSError:
        incoming.unlink(missing_ok=True)
        raise


def _write_backup(app_root: Path, changed: list[str], from_version: str, to_version: str) -> Path:
    """Copy the files an update will replace into a new backup folder beside
    the kept one. Returns the new folder; it becomes `update-backup/` only
    once the update has been applied."""
    kept = backup_info(app_root)
    if kept and kept["partial"]:
        # Its files are the only way back from a half-finished update or
        # restore; a new update would replace them.
        raise UpdateError("An earlier update or undo stopped partway. Press Go back to "
                          f"version {kept['from_version']} first (or run: python updater.py "
                          "--restore).", 409)
    incoming = app_root / f".{BACKUP_DIR_NAME}.new"
    for leftover in (incoming, app_root / f".{BACKUP_DIR_NAME}.old"):
        if leftover.exists():
            # Left by an update that stopped partway and could not be undone:
            # it may hold the only copy of old files, so never overwrite it.
            raise UpdateError(f'An earlier update stopped partway. Its old files are in the '
                              f'"{leftover.name}" folder in the app folder. Copy back what you '
                              'need, then move that folder somewhere else before updating '
                              'again.', 409)
    files_root = incoming / "files"
    try:
        files_root.mkdir(parents=True)
        replaced, created = [], []
        for relative in changed:
            parts = PurePosixPath(relative).parts
            source = app_root.joinpath(*parts)
            if source.is_file():
                target = files_root.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                replaced.append(relative)
            else:
                created.append(relative)
        manifest = {
            "from_version": from_version,
            "to_version": to_version,
            "created_at": time.time(),
            "replaced": replaced,
            "created": created,
        }
        _write_manifest(incoming, manifest)
    except OSError:
        # Nothing was installed yet, so this partial copy holds nothing
        # the app folder doesn't; remove it so it can't block later updates.
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    return incoming


def _keep_backup(app_root: Path, incoming: Path) -> None:
    """Make `incoming` the kept backup, removing the older one. If that
    fails, the older backup is moved back; if even that fails it stays in
    `.update-backup.old/`, which blocks updates until the user moves it
    (see _write_backup)."""
    kept = app_root / BACKUP_DIR_NAME
    old = app_root / f".{BACKUP_DIR_NAME}.old"
    had_kept = kept.exists()
    if had_kept:
        os.replace(kept, old)
    try:
        os.replace(incoming, kept)
    except OSError:
        if had_kept:
            os.replace(old, kept)
        raise
    shutil.rmtree(old, ignore_errors=True)


def _put_back(app_root: Path, files_root: Path, replaced: list[str], created: list[str]) -> None:
    """Copy the kept files back and remove the added ones. Raises OSError if
    any of it fails, so the caller keeps the backup."""
    for relative in reversed(created):
        app_root.joinpath(*PurePosixPath(relative).parts).unlink(missing_ok=True)
    for relative in _backend_last(replaced):
        parts = PurePosixPath(relative).parts
        destination = app_root.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        incoming = destination.with_name(f".{destination.name}.restoring")
        shutil.copy2(files_root.joinpath(*parts), incoming)
        os.replace(incoming, destination)


def _apply_staged(app_root: Path, stage_root: Path, changed: list[str],
                  done_replaced: list[str], done_created: list[str]) -> None:
    """Install the staged files, recording each one done so a failure can be
    undone."""
    for relative in _backend_last(changed):
        parts = PurePosixPath(relative).parts
        destination = app_root.joinpath(*parts)
        existed = destination.is_file()
        destination.parent.mkdir(parents=True, exist_ok=True)
        incoming = destination.with_name(f".{destination.name}.updating")
        try:
            shutil.copy2(stage_root.joinpath(*parts), incoming)
            os.replace(incoming, destination)
        except OSError:
            incoming.unlink(missing_ok=True)
            raise
        (done_replaced if existed else done_created).append(relative)


def _install(app_root: Path, stage_root: Path, changed: list[str], incoming: Path) -> None:
    """Install the staged files and keep `incoming` as the backup, as one
    step: if any part fails, the old files are put back. If even that fails,
    the old files are kept (never deleted) and the user is told how to
    restore them."""
    done_replaced, done_created = [], []
    try:
        _apply_staged(app_root, stage_root, changed, done_replaced, done_created)
        _keep_backup(app_root, incoming)
    except Exception as exc:
        try:
            _put_back(app_root, incoming / "files", done_replaced, done_created)
        except OSError:
            try:
                # Mark it: the app folder is now a mix of versions, and these
                # files are the way back, whatever version.py says.
                manifest_path = incoming / MANIFEST_NAME
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["partial"] = True
                _write_manifest(incoming, manifest)
                _keep_backup(app_root, incoming)
                where = f'the "{BACKUP_DIR_NAME}" folder. Run: python updater.py --restore'
            except (OSError, ValueError):
                where = f'the ".{BACKUP_DIR_NAME}.new" folder in the app folder. Copy them back by hand'
            raise UpdateError("The update stopped partway and some old files could not be put "
                              f"back. The old files are kept in {where}.", 500) from exc
        shutil.rmtree(incoming, ignore_errors=True)
        raise


def _check_release_version(archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo],
                           remote_files: dict[str, str], wanted: str) -> None:
    """The release's own version.py must say the version its tag says, so a
    release tagged on the wrong commit is never installed."""
    info = members.get("version.py")
    data = archive.read(info) if info is not None else b""
    if not data or _git_blob_sha(data) != remote_files.get("version.py"):
        found = ""
    else:
        match = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", data.decode("utf-8", "replace"))
        found = match.group(1) if match else ""
    if found != wanted:
        raise UpdateError(f"The release on GitHub is labelled {wanted}, but its files "
                          f"say {found or 'no version'}. Nothing was changed.", 409)


def apply_update(version: str, commit: str, app_root: Path | None = None) -> dict:
    """Install the release the user was shown: `version` and `commit` come
    from check_for_update(). If the newest release is no longer that version,
    or its tag now points at a different commit, nothing is changed."""
    root = _app_root(app_root)
    wanted = str(version or "").strip().lstrip("v")
    wanted_commit = str(commit or "").strip()
    if not parse_version(wanted) or not wanted_commit:
        raise UpdateError("Say which version to install.", 400)

    def work():
        installed = current_version(root)
        release = _latest_release()
        if release["version"] != wanted:
            raise UpdateError("A different version is now the newest release. "
                              "Check for updates again to see what it changes.", 409)
        if parse_version(installed) and parse_version(wanted) <= parse_version(installed):
            raise UpdateError("This version is already installed.", 409)

        commit_sha = _tag_commit(release["tag"])
        if commit_sha != wanted_commit:
            raise UpdateError("This release was changed on GitHub after you checked. "
                              "Check for updates again to see what it changes now.", 409)
        remote_files = _commit_tree(commit_sha)
        if not remote_files:
            raise UpdateError("No application files were found in the release.")
        changed = _changed_files(root, remote_files)
        if not changed:
            raise UpdateError("The app's files already match this version.", 409)

        archive = _download_archive(commit_sha)
        with archive:
            members = _archive_member_map(archive)
            _check_release_version(archive, members, remote_files, wanted)
            temp_dir = None
            try:
                temp_dir = Path(tempfile.mkdtemp(prefix="chv-update-"))
                stage_root = temp_dir / "staged"
                stage_root.mkdir()
                _stage_files(archive, members, remote_files, changed, stage_root)
                incoming = _write_backup(root, changed, installed, wanted)
                _install(root, stage_root, changed, incoming)
            except PermissionError as exc:
                raise UpdateError("The app folder could not be changed. Nothing was updated.", 500) from exc
            except OSError as exc:
                raise UpdateError("The app's files could not be replaced. Nothing was updated.", 500) from exc
            finally:
                # A file the system still holds open must not turn a finished
                # update into a reported failure; the temp folder is left.
                if temp_dir is not None:
                    shutil.rmtree(temp_dir, ignore_errors=True)
        return {
            "status": "updated",
            "version": wanted,
            "previous_version": installed,
            "backup": backup_info(root),
        }

    return _locked(root, work)


def restore_backup(app_root: Path | None = None) -> dict:
    """Put back the files kept by the last update and remove the files it
    added. The backup is used up: it is deleted once restored."""
    root = _app_root(app_root)

    def work():
        folder = root / BACKUP_DIR_NAME
        info = backup_info(root)
        if info is None:
            raise UpdateError("There is no earlier version to go back to.", 404)
        installed = current_version(root)
        # A half-finished update may or may not have replaced version.py yet,
        # so either of its two versions is the interrupted state.
        expected = {info["from_version"], info["to_version"]} if info["partial"] else {info["to_version"]}
        if info["to_version"] and installed not in expected:
            # The app was changed since (by hand, say): putting back only the
            # files that update replaced would leave a mix of versions.
            raise UpdateError(f"The kept files undo the update to version {info['to_version']}, "
                              f"but version {installed} is installed now, so they were not used.",
                              409)
        manifest = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
        replaced = [p for p in manifest.get("replaced") or [] if _is_managed_file(str(p))]
        created = [p for p in manifest.get("created") or [] if _is_managed_file(str(p))]
        files_root = folder / "files"
        missing = [p for p in replaced if not files_root.joinpath(*PurePosixPath(p).parts).is_file()]
        if missing:
            raise UpdateError("The kept copy of the old files is incomplete, so it was not used.", 409)
        if not manifest.get("partial"):
            # From here on the app folder may be a mix of versions until the
            # restore finishes, so a retry must be allowed from either one.
            manifest["partial"] = True
            try:
                _write_manifest(folder, manifest)
            except OSError as exc:
                raise UpdateError("The app folder could not be changed. Nothing was put back.", 500) from exc
        try:
            _put_back(root, files_root, replaced, created)
        except PermissionError as exc:
            raise UpdateError("The app folder could not be changed. The old files are still kept "
                              f'in the "{BACKUP_DIR_NAME}" folder, so you can try again.', 500) from exc
        except OSError as exc:
            raise UpdateError("Some of the old files could not be put back. They are still kept "
                              f'in the "{BACKUP_DIR_NAME}" folder, so you can try again.', 500) from exc
        # Remove the manifest first: without it the folder is no longer a
        # backup, so it can't be offered or restored a second time.
        try:
            (folder / MANIFEST_NAME).unlink()
        except OSError as exc:
            raise UpdateError("The old files were put back, but the used copy in the "
                              f'"{BACKUP_DIR_NAME}" folder could not be cleared. Delete that '
                              "folder before going back again.", 500) from exc
        shutil.rmtree(folder, ignore_errors=True)
        return {"status": "restored", "version": info["from_version"] or current_version(root),
                "backup": None}

    return _locked(root, work)


def _main(argv: list[str]) -> int:
    """`python updater.py --restore` puts back the last update's backup, for
    when the new version will not start."""
    if argv != ["--restore"]:
        print("Usage: python updater.py --restore")
        return 2
    try:
        result = restore_backup()
    except UpdateError as exc:
        print(exc.message)
        return 1
    print(f"Restored version {result['version']}. Start the app again.")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
