"""The record of the last sync, one sync at a time, the due rule, the confirmation window and
the scheduler's jobs. Time is injected; nothing waits for the real delay and nothing reaches
the network (the sync's steps are replaced)."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from models.db import get_db
from services import sync_schedule as ss
from services import wiki_sync

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
ABSENT = ss._ABSENT


def stamp(delta):
    return (NOW + delta).strftime('%Y-%m-%dT%H:%M:%SZ')


def rec(finished=ABSENT, result=ABSENT, started=ABSENT):
    return {ss.KEY_STARTED: started, ss.KEY_FINISHED: finished, ss.KEY_RESULT: result}


H, M, S = timedelta(hours=1), timedelta(minutes=1), timedelta(seconds=1)


# ── The due rule (interval 24 h) ─────────────────────────────────────────────

DUE = [
    ('no record',                       rec(),                                       True),
    ('finished 1 s before the interval', rec(stamp(-24 * H + S), 'ok'),              False),
    ('finished exactly the interval ago', rec(stamp(-24 * H), 'ok'),                 True),
    ('finished 1 s after the interval', rec(stamp(-24 * H - S), 'ok'),               True),
    ('finished 1 h ago',                rec(stamp(-H), 'ok'),                         False),
    ('failed 1 h ago',                  rec(stamp(-H), 'failed'),                     False),
    ('failed 25 h ago',                 rec(stamp(-25 * H), 'failed'),                True),
    ('start without finish',            rec(started=stamp(-H)),                      True),
    ('start after the last finish',     rec(stamp(-2 * H), 'ok', stamp(-H)),          False),
    ('finish not in the format',        rec('2026-11-10 11:00:00', 'ok'),             True),
    ('finish garbage',                  rec('<script>', 'ok'),                       True),
    ('finish empty',                    rec('', 'ok'),                                True),
    ('finish NULL',                     rec(None, 'ok'),                              True),
    ('result other than ok/failed',     rec(stamp(-H), 'OK'),                         True),
    ('result empty',                    rec(stamp(-H), ''),                           True),
    ('result NULL',                     rec(stamp(-H), None),                         True),
    ('result missing',                  rec(stamp(-H)),                               True),
    ('finish 1 s ahead',                rec(stamp(S), 'ok'),                          False),
    ('finish 5 min ahead',              rec(stamp(5 * M), 'ok'),                      False),
    ('finish 5 min + 1 s ahead',        rec(stamp(5 * M + S), 'ok'),                  True),
    ('finish a year ahead',             rec(stamp(365 * 24 * H), 'ok'),               True),
]


@pytest.mark.parametrize('label,record,due', DUE, ids=[row[0] for row in DUE])
def test_a_sync_is_due_by_the_stored_finish_and_the_interval(label, record, due):
    assert ss.is_due(record, NOW, 24) is due


def test_a_finish_slightly_ahead_counts_as_finished_now():
    assert ss.finished_at(rec(stamp(4 * M), 'ok'), NOW) == NOW
    assert ss.finished_at(rec(stamp(4 * M), 'failed'), NOW) == NOW


# ── The confirmation window ──────────────────────────────────────────────────

CONFIRM = [
    ('ok 1 min ago',                rec(stamp(-M), 'ok'),                 True),
    ('ok 59 min 59 s ago',          rec(stamp(-60 * M + S), 'ok'),        True),
    ('ok exactly 60 min ago',       rec(stamp(-60 * M), 'ok'),            False),
    ('ok 61 min ago',               rec(stamp(-61 * M), 'ok'),            False),
    ('failed 1 min ago',            rec(stamp(-M), 'failed'),             False),
    ('no record',                   rec(),                                False),
    ('start without finish',        rec(started=stamp(-M)),               False),
    ('finish unreadable',           rec('yesterday', 'ok'),               False),
    ('result unreadable',           rec(stamp(-M), 'okay'),               False),
    ('ok 1 s ahead',                rec(stamp(S), 'ok'),                  True),
    ('ok 5 min ahead',              rec(stamp(5 * M), 'ok'),              True),
    ('ok 5 min + 1 s ahead',        rec(stamp(5 * M + S), 'ok'),          False),
]


@pytest.mark.parametrize('label,record,asks', CONFIRM, ids=[row[0] for row in CONFIRM])
def test_run_sync_now_asks_only_within_the_hour_after_a_success(label, record, asks):
    assert ss.needs_confirmation(record, NOW) is asks


# ── The page's last-sync line ────────────────────────────────────────────────

LINE = [
    ('never',            rec(),                                   'never', None, None),
    ('ok',               rec(stamp(-H), 'ok'),                     'ok', stamp(-H), None),
    ('failed',           rec(stamp(-H), 'failed'),                 'failed', stamp(-H), None),
    ('unknown result',   rec(stamp(-H), 'maybe'),                  'unknown', None, None),
    ('unknown finish',   rec('', 'ok'),                            'unknown', None, None),
    ('NULL finish',      rec(None, ABSENT),                        'unknown', None, None),
    ('far future',       rec(stamp(H), 'ok'),                      'unknown', None, None),
    ('just ahead',       rec(stamp(M), 'ok'),                      'ok', stamp(timedelta()), None),
    ('unfinished, none', rec(started=stamp(-M)),                  'never', None, stamp(-M)),
    ('unfinished after', rec(stamp(-H), 'ok', stamp(-M)),          'ok', stamp(-H), stamp(-M)),
    ('start before end', rec(stamp(-M), 'ok', stamp(-H)),          'ok', stamp(-M), None),
    ('start garbage',    rec(stamp(-M), 'ok', 'x'),                'ok', stamp(-M), None),
    # A sync that started and finished a little ahead of this clock (within the allowance)
    # did finish: its stored start is before its stored finish, so nothing is unfinished.
    ('start and finish just ahead', rec(stamp(4 * M), 'ok', stamp(2 * M)),
     'ok', stamp(timedelta()), None),
    ('start just ahead, finish just ahead, failed', rec(stamp(5 * M), 'failed', stamp(S)),
     'failed', stamp(timedelta()), None),
    # An unreadable result with a readable finish after the start: the page says "unknown",
    # never that a sync which ended "did not finish".
    ('unknown result, start before finish', rec(stamp(-M), 'maybe', stamp(-H)),
     'unknown', None, None),
]


@pytest.mark.parametrize('label,record,state,finished,unfinished', LINE,
                         ids=[row[0] for row in LINE])
def test_the_last_sync_line_names_each_state(label, record, state, finished, unfinished):
    line = ss.last_sync(record, NOW, running=False)
    assert (line['state'], line['finished_at'], line['unfinished_started_at']) == \
        (state, finished, unfinished)


def test_a_running_sync_is_not_shown_as_unfinished():
    line = ss.last_sync(rec(stamp(-H), 'ok', stamp(-M)), NOW, running=True)
    assert line['running'] is True and line['unfinished_started_at'] is None


# ── The record each sync writes ──────────────────────────────────────────────

@pytest.fixture
def db(app):
    with app.app_context():
        yield get_db()


def stored(db):
    return {r['key']: r['value'] for r in db.execute(
        'SELECT key, value FROM settings WHERE user_id IS NULL AND key LIKE ?', ('sync_%',))}


def rows(db):
    return db.execute(
        'SELECT COUNT(*) FROM settings WHERE user_id IS NULL AND key LIKE ?', ('sync_%',)
    ).fetchone()[0]


class Clock:
    def __init__(self, *moments):
        self.moments = list(moments)

    def __call__(self):
        return self.moments.pop(0)


def test_a_successful_sync_records_its_start_finish_and_ok(db):
    seen = {}

    def steps():
        seen.update(stored(get_db()))   # the start is committed before the first step
        return {'ok': True}

    result = ss.run_recorded(steps, now=Clock(NOW, NOW + 3 * M))
    assert result == {'ok': True}
    assert seen == {'sync_started_at': stamp(timedelta())}
    assert stored(db) == {'sync_started_at': stamp(timedelta()),
                          'sync_finished_at': stamp(3 * M), 'sync_result': 'ok'}
    ss.run_recorded(lambda: {'ok': True}, now=Clock(NOW + H, NOW + 2 * H))
    assert rows(db) == 3   # updated in place, never a second row


@pytest.mark.parametrize('steps', [
    lambda: {'ok': False, 'error': 'boom'},
    lambda: 1 / 0,
    lambda: {'ok': 'yes'},
    lambda: None,
], ids=['reports a failure', 'raises', 'ok not true', 'no result'])
def test_a_failed_sync_records_failed_and_keeps_its_result(db, steps):
    result = ss.run_recorded(steps, now=Clock(NOW, NOW + M))
    assert stored(db)['sync_result'] == 'failed'
    assert stored(db)['sync_finished_at'] == stamp(M)
    assert not ss.is_running()
    assert result is None or result.get('ok') is not True


def test_a_failed_sync_does_not_commit_its_partial_writes(db):
    def steps():
        get_db().execute("INSERT INTO settings (user_id, key, value) VALUES (NULL, 'x', 'y')")
        return {'ok': False, 'error': 'half way'}

    ss.run_recorded(steps, now=Clock(NOW, NOW + M))
    assert db.execute("SELECT COUNT(*) FROM settings WHERE key = 'x'").fetchone()[0] == 0


def test_the_full_sync_writes_the_record_when_a_step_fails(app, db, monkeypatch):
    def broken():
        raise RuntimeError('wiki down')
    monkeypatch.setattr(wiki_sync.image_cache, 'repair_stale_items', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_warframes', broken)
    result = wiki_sync.run_sync(app)
    assert result == {'ok': False, 'error': 'wiki down'}
    assert stored(db)['sync_result'] == 'failed'
    assert set(stored(db)) == {'sync_started_at', 'sync_finished_at', 'sync_result'}


@pytest.mark.parametrize('fail_at', ['start', 'finish'])
def test_a_failed_record_write_is_logged_and_the_sync_result_stays(db, monkeypatch, caplog,
                                                                   fail_at):
    real = ss.upsert_global_setting

    def flaky(conn, key, value):
        if (fail_at == 'start') == (key == ss.KEY_STARTED):
            raise RuntimeError('disk full')
        return real(conn, key, value)

    monkeypatch.setattr(ss, 'upsert_global_setting', flaky)
    result = ss.run_recorded(lambda: {'ok': True, 'total': 7}, now=Clock(NOW, NOW + M))
    assert result == {'ok': True, 'total': 7}
    assert 'Could not record the sync' in caplog.text
    assert not ss.is_running()
    expected = ({'sync_finished_at': stamp(M), 'sync_result': 'ok'} if fail_at == 'start'
                else {'sync_started_at': stamp(timedelta())})
    assert stored(db) == expected


def test_a_sync_that_never_finishes_leaves_only_its_start(db):
    """The process ending mid-sync: here a BaseException, which nothing records."""
    class ProcessEnds(BaseException):
        pass

    def steps():
        raise ProcessEnds()

    with pytest.raises(ProcessEnds):
        ss.run_recorded(steps, now=Clock(NOW, NOW + M))
    assert stored(db) == {'sync_started_at': stamp(timedelta())}
    assert not ss.is_running()   # released on every exit path
    assert ss.is_due(ss.read_record(), NOW + M, 24) is True


# ── One sync at a time ───────────────────────────────────────────────────────

def test_a_second_sync_while_one_runs_starts_nothing_and_records_nothing(db):
    calls = []
    inner = {}

    def steps():
        calls.append('outer')
        inner['result'] = ss.run_recorded(lambda: calls.append('inner') or {'ok': True},
                                          now=Clock(NOW + M, NOW + M))
        return {'ok': True}

    ss.run_recorded(steps, now=Clock(NOW, NOW + 2 * M))
    assert calls == ['outer']
    assert inner['result'] == {'ok': False, 'running': True, 'error': ss.RUNNING_ERROR}
    assert stored(db)['sync_started_at'] == stamp(timedelta())
    assert stored(db)['sync_finished_at'] == stamp(2 * M)


def test_two_threads_asking_at_once_run_one_sync(app):
    entered, release = threading.Event(), threading.Event()
    calls, answers = [], []

    def steps():
        calls.append(1)
        entered.set()
        release.wait(5)
        return {'ok': True}

    def worker():
        with app.app_context():
            answers.append(ss.run_recorded(steps))

    first = threading.Thread(target=worker)
    first.start()
    assert entered.wait(5)
    with app.app_context():
        second = ss.run_recorded(steps)
    release.set()
    first.join(5)
    assert calls == [1]
    assert second['running'] is True and answers == [{'ok': True}]
    assert not ss.is_running()


# ── The scheduler ────────────────────────────────────────────────────────────

class FakeApp:
    def __init__(self, real, **config):
        self.config = config
        self._real = real

    def app_context(self):
        return self._real.app_context()


@pytest.mark.parametrize('label,record,started', [
    ('due: never synced', {}, True),
    ('due: finished 24 h ago', {'sync_finished_at': stamp(-24 * H), 'sync_result': 'ok'}, True),
    ('not due: finished 23 h ago',
     {'sync_finished_at': stamp(-23 * H), 'sync_result': 'ok'}, False),
    ('due: unreadable', {'sync_finished_at': 'soon', 'sync_result': 'ok'}, True),
], ids=['never synced', 'finished 24 h ago', 'finished 23 h ago', 'unreadable'])
def test_the_scheduled_check_syncs_only_when_due(app, db, label, record, started):
    for key, value in record.items():
        ss.upsert_global_setting(db, key, value)
    db.commit()
    runs = []
    fake = FakeApp(app, SYNC_INTERVAL_HOURS=24)
    assert ss.scheduled_check(fake, runs.append, now=NOW) is started
    assert runs == ([fake] if started else [])


def test_the_scheduled_check_skips_while_a_sync_runs(app):
    runs = []
    assert ss._lock.acquire(blocking=False)
    try:
        assert ss.scheduled_check(FakeApp(app, SYNC_INTERVAL_HOURS=24), runs.append,
                                  now=NOW) is False
    finally:
        ss._lock.release()
    assert runs == []


def test_a_restart_within_the_interval_starts_no_sync(app, db):
    ss.run_recorded(lambda: {'ok': True}, now=Clock(NOW - H, NOW - H))
    runs = []
    for minutes in (2, 17, 32):   # the checks after a restart
        ss.scheduled_check(FakeApp(app, SYNC_INTERVAL_HOURS=24), runs.append,
                           now=NOW + minutes * M)
    assert runs == []


@pytest.fixture
def scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone='UTC')
    yield sched
    if sched.running:
        sched.shutdown(wait=False)


def jobs(sched):
    out = {}
    for job in sched.get_jobs():
        trigger = job.trigger
        if hasattr(trigger, 'run_date'):
            out[job.id] = ('date', trigger.run_date)
        else:
            out[job.id] = ('interval', trigger.start_date, trigger.interval)
    return out


@pytest.mark.parametrize('auto,hours,expected', [
    (False, 24, {'sync-check'}),
    (True, 24, {'sync-check', 'sync-on-startup'}),
    (True, 0, {'sync-on-startup'}),
    (False, 0, set()),
    (False, -1, set()),
    (False, 1, {'sync-check'}),
], ids=['interval only', 'both', 'start-up only', 'neither', 'negative interval',
        'one hour'])
def test_the_scheduler_jobs_follow_the_configuration(app, scheduler, auto, hours, expected):
    fake = FakeApp(app, AUTO_SYNC_ON_STARTUP=auto, SYNC_INTERVAL_HOURS=hours)
    def run(app):
        return None

    assert ss.add_jobs(scheduler, fake, run, now=NOW) == len(expected)
    found = jobs(scheduler)
    assert set(found) == expected
    first = NOW + 2 * M
    if 'sync-check' in found:
        assert found['sync-check'] == ('interval', first, timedelta(minutes=15))
        job = scheduler.get_job('sync-check')
        assert job.func is ss.scheduled_check and job.args == (fake, run)
        assert job.max_instances == 1 and job.coalesce is True
    if 'sync-on-startup' in found:
        assert found['sync-on-startup'] == ('date', first)
        assert scheduler.get_job('sync-on-startup').args == (fake,)


@pytest.mark.parametrize('auto,hours,started', [
    (False, 0, False), (True, 0, True), (False, 24, True), (False, -3, False),
])
def test_no_scheduler_starts_without_an_interval_or_a_start_up_sync(app, auto, hours,
                                                                    started, monkeypatch):
    import app as app_module
    from apscheduler.schedulers.background import BackgroundScheduler
    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self, *a, **k: None)
    app.config.update(AUTO_SYNC_ON_STARTUP=auto, SYNC_INTERVAL_HOURS=hours)
    if hasattr(app, 'scheduler'):
        del app.scheduler
    app_module._start_scheduler(app)
    assert hasattr(app, 'scheduler') is started
    if started:
        ids = {job.id for job in app.scheduler.get_jobs()}
        assert ids == ({'sync-on-startup'} if auto else {'sync-check'})
        del app.scheduler   # never started: nothing to shut down
