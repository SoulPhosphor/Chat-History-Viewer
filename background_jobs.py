"""One general background-job runner, shared by all copies of the app (#33).

Only one job is admitted; a second is refused immediately. The OS lock is
held from admission through backup, work and final status. JSON receipts and
cancel requests live beside the databases, outside either SQLite writer lock.
They contain progress/results only, never export records or prompts. A future
AI runner uses start(db, kind, callback) and the same Job.check/progress API.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from api_common import ApiError
import errorlog

LOCK_FILE_NAME = ".import.lock"  # keep the session-8 OS lock during upgrades
STATE_DIR = ".background-jobs"
BATCH_SIZE = 20
BATCH_SECONDS = 0.25
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class JobBusy(ApiError):
    def __init__(self):
        super().__init__("Another background job is already running. Wait for it to finish, then try again.", 409)


class Cancelled(Exception):
    pass


def _folder(db):
    return Path(db).resolve().parent / STATE_DIR


def _write(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _lock_file(handle):
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


@contextmanager
def hold(db):
    """Also used for startup maintenance; never waits on another process."""
    path = Path(db).resolve().parent / LOCK_FILE_NAME
    with _LOCKS_GUARD:
        mutex = _LOCKS.setdefault(str(path), threading.Lock())
    if not mutex.acquire(blocking=False):
        raise JobBusy()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a+b') as handle:
            if not _lock_file(handle):
                raise JobBusy()
            yield
    finally:
        mutex.release()


def running(db):
    try:
        with hold(db):
            return False
    except JobBusy:
        return True


def get(db, job_id=None):
    folder = _folder(db)
    if job_id is None:
        try:
            job_id = json.loads((folder / 'latest').read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
    try:
        job_id = str(uuid.UUID(job_id))
    except (ValueError, TypeError, AttributeError):
        raise ApiError('That background job could not be found.', 404)
    receipt_path = folder / (job_id + '.json')
    try:
        state = json.loads(receipt_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ApiError('That background job could not be found.', 404)
    # An abrupt process exit frees the OS lock. Never leave a receipt claiming
    # to run forever, and never automatically replay a partially saved job.
    if state['status'] == 'running' and not running(db):
        # Completion saves its terminal receipt before releasing the lock. Our
        # first read may precede that save, so use the now-current receipt.
        state = json.loads(receipt_path.read_text(encoding='utf-8'))
        if state['status'] == 'running':
            state = dict(state, status='interrupted', message=(
                'The app stopped before this job finished. Chats already saved are kept. '
                'You can run it again to finish.'), finished_at=time.time())
        # Read-only: a new admission may be racing us; don't overwrite its state.
    return state


def cancel(db, job_id):
    state = get(db, job_id)
    if state['status'] == 'running':
        (_folder(db) / (state['id'] + '.cancel')).touch()
    return state


class Job:
    def __init__(self, db, kind):
        self.db = Path(db)
        self.folder = _folder(db)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.id = str(uuid.uuid4())
        self.state = dict(id=self.id, kind=kind, status='running', progress=0,
                          total=None, message='Starting…', result=None,
                          started_at=time.time(), finished_at=None)
        self.result = None
        self._last_save = 0
        previous = get(db)
        if previous and previous['status'] == 'running':
            previous.update(status='interrupted', finished_at=time.time(), message=(
                'The app stopped before this job finished. Chats already saved are kept. '
                'You can run it again to finish.'))
            _write(self.folder / (previous['id'] + '.json'), previous)
        self.save()
        _write(self.folder / 'latest', self.id)

    def save(self):
        _write(self.folder / (self.id + '.json'), self.state)
        self._last_save = time.monotonic()

    def check(self):
        if (self.folder / (self.id + '.cancel')).exists():
            raise Cancelled()

    def progress(self, message=None, done=None, total=None, *, force=False):
        self.check()
        if message is not None:
            self.state['message'] = message
        if done is not None:
            self.state['progress'] = done
            self.state['total'] = total
        if force or time.monotonic() - self._last_save >= BATCH_SECONDS:
            self.save()

    def batch(self, conn, done):
        """Commit only between complete records, by count or elapsed time."""
        if done % BATCH_SIZE == 0 or time.monotonic() - self._last_save >= BATCH_SECONDS:
            conn.commit()
            self.save()
        self.check()


def _execute(job, callback, *, propagate=False):
    try:
        job.check()
        result = callback(job)
        job.state.update(status='completed', result=result, message='Finished.')
        return result
    except Cancelled:
        job.state.update(status='cancelled', result=job.result, message=(
            'Cancelled. Chats already saved are kept. You can run it again to finish.'))
        if propagate:
            raise
    except Exception as exc:
        errorlog.record_exception(exc, action='Background job', ids={'job': job.id, 'kind': job.state['kind']})
        message = exc.message if isinstance(exc, ApiError) else (
            'This job could not finish. The details are in the error log.')
        job.state.update(status='failed', error=message, error_status=getattr(exc, 'status', 500), result=job.result, message=(
            message + ' Chats already saved are kept. You can run it again to finish.'))
        if propagate:
            raise
    finally:
        job.state['finished_at'] = time.time()
        job.save()
        (job.folder / (job.id + '.cancel')).unlink(missing_ok=True)


def start(db, kind, callback):
    """Admit before returning 202. Work owns its connections and backup."""
    lock = hold(db)
    lock.__enter__()
    try:
        job = Job(db, kind)
        receipt = dict(job.state)
        def run():
            try:
                _execute(job, callback)
            finally:
                lock.__exit__(None, None, None)
        # A normal thread lets complete writes finish on a normal server exit.
        threading.Thread(target=run, name='job-' + job.id, daemon=False).start()
        return receipt
    except BaseException:
        lock.__exit__(None, None, None)
        raise


def run_sync(db, kind, callback):
    """CLI/first build uses exactly the same lock, receipts and cancellation."""
    with hold(db):
        job = Job(db, kind)
        return _execute(job, callback, propagate=True)
