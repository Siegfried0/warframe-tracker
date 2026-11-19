"""The admin's runtime settings: auto-sync on startup, the sync interval and the session lifetime.

Each has three sources: a pin (an environment variable that is set, or a ``create_app``
override that is not None), a stored app-wide settings row, the default. A pin the app cannot
use refuses the start; a stored value that is not valid is read as the default, logged once and
left stored. The scheduler's start, its jobs and every scheduled check read the effective
values. The rows are written straight into the database here.
"""
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask

import app as app_module
import config
from config import SettingError
from models.db import get_db
from services import auth_service
from services import runtime_settings as rs
from services import sync_schedule as ss

APP_DIR = Path(__file__).resolve().parent.parent / 'app'
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
NAMES = ('AUTO_SYNC_ON_STARTUP', 'SYNC_INTERVAL_HOURS', 'SESSION_LIFETIME_DAYS')


@pytest.fixture(autouse=True)
def fresh_log_memory(monkeypatch):
    """Each test starts with no invalid stored value logged yet."""
    monkeypatch.setattr(rs, '_logged', set())


@pytest.fixture
def no_scheduler_start(monkeypatch):
    """The scheduler an unpinned app builds never runs (its jobs are only inspected)."""
    from apscheduler.schedulers.background import BackgroundScheduler
    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self, *a, **k: None)


def make_app(tmp_path, **pins):
    """An app with the three settings unpinned (override None) unless given here."""
    overrides = {'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'rs.db'),
                 'SECRET_KEY': 'test-only', **{name: None for name in NAMES}}
    overrides.update(pins)
    flask_app = app_module.create_app(overrides)
    if hasattr(flask_app, 'scheduler'):
        del flask_app.scheduler      # never started (no_scheduler_start): nothing to stop
    return flask_app


@pytest.fixture
def free_app(tmp_path, no_scheduler_start):
    return make_app(tmp_path)


def store(app, **rows):
    with app.app_context():
        db = get_db()
        for key, value in rows.items():
            auth_service.upsert_global_setting(db, key, value)
        db.commit()


def bare_app(**config_values):
    """A Flask app with only the reader installed: the pins, nothing opened."""
    flask_app = Flask(__name__)
    flask_app.config.update({name: None for name in NAMES})
    flask_app.config.update(config_values)
    rs.init_app(flask_app)
    return flask_app


# ── Axis 1: the pins' spellings ──────────────────────────────────────────────

NUMBER_PINS = [
    # (raw, value for the interval 0-8760 or None = refused)
    ('0', 0), ('24', 24), (' 24 ', 24), ('\t7\n', 7), ('0024', 24), ('8760', 8760),
    ('8761', None), ('', None), (' ', None), ('-1', None), ('+7', None), ('7.0', None),
    ('7.5', None), ('1e3', None), ('1_000', None), ('١٢', None), ('７', None),
    ('99999999999999999999999', None), ('12345', None), ('0x10', None), ('seven', None),
    ('7 7', None), ('\ud800', None), ('7\x00', None),
    (0, 0), (24, 24), (8760, 8760), (8761, None), (-1, None), (True, None), (False, None),
    (2 ** 64, None), (7.0, None),
]


@pytest.mark.parametrize('raw,value', NUMBER_PINS, ids=[repr(r[0]) for r in NUMBER_PINS])
def test_a_pinned_interval_is_used_or_refuses_the_start(raw, value):
    if value is None:
        with pytest.raises(SettingError) as refused:
            bare_app(SYNC_INTERVAL_HOURS=raw)
        message = str(refused.value)
        assert 'SYNC_INTERVAL_HOURS' in message and '0 to 8760' in message
        assert len(message) < 260
    else:
        flask_app = bare_app(SYNC_INTERVAL_HOURS=raw)
        assert rs.pinned(flask_app, rs.INTERVAL)
        assert rs.effective(flask_app, rs.INTERVAL, get=_never_read) == value


@pytest.mark.parametrize('raw,value', [
    ('1', 1), ('365', 365), (' 30 ', 30), ('0', None), ('366', None), ('', None), (1, 1),
    (365, 365), (0, None), (366, None),
], ids=lambda v: repr(v))
def test_a_pinned_session_lifetime_is_used_or_refuses_the_start(raw, value):
    if value is None:
        with pytest.raises(SettingError, match='SESSION_LIFETIME_DAYS'):
            bare_app(SESSION_LIFETIME_DAYS=raw)
    else:
        flask_app = bare_app(SESSION_LIFETIME_DAYS=raw)
        assert rs.session_lifetime(flask_app, get=_never_read) == timedelta(days=value)


SWITCH_PINS = [
    ('true', True), ('false', False), ('TRUE', True), (' False\n', False), ('tRuE', True),
    (True, True), (False, False),
    ('', None), (' ', None), ('on', None), ('1', None), ('0', None), ('yes', None),
    ('truee', None), ('TRUＥ', None), ('ＴＲＵＥ', None), ('İtrue', None), (1, None), (0, None),
    ('\ud800', None),
]


@pytest.mark.parametrize('raw,value', SWITCH_PINS, ids=[repr(r[0]) for r in SWITCH_PINS])
def test_a_pinned_auto_sync_is_used_or_refuses_the_start(raw, value):
    if value is None:
        with pytest.raises(SettingError, match='AUTO_SYNC_ON_STARTUP'):
            bare_app(AUTO_SYNC_ON_STARTUP=raw)
    else:
        flask_app = bare_app(AUTO_SYNC_ON_STARTUP=raw)
        assert rs.effective(flask_app, rs.AUTO_SYNC, get=_never_read) is value


def _never_read(key):
    raise AssertionError(f'a pinned setting was read from the database: {key}')


def test_a_none_override_leaves_the_setting_unpinned():
    flask_app = bare_app()
    assert not any(rs.pinned(flask_app, key) for key in rs.ORDER)


def test_a_refused_pin_stops_the_start_before_the_database_is_opened(tmp_path):
    db = tmp_path / 'never.db'
    with pytest.raises(SettingError):
        app_module.create_app({'TESTING': True, 'DATABASE_PATH': str(db),
                               'SECRET_KEY': 'test-only', 'SYNC_INTERVAL_HOURS': '-1',
                               'AUTO_SYNC_ON_STARTUP': False})
    assert not db.exists()


def test_the_environment_pins_through_the_config(tmp_path, monkeypatch, no_scheduler_start):
    """``Config`` keeps the environment's raw text; an attribute set here is what the
    environment would have given at import."""
    monkeypatch.setattr(config.Config, 'SYNC_INTERVAL_HOURS', ' 0 ')
    monkeypatch.setattr(config.Config, 'AUTO_SYNC_ON_STARTUP', 'false')
    monkeypatch.setattr(config.Config, 'SESSION_LIFETIME_DAYS', '3')
    flask_app = app_module.create_app({'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'e.db'),
                                       'SECRET_KEY': 'test-only'})
    store(flask_app, sync_interval_hours='24', auto_sync_on_startup='true',
          session_lifetime_days='30')
    assert not hasattr(flask_app, 'scheduler')
    assert rs.start_values(flask_app) == rs.StartValues(False, 0)
    assert rs.session_lifetime(flask_app) == timedelta(days=3)


def _child(env_updates, unset=()):
    env = {k: v for k, v in os.environ.items() if k not in ('MSSQL_CONNECTION_STRING',)}
    for name in unset:
        env.pop(name, None)
    env.update(PYTHONIOENCODING='utf-8', **env_updates)
    code = ('import config, sys; from services import runtime_settings as rs; from flask import '
            'Flask; a = Flask("x"); a.config.from_object(config.Config); rs.init_app(a); '
            'print(sorted(k for k in rs.ORDER if rs.pinned(a, k)))')
    return subprocess.run([sys.executable, '-c', code], cwd=str(APP_DIR), capture_output=True,
                          text=True, timeout=120, env=env)


def test_a_variable_set_to_an_empty_text_pins_and_refuses_the_start():
    result = _child({'SYNC_INTERVAL_HOURS': '', 'AUTO_SYNC_ON_STARTUP': 'false'})
    assert result.returncode != 0
    assert 'SettingError' in result.stderr and 'SYNC_INTERVAL_HOURS' in result.stderr


def test_an_unset_variable_pins_nothing_and_a_set_one_pins():
    result = _child({'SYNC_INTERVAL_HOURS': '0'},
                    unset=('AUTO_SYNC_ON_STARTUP', 'SESSION_LIFETIME_DAYS'))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['sync_interval_hours']"


# ── Axis 1 and 5: the stored rows ────────────────────────────────────────────

STORED_NUMBERS = [
    # (stored text for the interval, effective value; 24 = the default)
    ('0', 0), ('12', 12), (' 12 ', 12), ('0012', 12), ('8760', 8760), ('8761', 24),
    ('', 24), (' ', 24), ('-1', 24), ('+7', 24), ('7.0', 24), ('1e3', 24), ('1_000', 24),
    ('٧', 24), ('99999999999999999999', 24), ('\ud800', 24), ('seven', 24),
]


@pytest.mark.parametrize('raw,value', STORED_NUMBERS, ids=[repr(r[0]) for r in STORED_NUMBERS])
def test_a_stored_interval_applies_or_reads_as_the_default(raw, value):
    flask_app = bare_app()
    assert rs.effective(flask_app, rs.INTERVAL, get={rs.INTERVAL: raw}.get) == value


@pytest.mark.parametrize('raw,value', [
    ('1', 1), ('365', 365), ('0', 30), ('366', 30), ('', 30), ('7 days', 30),
], ids=lambda v: repr(v))
def test_a_stored_session_lifetime_applies_or_reads_as_the_default(raw, value):
    flask_app = bare_app()
    assert rs.effective(flask_app, rs.LIFETIME, get={rs.LIFETIME: raw}.get) == value


@pytest.mark.parametrize('raw,value', [
    ('true', True), ('false', False), ('TRUE', True), (' false ', False), ('on', False),
    ('1', False), ('yes', False), ('', False),
], ids=lambda v: repr(v))
def test_a_stored_auto_sync_applies_or_reads_as_the_default(raw, value):
    flask_app = bare_app()
    assert rs.effective(flask_app, rs.AUTO_SYNC, get={rs.AUTO_SYNC: raw}.get) is value


def test_no_row_and_a_null_value_read_as_the_defaults():
    flask_app = bare_app()
    assert [rs.effective(flask_app, key, get=lambda key: None) for key in rs.ORDER] == \
        [False, 24, 30]


def test_an_invalid_stored_value_is_logged_once_and_left_stored(free_app, caplog):
    store(free_app, sync_interval_hours='-5')
    with caplog.at_level(logging.WARNING, logger=rs.log.name):
        with free_app.app_context():
            assert rs.effective(free_app, rs.INTERVAL) == 24
            assert rs.effective(free_app, rs.INTERVAL) == 24
            assert auth_service.get_global_setting(rs.INTERVAL) == '-5'
    warnings = [r for r in caplog.records if rs.INTERVAL in r.getMessage()]
    assert len(warnings) == 1


def test_a_failed_read_of_an_unpinned_setting_raises_from_the_reader():
    flask_app = bare_app()

    def locked(key):
        raise sqlite3.OperationalError('database is locked')
    with pytest.raises(sqlite3.OperationalError):
        rs.effective(flask_app, rs.INTERVAL, get=locked)


# ── The scheduled check reads the interval at each check ─────────────────────

def _finished(app, hours_ago):
    store(app, sync_finished_at=(NOW - hours_ago * H).strftime('%Y-%m-%dT%H:%M:%SZ'),
          sync_result='ok')


@pytest.mark.parametrize('stored,hours_ago,started', [
    (None, 24, True), (None, 23, False), ('12', 13, True), ('12', 11, False),
    ('48', 24, False), ('0', 1000, False), ('-1', 24, True),
], ids=['default due', 'default not due', 'stored 12 due', 'stored 12 not due',
        'stored 48 not due', 'stored 0 never', 'invalid reads as 24'])
def test_the_scheduled_check_uses_the_stored_interval(free_app, stored, hours_ago, started):
    if stored is not None:
        store(free_app, sync_interval_hours=stored)
    _finished(free_app, hours_ago)
    runs = []
    assert ss.scheduled_check(free_app, runs.append, now=NOW) is started
    assert runs == ([free_app] if started else [])


def test_a_new_stored_interval_applies_at_the_next_check(free_app):
    _finished(free_app, 13)
    runs = []
    assert ss.scheduled_check(free_app, runs.append, now=NOW) is False
    store(free_app, sync_interval_hours='12')
    assert ss.scheduled_check(free_app, runs.append, now=NOW) is True
    store(free_app, sync_interval_hours='0')
    assert ss.scheduled_check(free_app, runs.append, now=NOW) is False
    assert runs == [free_app]


def test_a_pinned_interval_beats_the_stored_one_at_the_check(tmp_path):
    flask_app = make_app(tmp_path, SYNC_INTERVAL_HOURS=0, AUTO_SYNC_ON_STARTUP=False)
    store(flask_app, sync_interval_hours='1')
    runs = []
    assert ss.scheduled_check(flask_app, runs.append, now=NOW) is False
    assert runs == []


def test_a_failed_interval_read_skips_the_check_and_logs(free_app, monkeypatch, caplog):
    def locked(key):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(auth_service, 'get_global_setting', locked)
    runs = []
    with caplog.at_level(logging.ERROR, logger=ss.log.name):
        assert ss.scheduled_check(free_app, runs.append, now=NOW) is False
    assert runs == []
    assert any('could not be read' in r.getMessage() for r in caplog.records)


# ── The start: the scheduler and its jobs follow the effective values ────────

def _restart(app):
    if hasattr(app, 'scheduler'):
        del app.scheduler
    with app.app_context():
        app_module._start_scheduler(app)
    found = {job.id for job in app.scheduler.get_jobs()} if hasattr(app, 'scheduler') else None
    if hasattr(app, 'scheduler'):
        del app.scheduler          # never started: nothing to shut down
    return found


@pytest.mark.parametrize('rows,jobs', [
    ({}, {'sync-check'}),
    ({'sync_interval_hours': '0'}, None),
    ({'sync_interval_hours': '0', 'auto_sync_on_startup': 'true'}, {'sync-on-startup'}),
    ({'auto_sync_on_startup': 'true'}, {'sync-check', 'sync-on-startup'}),
    ({'sync_interval_hours': '6', 'auto_sync_on_startup': 'false'}, {'sync-check'}),
    ({'sync_interval_hours': 'x', 'auto_sync_on_startup': 'on'}, {'sync-check'}),
], ids=['no rows: default 24', 'stored 0', 'start-up only', 'both', 'interval only',
        'invalid rows read as the defaults'])
def test_the_start_follows_the_stored_values(free_app, rows, jobs):
    store(free_app, **rows)
    assert _restart(free_app) == jobs


def test_pinned_variables_start_no_scheduler_whatever_is_stored(app, no_scheduler_start):
    """The guard the tests, the gate and the preview rely on (``tests/conftest.py`` pins)."""
    store(app, sync_interval_hours='1', auto_sync_on_startup='true')
    assert _restart(app) is None
    runs = []
    assert ss.scheduled_check(app, runs.append, now=NOW) is False
    assert runs == []


def test_a_failed_read_at_the_start_counts_as_off(free_app, monkeypatch, caplog):
    def locked(key):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(auth_service, 'get_global_setting', locked)
    with caplog.at_level(logging.ERROR, logger=rs.log.name):
        assert rs.start_values(free_app) == rs.StartValues(False, 0)
        assert _restart(free_app) is None
    assert any('could not be read at the start' in r.getMessage() for r in caplog.records)


def test_a_failed_read_at_the_start_leaves_a_pin_in_force(tmp_path, no_scheduler_start,
                                                          monkeypatch):
    flask_app = make_app(tmp_path, AUTO_SYNC_ON_STARTUP='true', SYNC_INTERVAL_HOURS=None)
    monkeypatch.setattr(auth_service, 'get_global_setting',
                        lambda key: (_ for _ in ()).throw(sqlite3.OperationalError('locked')))
    assert rs.start_values(flask_app) == rs.StartValues(True, 0)


def test_the_apps_never_share_their_pins(tmp_path, no_scheduler_start):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    one = make_app(tmp_path / 'a', SYNC_INTERVAL_HOURS=5)
    two = make_app(tmp_path / 'b')
    assert rs.pinned(one, rs.INTERVAL) and not rs.pinned(two, rs.INTERVAL)
