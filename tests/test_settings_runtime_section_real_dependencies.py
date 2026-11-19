"""The runtime section's save against the real dependencies, not doubles: a SQLite write lock
another connection holds, a sync that runs while the admin saves, two global rows of one key,
and two saves that add the scheduled check at once. A save that cannot write says so and
writes nothing; two saves at once start one scheduler with one check job.

The apps, clients and helpers are the section tests' own (every test on its own temporary
database, the scheduler never started).
"""
import sqlite3
import threading
import time as time_module

import pytest

from models.db import get_db
from services import runtime_page
from services import runtime_settings as rs
from services import sync_schedule as ss
from test_settings_runtime_section import (  # noqa: F401 - fixtures used by name
    NOW, FakeScheduler, admin, flashes, form, fresh_log_memory, jobs, no_scheduler_start, post,
    rapp, rows, store)


def test_a_save_while_another_connection_holds_the_write_lock_says_not_saved(rapp, admin,
                                                                            jobs):
    store(rapp, sync_interval_hours='6', session_lifetime_days='9')
    before = rows(rapp)
    holder = sqlite3.connect(rapp.config['DATABASE_PATH'], timeout=0)
    try:
        holder.execute('BEGIN IMMEDIATE')
        page = post(admin, form(**{rs.INTERVAL: '12', rs.LIFETIME: '',
                                   rs.LIFETIME + '_default': 'on'}))
    finally:
        holder.rollback()
        holder.close()
    assert flashes(page) == [runtime_page.NOT_SAVED + runtime_page.WRITE_FAILED + '.']
    assert 'locked' not in page.get_data(as_text=True)
    assert rows(rapp) == before and jobs == []
    again = post(admin, form(**{rs.INTERVAL: '12', rs.LIFETIME: '',
                                rs.LIFETIME + '_default': 'on'}))
    assert runtime_page.SAVED in flashes(again)
    assert dict(rows(rapp)) == {rs.AUTO_SYNC: 'true', rs.INTERVAL: '12'}


def test_a_save_while_a_sync_runs_is_saved_and_the_check_reads_it_next(rapp, admin, jobs):
    store(rapp, sync_interval_hours='6')
    assert ss._lock.acquire(blocking=False)
    try:
        page = post(admin, form(**{rs.INTERVAL: '0'}))
        runs = []
        assert ss.scheduled_check(rapp, runs.append, now=NOW) is False   # the sync runs
    finally:
        ss._lock.release()
    assert runtime_page.SAVED in flashes(page) and dict(rows(rapp))[rs.INTERVAL] == '0'
    assert ss.scheduled_check(rapp, runs.append, now=NOW) is False and runs == []


@pytest.mark.parametrize('sent,left', [
    ({rs.INTERVAL: '12'}, [(rs.INTERVAL, '12'), (rs.INTERVAL, '12')]),
    ({rs.INTERVAL: '', rs.INTERVAL + '_default': 'on'}, []),
], ids=['a value updates both rows', 'use the default removes both rows'])
def test_two_global_rows_of_one_key_are_both_saved_or_both_removed(rapp, admin, jobs, sent,
                                                                   left):
    with rapp.app_context():          # the state two first saves at once leave
        db = get_db()
        for value in ('0', '6'):
            db.execute('INSERT INTO settings (user_id, key, value) VALUES (NULL, ?, ?)',
                       (rs.INTERVAL, value))
        db.commit()
    assert runtime_page.SAVED in flashes(post(admin, form(**sent)))
    assert [r for r in rows(rapp) if r[0] == rs.INTERVAL] == left


def test_two_saves_at_once_start_one_scheduler_with_one_check_job(rapp):
    made, outcomes = [], []

    def slow_scheduler():
        time_module.sleep(0.3)        # the window two saves meet in
        made.append(FakeScheduler())
        return made[-1]

    def save():
        outcomes.append(ss.ensure_check_job(rapp, run=print, now=NOW,
                                            make_scheduler=slow_scheduler))

    threads = [threading.Thread(target=save) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    try:
        assert len(made) == 1 and sorted(outcomes) == ['added', 'present']
        assert rapp.scheduler is made[0] and list(made[0].jobs) == [ss.CHECK_JOB]
    finally:
        if hasattr(rapp, 'scheduler'):
            del rapp.scheduler
