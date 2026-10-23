"""Session 9; synthetic exports and throwaway databases only."""
import tempfile
import unittest
from pathlib import Path

import server
from tests.test_advanced_search import conversation
from tests.test_importer import write_export
from tests.test_scratchpads import build_history


class RecordedBehaviourTests(unittest.TestCase):
    """Recorded before changing imports: replay keeps identity and user state."""

    def test_replaying_an_export_preserves_title_and_chat_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = conversation("one", "Original", ["gpt-4o"])
            _, db = build_history(root, [chat])
            conn = server.open_db(db)
            conn.execute("INSERT INTO udb.conversation_meta (conversation_id, custom_title) VALUES ('one', 'My title')")
            conn.commit()
            # Different bytes, same conversation: an overlapping backup.
            write_export(root / "overlap.json", [chat, conversation("two", "New", ["gpt-4o"])])
            result = server.import_new_backups(conn, root)
            conn.commit()
            self.assertEqual((result["added"], result["unchanged"]), (1, 1))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations WHERE id = 'one'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT custom_title FROM udb.conversation_meta WHERE conversation_id = 'one'").fetchone()[0], "My title")
            conn.close()

import contextlib
import http.client
import json
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from unittest import mock

import background_jobs as jobs
import build_db
import errorlog


def wait_job(db, job_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = jobs.get(db, job_id)
        if job['status'] != 'running':
            # A terminal receipt is saved just before releasing the OS lock.
            while jobs.running(db) and time.monotonic() < deadline:
                time.sleep(.005)
            return job
        time.sleep(.01)
    raise AssertionError('Job did not finish')


@contextlib.contextmanager
def app_server(root, db):
    handler = type('JobTestHandler', (server.Handler,), {'db_path': db, 'source_dir': root})
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    def request(method, path, payload=None):
        c = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'} if payload is not None else {}
        c.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        return r.status, body
    try:
        yield request
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.db = build_history(self.root, [conversation('seed', 'Seed', ['gpt-4o'])])
        errorlog.configure(log_dir=self.root / 'logs', db_path=self.db)

    def tearDown(self):
        self.assertFalse(jobs.running(self.db))
        errorlog.configure(log_dir=server.app_paths.LOG_DIR, db_path=server.app_paths.DB_PATH)
        self.temp.cleanup()

    def new_export(self, n=65):
        return write_export(self.root / 'new.json', [conversation('new-' + str(i), 'New', ['gpt-4o']) for i in range(n)])

    def query(self, sql):
        conn = server.open_db(self.db)
        try:
            return [tuple(r) for r in conn.execute(sql)]
        finally:
            conn.close()

    def test_async_receipt_progress_cancel_and_retry_keeps_whole_chats(self):
        self.new_export()
        entered, release = threading.Event(), threading.Event()
        real = build_db._reconcile_one
        def pause(conn, rec, *args):
            if rec['meta']['id'] == 'new-24':
                entered.set()
                self.assertTrue(release.wait(10))
            return real(conn, rec, *args)
        with app_server(self.root, self.db) as request, mock.patch.object(build_db, '_reconcile_one', pause):
            try:
                status, body = request('POST', '/api/import-new', {})
                self.assertEqual(status, 202)
                job_id = body['job']['id']
                self.assertTrue(entered.wait(5))
                self.assertGreaterEqual(self.query("SELECT COUNT(*) FROM conversations WHERE id LIKE 'new-%'")[0][0], 20)
                # Both refusal and cancellation work through another HTTP handler.
                status, body = request('POST', '/api/import-new', {})
                self.assertEqual((status, list(body)), (409, ['error']))
                status, body = request('GET', '/api/jobs/' + job_id)
                self.assertEqual(status, 200)
                self.assertGreaterEqual(body['job']['progress'], 20)
                status, _ = request('POST', '/api/jobs/' + job_id + '/cancel', {})
                self.assertEqual(status, 200)
            finally:
                release.set()
            result = wait_job(self.db, job_id)
        self.assertEqual(result['status'], 'cancelled')
        self.assertIn('already saved are kept', result['message'])
        self.assertEqual(self.query("SELECT COUNT(*) FROM conversations WHERE id LIKE 'new-%'")[0][0], 25)
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages WHERE conversation_id LIKE 'new-%'")[0][0], 50)
        self.assertEqual(self.query("SELECT COUNT(*) FROM imported_backups")[0][0], 1)
        self.assertTrue((self.root / 'new.json').exists())  # not renamed/marked complete
        with app_server(self.root, self.db) as request:
            _, data = request('POST', '/api/import-new', {})
            result = wait_job(self.db, data['job']['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.query("SELECT COUNT(*) FROM conversations WHERE id LIKE 'new-%'")[0][0], 65)
        self.assertEqual(self.query("SELECT COUNT(*) FROM search_index WHERE substr(conversation_id, 1, 4) = 'new-'")[0][0], 65)
        self.assertEqual(self.query("SELECT COUNT(*) FROM imported_backups")[0][0], 2)

    def test_second_process_serves_reads_writes_and_refuses_another_job(self):
        self.new_export(25)
        entered, release = threading.Event(), threading.Event()
        real = jobs.Job.batch
        def pause(job, conn, done):
            real(job, conn, done)
            if done == 20:
                entered.set()
                self.assertTrue(release.wait(15))
        with app_server(self.root, self.db) as request, mock.patch.object(jobs.Job, 'batch', pause):
            _, data = request('POST', '/api/import-new', {})
            process = None
            try:
                self.assertTrue(entered.wait(5))
                # A genuinely independent Python process runs the real startup
                # and serves HTTP against only this throwaway database.
                script = '''
import json, sys, threading, http.client, socket
from pathlib import Path
import server
root=Path(sys.argv[1]); ready=threading.Event(); port=[]
def on_ready(p): port.append(p); ready.set()
sock=socket.socket(); sock.bind(('127.0.0.1',0)); chosen=sock.getsockname()[1]; sock.close()
threading.Thread(target=lambda: server.serve(port=chosen, db_path=root/'history.db', source_dir=root, on_ready=on_ready, autoreload=False), daemon=True).start()
assert ready.wait(8), 'second app failed to start'
def call(method,path,body=None):
 c=http.client.HTTPConnection('127.0.0.1',port[0],timeout=3)
 c.request(method,path,json.dumps(body) if body is not None else None,{'Content-Type':'application/json'})
 r=c.getresponse(); data=json.loads(r.read()); c.close(); return r.status,data
assert call('GET','/api/conversations')[0]==200
status,data=call('POST','/api/import-new',{})
assert status==409 and list(data)==['error'],(status,data)
assert call('PATCH','/api/preferences',{'preferences':{'sidebarWidth':350}})[0]==200
assert call('GET','/api/preferences')[1]['preferences']['sidebarWidth']==350
job=call('GET','/api/jobs')[1]['job']
assert job['status']=='running'
assert call('POST','/api/jobs/'+job['id']+'/cancel',{})[0]==200
print('SECOND_COPY_OK',flush=True)
'''
                process = subprocess.run([sys.executable, '-c', script, str(self.root)], capture_output=True, text=True, timeout=12)
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                self.assertIn('SECOND_COPY_OK', process.stdout)
            finally:
                release.set()
            self.assertEqual(wait_job(self.db, data['job']['id'])['status'], 'cancelled')

    def test_generic_callback_and_os_lock_released_after_failure_and_interruption(self):
        entered, release = threading.Event(), threading.Event()
        def work(job):
            job.progress('Working…', done=1, total=2, force=True)
            entered.set()
            release.wait(5)
            raise RuntimeError('made-up failure')
        receipt = jobs.start(self.db, 'future-ai-test', work)
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(jobs.JobBusy):
                jobs.start(self.db, 'import', lambda _: None)
        finally:
            release.set()
        state = wait_job(self.db, receipt['id'])
        self.assertEqual(state['status'], 'failed')
        self.assertNotIn('made-up failure', state['message'])
        receipt = jobs.start(self.db, 'future-ai-test', lambda job: {'ok': True})
        self.assertTrue(wait_job(self.db, receipt['id'])['result']['ok'])
        # A receipt left by a killed process is read as interrupted.
        state = dict(state, status='running')
        (self.root / jobs.STATE_DIR / (state['id'] + '.json')).write_text(json.dumps(state))
        self.assertEqual(jobs.get(self.db, state['id'])['status'], 'interrupted')
        with self.assertRaises(server.ApiError):
            jobs.get(self.db, '../history.db')

    def test_poll_racing_with_completion_returns_the_saved_result(self):
        with jobs.hold(self.db):
            task = jobs.Job(self.db, 'receipt-race-test')
        def finish_before_lock_check_returns(_db):
            task.state.update(status='completed', result={'added': 2}, message='Finished.')
            task.save()
            return False
        with mock.patch.object(jobs, 'running', side_effect=finish_before_lock_check_returns):
            state = jobs.get(self.db, task.id)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['message'], 'Finished.')
        self.assertEqual(state['result'], {'added': 2})

    def test_backup_failure_changes_no_chats(self):
        self.new_export()
        with app_server(self.root, self.db) as request, mock.patch.object(server.backups, 'backup_all', side_effect=OSError('full')):
            _, body = request('POST', '/api/import-new', {})
            self.assertEqual(wait_job(self.db, body['job']['id'])['status'], 'failed')
        self.assertEqual(self.query('SELECT COUNT(*) FROM conversations')[0][0], 1)

    def make_review(self, n):
        conn = server.open_db(self.db)
        for i in range(n):
            cid = 'review-' + str(i)
            rec = build_db.import_record(conversation(cid, 'Review', ['gpt-4o']), i, 'chatgpt')
            conn.execute('INSERT INTO udb.pending_reimport (conversation_id, provider, record_json) VALUES (?, ?, ?)', (cid, 'chatgpt', json.dumps(rec)))
            conn.execute('INSERT INTO udb.deleted_records (conversation_id, provider) VALUES (?, ?)', (cid, 'chatgpt'))
        conn.commit()
        conn.close()

    def test_review_cancel_preserves_remaining_review_and_deletion_records(self):
        self.make_review(45)
        real = build_db._reconcile_one
        attempted = 0
        def cancel_at_25(conn, rec, *args):
            nonlocal attempted
            attempted += 1
            result = real(conn, rec, *args)
            if attempted == 25:
                jobs.cancel(self.db, jobs.get(self.db)['id'])
            return result
        with app_server(self.root, self.db) as request, mock.patch.object(build_db, '_reconcile_one', cancel_at_25):
            _, body = request('POST', '/api/import-new/review-import', {'import_all': True})
            result = wait_job(self.db, body['job']['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['result']['imported'], 25)
        self.assertEqual(self.query('SELECT COUNT(*) FROM udb.pending_reimport')[0][0], 20)
        self.assertEqual(self.query('SELECT COUNT(*) FROM udb.deleted_records')[0][0], 20)
        self.assertEqual(self.query("SELECT COUNT(*) FROM conversations WHERE id LIKE 'review-%'")[0][0], 25)

    def test_review_failed_chat_keeps_tombstone_and_review_row(self):
        self.make_review(1)
        with app_server(self.root, self.db) as request, mock.patch.object(build_db, '_insert_conv_rows', side_effect=RuntimeError('test error')):
            _, body = request('POST', '/api/import-new/review-import', {'import_all': True})
            result = wait_job(self.db, body['job']['id'])
        self.assertEqual(result['result']['errors'], 1)
        self.assertEqual(self.query('SELECT COUNT(*) FROM udb.pending_reimport')[0][0], 1)
        self.assertEqual(self.query('SELECT COUNT(*) FROM udb.deleted_records')[0][0], 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages WHERE conversation_id LIKE 'review-%'")[0][0], 0)

    def test_rebuild_cancel_is_partial_and_keeps_user_state_and_deleted_chats_out(self):
        self.new_export(45)
        # Seed itself has enough chats to cancel during its write phase.
        write_export(self.root / 'conversations.json', [conversation('seed-' + str(i), 'Seed', ['gpt-4o']) for i in range(45)])
        conn = server.open_db(self.db)
        conn.execute("INSERT INTO udb.conversation_meta (conversation_id, custom_title) VALUES ('seed-0', 'Keep me')")
        conn.execute("INSERT INTO udb.deleted_records (conversation_id, provider) VALUES ('seed-1', 'chatgpt')")
        conn.commit(); conn.close()
        real = build_db._reconcile_one
        def cancel_seed(conn, rec, *args):
            value = real(conn, rec, *args)
            if rec['meta']['id'] == 'seed-24':
                jobs.cancel(self.db, jobs.get(self.db)['id'])
            return value
        with app_server(self.root, self.db) as request, mock.patch.object(build_db, '_reconcile_one', cancel_seed):
            _, body = request('POST', '/api/rebuild', {})
            result = wait_job(self.db, body['job']['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.query('SELECT custom_title FROM udb.conversation_meta')[0][0], 'Keep me')
        self.assertEqual(self.query("SELECT COUNT(*) FROM conversations WHERE id='seed-1'")[0][0], 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM import_sources WHERE conversation_id='seed-1'")[0][0], 0)
        self.assertEqual(self.query('SELECT COUNT(*) FROM conversations')[0][0], 24)
        self.assertEqual(self.query('SELECT COUNT(*) FROM imported_backups')[0][0], 0)
        with app_server(self.root, self.db) as request:
            _, body = request('POST', '/api/rebuild', {})
            self.assertEqual(wait_job(self.db, body['job']['id'])['status'], 'completed')
        self.assertEqual(self.query('SELECT COUNT(*) FROM conversations')[0][0], 89)
        self.assertEqual(self.query('SELECT version FROM schema_version')[0][0], 6)
        self.assertEqual(self.query('SELECT version FROM udb.schema_version')[0][0], 4)

    def test_a_chat_deleted_between_batches_is_not_resurrected(self):
        self.new_export(45)
        real_batch = jobs.Job.batch
        tombstoned = False
        def between_batches(job, conn, done):
            nonlocal tombstoned
            real_batch(job, conn, done)
            if done == 20 and not tombstoned:
                tombstoned = True
                other = server.open_db(self.db)
                other.execute("INSERT INTO udb.deleted_records (conversation_id, provider) VALUES ('new-30', 'chatgpt')")
                other.commit()
                other.close()
        with app_server(self.root, self.db) as request, mock.patch.object(jobs.Job, 'batch', between_batches):
            _, body = request('POST', '/api/import-new', {})
            result = wait_job(self.db, body['job']['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(tombstoned)
        self.assertEqual(self.query("SELECT COUNT(*) FROM conversations WHERE id='new-30'")[0][0], 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM conversations WHERE id LIKE 'new-%'")[0][0], 44)

    def test_first_build_exposes_current_empty_schema_before_scanning(self):
        fresh = self.root / 'fresh'
        fresh.mkdir()
        db = fresh / 'history.db'
        write_export(fresh / 'conversations.json', [conversation('first', 'First', ['gpt-4o'])])
        entered, release = threading.Event(), threading.Event()
        real = build_db.scan_export
        def scan(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return real(*args, **kwargs)
        with mock.patch.object(build_db, 'scan_export', scan):
            receipt = jobs.start(db, 'rebuild', lambda task: server.rebuild_history(db, fresh, job=task))
            try:
                self.assertTrue(entered.wait(3))
                server.prepare_startup(db, fresh)
                conn = server.open_db(db)
                self.assertEqual(server.schema_changes.get_version(conn), 6)
                self.assertEqual(server.schema_changes.get_version(conn, 'udb'), 4)
                conn.close()
                jobs.cancel(db, receipt['id'])
            finally:
                release.set()
            self.assertEqual(wait_job(db, receipt['id'])['status'], 'cancelled')

    def test_bad_seed_does_not_clear_existing_history(self):
        (self.root / 'conversations.json').write_text('[{"mapping": {}, "id":"x"},')
        with app_server(self.root, self.db) as request:
            _, body = request('POST', '/api/rebuild', {})
            self.assertEqual(wait_job(self.db, body['job']['id'])['status'], 'failed')
        self.assertEqual(self.query('SELECT id FROM conversations'), [('seed',)])


class BrowserJobTests(unittest.TestCase):
    def test_progress_cancellation_reload_failure_and_confirmation(self):
        result = subprocess.run(['node', 'tests/import_jobs_ui.cjs'], cwd=Path(server.__file__).parent,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
