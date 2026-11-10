"""The record of the last sync, one sync at a time, and when a sync is due or must be confirmed.

Every sync, from the page or the scheduler, runs through ``run_recorded``: it holds a
process-wide lock (a second sync asked for meanwhile starts nothing), writes when the sync
started before its first fetch, and when it ended and whether it succeeded when it ends. The
three values are app-wide rows in the ``settings`` table (``user_id`` NULL), in UTC as
``YYYY-MM-DDTHH:MM:SSZ``; they hold no user's data and are never exported.

The scheduler does not count from the app's start: one check, first 2 minutes after the start
and then every 15 minutes, syncs when no finished sync is recorded or ``SYNC_INTERVAL_HOURS``
have passed since the last one finished. So a restart never starts the clock again, and a
manual sync also resets the schedule.

A stored finish time that cannot be read (empty, NULL, another format), a result other than
``ok`` / ``failed``, or a finish more than 5 minutes in the future (the clock was set back)
counts as no finished sync: a sync is due and the page asks nothing. This fails toward one
extra sync, which rewrites valid values, never toward a schedule that stops. A finish up to
5 minutes ahead counts as finished just now, so instances whose clocks differ a little do not
sync again right after each other.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

from services.auth_service import get_global_setting, upsert_global_setting
from services.resurgence_sync import format_utc, parse_utc

log = logging.getLogger(__name__)

KEY_STARTED = 'sync_started_at'     # when the last sync started
KEY_FINISHED = 'sync_finished_at'   # when the last sync ended, succeeded or failed
KEY_RESULT = 'sync_result'          # 'ok' or 'failed'
RESULTS = ('ok', 'failed')

CONFIRM_WINDOW = timedelta(minutes=60)   # Run Sync Now asks within this time after a success
CLOCK_ALLOWANCE = timedelta(minutes=5)   # a finish this far ahead counts as finished just now
START_DELAY = timedelta(minutes=2)       # the first check (and a start-up sync) after the start
CHECK_PERIOD = timedelta(minutes=15)     # how often the scheduler checks whether a sync is due

RUNNING_ERROR = 'A sync is already running.'

_lock = threading.Lock()
_ABSENT = object()   # no row for the key, as opposed to a row holding NULL


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── The record ───────────────────────────────────────────────────────────────

def _write(values: dict, discard_pending: bool = False) -> None:
    """Write the given keys in one commit. A failure is logged and swallowed: the record never
    changes a sync's own result. ``discard_pending`` rolls back what a failed sync left
    uncommitted first, so the record's commit never keeps a failed sync's partial writes."""
    from models.db import get_db
    try:
        db = get_db()
        if discard_pending:
            db.rollback()
        for key, value in values.items():
            upsert_global_setting(db, key, value)
        db.commit()
    except Exception as exc:  # noqa: BLE001 (any storage error is logged, never raised)
        log.exception('Could not record the sync (%s): %s', ', '.join(values), exc)
        try:
            get_db().rollback()
        except Exception:  # noqa: BLE001
            pass


def read_record() -> dict:
    """The stored values; a key with no row maps to ``_ABSENT``, a NULL value to None."""
    return {key: get_global_setting(key, _ABSENT)
            for key in (KEY_STARTED, KEY_FINISHED, KEY_RESULT)}


def is_running() -> bool:
    return _lock.locked()


def run_recorded(steps, now=None):
    """Run ``steps()`` (the sync itself, returning a dict with ``ok``) under the lock, with
    its record. A sync asked for while one runs starts nothing and answers ``running``."""
    clock = now or _utc_now
    if not _lock.acquire(blocking=False):
        log.info('A sync was asked for while one runs: not started')
        return {'ok': False, 'running': True, 'error': RUNNING_ERROR}
    try:
        _write({KEY_STARTED: format_utc(clock())})
        try:
            result = steps()
        except Exception as exc:  # noqa: BLE001 (a step that escapes still ends as a failure)
            log.exception('Sync failed: %s', exc)
            result = {'ok': False, 'error': str(exc)}
        succeeded = isinstance(result, dict) and result.get('ok') is True
        _write({KEY_FINISHED: format_utc(clock()), KEY_RESULT: 'ok' if succeeded else 'failed'},
               discard_pending=not succeeded)
        return result
    finally:
        _lock.release()


# ── What the record means ────────────────────────────────────────────────────

def finished_at(record: dict, now: datetime):
    """The finish time that counts, or None for no finished sync. A finish up to the clock
    allowance ahead counts as ``now``; further ahead, or unreadable, as none."""
    if record.get(KEY_RESULT) not in RESULTS:
        return None
    finish = parse_utc(record.get(KEY_FINISHED))
    if finish is None or finish > now + CLOCK_ALLOWANCE:
        return None
    return min(finish, now)


def is_due(record: dict, now: datetime, interval_hours) -> bool:
    finish = finished_at(record, now)
    return finish is None or now >= finish + timedelta(hours=interval_hours)


def needs_confirmation(record: dict, now: datetime) -> bool:
    finish = finished_at(record, now)
    return finish is not None and record.get(KEY_RESULT) == 'ok' and now - finish < CONFIRM_WINDOW


def last_sync(record: dict, now: datetime, running: bool) -> dict:
    """What the sync page shows: ``state`` is ``ok``, ``failed``, ``unknown`` (a record that
    cannot be read) or ``never``; ``unfinished_started_at`` names a sync that started after
    the stored finish (or with none readable) and is not running in this process."""
    finish = finished_at(record, now)
    if finish is not None:
        state, finished = record[KEY_RESULT], format_utc(finish)
    elif record.get(KEY_FINISHED, _ABSENT) is _ABSENT and \
            record.get(KEY_RESULT, _ABSENT) is _ABSENT:
        state, finished = 'never', None
    else:
        state, finished = 'unknown', None
    # The start is compared with the stored finish, as both were written: a sync that started
    # and finished ahead of this clock, or whose result cannot be read, still ended.
    started = parse_utc(record.get(KEY_STARTED))
    stored_finish = parse_utc(record.get(KEY_FINISHED))
    unfinished = None
    if started is not None and not running and             (stored_finish is None or started > stored_finish):
        unfinished = format_utc(started)
    return {'state': state, 'finished_at': finished, 'running': running,
            'unfinished_started_at': unfinished, 'now': format_utc(now)}


def current_status(now=None) -> dict:
    """The page's last-sync line, from the stored record and this process's lock."""
    return last_sync(read_record(), now or _utc_now(), is_running())


# ── Run Sync Now ─────────────────────────────────────────────────────────────

def run_from_page(app, body, run, now=None):
    """Answer a Run Sync Now request: (HTTP status, JSON answer). Only a JSON object whose
    ``confirm`` is JSON ``true`` skips the question within the hour after a success."""
    confirmed = isinstance(body, dict) and body.get('confirm') is True
    if is_running():
        return 409, {'ok': False, 'running': True, 'error': RUNNING_ERROR,
                     'last_sync': current_status(now)}
    if not confirmed:
        moment = now or _utc_now()
        record = read_record()
        if needs_confirmation(record, moment):
            finish = finished_at(record, moment)
            return 200, {'ok': False, 'needs_confirmation': True,
                         'finished_at': format_utc(finish),
                         'minutes_ago': int((moment - finish).total_seconds() // 60),
                         'last_sync': last_sync(record, moment, False)}
    result = run(app)
    status = 409 if isinstance(result, dict) and result.get('running') else 200
    return status, {**result, 'last_sync': current_status()}


# ── The scheduler ────────────────────────────────────────────────────────────

def scheduled_check(app, run, now=None) -> bool:
    """The repeating check: sync when due. A sync already running is skipped (the next
    check tries again). Returns whether a sync was started."""
    with app.app_context():
        moment = now or _utc_now()
        if is_running():
            log.info('Scheduled sync check: a sync is running, skipped')
            return False
        if not is_due(read_record(), moment, app.config.get('SYNC_INTERVAL_HOURS', 0)):
            return False
    log.info('Scheduled sync is due: starting it')
    run(app)
    return True


def add_jobs(scheduler, app, run, now=None) -> int:
    """Add the sync jobs the configuration asks for; returns how many. With
    ``SYNC_INTERVAL_HOURS`` above 0 one check every 15 minutes, its first 2 minutes after
    ``now``; with ``AUTO_SYNC_ON_STARTUP`` one sync 2 minutes after ``now``."""
    start = (now or _utc_now()) + START_DELAY
    added = 0
    if app.config.get('AUTO_SYNC_ON_STARTUP'):
        scheduler.add_job(run, 'date', run_date=start, args=[app], id='sync-on-startup')
        added += 1
    interval = app.config.get('SYNC_INTERVAL_HOURS') or 0
    if interval > 0:
        scheduler.add_job(scheduled_check, 'interval', args=[app, run], id='sync-check',
                          seconds=int(CHECK_PERIOD.total_seconds()), start_date=start,
                          coalesce=True, max_instances=1)
        added += 1
    return added
