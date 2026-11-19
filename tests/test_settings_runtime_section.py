"""The Settings page's runtime section is the admin's: auto-sync on startup, the scheduled sync
interval and the session lifetime, each shown with its value and its source (saved, the
default, or set by its environment variable and read-only), saved together in one commit or
not at all. A save that leaves the interval above 0 adds the scheduled check in the saving
process when it has none, never a second one.

Every test runs on its own temporary database. The apps here leave the three settings unpinned
(override None) unless a test pins one; their scheduler never starts, and the check job a save
adds goes to a fake scheduler or to an APScheduler whose start does nothing.
"""
import html
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.datastructures import MultiDict

import app as app_module
from conftest import TokenClient
from models.db import get_db
from services import auth_service, runtime_page
from services import runtime_settings as rs
from services import sync_schedule as ss

NAMES = ('AUTO_SYNC_ON_STARTUP', 'SYNC_INTERVAL_HOURS', 'SESSION_LIFETIME_DAYS')
KEYS = (rs.AUTO_SYNC, rs.INTERVAL, rs.LIFETIME)
LABEL = {key: rs.SETTINGS[key].label for key in KEYS}
NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)


# ── Fixtures and helpers ─────────────────────────────────────────────────────

class FakeScheduler:
    """APScheduler's add_job / get_job / start, recorded."""

    def __init__(self):
        self.jobs, self.started = {}, 0

    def add_job(self, func, trigger, **kwargs):
        if kwargs['id'] in self.jobs:
            raise AssertionError('a second job with the id ' + kwargs['id'])
        self.jobs[kwargs['id']] = (func, trigger, kwargs)

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def start(self):
        self.started += 1


@pytest.fixture(autouse=True)
def fresh_log_memory(monkeypatch):
    monkeypatch.setattr(rs, '_logged', set())


@pytest.fixture(autouse=True)
def no_scheduler_start(monkeypatch):
    """No APScheduler built here ever runs (its jobs are only inspected)."""
    from apscheduler.schedulers.background import BackgroundScheduler
    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self, *a, **k: None)


def make_app(tmp_path, name='rt', **pins):
    """An app with the three settings unpinned unless given here, and no scheduler."""
    overrides = {'TESTING': True, 'DATABASE_PATH': str(tmp_path / f'{name}.db'),
                 'SECRET_KEY': 'test-only', **{n: None for n in NAMES}}
    overrides.update(pins)
    flask_app = app_module.create_app(overrides)
    if hasattr(flask_app, 'scheduler'):
        del flask_app.scheduler        # never started: nothing to stop
    return flask_app


@pytest.fixture
def rapp(tmp_path):
    return make_app(tmp_path)


def _login(app, name, role):
    from models.user import User
    with app.app_context():
        User.create(name, password='pw-' + name + '-1', role=role)
    client = TokenClient(app, app.response_class, use_cookies=True)
    response = client.post('/auth/login', data={'username': name,
                                                'password': 'pw-' + name + '-1'})
    assert response.status_code == 302
    client.get('/settings/')            # the first-run welcome is shown once
    return client


@pytest.fixture
def admin(rapp):
    return _login(rapp, 'root', 'admin')


def rows(app):
    with app.app_context():
        return sorted(tuple(r) for r in get_db().execute(
            'SELECT key, value FROM settings WHERE user_id IS NULL AND key IN (?, ?, ?) '
            'ORDER BY key, id', KEYS).fetchall())


def store(app, **values):
    with app.app_context():
        db = get_db()
        for key, value in values.items():
            auth_service.upsert_global_setting(db, key, value)
        db.commit()


def form(**fields):
    """The section's form as a browser sends it: action, the ticked switch, the two numbers."""
    data = {'action': runtime_page.ACTION, rs.AUTO_SYNC: 'on', rs.INTERVAL: '24',
            rs.LIFETIME: '30'}
    data.update(fields)
    return {k: v for k, v in data.items() if v is not None}


def post(client, data, follow=True):
    return client.post('/settings/', data=data, follow_redirects=follow)


def flashes(response):
    text = response.get_data(as_text=True)
    return [html.unescape(' '.join(m.split())) for m in re.findall(
        r'<div class="card-panel [^"]*white-text" style="margin-bottom:1rem">(.*?)</div>',
        text, re.S)]


def section(response):
    found = re.search(r'<div class="card blue-grey darken-2" id="runtime-card">(.*?)'
                      r'<!-- Backup: the admin\'s -->', response.get_data(as_text=True), re.S)
    return found.group(1) if found else None


def check(app, data):
    with app.test_request_context():
        return runtime_page.check_runtime_form(MultiDict(data), app)


@pytest.fixture
def jobs(monkeypatch):
    """Every ensure_check_job call a save makes, answered 'added'."""
    calls = []
    monkeypatch.setattr(ss, 'ensure_check_job', lambda app: calls.append(app) or 'added')
    return calls


# ── Who sees it, who saves ───────────────────────────────────────────────────

def test_an_admin_sees_the_section_and_a_user_does_not(rapp, admin):
    shown = section(admin.get('/settings/'))
    assert shown is not None and 'id="runtime-form"' in shown
    for key in KEYS:
        assert f'name="{key}"' in shown
    page = _login(rapp, 'alice', 'user').get('/settings/').get_data(as_text=True)
    assert 'runtime-card' not in page and runtime_page.ACTION not in page


@pytest.mark.parametrize('data', [form(), form(**{rs.INTERVAL + '_default': 'on'})],
                         ids=['values', 'use the default'])
def test_a_non_admin_save_is_403_and_writes_nothing(rapp, admin, jobs, data):
    store(rapp, sync_interval_hours='6')
    response = post(_login(rapp, 'alice', 'user'), data, follow=False)
    assert response.status_code == 403
    assert rows(rapp) == [(rs.INTERVAL, '6')] and jobs == []


def test_a_save_without_the_csrf_token_is_refused(rapp, admin):
    response = admin.post('/settings/', headers={'X-CSRF-Token': 'wrong'}, data=form())
    assert response.status_code in (400, 403)
    assert rows(rapp) == []


@pytest.mark.parametrize('action', ['save_runtime', 'Save_Runtime_Settings', '', 'on'])
def test_an_unknown_action_saves_nothing(rapp, admin, action):
    response = post(admin, form(action=action), follow=False)
    assert response.status_code == 302 and rows(rapp) == []


# ── What it shows ────────────────────────────────────────────────────────────

def test_each_value_shows_with_its_source(tmp_path):
    app = make_app(tmp_path, SESSION_LIFETIME_DAYS='7')
    store(app, sync_interval_hours='6')
    shown = section(_login(app, 'root', 'admin').get('/settings/'))
    interval = shown.split('id="runtime-sync_interval_hours-row"')[1].split('</section>')[0]
    assert 'value="6"' in interval and 'Saved here (default: 24 hours)' in ' '.join(
        interval.split())
    switch = shown.split('id="runtime-auto_sync_on_startup-row"')[1].split('</section>')[0]
    assert 'The default.' in switch and ' checked' not in switch
    assert 'Applies from the next start of the app.' in switch
    lifetime = shown.split('id="runtime-session_lifetime_days-row"')[1].split('</section>')[0]
    assert 'name="session_lifetime_days"' not in lifetime and '<input' not in lifetime
    assert '7 days' in lifetime
    assert ('Set by the environment variable SESSION_LIFETIME_DAYS; remove it there to set '
            'it here.') in ' '.join(lifetime.split())


def test_the_use_the_default_box_shows_only_for_a_stored_row(rapp, admin):
    store(rapp, sync_interval_hours='6')
    shown = section(admin.get('/settings/'))
    assert 'name="sync_interval_hours_default"' in shown
    assert 'name="session_lifetime_days_default"' not in shown
    assert 'name="auto_sync_on_startup_default"' not in shown


def test_a_stored_invalid_value_shows_as_the_default_and_stays_stored(rapp, admin):
    store(rapp, sync_interval_hours='99999', session_lifetime_days='seven')
    shown = ' '.join(section(admin.get('/settings/')).split())
    assert 'value="24"' in shown and 'value="30"' in shown
    assert shown.count('The saved value is not valid') == 2
    assert rows(rapp) == [(rs.LIFETIME, 'seven'), (rs.INTERVAL, '99999')]


def test_a_save_of_the_shown_default_replaces_an_invalid_row(rapp, admin, jobs):
    store(rapp, sync_interval_hours='99999')
    post(admin, form())
    assert dict(rows(rapp))[rs.INTERVAL] == '24'
    assert 'Saved here' in section(admin.get('/settings/'))


def test_a_settings_read_failure_shows_no_form_and_still_the_pins(tmp_path, monkeypatch):
    app = make_app(tmp_path, SYNC_INTERVAL_HOURS='0')
    client = _login(app, 'root', 'admin')
    real = auth_service.get_global_setting

    def locked(key, default=None):
        if key in KEYS:
            raise sqlite3.OperationalError('database is locked')
        return real(key, default)

    monkeypatch.setattr(auth_service, 'get_global_setting', locked)
    response = client.get('/settings/')
    assert response.status_code == 200
    shown = ' '.join(section(response).split())
    assert 'Settings not shown: the settings could not be read.' in shown
    assert '<form' not in shown and 'name="' not in shown
    assert 'Set by the environment variable SYNC_INTERVAL_HOURS' in shown
    assert 'database is locked' not in response.get_data(as_text=True)


def test_every_pinned_setting_leaves_no_form(tmp_path):
    app = make_app(tmp_path, AUTO_SYNC_ON_STARTUP='false', SYNC_INTERVAL_HOURS='0',
                   SESSION_LIFETIME_DAYS='30')
    shown = section(_login(app, 'root', 'admin').get('/settings/'))
    assert shown.count('Set by the environment variable') == 3 and '<form' not in shown


# ── The save ─────────────────────────────────────────────────────────────────

def test_a_valid_save_writes_every_row_normalised_and_redirects(rapp, admin, jobs, caplog):
    with caplog.at_level(logging.INFO, logger=runtime_page.log.name):
        response = post(admin, form(**{rs.INTERVAL: ' 0012 ', rs.LIFETIME: '007'}),
                        follow=False)
    assert response.status_code == 302
    assert rows(rapp) == [(rs.AUTO_SYNC, 'true'), (rs.LIFETIME, '7'), (rs.INTERVAL, '12')]
    said = [r.getMessage() for r in caplog.records if 'saved by user' in r.getMessage()]
    assert len(said) == 1 and "'sync_interval_hours': '12'" in said[0]
    page = admin.get('/settings/')
    assert runtime_page.SAVED in flashes(page) and runtime_page.AT_NEXT_START in flashes(page)


def test_an_unticked_switch_is_saved_as_false(rapp, admin, jobs):
    post(admin, form(**{rs.AUTO_SYNC: None}))
    assert dict(rows(rapp))[rs.AUTO_SYNC] == 'false'


def test_a_refused_save_names_every_invalid_field_and_writes_nothing(rapp, admin, jobs):
    store(rapp, sync_interval_hours='6')
    response = post(admin, form(**{rs.INTERVAL: '9000', rs.LIFETIME: '0',
                                   rs.AUTO_SYNC: 'yes'}))
    said = flashes(response)
    assert len(said) == 1 and said[0].startswith(runtime_page.NOT_SAVED)
    for name in LABEL.values():
        assert name in said[0]
    assert 'from 0 to 8760 hours' in said[0] and 'from 1 to 365 days' in said[0]
    assert '9000' not in said[0]
    assert rows(rapp) == [(rs.INTERVAL, '6')] and jobs == []


def test_the_same_save_twice_leaves_the_same_rows_and_one_check_job(rapp, admin):
    store(rapp, sync_interval_hours='0')
    fake = FakeScheduler()
    rapp.scheduler = fake
    post(admin, form(**{rs.INTERVAL: '6'}))
    first = rows(rapp)
    post(admin, form(**{rs.INTERVAL: '6'}))
    assert rows(rapp) == first and list(fake.jobs) == [ss.CHECK_JOB]
    del rapp.scheduler


def test_two_admins_in_two_tabs_the_last_save_wins(rapp, admin, jobs):
    other = _login(rapp, 'root2', 'admin')
    post(admin, form(**{rs.LIFETIME: '10'}))
    post(other, form(**{rs.LIFETIME: '20', rs.AUTO_SYNC: None}))
    assert dict(rows(rapp)) == {rs.AUTO_SYNC: 'false', rs.INTERVAL: '24', rs.LIFETIME: '20'}


def test_a_save_clears_this_processes_cached_lifetime(rapp, admin, jobs):
    with rapp.app_context():
        assert rs.session_lifetime(rapp) == timedelta(days=30)
    assert rs._state(rapp).lifetime is not None
    post(admin, form(**{rs.LIFETIME: '3'}))
    with rapp.app_context():
        assert rs.session_lifetime(rapp) == timedelta(days=3)


# ── Axis 2 and 4: a failed write leaves nothing ──────────────────────────────

@pytest.mark.parametrize('fail_at', ['first write', 'second write', 'delete', 'commit'])
def test_a_failed_write_rolls_back_and_says_not_saved(rapp, admin, jobs, monkeypatch, fail_at):
    store(rapp, sync_interval_hours='6', session_lifetime_days='9')
    before = rows(rapp)
    real_write, real_delete = auth_service.upsert_global_setting, auth_service.delete_global_setting
    count = []

    def write(db, key, value):
        count.append(key)
        if (fail_at == 'first write' and len(count) == 1) or \
                (fail_at == 'second write' and len(count) == 2):
            raise sqlite3.OperationalError('database is locked SECRET-DETAIL')
        real_write(db, key, value)

    def delete(db, key):
        real_delete(db, key)
        if fail_at == 'delete':
            raise sqlite3.OperationalError('database is locked SECRET-DETAIL')

    monkeypatch.setattr(auth_service, 'upsert_global_setting', write)
    monkeypatch.setattr(auth_service, 'delete_global_setting', delete)
    if fail_at == 'commit':
        import models.db as db_module
        real_get = db_module.get_db

        class Failing:
            def __init__(self, db):
                self._db = db

            def __getattr__(self, name):
                return getattr(self._db, name)

            def commit(self):
                raise sqlite3.OperationalError('database is locked SECRET-DETAIL')

        monkeypatch.setattr(db_module, 'get_db', lambda: Failing(real_get()))
    response = post(admin, form(**{rs.INTERVAL: '12', rs.LIFETIME: '',
                                   rs.LIFETIME + '_default': 'on'}))
    assert flashes(response) == [runtime_page.NOT_SAVED + runtime_page.WRITE_FAILED + '.']
    assert 'SECRET-DETAIL' not in response.get_data(as_text=True)
    assert rows(rapp) == before and jobs == []


def test_a_failed_read_of_the_interval_before_the_save_writes_nothing(rapp, admin, jobs,
                                                                     monkeypatch):
    order = []

    def effective(app, key, get=None):
        order.append(('read', key))
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(rs, 'effective', effective)
    monkeypatch.setattr(auth_service, 'upsert_global_setting',
                        lambda db, k, v: order.append(('write', k)))
    response = post(admin, form())
    assert flashes(response) == [runtime_page.NOT_SAVED + runtime_page.WRITE_FAILED + '.']
    assert order == [('read', rs.INTERVAL)] and rows(rapp) == [] and jobs == []


def test_the_interval_before_is_read_on_the_saves_connection_before_any_write(rapp,
                                                                              monkeypatch):
    seen = []
    real_effective, real_write = rs.effective, auth_service.upsert_global_setting

    def effective(app, key, get=None):
        value = real_effective(app, key, get)
        seen.append(('read', key, get))
        return value

    def write(db, key, value):
        seen.append(('write', key, db))
        real_write(db, key, value)

    monkeypatch.setattr(rs, 'effective', effective)
    monkeypatch.setattr(auth_service, 'upsert_global_setting', write)
    with rapp.test_request_context():
        runtime_page.save_runtime(MultiDict(form(**{rs.INTERVAL: '0'})), rapp, 1)
        db = get_db()
    assert seen[0][0] == 'read' and seen[0][2] is not None
    assert {entry[2] for entry in seen[1:]} == {db}


# ── The scheduler: the check job after a save ────────────────────────────────

@pytest.mark.parametrize('stored,sent,ensured,notice', [
    ('0', '6', True, True),
    (None, '6', True, False),
    ('12', '6', True, False),
    ('6', '0', False, False),
    ('0', '0', False, False),
    ('x', '6', True, False),
], ids=['0 to 6: switched on', 'default 24 to 6', '12 to 6', '6 to 0', '0 to 0',
        'invalid (24) to 6'])
def test_a_save_ensures_the_check_job_when_the_interval_stays_above_0(rapp, admin, jobs,
                                                                     stored, sent, ensured,
                                                                     notice):
    if stored is not None:
        store(rapp, sync_interval_hours=stored)
    page = post(admin, form(**{rs.INTERVAL: sent}))
    assert len(jobs) == (1 if ensured else 0)
    assert (runtime_page.SWITCHED_ON in flashes(page)) is notice


def test_use_the_default_on_a_stored_0_switches_the_schedule_on(rapp, admin, jobs):
    store(rapp, sync_interval_hours='0')
    page = post(admin, form(**{rs.INTERVAL: '', rs.INTERVAL + '_default': 'on'}))
    assert rows(rapp) == [(rs.AUTO_SYNC, 'true'), (rs.LIFETIME, '30')]
    assert len(jobs) == 1 and runtime_page.SWITCHED_ON in flashes(page)


def test_a_save_of_the_lifetime_alone_never_touches_the_scheduler(tmp_path, monkeypatch):
    app = make_app(tmp_path, SYNC_INTERVAL_HOURS='0')
    calls = []
    monkeypatch.setattr(ss, 'ensure_check_job', lambda a: calls.append(a) or 'added')
    post(_login(app, 'root', 'admin'), form(**{rs.INTERVAL: None, rs.LIFETIME: '5'}))
    assert dict(rows(app)) == {rs.AUTO_SYNC: 'true', rs.LIFETIME: '5'} and calls == []


def test_the_check_job_is_added_after_the_rows_are_committed(rapp, admin, monkeypatch):
    store(rapp, sync_interval_hours='0')
    seen = []

    def ensure(app):
        other = sqlite3.connect(app.config['DATABASE_PATH'])
        seen.append(other.execute("SELECT value FROM settings WHERE user_id IS NULL AND "
                                  "key = 'sync_interval_hours'").fetchone())
        other.close()
        return 'added'

    monkeypatch.setattr(ss, 'ensure_check_job', ensure)
    post(admin, form(**{rs.INTERVAL: '6'}))
    assert seen == [('6',)]


@pytest.mark.parametrize('outcome', ['unavailable', 'raises'])
def test_a_scheduler_that_cannot_run_is_said_on_the_page(rapp, admin, monkeypatch, outcome):
    store(rapp, sync_interval_hours='0')

    def ensure(app):
        if outcome == 'raises':
            raise RuntimeError('scheduler broke')
        return outcome

    monkeypatch.setattr(ss, 'ensure_check_job', ensure)
    said = flashes(post(admin, form(**{rs.INTERVAL: '6'})))
    assert runtime_page.SAVED in said and runtime_page.NO_SCHEDULER in said
    assert runtime_page.SWITCHED_ON not in said and dict(rows(rapp))[rs.INTERVAL] == '6'


def test_ensure_check_job_starts_a_scheduler_when_the_process_has_none(rapp):
    fake = FakeScheduler()
    assert ss.ensure_check_job(rapp, run=print, now=NOW, make_scheduler=lambda: fake) == 'added'
    func, trigger, kwargs = fake.jobs[ss.CHECK_JOB]
    assert func is ss.scheduled_check and trigger == 'interval' and kwargs['seconds'] == 900
    assert kwargs['start_date'] == NOW + timedelta(minutes=2) and fake.started == 1
    assert rapp.scheduler is fake
    assert ss.ensure_check_job(rapp, run=print, now=NOW) == 'present'
    assert len(fake.jobs) == 1 and fake.started == 1
    del rapp.scheduler


def test_ensure_check_job_adds_to_a_running_scheduler_without_the_job(rapp):
    fake = FakeScheduler()
    fake.add_job(print, 'interval', id='backup-check')
    rapp.scheduler = fake
    assert ss.ensure_check_job(rapp, run=print, now=NOW) == 'added'
    assert set(fake.jobs) == {'backup-check', ss.CHECK_JOB} and fake.started == 0
    del rapp.scheduler


def test_ensure_check_job_without_apscheduler_is_unavailable_and_logged(rapp, caplog):
    def missing():
        raise ImportError('No module named apscheduler')

    with caplog.at_level(logging.WARNING, logger=ss.log.name):
        assert ss.ensure_check_job(rapp, run=print, make_scheduler=missing) == 'unavailable'
    assert not hasattr(rapp, 'scheduler')
    assert any('APScheduler not installed' in r.getMessage() for r in caplog.records)


def test_ensure_check_job_with_apscheduler_keeps_one_job_under_its_fixed_id(rapp):
    assert ss.ensure_check_job(rapp, run=print, now=NOW) == 'added'
    assert ss.ensure_check_job(rapp, run=print, now=NOW) == 'present'
    assert [job.id for job in rapp.scheduler.get_jobs()] == [ss.CHECK_JOB]
    del rapp.scheduler


# ── delete_global_setting ────────────────────────────────────────────────────

def test_delete_global_setting_removes_the_app_row_only_and_does_not_commit(rapp):
    from models.user import User
    with rapp.app_context():
        user = User.create('bob', password='pw-bob-111', role='user')
        db = get_db()
        auth_service.upsert_global_setting(db, rs.INTERVAL, '6')
        db.execute('INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)',
                   (user.id, rs.INTERVAL, '9'))
        db.commit()
        auth_service.delete_global_setting(db, rs.INTERVAL)
        db.rollback()
        assert auth_service.get_global_setting(rs.INTERVAL) == '6'
        auth_service.delete_global_setting(db, rs.INTERVAL)
        auth_service.delete_global_setting(db, rs.LIFETIME)       # no row: nothing
        db.commit()
        left = db.execute('SELECT user_id, value FROM settings WHERE key = ?',
                          (rs.INTERVAL,)).fetchall()
    assert [tuple(r) for r in left] == [(user.id, '9')]


def test_delete_global_setting_passes_the_key_as_a_parameter(rapp):
    class Recorder:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, params))

    db = Recorder()
    auth_service.delete_global_setting(db, "x' OR 1=1 --")
    assert db.calls == [('DELETE FROM settings WHERE user_id IS NULL AND key = ?',
                         ("x' OR 1=1 --",))]
