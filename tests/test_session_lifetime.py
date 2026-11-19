"""The session lifetime and "remember me".

A session's age is its idle time: the time since its cookie was last signed. The effective
lifetime (pin, stored row, default 30 days; the stored row cached for at most a minute) bounds
that age at every request, so a shortened lifetime ends an older session at its next request
and a longer one keeps it. "Remember me" makes the session permanent (its cookie outlives the
browser, with an expiry of the lifetime); no sign-in sets Flask-Login's remember cookie, and a
request that carries one already issued is answered as if it had none and the cookie removed.
"""
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import pytest
from flask_login import login_user
from itsdangerous import TimestampSigner

import app as app_module
from conftest import ADMIN, TokenClient
from models.db import get_db
from models.user import User
from services import auth_service
from services import runtime_settings as rs
from oidc_fixtures import (  # noqa: F401  (the fixtures are found by name)
    CODE, callback, oidc_app, oidc_client, provider, seeded, start)

DAY = 24 * 3600
PAGE = '/dashboard'


@pytest.fixture
def free_app(tmp_path):
    """The session lifetime unpinned; the sync stays pinned off."""
    return app_module.create_app({'TESTING': True, 'DATABASE_PATH': str(tmp_path / 's.db'),
                                  'SECRET_KEY': 'test-only', 'SYNC_INTERVAL_HOURS': 0,
                                  'AUTO_SYNC_ON_STARTUP': False, 'SESSION_LIFETIME_DAYS': None})


def client_of(flask_app):
    return TokenClient(flask_app, flask_app.response_class, use_cookies=True)


def store_lifetime(flask_app, value):
    with flask_app.app_context():
        db = get_db()
        auth_service.upsert_global_setting(db, rs.LIFETIME, value)
        db.commit()
    flask_app.extensions[rs.EXTENSION].lifetime = None   # as if the minute had passed


def signed_in_session(flask_app):
    """The session of the admin signed in and remembered (the setup's, or once the admin
    exists, a sign-in with "remember me")."""
    client = client_of(flask_app)
    assert client.post('/auth/setup', data=ADMIN).status_code == 302
    with client.session_transaction() as sess:
        signed_in = '_user_id' in sess
    if not signed_in:
        client.post('/auth/login', data={'username': ADMIN['username'],
                                         'password': ADMIN['password'], 'remember': 'on'})
    with client.session_transaction() as sess:
        assert '_user_id' in sess and sess.permanent
        return dict(sess)


def aged_cookie(flask_app, data, age_seconds, monkeypatch):
    """The session cookie for ``data`` as Flask would have signed it ``age_seconds`` ago."""
    serializer = flask_app.session_interface.get_signing_serializer(flask_app)
    signed_at = int(time.time()) - age_seconds
    with monkeypatch.context() as patch:
        patch.setattr(TimestampSigner, 'get_timestamp', lambda self: signed_at)
        return serializer.dumps(data)


def answer(flask_app, cookie, **cookies):
    client = flask_app.test_client()
    client.set_cookie('session', cookie)
    for name, value in cookies.items():
        client.set_cookie(name, value)
    return client, client.get(PAGE)


def set_cookies(response):
    return response.headers.getlist('Set-Cookie')


def cookie_header(response, name):
    found = [h for h in set_cookies(response) if h.startswith(name + '=')]
    assert len(found) == 1, set_cookies(response)
    return found[0]


# ── A session's age against the effective lifetime ───────────────────────────

@pytest.mark.parametrize('stored,age,signed_in', [
    (None, 29 * DAY, True), (None, 31 * DAY, False),
    ('1', DAY - 60, True), ('1', DAY + 60, False), ('1', 2 * DAY, False),
    ('3', 2 * DAY, True), ('365', 300 * DAY, True), ('0', 29 * DAY, True),
    ('0', 31 * DAY, False), ('soon', 2 * DAY, True),
], ids=['default, 29 days idle', 'default, 31 days idle', '1 day, a minute less',
        '1 day, a minute more', '1 day, 2 days idle', '3 days, 2 days idle',
        '365 days, 300 days idle', 'invalid 0 reads as 30, 29 days', 'invalid 0 reads as 30, 31',
        'invalid text reads as 30'])
def test_a_session_older_than_the_lifetime_is_signed_out(free_app, monkeypatch, stored, age,
                                                         signed_in):
    data = signed_in_session(free_app)
    if stored is not None:
        store_lifetime(free_app, stored)
    _, response = answer(free_app, aged_cookie(free_app, data, age, monkeypatch))
    if signed_in:
        assert response.status_code == 200
    else:
        assert response.status_code == 302 and '/auth/login' in response.headers['Location']


def test_a_shortened_lifetime_ends_an_older_session_and_a_longer_one_keeps_it(free_app,
                                                                             monkeypatch):
    data = signed_in_session(free_app)
    cookie = aged_cookie(free_app, data, 2 * DAY, monkeypatch)
    assert answer(free_app, cookie)[1].status_code == 200          # the default 30 days
    store_lifetime(free_app, '1')
    assert answer(free_app, cookie)[1].status_code == 302          # shortened: signed out
    store_lifetime(free_app, '7')
    assert answer(free_app, cookie)[1].status_code == 200          # longer again: kept


def test_a_pinned_lifetime_beats_the_stored_one(tmp_path, monkeypatch):
    flask_app = app_module.create_app({'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'p.db'),
                                       'SECRET_KEY': 'test-only', 'SYNC_INTERVAL_HOURS': 0,
                                       'AUTO_SYNC_ON_STARTUP': False,
                                       'SESSION_LIFETIME_DAYS': '1'})
    data = signed_in_session(flask_app)
    store_lifetime(flask_app, '30')
    _, response = answer(flask_app, aged_cookie(flask_app, data, 2 * DAY, monkeypatch))
    assert response.status_code == 302


def test_a_request_re_signs_the_permanent_session_so_the_age_is_idle_time(free_app,
                                                                          monkeypatch):
    data = signed_in_session(free_app)
    store_lifetime(free_app, '3')
    client, response = answer(free_app, aged_cookie(free_app, data, 2 * DAY, monkeypatch))
    assert response.status_code == 200
    header = cookie_header(response, 'session')          # signed again now
    expires = parsedate_to_datetime(header.split('Expires=')[1].split(';')[0])
    left = expires - datetime.now(timezone.utc)
    assert timedelta(days=3) - timedelta(minutes=5) < left <= timedelta(days=3)
    assert client.get(PAGE).status_code == 200


def test_the_stored_lifetime_is_cached_for_a_minute(free_app, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rs, '_clock', lambda: now[0])
    reads = []

    def get(key):
        reads.append(key)
        return stored[0]
    stored = ['5']
    assert rs.session_lifetime(free_app, get=get) == timedelta(days=5)
    stored[0] = '1'
    now[0] += 59.9
    assert rs.session_lifetime(free_app, get=get) == timedelta(days=5)
    now[0] += 0.1
    assert rs.session_lifetime(free_app, get=get) == timedelta(days=1)
    now[0] -= 500        # a clock that went back reads again
    assert rs.session_lifetime(free_app, get=get) == timedelta(days=1)
    assert len(reads) == 3


def test_a_failed_lifetime_read_applies_the_default_and_fails_no_request(free_app, monkeypatch,
                                                                         caplog):
    data = signed_in_session(free_app)
    store_lifetime(free_app, '1')
    cookie = aged_cookie(free_app, data, 2 * DAY, monkeypatch)

    def locked(key):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(auth_service, 'get_global_setting', locked)
    with caplog.at_level(logging.ERROR, logger=rs.log.name):
        assert answer(free_app, cookie)[1].status_code == 200     # the default 30 days
        assert answer(free_app, cookie)[1].status_code == 200
    failures = [r for r in caplog.records if 'could not be read' in r.getMessage()]
    assert len(failures) == 1                      # logged once until a read succeeds
    assert free_app.extensions[rs.EXTENSION].lifetime is None     # a failure is not cached


# ── "Remember me" is a permanent session ─────────────────────────────────────

def _sign_in(flask_app, remember):
    client = client_of(flask_app)
    client.post('/auth/setup', data=ADMIN)
    client.post('/auth/logout')
    form = {'username': ADMIN['username'], 'password': ADMIN['password']}
    if remember is not None:
        form['remember'] = remember
    return client, client.post('/auth/login', data=form)


@pytest.mark.parametrize('remember,permanent', [
    (None, False), ('on', True), ('', False), ('anything', True),
], ids=['box absent', 'box ticked', 'empty value', 'other text'])
def test_the_sign_in_form_sets_no_remember_cookie(free_app, remember, permanent):
    client, response = _sign_in(free_app, remember)
    assert response.status_code == 302 and response.headers['Location'].endswith('/dashboard')
    assert not any(h.startswith(rs.REMEMBER_COOKIE + '=') for h in set_cookies(response))
    header = cookie_header(response, 'session')
    assert ('Expires=' in header) is permanent
    with client.session_transaction() as sess:
        assert bool(sess.permanent) is permanent
    assert client.get(PAGE).status_code == 200


def test_setup_and_registration_make_the_session_permanent(free_app):
    client = client_of(free_app)
    response = client.post('/auth/setup', data=ADMIN)
    assert 'Expires=' in cookie_header(response, 'session')
    assert not any(h.startswith(rs.REMEMBER_COOKIE + '=') for h in set_cookies(response))
    client.post('/auth/logout')
    response = client.post('/auth/register', data={'username': 'tenno', 'password': 'pw-12345678',
                                                   'confirm': 'pw-12345678'})
    assert response.status_code == 302
    assert 'Expires=' in cookie_header(response, 'session')
    assert not any(h.startswith(rs.REMEMBER_COOKIE + '=') for h in set_cookies(response))
    assert client.get(PAGE).status_code == 200


def test_an_oidc_sign_in_makes_the_session_permanent(seeded, provider):
    _, query = start(seeded, provider)
    response = callback(seeded, code=CODE, state=query['state'])
    assert response.status_code == 302 and response.headers['Location'].endswith('/dashboard')
    assert 'Expires=' in cookie_header(response, 'session')
    assert not any(h.startswith(rs.REMEMBER_COOKIE + '=') for h in set_cookies(response))
    with seeded.session_transaction() as sess:
        assert sess.permanent


def flask_login_remember_cookie(flask_app):
    """A remember cookie made by Flask-Login's own ``login_user(..., remember=True)``, as
    every sign-in issued before."""
    signed_in_session(flask_app)
    with flask_app.test_request_context():
        login_user(User.get_by_username(ADMIN['username']), remember=True)
        response = flask_app.response_class()
        flask_app.login_manager._update_remember_cookie(response)
    header = cookie_header(response, rs.REMEMBER_COOKIE)
    return header.split(';')[0].split('=', 1)[1]


def removed(response, name):
    header = cookie_header(response, name)
    return 'Expires=Thu, 01 Jan 1970' in header or 'Max-Age=0' in header


def test_an_old_remember_cookie_signs_nobody_in_and_is_removed(free_app):
    token = flask_login_remember_cookie(free_app)
    client = free_app.test_client()
    client.set_cookie(rs.REMEMBER_COOKIE, token)
    response = client.get(PAGE)
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']
    assert removed(response, rs.REMEMBER_COOKIE)
    assert client.get_cookie(rs.REMEMBER_COOKIE) is None
    with client.session_transaction() as sess:
        assert '_user_id' not in sess and '_remember' not in sess


def test_an_old_remember_cookie_beside_a_live_session_is_removed_and_the_session_kept(
        free_app, monkeypatch):
    token = flask_login_remember_cookie(free_app)
    data = signed_in_session(free_app)
    client, response = answer(free_app, aged_cookie(free_app, data, 60, monkeypatch),
                              **{rs.REMEMBER_COOKIE: token})
    assert response.status_code == 200
    assert removed(response, rs.REMEMBER_COOKIE)
    assert client.get(PAGE).status_code == 200


def test_an_old_remember_cookie_with_an_expired_session_is_signed_out(free_app, monkeypatch):
    token = flask_login_remember_cookie(free_app)
    data = signed_in_session(free_app)
    store_lifetime(free_app, '1')
    _, response = answer(free_app, aged_cookie(free_app, data, 2 * DAY, monkeypatch),
                         **{rs.REMEMBER_COOKIE: token})
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']
    assert removed(response, rs.REMEMBER_COOKIE)


@pytest.mark.parametrize('token', ['', 'garbage', '1|' + 'a' * 128, 'é'],
                         ids=['empty', 'garbage', 'forged digest', 'non-ascii'])
def test_any_remember_cookie_value_signs_nobody_in(free_app, token):
    signed_in_session(free_app)
    client = free_app.test_client()
    client.set_cookie(rs.REMEMBER_COOKIE, token)
    response = client.get(PAGE)
    assert response.status_code == 302
    assert removed(response, rs.REMEMBER_COOKIE)


@pytest.mark.parametrize('remember', [None, 'on'], ids=['box absent', 'box ticked'])
def test_a_sign_in_with_an_old_remember_cookie_removes_it_and_sets_none(free_app, remember):
    token = flask_login_remember_cookie(free_app)
    client = client_of(free_app)
    client.post('/auth/logout')
    client.set_cookie(rs.REMEMBER_COOKIE, token)
    form = {'username': ADMIN['username'], 'password': ADMIN['password']}
    if remember:
        form['remember'] = remember
    response = client.post('/auth/login', data=form)
    assert response.status_code == 302 and response.headers['Location'].endswith('/dashboard')
    assert removed(response, rs.REMEMBER_COOKIE)
    assert client.get_cookie(rs.REMEMBER_COOKIE) is None
    with client.session_transaction() as sess:
        assert bool(sess.permanent) is bool(remember)
        assert '_remember' not in sess
    assert client.get('/dashboard').status_code == 200


def test_an_old_remember_cookie_on_a_static_file_is_removed(free_app):
    token = flask_login_remember_cookie(free_app)
    client = free_app.test_client()
    client.set_cookie(rs.REMEMBER_COOKIE, token)
    response = client.get('/static/vendor/jquery.min.js')
    assert removed(response, rs.REMEMBER_COOKIE)


def test_a_sign_in_without_remember_after_a_remembered_one_is_a_browser_session(free_app):
    client = client_of(free_app)
    client.post('/auth/setup', data=ADMIN)
    client.post('/auth/logout')
    response = client.post('/auth/login', data={'username': ADMIN['username'],
                                                'password': ADMIN['password']})
    assert 'Expires=' not in cookie_header(response, 'session')
    assert not any(h.startswith(rs.REMEMBER_COOKIE + '=') for h in set_cookies(response))
