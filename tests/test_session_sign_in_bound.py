"""A sign-in's absolute bound, beside the session's idle lifetime.

Every app sign-in (setup, the sign-in form with or without "remember me", registration, an
OpenID Connect sign-in) stores its time in the session as whole seconds since the epoch,
replacing an earlier one; signing out removes it. A signed-in session whose sign-in is three
effective session lifetimes ago or more is answered as signed out at its next request, whatever
the use, and the response clears the cookie (a page goes to the login; an unsafe request gets
the CSRF refusal, the session's token having gone with it). A session without a sign-in time, or
one that is not a whole number, is kept and stamped with now; one later than now is replaced by
now. Under EasyAuth the platform bounds its own sessions: nothing is stamped or checked.
"""
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from markupsafe import Markup

import pytest

import app as app_module
from conftest import ADMIN, TokenClient
from models.db import get_db
from models.user import User
from services import auth_service, csrf, oidc
from services import runtime_settings as rs
from oidc_fixtures import (  # noqa: F401  (the fixtures are found by name)
    CODE, callback, oidc_app, oidc_client, provider, seeded, start)

DAY = 24 * 3600
PAGE = '/dashboard'
KEY = rs.SIGNED_IN_AT
USER = {'username': 'tenno', 'password': 'pw-12345678', 'confirm': 'pw-12345678'}


class Wall:
    """The sign-in time's wall clock, set by the test (near the real time, since the cookie's
    idle age is measured by the real clock)."""

    def __init__(self):
        self.now = int(time.time())

    def __call__(self):
        return self.now


@pytest.fixture
def wall(monkeypatch):
    clock = Wall()
    monkeypatch.setattr(rs, '_wall', clock)
    return clock


def make_app(tmp_path, name='s.db', **config):
    settings = {'TESTING': True, 'DATABASE_PATH': str(tmp_path / name), 'SECRET_KEY': 'test-only',
                'SYNC_INTERVAL_HOURS': 0, 'AUTO_SYNC_ON_STARTUP': False,
                'SESSION_LIFETIME_DAYS': None}
    settings.update(config)
    return app_module.create_app(settings)


@pytest.fixture
def free_app(tmp_path):
    """The session lifetime unpinned (stored row or default 30); the sync pinned off."""
    return make_app(tmp_path)


def client_of(flask_app):
    return TokenClient(flask_app, flask_app.response_class, use_cookies=True)


def store_lifetime(flask_app, value):
    with flask_app.app_context():
        db = get_db()
        auth_service.upsert_global_setting(db, rs.LIFETIME, value)
        db.commit()
    rs.forget_cached_lifetime(flask_app)


def signed_in(flask_app):
    """A client signed in as the admin through the setup (a remembered session)."""
    client = client_of(flask_app)
    assert client.post('/auth/setup', data=ADMIN).status_code == 302
    return client


def set_sign_in_time(client, value=None, *, drop=False, token=None):
    """Write the session's sign-in time in one transaction (the next request is the one under
    test: a transaction opens the session through the same interface, so it checks too)."""
    with client.session_transaction() as sess:
        if drop:
            sess.pop(KEY, None)
        else:
            sess[KEY] = value
        if token is not None:
            sess[csrf.SESSION_KEY] = token


def session_of(client) -> dict:
    with client.session_transaction() as sess:
        return dict(sess)


def session_cookie_set(response) -> bool:
    return any(h.startswith('session=') for h in response.headers.getlist('Set-Cookie'))


def assert_signed_out(client, response):
    """A page went to the login and the response cleared the cookie: deleted, or replaced by
    one that holds no user and no sign-in time."""
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']
    assert session_cookie_set(response)
    left = session_of(client)
    assert '_user_id' not in left and KEY not in left, left


# ── Every app sign-in stores its time ────────────────────────────────────────

def test_setup_stores_the_sign_in_time(free_app, wall, monkeypatch):
    monkeypatch.setattr(rs, '_clock', lambda: 5.0)   # the cache's monotonic clock is not it
    client = signed_in(free_app)
    stored = session_of(client)[KEY]
    assert stored == wall.now and type(stored) is int


@pytest.mark.parametrize('remember', [None, 'on'], ids=['box absent', 'box ticked'])
def test_the_sign_in_form_stores_the_sign_in_time(free_app, wall, remember):
    client = signed_in(free_app)
    client.post('/auth/logout')
    wall.now += 600
    form = {'username': ADMIN['username'], 'password': ADMIN['password']}
    if remember:
        form['remember'] = remember
    assert client.post('/auth/login', data=form).status_code == 302
    sess = session_of(client)
    assert sess[KEY] == wall.now and '_user_id' in sess
    assert bool(sess.get('_permanent')) is bool(remember)


def test_registration_stores_the_sign_in_time(free_app, wall):
    client = signed_in(free_app)
    client.post('/auth/logout')
    wall.now += 60
    assert client.post('/auth/register', data=USER).status_code == 302
    assert session_of(client)[KEY] == wall.now


def test_an_oidc_sign_in_stores_the_sign_in_time(seeded, provider, wall):
    wall.now += 60
    _, query = start(seeded, provider)
    response = callback(seeded, code=CODE, state=query['state'])
    assert response.status_code == 302 and response.headers['Location'].endswith('/dashboard')
    sess = session_of(seeded)
    assert sess[KEY] == wall.now and '_user_id' in sess


def test_a_second_sign_in_in_one_browser_replaces_the_time(free_app, wall):
    client = signed_in(free_app)
    set_sign_in_time(client, wall.now - 5 * DAY)
    wall.now += 30
    # Registration does not turn a signed-in browser away: the new account is signed in.
    assert client.post('/auth/register', data=USER).status_code == 302
    assert session_of(client)[KEY] == wall.now


def test_an_oidc_sign_in_replaces_an_earlier_time(seeded, provider, wall):
    _, query = start(seeded, provider)
    callback(seeded, code=CODE, state=query['state'])
    with seeded.session_transaction() as sess:
        sess.pop('_user_id')   # the OIDC start turns a signed-in browser away
        sess[KEY] = wall.now - 5 * DAY
    wall.now += 30
    _, query = start(seeded, provider)
    callback(seeded, code=CODE, state=query['state'])
    assert session_of(seeded)[KEY] == wall.now


def test_a_stale_time_in_a_signed_out_session_is_replaced_at_sign_in(free_app, wall):
    client = signed_in(free_app)
    client.post('/auth/logout')
    set_sign_in_time(client, wall.now - 100 * DAY)
    wall.now += 5
    client.post('/auth/login', data={'username': ADMIN['username'],
                                     'password': ADMIN['password']})
    assert session_of(client)[KEY] == wall.now


def test_signing_out_removes_the_sign_in_time(free_app, wall):
    client = signed_in(free_app)
    response = client.post('/auth/logout')
    assert response.status_code == 302
    sess = session_of(client)
    assert '_user_id' not in sess and KEY not in sess


def test_deleting_ones_own_account_removes_the_sign_in_time(free_app, wall):
    client = signed_in(free_app)
    client.post('/auth/logout')
    client.post('/auth/register', data=USER)
    response = client.post('/settings/', data={'action': 'delete_account',
                                               'confirm_delete': 'DELETE'})
    assert response.status_code == 302
    sess = session_of(client)
    assert '_user_id' not in sess and KEY not in sess


def test_a_refused_sign_in_stores_no_time(free_app, wall):
    client = signed_in(free_app)
    client.post('/auth/logout')
    client.post('/auth/login', data={'username': ADMIN['username'], 'password': 'wrong-pass'})
    sess = session_of(client)
    assert '_user_id' not in sess and KEY not in sess


def test_a_sign_in_failing_after_login_leaves_no_time_without_a_user(free_app, wall,
                                                                     monkeypatch):
    client = signed_in(free_app)
    client.post('/auth/logout')
    free_app.config['PROPAGATE_EXCEPTIONS'] = False

    def broken(self):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(User, 'update_last_login', broken)
    response = client.post('/auth/login', data={'username': ADMIN['username'],
                                                'password': ADMIN['password']})
    assert response.status_code == 500
    sess = session_of(client)
    assert ('_user_id' in sess) == (KEY in sess)


def test_an_anonymous_session_is_never_stamped(free_app, wall):
    client = client_of(free_app)
    client.post('/auth/setup', data=ADMIN)
    client.post('/auth/logout')
    client.get('/auth/login')
    assert KEY not in session_of(client)


def test_an_easyauth_session_is_neither_stamped_nor_checked(tmp_path, wall):
    easy = make_app(tmp_path, 'easy.db', EASYAUTH_ENABLED=True)
    headers = {'X-MS-CLIENT-PRINCIPAL-NAME': 'erin@example.org',
               'X-MS-CLIENT-PRINCIPAL-ID': 'sub-erin'}
    client = easy.test_client()
    assert client.get(PAGE, headers=headers).status_code == 200
    assert KEY not in session_of(client)
    set_sign_in_time(client, wall.now - 5000 * DAY)
    assert client.get(PAGE, headers=headers).status_code == 200
    assert session_of(client)[KEY] == wall.now - 5000 * DAY


# ── The bound: three effective lifetimes ─────────────────────────────────────

LIFETIMES = [
    ('default 30', {}, None, 30),
    ('stored 1', {}, '1', 1),
    ('stored 365', {}, '365', 365),
    ('pinned 1', {'SESSION_LIFETIME_DAYS': 1}, None, 1),
    ('pinned 365', {'SESSION_LIFETIME_DAYS': '365'}, None, 365),
]
OFFSETS = [(-1, True), (0, False), (1, False)]


@pytest.mark.parametrize('config,stored,days', [row[1:] for row in LIFETIMES],
                         ids=[row[0] for row in LIFETIMES])
@pytest.mark.parametrize('offset,kept', OFFSETS,
                         ids=['a second inside', 'at the bound', 'a second past'])
def test_a_sign_in_three_lifetimes_old_is_signed_out(tmp_path, wall, config, stored, days,
                                                     offset, kept):
    flask_app = make_app(tmp_path, **config)
    client = signed_in(flask_app)
    if stored is not None:
        store_lifetime(flask_app, stored)
    signed_in_at = wall.now - 3 * days * DAY - offset
    set_sign_in_time(client, signed_in_at)
    response = client.get(PAGE)
    if kept:
        assert response.status_code == 200
        assert session_of(client)[KEY] == signed_in_at
    else:
        assert_signed_out(client, response)


def test_many_requests_inside_the_bound_never_move_the_time(free_app, wall):
    client = signed_in(free_app)
    signed_in_at = wall.now
    for hours in range(1, 6):
        wall.now = signed_in_at + hours * 3600
        assert client.get(PAGE).status_code == 200
        assert session_of(client)[KEY] == signed_in_at
    wall.now = signed_in_at + 90 * DAY   # used all along, the sign-in still ends
    assert_signed_out(client, client.get(PAGE))


def test_a_shortened_lifetime_shortens_the_bound_from_the_next_request(free_app, wall):
    client = signed_in(free_app)
    set_sign_in_time(client, wall.now - 10 * DAY)
    assert client.get(PAGE).status_code == 200          # 30 days: bound 90
    store_lifetime(free_app, '3')                        # bound 9 days, cache cleared
    assert_signed_out(client, client.get(PAGE))


def test_a_shortened_lifetime_applies_once_the_cache_has_aged(free_app, wall, monkeypatch):
    client = signed_in(free_app)
    tick = [1000.0]
    monkeypatch.setattr(rs, '_clock', lambda: tick[0])
    set_sign_in_time(client, wall.now - 10 * DAY)
    assert client.get(PAGE).status_code == 200
    with free_app.app_context():
        db = get_db()
        auth_service.upsert_global_setting(db, rs.LIFETIME, '3')
        db.commit()
    tick[0] += rs.CACHE_SECONDS - 1
    set_sign_in_time(client, wall.now - 10 * DAY)
    assert client.get(PAGE).status_code == 200          # still the cached 30
    tick[0] += 2
    assert_signed_out(client, client.get(PAGE))


def test_a_longer_lifetime_keeps_a_sign_in_the_old_bound_would_end(free_app, wall):
    client = signed_in(free_app)
    store_lifetime(free_app, '60')
    set_sign_in_time(client, wall.now - 100 * DAY)
    assert client.get(PAGE).status_code == 200


def test_an_unsafe_request_past_the_bound_gets_the_csrf_refusal(free_app, wall):
    client = signed_in(free_app)
    token = client.csrf_token()
    set_sign_in_time(client, wall.now - 90 * DAY, token=token)
    plain = free_app.test_client()
    plain.set_cookie('session', client.get_cookie('session').value)
    response = plain.post('/settings/', data={'action': 'save_preferences'},
                          headers={'X-CSRF-Token': token})
    assert response.status_code == 400
    left = session_of(plain)
    assert '_user_id' not in left and KEY not in left
    response = plain.get(PAGE)
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']


def test_an_expired_session_loses_its_token_pending_sign_in_and_messages(free_app, wall):
    client = signed_in(free_app)
    with client.session_transaction() as sess:
        sess[KEY] = wall.now - 90 * DAY
        sess[oidc.PENDING_KEY] = {'state': 'x'}
        sess[csrf.SESSION_KEY] = 'old-token'
    response = client.get(PAGE)
    assert_signed_out(client, response)
    left = session_of(client)
    assert oidc.PENDING_KEY not in left and left.get(csrf.SESSION_KEY) != 'old-token'
    assert all(message != 'old' for _, message in left.get('_flashes', []))


def test_an_expired_session_on_a_static_file_has_its_cookie_deleted(free_app, wall):
    client = signed_in(free_app)
    set_sign_in_time(client, wall.now - 90 * DAY)
    response = client.get('/static/vendor/jquery.min.js')
    header = [h for h in response.headers.getlist('Set-Cookie') if h.startswith('session=')]
    assert len(header) == 1 and header[0].startswith('session=;'), header
    assert client.get_cookie('session') is None


def test_an_expired_session_with_an_old_remember_cookie_removes_both(free_app, wall):
    client = signed_in(free_app)
    set_sign_in_time(client, wall.now - 90 * DAY)
    client.set_cookie(rs.REMEMBER_COOKIE, 'old-token')
    response = client.get(PAGE)
    assert_signed_out(client, response)
    assert client.get_cookie(rs.REMEMBER_COOKIE) is None


# ── Sign-in times that are missing or not a whole number ─────────────────────

ODD = [
    ('missing', None, True),
    ('None', None, False),
    ('True', True, False),
    ('False', False, False),
    ('text of digits', 'NOW-60', False),
    ('empty text', '', False),
    ('lone surrogate', '\ud800', False),
    ('float', 'NOW-60.0', False),
    ('float far past', -1.5e9, False),
    ('list', [1], False),
    ('nested', {'a': [{'b': 1}]}, False),
    ('far future', 'NOW+1e9', False),
    ('a second ahead', 'NOW+1', False),
    ('huge', 10 ** 30, False),
    # Flask's tagged session serializer round-trips these types: a future format change.
    ('tagged datetime', datetime(2026, 1, 1, tzinfo=timezone.utc), False),
    ('tagged bytes', b'1700000000', False),
    ('tagged tuple', (1700000000,), False),
    ('tagged uuid', uuid.UUID(int=1), False),
    ('tagged markup', Markup('1700000000'), False),
    ('float infinity', float('inf'), False),
    ('float -infinity', float('-inf'), False),
    ('float nan', float('nan'), False),
]


STEP = 7   # seconds between writing the sign-in time and the request under test


def _resolve(value, now):
    if value == 'NOW-60':
        return str(now - 60)
    if value == 'NOW-60.0':
        return float(now - 60)
    if value == 'NOW+1e9':
        return now + 10 ** 9
    if value == 'NOW+1':
        return now + STEP + 1     # a second ahead of the request's now
    return value


@pytest.mark.parametrize('value,drop', [row[1:] for row in ODD], ids=[row[0] for row in ODD])
def test_a_missing_or_odd_sign_in_time_is_kept_and_stamped_with_now(free_app, wall, value,
                                                                    drop):
    client = signed_in(free_app)
    set_sign_in_time(client, _resolve(value, wall.now), drop=drop)
    wall.now += STEP
    response = client.get(PAGE)
    assert response.status_code == 200
    assert session_cookie_set(response)
    stamped = session_of(client)[KEY]
    assert stamped == wall.now and type(stamped) is int
    wall.now += 90 * DAY - 1                             # the bound counts from the stamp
    assert client.get(PAGE).status_code == 200
    wall.now += 1
    assert_signed_out(client, client.get(PAGE))


@pytest.mark.parametrize('value', [0, -1, -(10 ** 30)], ids=['zero', 'negative', 'huge negative'])
def test_a_whole_sign_in_time_long_past_is_signed_out(free_app, wall, value):
    client = signed_in(free_app)
    set_sign_in_time(client, value)
    assert_signed_out(client, client.get(PAGE))


# ── Dependencies, corruption, concurrency ────────────────────────────────────

@pytest.mark.parametrize('age_days,kept', [(89, True), (90, False)],
                         ids=['inside the default bound', 'at the default bound'])
def test_a_failed_settings_read_applies_the_default_bound(free_app, wall, monkeypatch,
                                                          age_days, kept):
    client = signed_in(free_app)
    store_lifetime(free_app, '365')
    set_sign_in_time(client, wall.now - age_days * DAY)

    def locked(key, default=None):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(auth_service, 'get_global_setting', locked)
    rs.forget_cached_lifetime(free_app)      # the transaction above cached the stored 365
    response = client.get('/auth/login')     # a page that reads no setting of its own
    if kept:
        assert response.status_code == 302 and response.headers['Location'].endswith(PAGE)
    else:
        assert response.status_code == 200   # the login page, signed out
        left = session_of(client)
        assert '_user_id' not in left and KEY not in left


def test_a_cookie_signed_with_another_key_signs_nobody_in(free_app, tmp_path, wall):
    data = session_of(signed_in(free_app))
    stranger = make_app(tmp_path, 'other.db', SECRET_KEY='another-key')
    serializer = stranger.session_interface.get_signing_serializer(stranger)
    client = free_app.test_client()
    client.set_cookie('session', serializer.dumps(dict(data, **{KEY: wall.now})))
    response = client.get(PAGE)
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']


def test_two_tabs_of_one_session_are_signed_out_together(free_app, wall):
    client = signed_in(free_app)
    set_sign_in_time(client, wall.now - 90 * DAY)
    cookie = client.get_cookie('session').value
    tabs = [free_app.test_client(), free_app.test_client()]
    for tab in tabs:
        tab.set_cookie('session', cookie)
    for tab in tabs:
        assert_signed_out(tab, tab.get(PAGE))


@pytest.mark.parametrize('age_days,kept', [(89, True), (90, False)],
                         ids=['inside the bound', 'at the bound'])
def test_two_worker_processes_read_the_same_sign_in_time(tmp_path, wall, age_days, kept):
    first = make_app(tmp_path, 'shared.db')
    second = make_app(tmp_path, 'shared.db')
    client = signed_in(first)
    set_sign_in_time(client, wall.now - age_days * DAY)
    cookie = client.get_cookie('session').value
    for worker in (first, second):
        other = worker.test_client()
        other.set_cookie('session', cookie)
        assert (other.get(PAGE).status_code == 200) is kept


def test_the_sign_in_helper_stores_no_time_when_the_login_is_refused(free_app, wall):
    """No sign-in time without a signed-in user: Flask-Login's login_user refuses an inactive
    account (it returns False and stores no user), so the helper must store no time either."""
    with free_app.app_context():
        user = User.create(username='idle', password='pw-12345678', email=None)
    user._is_active = False
    with free_app.test_request_context('/'):
        from flask import session
        auth_service.sign_in(session, user, remember=True)
        assert '_user_id' not in session
        assert KEY not in session, dict(session)
