"""The runtime section's save, as its record states it: a repeated `action` is dispatched on its
first value and then refused by the runtime save, nothing written; the interval before the save
is read on the save's own connection before the write, and on SQLite (the driver's default
isolation) that read opens no transaction: the transaction starts at the first write, so a
commit another connection makes between the read and the write lands, and the save's notice
follows what its own read saw. Every test runs on its own temporary database.
"""
import sqlite3

import pytest

import app as app_module
from conftest import TokenClient
from models.db import get_db
from services import runtime_page
from services import runtime_settings as rs

NAMES = ('AUTO_SYNC_ON_STARTUP', 'SYNC_INTERVAL_HOURS', 'SESSION_LIFETIME_DAYS')
KEYS = (rs.AUTO_SYNC, rs.INTERVAL, rs.LIFETIME)


@pytest.fixture(autouse=True)
def no_scheduler_start(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self, *a, **k: None)


@pytest.fixture
def rapp(tmp_path):
    overrides = {'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'rt.db'),
                 'SECRET_KEY': 'test-only', **{n: None for n in NAMES}}
    flask_app = app_module.create_app(overrides)
    if hasattr(flask_app, 'scheduler'):
        del flask_app.scheduler
    return flask_app


@pytest.fixture
def admin(rapp):
    from models.user import User
    with rapp.app_context():
        User.create('root', password='pw-root-1', role='admin')
    client = TokenClient(rapp, rapp.response_class, use_cookies=True)
    assert client.post('/auth/login', data={'username': 'root',
                                            'password': 'pw-root-1'}).status_code == 302
    client.get('/settings/')
    return client


def rows(app):
    with app.app_context():
        return sorted(tuple(r) for r in get_db().execute(
            'SELECT key, value FROM settings WHERE user_id IS NULL AND key IN (?, ?, ?)',
            KEYS).fetchall())


def pairs(*actions):
    return [('action', a) for a in actions] + [(rs.AUTO_SYNC, 'on'), (rs.INTERVAL, '24'),
                                               (rs.LIFETIME, '30')]


# ── Item 35: a repeated action, through the page ─────────────────────────────

@pytest.mark.parametrize('second', [runtime_page.ACTION, 'save_preferences', ''],
                         ids=['same', 'another action', 'empty'])
def test_a_repeated_action_reaching_the_runtime_save_is_refused_nothing_written(
        rapp, admin, second):
    from werkzeug.datastructures import MultiDict
    response = admin.post('/settings/', data=MultiDict(pairs(runtime_page.ACTION, second)),
                          follow_redirects=True)
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Action was sent more than once' in page
    assert runtime_page.SAVED not in page
    assert rows(rapp) == []


# ── Item 29: the read before the write, on SQLite ────────────────────────────

def test_the_interval_read_on_sqlite_opens_no_transaction_and_a_commit_between_lands(
        rapp, admin, monkeypatch):
    seen = {}
    real_effective = rs.effective
    path = rapp.config['DATABASE_PATH']

    def effective(app, key, read):
        value = real_effective(app, key, read)
        if key == rs.INTERVAL and 'in_tx' not in seen:
            seen['in_tx'] = get_db().in_transaction
            seen['before'] = value
            other = sqlite3.connect(path, timeout=2)   # another admin commits in between
            try:
                other.execute("INSERT INTO settings (user_id, key, value) "
                              "VALUES (NULL, ?, '5')", (rs.INTERVAL,))
                other.commit()
            finally:
                other.close()
        return value

    monkeypatch.setattr(rs, 'effective', effective)
    response = admin.post('/settings/', data=dict(pairs(runtime_page.ACTION)),
                          follow_redirects=True)
    page = response.get_data(as_text=True)
    assert seen['in_tx'] is False                    # the SELECT opened no transaction
    assert seen['before'] == rs.SETTINGS[rs.INTERVAL].default   # read before the other commit
    assert runtime_page.SAVED in page                # the other commit did not block the save
    assert (rs.INTERVAL, '24') in rows(rapp)         # the last commit wins
