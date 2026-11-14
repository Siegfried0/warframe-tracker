"""Backups of the SQLite database to the configured targets, kept by each target's retention.

Targets come from the environment: the local folder ``BACKUP_LOCAL_PATH`` and the S3 bucket
``BACKUP_S3_BUCKET`` (under ``BACKUP_S3_PREFIX``, with ``BACKUP_S3_ACCESS_KEY`` and
``BACKUP_S3_SECRET_KEY``). Each target's retention is read from the global settings
(``services/backup_settings.py``); which backups it keeps and whether one is due is
``services/backup_retention.py``; the files and objects are ``services/backup_targets.py``.

Per target: read its retention once; (unless forced) list its backups and back up only when
due; write the backup; then list again and delete what its retention does not keep, never the
backup just written. Retention runs only after that target's backup was written, never on its
own. One backup at a time in a process: one lock covers every target's backup and retention,
and a backup asked for while it is held makes none. The scheduler checks every 15 minutes,
first 2 minutes after the start. In MSSQL mode no file backup is made.

Every function that needs the time takes ``now`` (an aware datetime), so the name, the due
check and retention see the same moment.
"""
import logging
import threading
from datetime import datetime, timezone

from services import backup_targets as targets
from services.backup_retention import backup_name, plan
from services.backup_settings import read_retention
from services.sync_schedule import CHECK_PERIOD, START_DELAY

log = logging.getLogger(__name__)

RUNNING_REASON = 'a backup is already running'
MSSQL_REASON = 'the database runs on MSSQL: its own backups apply, no file backup is made'
NONE_CONFIGURED = 'No backup targets configured'
NOT_DUE = 'no backup is due'
NO_APSCHEDULER = 'scheduled backups are not running: APScheduler is not installed'

_lock = threading.Lock()
_reported = {}    # target -> the failure the scheduled check last logged for it


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def configured_targets(config) -> list:
    """The targets Back up now reports on: a local path that is not blank (a relative one too,
    which reports its refusal) and a bucket that is not blank (without both keys it reports
    that they are missing)."""
    found = []
    if targets.local_folder(config) is not None:
        found.append('local')
    if targets.s3_bucket(config) is not None:
        found.append('s3')
    return found


def scheduled_targets(config) -> list:
    """The targets the scheduler backs up: as ``configured_targets``, but a bucket counts only
    with both keys."""
    return [target for target in configured_targets(config)
            if target != 's3' or not targets.s3_keys_missing(config)]


def _is_mssql(config) -> bool:
    return config.get('DB_BACKEND') == 'mssql'


def is_running() -> bool:
    return _lock.locked()


class _Local:
    def __init__(self, config):
        self.folder = targets.check_local_folder(targets.local_folder(config))

    def list(self):
        return targets.list_local(self.folder)

    def write(self, database, now):
        return targets.write_local(self.folder, database, now)

    def delete(self, names):
        return targets.delete_local(self.folder, names)


class _S3:
    def __init__(self, config):
        reason = targets.s3_keys_missing(config)
        if reason:
            raise ValueError(reason)     # no client, no call
        self.bucket, self.prefix = targets.s3_bucket(config), targets.s3_prefix(config)
        try:
            self.client = targets._s3_client(config)
        except ImportError:
            raise RuntimeError('boto3 is not installed (pip install boto3)') from None

    def list(self):
        return targets.list_s3(self.client, self.bucket, self.prefix)

    def write(self, database, now):
        return targets.write_s3(self.client, self.bucket, self.prefix, database, now)

    def delete(self, names):
        return targets.delete_s3(self.client, self.bucket, self.prefix, names)


_KINDS = {'local': _Local, 's3': _S3}


def _failed(error, **extra):
    return {'ok': False, 'error': error, **extra}


def backup_target(app, target, force, now) -> dict:
    """Back up one target (unless not due) and apply its retention. The result says what was
    written or skipped and why, and how many old backups were removed."""
    try:
        retention = read_retention(target)   # read once: it decides the write and retention
    except Exception as exc:  # noqa: BLE001 (a failed read skips the target, never defaults)
        return _failed(f'the retention settings could not be read: {exc}')
    try:
        store = _KINDS[target](app.config)
    except Exception as exc:  # noqa: BLE001 (not usable: no backup, nothing deleted)
        return _failed(str(exc))

    if not force:
        try:
            due = plan(store.list(), retention, now).due
        except Exception as exc:  # noqa: BLE001 (no listing: no backup, nothing deleted)
            return _failed(f'the backups could not be listed: {exc}', skipped=True)
        if not due:
            return {'ok': True, 'skipped': True, 'reason': NOT_DUE}

    try:
        written = store.write(app.config['DATABASE_PATH'], now)
    except targets.Skipped as skip:
        log.info('Backup of %s skipped: %s', target, skip)
        return {'ok': True, 'skipped': True, 'reason': str(skip)}
    except Exception as exc:  # noqa: BLE001 (a failed backup: retention does not run)
        return _failed(f'the backup failed: {exc}')

    result = {'ok': True, 'written': written, 'removed': 0, 'not_removed': 0}
    try:
        names = store.list()
    except Exception as exc:  # noqa: BLE001
        log.warning('Retention of %s skipped: the backups could not be listed: %s', target, exc)
        result['retention_error'] = f'the backups could not be listed: {exc}'
        return result
    # The backup just written is always kept, even when a name ahead of the clock outranks it.
    doomed = [name for name in plan(names, retention, now).delete if name != backup_name(now)]
    result['removed'], result['not_removed'] = store.delete(doomed)
    return result


def _log_result(target, result, scheduled, now):
    """Log a failed target. The scheduled check logs a failure once per target and reason, and
    again when the reason changes or after the target worked; Back up now logs every one.
    The reason is the error without this check's backup name: an OS error names the file it
    was writing, whose name carries the time, so the same failure would read new every check."""
    error = result.get('error') if result.get('ok') is False else None
    if error is None:
        _reported.pop(target, None)
        return
    if scheduled:
        reason = error.replace(backup_name(now), '<backup>')
        if _reported.get(target) == reason:
            return
        _reported[target] = reason
    log.warning('Backup of %s: %s', target, error)


def run_backups(app, force: bool, now=None, scheduled: bool = False) -> dict:
    """Back up every configured target; one result per target."""
    found = (scheduled_targets if scheduled else configured_targets)(app.config)
    if not found:
        return {'ok': False, 'error': NONE_CONFIGURED}
    if _is_mssql(app.config):
        return {target: {'ok': True, 'skipped': True, 'reason': MSSQL_REASON}
                for target in found}
    if not _lock.acquire(blocking=False):
        log.info('A backup was asked for while one runs: none made')
        return {target: {'ok': False, 'skipped': True, 'running': True,
                         'reason': RUNNING_REASON} for target in found}
    try:
        moment = now or _utc_now()
        results = {}
        for target in found:   # the due check lists only now, with the lock held
            results[target] = backup_target(app, target, force, moment)
            _log_result(target, results[target], scheduled, moment)
        return results
    finally:
        _lock.release()


def backup_now(app, force: bool = True, now=None) -> dict:
    """Back up now (the Settings page's button): bypasses the due check unless ``force`` is
    False, then applies each target's retention."""
    return run_backups(app, force, now)


def scheduled_check(app, now=None) -> dict:
    """The repeating check: back up each target that is due."""
    with app.app_context():
        return run_backups(app, force=False, now=now, scheduled=True)


def wants_scheduler(config) -> bool:
    """A target the scheduler would back up is configured and the database is SQLite."""
    return bool(scheduled_targets(config)) and not _is_mssql(config)


def add_jobs(scheduler, app, now=None) -> int:
    """Add the job ``backup-check`` when ``wants_scheduler``: every 15 minutes, first 2
    minutes after ``now``. Returns how many jobs were added."""
    if not wants_scheduler(app.config):
        return 0
    start = (now or _utc_now()) + START_DELAY
    scheduler.add_job(scheduled_check, 'interval', args=[app], id='backup-check',
                      seconds=int(CHECK_PERIOD.total_seconds()), start_date=start,
                      coalesce=True, max_instances=1)
    return 1
