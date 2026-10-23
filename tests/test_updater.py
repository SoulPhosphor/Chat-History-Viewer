"""The updater (AUDIT.md #16): updates come from tagged releases, a check
shows the version and changes without touching anything, and the last
update's old files are kept in update-backup/ so they can be put back.

Runs against a fake GitHub (no network) and throwaway app folders."""
import base64
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import errorlog
import server
import updater
from server import Handler
from tests.test_advanced_search import conversation
from tests.test_scratchpads import build_history


def version_py(version):
    return f'__version__ = "{version}"\n'.encode()


CHANGELOG = b"""# Changelog

## 1.4.0 - 2026-11-02

- Third change.

## 1.3.0 - 2026-11-01

- Second change.
- With two lines.

## 1.2.0 - 2026-10-18

- First change.
"""


class FakeGitHub:
    """Answers the few GitHub API calls the updater makes, from an in-memory
    set of releases: {tag: {path: bytes}}."""

    def __init__(self, releases, latest):
        self.releases = releases
        self.latest = latest
        self.urls = []

    def _not_found(self, url):
        return urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def fetch_json(self, url):
        self.urls.append(url)
        tail = url[len(updater.API_ROOT):]
        if tail == "/releases/latest":
            if self.latest is None:
                raise self._not_found(url)
            return {"tag_name": self.latest, "body": "Release notes.", "published_at": "2026-11-01T00:00:00Z"}
        if tail.startswith("/commits/"):
            tag = tail[len("/commits/"):]
            if tag not in self.releases:
                raise self._not_found(url)
            return {"sha": f"commit-{tag}"}
        if tail.startswith("/git/trees/commit-"):
            tag = tail[len("/git/trees/commit-"):].split("?")[0]
            return {"truncated": False, "tree": [
                {"path": path, "type": "blob", "sha": updater._git_blob_sha(data)}
                for path, data in self.releases[tag].items()
            ]}
        if tail.startswith("/git/blobs/"):
            sha = tail[len("/git/blobs/"):]
            for files in self.releases.values():
                for data in files.values():
                    if updater._git_blob_sha(data) == sha:
                        return {"content": base64.b64encode(data).decode()}
            raise self._not_found(url)
        raise self._not_found(url)

    def download(self, commit_sha):
        tag = commit_sha[len("commit-"):]
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            for name, data in self.releases[tag].items():
                archive.writestr(f"repo-{tag}/{name}", data)
        return zipfile.ZipFile(io.BytesIO(payload.getvalue()))

    def patch(self):
        return mock.patch.multiple(updater, _fetch_json=self.fetch_json, _download_archive=self.download)


OLD = {
    "version.py": version_py("1.2.0"),
    "server.py": b"old server",
    "static/app.js": b"old app",
    "CHANGELOG.md": CHANGELOG.split(b"## 1.3.0")[0] + b"## 1.2.0" + CHANGELOG.split(b"## 1.2.0")[1],
}
NEW = {
    "version.py": version_py("1.3.0"),
    "server.py": b"new server",
    "static/app.js": b"old app",
    "static/new.js": b"brand new",
    "CHANGELOG.md": b"# Changelog\n\n## 1.3.0" + CHANGELOG.split(b"## 1.3.0")[1],
}
NEWER = {**NEW, "version.py": version_py("1.4.0"), "server.py": b"newer server", "CHANGELOG.md": CHANGELOG}


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for path, data in OLD.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (self.root / "history.db").write_bytes(b"database")
        (self.root / "source").mkdir()
        (self.root / "source" / "conversations.json").write_bytes(b"private chats")

    def tearDown(self):
        self.temp.cleanup()

    def github(self, latest="v1.3.0", **extra):
        releases = {"v1.2.0": OLD, "v1.3.0": NEW, **extra}
        return FakeGitHub(releases, latest)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and p.name != updater.LOCK_FILE_NAME}

    def test_runtime_data_is_never_managed(self):
        for path in ("source/conversations.json", "source/files/photo.png", "history.db",
                     "userdata.db", "backups/history-x.db", "logs/errors.json",
                     "update-backup/manifest.json", "update-backup/files/server.py", ".env"):
            self.assertFalse(updater._is_managed_file(path), path)
        self.assertTrue(updater._is_managed_file("server.py"))
        self.assertTrue(updater._is_managed_file("static/app.js"))

    def test_check_uses_latest_release_not_main(self):
        fake = self.github()
        before = self.snapshot()
        with fake.patch():
            result = updater.check_for_update(self.root)
        self.assertEqual(self.snapshot(), before)  # a check changes nothing
        self.assertFalse(any(url.endswith("/main") or "/main?" in url for url in fake.urls))
        self.assertIn(f"{updater.API_ROOT}/releases/latest", fake.urls)
        self.assertIn(f"{updater.API_ROOT}/commits/v1.3.0", fake.urls)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["current_version"], "1.2.0")
        self.assertEqual(result["latest_version"], "1.3.0")
        self.assertEqual(result["files_changed"], 3)  # version.py, server.py, CHANGELOG.md
        self.assertEqual(result["files_added"], 1)    # static/new.js
        self.assertEqual(result["local_edits"], [])
        self.assertEqual([c["version"] for c in result["changes"]], ["1.3.0"])
        self.assertIn("Second change.", result["changes"][0]["text"])

    def test_check_lists_every_skipped_version_and_local_edits(self):
        (self.root / "server.py").write_bytes(b"edited by hand")
        fake = self.github(latest="v1.4.0", **{"v1.4.0": NEWER})
        with fake.patch():
            result = updater.check_for_update(self.root)
        self.assertEqual([c["version"] for c in result["changes"]], ["1.4.0", "1.3.0"])
        self.assertEqual(result["local_edits"], ["server.py"])
        self.assertTrue(result["local_edits_known"])

    def test_no_update_when_latest_is_not_newer(self):
        fake = self.github(latest="v1.2.0")
        with fake.patch():
            result = updater.check_for_update(self.root)
        self.assertEqual(result["status"], "no_update")
        self.assertNotIn(f"{updater.API_ROOT}/commits/v1.2.0", fake.urls)

    def test_no_release_yet_is_a_plain_error(self):
        with self.github(latest=None).patch():
            with self.assertRaises(updater.UpdateError) as caught:
                updater.check_for_update(self.root)
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("No released version", caught.exception.message)

    def test_network_failure_is_a_plain_error(self):
        def offline(url):
            raise urllib.error.URLError("no network")
        with mock.patch.object(updater, "_fetch_json", offline):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.check_for_update(self.root)
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("could not be reached", caught.exception.message)

    def test_apply_installs_release_keeps_backup_and_data(self):
        with self.github().patch():
            result = updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["previous_version"], "1.2.0")
        for path, data in NEW.items():
            self.assertEqual((self.root / path).read_bytes(), data, path)
        self.assertEqual(updater.current_version(self.root), "1.3.0")
        self.assertEqual((self.root / "history.db").read_bytes(), b"database")
        self.assertEqual((self.root / "source" / "conversations.json").read_bytes(), b"private chats")

        backup = self.root / updater.BACKUP_DIR_NAME
        self.assertEqual((backup / "files" / "server.py").read_bytes(), b"old server")
        self.assertFalse((backup / "files" / "static" / "app.js").exists())  # unchanged, not copied
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(manifest["from_version"], "1.2.0")
        self.assertEqual(manifest["to_version"], "1.3.0")
        self.assertEqual(manifest["created"], ["static/new.js"])
        self.assertEqual(result["backup"]["from_version"], "1.2.0")
        self.assertEqual(updater.status(self.root)["backup"]["to_version"], "1.3.0")

    def test_restore_puts_old_files_back(self):
        before = self.snapshot()
        with self.github().patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        result = updater.restore_backup(self.root)
        self.assertEqual(result, {"status": "restored", "version": "1.2.0", "backup": None})
        self.assertEqual(self.snapshot(), before)  # new file removed, backup used up
        with self.assertRaises(updater.UpdateError) as caught:
            updater.restore_backup(self.root)
        self.assertEqual(caught.exception.status, 404)

    def test_command_line_restore(self):
        with self.github().patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        with mock.patch.object(updater, "_app_root", return_value=self.root), \
             mock.patch("builtins.print"):
            self.assertEqual(updater._main(["--restore"]), 0)
            self.assertEqual(updater._main(["--restore"]), 1)
            self.assertEqual(updater._main([]), 2)
        self.assertEqual((self.root / "server.py").read_bytes(), b"old server")

    def test_only_the_last_backup_is_kept(self):
        fake = self.github()
        with fake.patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        fake.releases["v1.4.0"] = NEWER
        fake.latest = "v1.4.0"
        with fake.patch():
            updater.apply_update("1.4.0", "commit-v1.4.0", self.root)
        backup = self.root / updater.BACKUP_DIR_NAME
        self.assertEqual((backup / "files" / "server.py").read_bytes(), b"new server")
        self.assertEqual(updater.backup_info(self.root)["from_version"], "1.3.0")
        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith(".update-backup")]
        self.assertEqual(leftovers, [])

    def test_apply_refuses_a_version_that_is_not_the_one_shown(self):
        before = self.snapshot()
        with self.github(latest="v1.4.0", **{"v1.4.0": NEWER}).patch():
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(updater.UpdateError) as caught:
            updater.apply_update("", "commit-v1.3.0", self.root)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.snapshot(), before)

    def test_failed_apply_puts_files_back_and_keeps_old_backup(self):
        fake = self.github()
        with fake.patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        updater.restore_backup(self.root)
        with fake.patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        kept = json.loads((self.root / "update-backup" / "manifest.json").read_text())
        fake.releases["v1.4.0"] = NEWER
        fake.latest = "v1.4.0"
        before = self.snapshot()

        real_replace = updater.os.replace
        calls = {"n": 0}

        def failing_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real_replace(src, dst)

        with fake.patch(), mock.patch.object(updater.os, "replace", failing_replace):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.4.0", "commit-v1.4.0", self.root)
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(json.loads((self.root / "update-backup" / "manifest.json").read_text()), kept)

    def updated_twice_then_offered_newer(self):
        """Install 1.3.0 (so a backup is kept), then offer 1.4.0."""
        fake = self.github()
        with fake.patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        fake.releases["v1.4.0"] = NEWER
        fake.latest = "v1.4.0"
        return fake

    def test_failed_backup_rotation_undoes_the_update(self):
        fake = self.updated_twice_then_offered_newer()
        kept = (self.root / "update-backup" / "manifest.json").read_text()
        before = self.snapshot()
        real_replace = updater.os.replace

        def refuse_new_backup(src, dst):
            if Path(src).name == ".update-backup.new":
                raise PermissionError("denied")
            return real_replace(src, dst)

        with fake.patch(), mock.patch.object(updater.os, "replace", refuse_new_backup):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.4.0", "commit-v1.4.0", self.root)
        self.assertIn("Nothing was updated", caught.exception.message)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.root / "update-backup" / "manifest.json").read_text(), kept)

    def test_failed_rollback_keeps_the_old_files(self):
        fake = self.updated_twice_then_offered_newer()
        real_replace = updater.os.replace
        calls = {"n": 0}

        def fail_late(src, dst):
            if str(src).endswith(".updating"):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("disk full")
            return real_replace(src, dst)

        with fake.patch(), mock.patch.object(updater.os, "replace", fail_late), \
             mock.patch.object(updater, "_put_back", side_effect=OSError("locked")):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.4.0", "commit-v1.4.0", self.root)
        self.assertIn("python updater.py --restore", caught.exception.message)
        info = updater.backup_info(self.root)
        self.assertEqual((info["from_version"], info["to_version"]), ("1.3.0", "1.4.0"))
        self.assertTrue(info["partial"])  # restorable although 1.4.0 never fully arrived
        self.assertEqual((self.root / "update-backup" / "files" / "server.py").read_bytes(),
                         b"new server")
        (self.root / "version.py").write_bytes(version_py("9.0.0"))  # repaired by hand
        with self.assertRaises(updater.UpdateError) as caught:
            updater.restore_backup(self.root)
        self.assertEqual(caught.exception.status, 409)
        (self.root / "version.py").write_bytes(version_py("1.3.0"))
        updater.restore_backup(self.root)
        self.assertEqual((self.root / "server.py").read_bytes(), b"new server")
        self.assertEqual(updater.current_version(self.root), "1.3.0")

    def test_incomplete_restore_keeps_the_backup(self):
        with self.github().patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        real_unlink = Path.unlink

        def locked(path, missing_ok=False):
            if path.name == "new.js":
                raise PermissionError("locked")
            return real_unlink(path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", locked):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.restore_backup(self.root)
        self.assertEqual(caught.exception.status, 500)
        self.assertIn("still kept", caught.exception.message)
        self.assertIsNotNone(updater.backup_info(self.root))
        updater.restore_backup(self.root)  # a retry works
        self.assertFalse((self.root / "static" / "new.js").exists())
        self.assertIsNone(updater.backup_info(self.root))

    def test_check_says_when_local_edits_cannot_be_listed(self):
        fake = self.github()
        del fake.releases["v1.2.0"]
        with fake.patch():
            result = updater.check_for_update(self.root)
        self.assertFalse(result["local_edits_known"])
        self.assertEqual(result["local_edits"], [])

    def test_apply_refuses_a_release_moved_after_the_check(self):
        fake = self.github()
        with fake.patch():
            checked = updater.check_for_update(self.root)
        self.assertEqual(checked["commit"], "commit-v1.3.0")
        before = self.snapshot()
        real_fetch = fake.fetch_json

        def retargeted(url):
            if url.endswith("/commits/v1.3.0"):
                return {"sha": "commit-v9.9.9"}
            return real_fetch(url)

        with mock.patch.multiple(updater, _fetch_json=retargeted, _download_archive=fake.download):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.3.0", checked["commit"], self.root)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("changed on GitHub", caught.exception.message)
        self.assertEqual(self.snapshot(), before)

    def test_leftover_recovery_folder_blocks_new_updates(self):
        recovery = self.root / ".update-backup.new" / "files"
        recovery.mkdir(parents=True)
        (recovery / "server.py").write_bytes(b"only copy")
        with self.github().patch():
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual((recovery / "server.py").read_bytes(), b"only copy")
        self.assertEqual((self.root / "server.py").read_bytes(), b"old server")

    def test_another_copy_of_the_app_updating_blocks_this_one(self):
        before = self.snapshot()
        with open(self.root / updater.LOCK_FILE_NAME, "a+b") as other_copy:
            self.assertTrue(updater._lock_file(other_copy))
            with self.github().patch():
                with self.assertRaises(updater.UpdateError) as caught:
                    updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
            self.assertEqual(caught.exception.status, 409)
            with self.assertRaises(updater.UpdateError):
                updater.restore_backup(self.root)
        self.assertEqual(self.snapshot(), before)
        with self.github().patch():  # free again once the other copy lets go
            self.assertEqual(updater.apply_update("1.3.0", "commit-v1.3.0", self.root)["status"],
                             "updated")

    def test_old_backup_survives_a_failed_move_back(self):
        fake = self.updated_twice_then_offered_newer()
        real_replace = updater.os.replace

        def stuck(src, dst):
            if Path(src).name in (".update-backup.new", ".update-backup.old"):
                raise PermissionError("denied")
            return real_replace(src, dst)

        with fake.patch(), mock.patch.object(updater.os, "replace", stuck):
            with self.assertRaises(updater.UpdateError):
                updater.apply_update("1.4.0", "commit-v1.4.0", self.root)
        old = self.root / ".update-backup.old"
        self.assertEqual((old / "files" / "server.py").read_bytes(), b"old server")
        with fake.patch():
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.4.0", "commit-v1.4.0", self.root)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn(".update-backup.old", caught.exception.message)
        self.assertEqual((old / "files" / "server.py").read_bytes(), b"old server")

    def test_failed_backup_copy_leaves_nothing_behind(self):
        before = self.snapshot()
        with self.github().patch(), \
             mock.patch.object(updater.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        self.assertIn("Nothing was updated", caught.exception.message)
        self.assertFalse((self.root / ".update-backup.new").exists())
        self.assertEqual(self.snapshot(), before)
        with self.github().patch():  # the next try is not blocked
            self.assertEqual(updater.apply_update("1.3.0", "commit-v1.3.0", self.root)["status"],
                             "updated")

    def test_release_whose_files_have_another_version_is_refused(self):
        fake = self.github()
        fake.releases["v1.3.0"] = {**NEW, "version.py": version_py("1.2.9")}
        before = self.snapshot()
        with fake.patch():
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("1.2.9", caught.exception.message)
        self.assertEqual(self.snapshot(), before)

    def test_temp_cleanup_failure_does_not_undo_a_finished_update(self):
        real_rmtree = updater.shutil.rmtree

        def locked_temp(path, *args, **kwargs):
            if Path(path).name.startswith("chv-update-"):
                if not kwargs.get("ignore_errors"):
                    raise PermissionError("locked")
                return None
            return real_rmtree(path, *args, **kwargs)

        with self.github().patch(), mock.patch.object(updater.shutil, "rmtree", locked_temp):
            result = updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(updater.current_version(self.root), "1.3.0")

    def test_used_backup_cannot_be_restored_twice(self):
        with self.github().patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        with mock.patch.object(updater.shutil, "rmtree"):  # the folder stays (locked file)
            updater.restore_backup(self.root)
        self.assertTrue((self.root / "update-backup").exists())
        self.assertIsNone(updater.backup_info(self.root))
        with self.assertRaises(updater.UpdateError) as caught:
            updater.restore_backup(self.root)
        self.assertEqual(caught.exception.status, 404)

    def test_restore_refuses_when_another_version_is_installed(self):
        with self.github().patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        for path in ("version.py", "server.py"):  # updated to 1.4.0 by hand
            (self.root / path).write_bytes(NEWER[path])
        before = self.snapshot()
        with self.assertRaises(updater.UpdateError) as caught:
            updater.restore_backup(self.root)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("1.4.0", caught.exception.message)
        self.assertEqual(self.snapshot(), before)

    def test_partial_backup_blocks_new_updates(self):
        fake = self.updated_twice_then_offered_newer()
        real_replace = updater.os.replace
        calls = {"n": 0}

        def fail_late(src, dst):
            if str(src).endswith(".updating"):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("disk full")
            return real_replace(src, dst)

        with fake.patch(), mock.patch.object(updater.os, "replace", fail_late), \
             mock.patch.object(updater, "_put_back", side_effect=OSError("locked")):
            with self.assertRaises(updater.UpdateError):
                updater.apply_update("1.4.0", "commit-v1.4.0", self.root)
        kept = (self.root / "update-backup" / "manifest.json").read_text()
        with fake.patch():
            with self.assertRaises(updater.UpdateError) as caught:
                updater.apply_update("1.4.0", "commit-v1.4.0", self.root)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("Go back", caught.exception.message)
        self.assertEqual((self.root / "update-backup" / "manifest.json").read_text(), kept)

    def test_restore_that_stops_halfway_can_be_retried(self):
        with self.github().patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        real_replace = updater.os.replace

        def server_locked(src, dst):
            if str(src).endswith(".restoring") and Path(dst).name == "server.py":
                raise PermissionError("locked")
            return real_replace(src, dst)

        with mock.patch.object(updater.os, "replace", server_locked):
            with self.assertRaises(updater.UpdateError):
                updater.restore_backup(self.root)
        self.assertEqual(updater.current_version(self.root), "1.2.0")  # version.py went first
        self.assertTrue(updater.backup_info(self.root)["partial"])
        updater.restore_backup(self.root)
        self.assertEqual((self.root / "server.py").read_bytes(), b"old server")

    def test_failed_manifest_write_keeps_the_backup_usable(self):
        with self.github().patch():
            updater.apply_update("1.3.0", "commit-v1.3.0", self.root)
        real_replace = updater.os.replace

        def disk_full(src, dst):
            if Path(src).name == ".manifest.json.writing":
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch.object(updater.os, "replace", disk_full):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.restore_backup(self.root)
        self.assertIn("Nothing was put back", caught.exception.message)
        self.assertIsNotNone(updater.backup_info(self.root))
        self.assertFalse((self.root / "update-backup" / ".manifest.json.writing").exists())
        updater.restore_backup(self.root)
        self.assertEqual((self.root / "server.py").read_bytes(), b"old server")

    def test_update_running_is_true_only_during_an_update(self):
        seen = []

        def work():
            seen.append(updater.update_running())
            return {}

        self.assertFalse(updater.update_running(self.root))
        updater._locked(self.root, work)
        self.assertEqual(seen, [True])
        self.assertFalse(updater.update_running(self.root))
        # Another copy of the app updating the same folder counts too.
        with open(self.root / updater.LOCK_FILE_NAME, "a+b") as other_copy:
            self.assertTrue(updater._lock_file(other_copy))
            self.assertTrue(updater.update_running(self.root))
        self.assertFalse(updater.update_running(self.root))

    def test_changelog_entries(self):
        text = CHANGELOG.decode()
        self.assertEqual([e["version"] for e in updater.changelog_entries(text, "1.2.0", "1.4.0")],
                         ["1.4.0", "1.3.0"])
        self.assertEqual(updater.changelog_entries(text, "1.3.0", "1.3.0"), [])
        entry = updater.changelog_entries(text, "1.2.0", "1.3.0")[0]
        self.assertEqual(entry["title"], "1.3.0 - 2026-11-01")
        self.assertEqual(entry["text"], "- Second change.\n- With two lines.")


class UpdateRouteTests(unittest.TestCase):
    """The /api/update/* routes on a real server: same-page requests only,
    and failures come back as {"error": "<plain message>"}."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        _, db_path = build_history(root, [conversation("one", "First chat", ["gpt-4o"])])
        errorlog.configure(log_dir=root / "logs", db_path=db_path)
        handler = type("TestHandler", (Handler,), {"db_path": db_path})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        errorlog.configure(log_dir=server.app_paths.LOG_DIR, db_path=server.app_paths.DB_PATH)
        self.temp.cleanup()

    def call(self, method, path, body=None, update_header=True):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if update_header:
            headers["X-CHV-Update"] = "1"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_status_reports_version_without_network(self):
        with mock.patch.object(updater, "_fetch_json", side_effect=AssertionError("no network")):
            status, body = self.call("GET", "/api/update/status", update_header=False)
        self.assertEqual(status, 200)
        self.assertEqual(body["current_version"], updater.current_version())

    def test_version_reports_a_running_update(self):
        with mock.patch.object(updater, "update_running", return_value=True):
            status, body = self.call("GET", "/api/version", update_header=False)
        self.assertEqual(status, 200)
        self.assertTrue(body["updating"])

    def test_update_needs_the_app_header(self):
        with mock.patch.object(updater, "check_for_update") as check:
            status, body = self.call("POST", "/api/update/check", {}, update_header=False)
        self.assertEqual(status, 403)
        self.assertIn("error", body)
        check.assert_not_called()

    def test_old_main_branch_route_is_gone(self):
        status, body = self.call("POST", "/api/update", {})
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_updater_errors_are_plain_json(self):
        problem = updater.UpdateError("A different version is now the newest release.", 409)
        with mock.patch.object(updater, "apply_update", side_effect=problem) as apply:
            status, body = self.call("POST", "/api/update/apply", {"version": "9.9.9"})
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "A different version is now the newest release."})
        self.assertEqual(apply.call_args.args[0], "9.9.9")

        def offline(*args):
            try:
                raise urllib.error.URLError("no network")
            except urllib.error.URLError as exc:
                raise updater.UpdateError("GitHub could not be reached.", 502) from exc
        with mock.patch.object(updater, "check_for_update", side_effect=offline):
            status, body = self.call("POST", "/api/update/check", {})
        self.assertEqual(status, 502)
        self.assertEqual(body, {"error": "GitHub could not be reached."})
        logged = errorlog.recent_groups(10)
        self.assertTrue(any("URLError" in json.dumps(group) for group in logged))

    def test_restore_route(self):
        with mock.patch.object(updater, "restore_backup",
                               return_value={"status": "restored", "version": "1.2.0", "backup": None}):
            status, body = self.call("POST", "/api/update/restore", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "restored")


if __name__ == "__main__":
    unittest.main()
