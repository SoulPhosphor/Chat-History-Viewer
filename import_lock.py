"""Compatibility names for session 8 callers; background_jobs owns the lock.

New work uses background_jobs.start/run_sync, which hold this same OS lock
through the backup and job. This module has no separate import lock.
"""
from background_jobs import hold, JobBusy as ImportBusy, LOCK_FILE_NAME
