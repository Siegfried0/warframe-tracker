"""EasyAuth matches only its own rows, and the first admin is chosen inside the insert.

An EasyAuth sign-in is the row with auth_type 'easyauth' and that principal id; a local or
OIDC row holding the same value is never signed in. The first account on an empty database,
EasyAuth or local setup, is decided by the insert itself, never by a count read before it.
"""
import pytest

import app as app_module
from conftest import ADMIN, TokenClient

ERIN = {'X-MS-CLIENT-PRINCIPAL-NAME': 'erin@example.org', 'X-MS-CLIENT-PRINCIPAL-ID': 'sub-erin'}
FINN = {'X-MS-CLIENT-PRINCIPAL-NAME': 'finn@example.org', 'X-MS-CLIENT-PRINCIPAL-ID': 'sub-finn'}


@pytest.fixture
def easy_app(tmp_path):
    return app_module.create_app({
        'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'easy.db'), 'SYNC_INTERVAL_HOURS': 0,
        'AUTO_SYNC_ON_STARTUP': False, 'SECRET_KEY': 'test-only', 'EASYAUTH_ENABLED': True,
    })


def _rows(application):
    from models.db import get_db
    with application.app_context():
        return [dict(r) for r in get_db().execute(
            'SELECT id, username, email, role, auth_type, oidc_sub FROM users ORDER BY id'
        ).fetchall()]


def _insert(application, username, auth_type, sub, role='user'):
    from models.db import get_db
    with application.app_context():
        db = get_db()
        db.execute('INSERT INTO users (username, role, auth_type, oidc_sub) VALUES (?, ?, ?, ?)',
                   (username, role, auth_type, sub))
        db.commit()


def _signed_in(client):
    with client.session_transaction() as sess:
        return sess.get('_user_id')


def test_the_first_easyauth_account_is_the_admin_and_the_next_a_user(easy_app):
    assert easy_app.test_client().get('/dashboard', headers=ERIN).status_code == 200
    assert easy_app.test_client().get('/dashboard', headers=FINN).status_code == 200
    assert [(r['username'], r['role'], r['auth_type'], r['oidc_sub'])
            for r in _rows(easy_app)] == [
        ('erin@example.org', 'admin', 'easyauth', 'sub-erin'),
        ('finn@example.org', 'user', 'easyauth', 'sub-finn')]


def test_a_returning_easyauth_user_is_signed_in_to_the_same_row(easy_app):
    _insert(easy_app, 'erin@example.org', 'easyauth', 'sub-erin', role='admin')
    before = _rows(easy_app)
    client = easy_app.test_client()
    assert client.get('/dashboard', headers=ERIN).status_code == 200
    assert _signed_in(client) == str(before[0]['id'])
    assert _rows(easy_app) == before


@pytest.mark.parametrize('auth_type', ['oidc', 'local'])
def test_a_principal_id_another_kind_of_account_holds_never_signs_it_in(easy_app, auth_type):
    _insert(easy_app, 'holder', auth_type, 'sub-erin', role='admin')
    before = _rows(easy_app)
    client = easy_app.test_client()
    response = client.get('/dashboard', headers=ERIN)
    assert response.status_code == 403
    assert 'cannot be matched to an account' in response.get_data(as_text=True)
    assert _signed_in(client) is None
    assert _rows(easy_app) == before


def test_a_principal_name_another_account_holds_refuses_and_writes_nothing(easy_app):
    _insert(easy_app, 'erin@example.org', 'easyauth', 'sub-someone-else', role='admin')
    before = _rows(easy_app)
    client = easy_app.test_client()
    assert client.get('/dashboard', headers=ERIN).status_code == 403
    assert _signed_in(client) is None
    assert _rows(easy_app) == before


def test_the_same_principal_created_at_the_same_moment_gives_the_one_account(
        easy_app, monkeypatch):
    """A parallel first request of the principal inserts between this one's lookup and
    insert: the insert fails on the unique id and the row the other made is signed in."""
    from models.user import User
    real = User.get_easyauth_account.__func__
    calls = []

    def lookup(cls, principal_id):
        calls.append(principal_id)
        if len(calls) == 1:
            _insert(easy_app, 'erin@example.org', 'easyauth', principal_id, role='admin')
            return None
        return real(cls, principal_id)

    monkeypatch.setattr(User, 'get_easyauth_account', classmethod(lookup))
    client = easy_app.test_client()
    assert client.get('/dashboard', headers=ERIN).status_code == 200
    rows = _rows(easy_app)
    assert [(r['username'], r['role']) for r in rows] == [('erin@example.org', 'admin')]
    assert _signed_in(client) == str(rows[0]['id'])


def test_a_first_easyauth_account_made_after_another_appeared_is_a_user(easy_app,
                                                                         monkeypatch):
    """Another principal's first request inserts after this one found no row: the role is
    decided inside this insert, so this account is a user, not a second admin."""
    from models.user import User
    real = User.get_easyauth_account.__func__
    calls = []

    def lookup(cls, principal_id):
        calls.append(principal_id)
        if len(calls) == 1:
            _insert(easy_app, 'finn@example.org', 'easyauth', 'sub-finn', role='admin')
        return real(cls, principal_id)

    monkeypatch.setattr(User, 'get_easyauth_account', classmethod(lookup))
    assert easy_app.test_client().get('/dashboard', headers=ERIN).status_code == 200
    assert [(r['username'], r['role']) for r in _rows(easy_app)] == [
        ('finn@example.org', 'admin'), ('erin@example.org', 'user')]


# ── Local setup: the admin or nothing ────────────────────────────────────────

def test_a_second_setup_racing_the_first_is_refused_and_writes_nothing(app, monkeypatch):
    """Both setups saw an empty database; the first made the admin. The second inserts
    nothing (no second admin, no user) and signs nobody in."""
    from routes import auth
    first = TokenClient(app, app.response_class, use_cookies=True)
    assert first.post('/auth/setup', data=ADMIN).status_code == 302
    before = _rows(app)
    monkeypatch.setattr(auth, '_first_run_needed', lambda: True)
    second = TokenClient(app, app.response_class, use_cookies=True)
    response = second.post('/auth/setup', data={'username': 'second', 'password': 'p' * 10,
                                                'confirm': 'p' * 10})
    assert response.status_code == 302 and response.headers['Location'].endswith('/auth/login')
    assert _signed_in(second) is None
    assert _rows(app) == before


class _ReadCommittedDb:
    """The connection as MSSQL under READ COMMITTED sees it for one racing setup: the
    insert's "no user yet" check still reads an empty table, so the insert goes ahead and
    the backend's own unique constraint refuses it."""

    def __init__(self, db):
        self._db = db

    def execute(self, sql, params=()):
        return self._db.execute(sql.replace('WHERE NOT EXISTS (SELECT 1 FROM users)', ''),
                                params)

    def __getattr__(self, name):
        return getattr(self._db, name)


@pytest.mark.parametrize('form', [
    pytest.param({'username': ADMIN['username']}, id='username-taken-by-the-other-setup'),
    pytest.param({'username': 'second', 'email': 'admin@example.org'},
                 id='email-taken-by-the-other-setup'),
])
def test_a_setup_failing_on_a_unique_column_is_refused_and_writes_nothing(app, monkeypatch,
                                                                         form):
    from models import user as user_module
    from routes import auth
    first = TokenClient(app, app.response_class, use_cookies=True)
    assert first.post('/auth/setup', data=dict(ADMIN, email='admin@example.org')
                      ).status_code == 302
    before = _rows(app)
    monkeypatch.setattr(auth, '_first_run_needed', lambda: True)
    real_get_db = user_module.get_db
    monkeypatch.setattr(user_module, 'get_db', lambda: _ReadCommittedDb(real_get_db()))
    second = TokenClient(app, app.response_class, use_cookies=True)
    response = second.post('/auth/setup', data=dict(form, password='p' * 10, confirm='p' * 10))
    assert response.status_code == 302 and response.headers['Location'].endswith('/auth/login')
    with second.session_transaction() as sess:
        assert [m for _, m in sess.get('_flashes', [])] == [
            'The administrator account already exists. Sign in.']
    assert _signed_in(second) is None
    assert _rows(app) == before


def test_setup_after_a_first_oidc_account_appeared_is_refused(app, monkeypatch):
    from routes import auth
    _insert(app, 'oidc-first', 'oidc', 'sub-0001', role='admin')
    before = _rows(app)
    monkeypatch.setattr(auth, '_first_run_needed', lambda: True)
    client = TokenClient(app, app.response_class, use_cookies=True)
    client.post('/auth/setup', data=ADMIN)
    assert _signed_in(client) is None
    assert _rows(app) == before


def test_setup_on_an_empty_database_still_makes_the_admin(client):
    response = client.post('/auth/setup', data=ADMIN)
    assert response.status_code == 302 and response.headers['Location'].endswith('/dashboard')
    rows = _rows(client.application)
    assert [(r['username'], r['role'], r['auth_type']) for r in rows] == [
        (ADMIN['username'], 'admin', 'local')]
    assert _signed_in(client) == str(rows[0]['id'])
