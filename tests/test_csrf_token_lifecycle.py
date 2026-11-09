"""The CSRF token's lifecycle: it is replaced at once by the response that signs in or out
(so tabs that reload together afterwards share one token and the old one is refused),
deleting one's own account signs out the same way, and a SameSite setting the cookie cannot
carry is refused when the app starts, never with a 500 on every page.
"""
import re

import pytest

import app as app_module
from models.db import get_db
from models.item import Item
from models.user import User
from services import csrf

ADMIN = {'username': 'admin', 'password': 'correct-horse-9', 'confirm': 'correct-horse-9'}
EASY = {'X-MS-CLIENT-PRINCIPAL-NAME': 'erin@example.org', 'X-MS-CLIENT-PRINCIPAL-ID': 'sub-erin'}
TOKEN_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


def page_token(client, path='/settings/'):
    response = client.get(path)
    match = TOKEN_RE.search(response.get_data(as_text=True))
    assert match, f'{path} has no csrf-token meta tag'
    return match.group(1)


def _signed_in(app):
    """A plain client (it sends no token by itself) signed in as the first-run admin, with an
    item in the database; returns (client, its token)."""
    raw = app.test_client()
    token = page_token(raw, '/auth/setup')
    response = raw.post('/auth/setup', data={**ADMIN, 'csrf_token': token})
    assert response.status_code == 302, response.status_code
    with app.app_context():
        Item.upsert('Braton', 'primary', source='standard')
    return raw, page_token(raw)


def _is_refusal(response):
    if response.status_code != 400:
        return False
    if response.is_json:
        return response.get_json() == {'error': csrf.REFUSED}
    return csrf.REFUSED in response.get_data(as_text=True)


def _watch(app, client, token):
    with app.app_context():
        item_id = get_db().execute("SELECT id FROM items WHERE name = 'Braton'").fetchone()[0]
    return client.post('/market/watch', headers={'X-CSRF-Token': token}, json={'item_id': item_id})


def _snapshot(app):
    """Every row of every table: a refused request leaves all of it as it was."""
    with app.app_context():
        db = get_db()
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {t: sorted(tuple(r) for r in db.execute(f'SELECT * FROM "{t}"'))
                for t in tables}


@pytest.fixture
def easy_app(tmp_path):
    return app_module.create_app({
        'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'easy.db'), 'SYNC_INTERVAL_HOURS': 0,
        'AUTO_SYNC_ON_STARTUP': False, 'SECRET_KEY': 'test-only', 'EASYAUTH_ENABLED': True,
    })


# ── The SameSite setting: every spelling starts cleanly or is refused at start ─────────────

def _config(tmp_path, **extra):
    return {'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'samesite.db'),
            'SYNC_INTERVAL_HOURS': 0, 'AUTO_SYNC_ON_STARTUP': False, 'SECRET_KEY': 'test-only',
            **extra}


SAMESITE_SPELLINGS = [
    ('Strict', 'Strict'), ('strict', 'Strict'), ('STRICT', 'Strict'), ('sTrIcT', 'Strict'),
    ('Lax', 'Lax'), ('lax', 'Lax'), ('LAX', 'Lax'), (' Lax', 'Lax'), ('Lax ', 'Lax'),
    ('\tlax\n', 'Lax'), ('None', 'None'), ('none', 'None'), ('  NONE  ', 'None'),
    ('', 'Lax'), ('   ', 'Lax'), ('\t\r\n', 'Lax'), (None, 'Lax'),
    ('off', None), ('yes', None), ('true', None), ('0', None), ('Laxx', None), ('La x', None),
    ('Lax;', None), ('Lax; Secure', None), ('Strict,Lax', None), ('"Lax"', None),
    ('Nonee', None), ('Kax', None), (1, None), (True, None),
]


@pytest.mark.parametrize('value,expected', SAMESITE_SPELLINGS)
def test_every_spelling_of_the_samesite_setting_starts_cleanly_or_is_refused_at_start(
        tmp_path, value, expected):
    if expected is None:
        with pytest.raises(app_module.SettingError) as refused:
            app_module.create_app(_config(tmp_path, SESSION_COOKIE_SAMESITE=value))
        message = str(refused.value)
        assert 'SESSION_COOKIE_SAMESITE' in message
        assert all(name in message for name in ('Strict', 'Lax', 'None'))
        assert not (tmp_path / 'samesite.db').exists()  # refused before anything was opened
        return
    flask_app = app_module.create_app(_config(tmp_path, SESSION_COOKIE_SAMESITE=value))
    assert flask_app.config['SESSION_COOKIE_SAMESITE'] == expected
    for path in ('/auth/setup', '/auth/login'):
        response = flask_app.test_client().get(path)
        assert response.status_code in (200, 302), (path, response.status_code)
    cookie = flask_app.test_client().get('/auth/setup').headers.get('Set-Cookie', '')
    assert f'SameSite={expected}' in cookie


def test_the_development_entry_exits_with_the_samesite_message(monkeypatch):
    def refuse(_overrides=None):
        raise app_module.SettingError('SESSION_COOKIE_SAMESITE refused')
    monkeypatch.setattr(app_module, 'create_app', refuse)
    with pytest.raises(SystemExit) as stopped:
        app_module.run_development_server()
    assert 'SESSION_COOKIE_SAMESITE' in str(stopped.value)


# ── The token is replaced at once at every sign-in and sign-out ────────────────────────────

def _token_in_session(client):
    with client.session_transaction() as sess:
        return sess.get(csrf.SESSION_KEY)


def _do_setup(app):
    client = app.test_client()
    token = page_token(client, '/auth/setup')
    response = client.post('/auth/setup', data={**ADMIN, 'csrf_token': token})
    with app.app_context():
        Item.upsert('Braton', 'primary', source='standard')
    return client, response, token


def _do_login(app):
    _signed_in(app)
    client = app.test_client()
    token = page_token(client, '/auth/login')
    return client, client.post('/auth/login', data={'username': 'admin',
                                                    'password': ADMIN['password'],
                                                    'csrf_token': token}), token


def _do_register(app):
    _signed_in(app)
    client = app.test_client()
    token = page_token(client, '/auth/register')
    return client, client.post('/auth/register', data={
        'username': 'dora', 'password': 'pw-dora-1234', 'confirm': 'pw-dora-1234',
        'csrf_token': token}), token


def _do_logout(app):
    client, token = _signed_in(app)
    return client, client.post('/auth/logout', data={'csrf_token': token}), token


def _do_delete_account(app):
    client, token = _signed_in(app)
    return client, client.post('/settings/', data={'csrf_token': token,
                                                   'action': 'delete_account',
                                                   'confirm_delete': 'DELETE'}), token


@pytest.mark.parametrize('sign', [_do_setup, _do_login, _do_register, _do_logout,
                                  _do_delete_account])
def test_the_response_that_signs_in_or_out_already_carries_the_new_token(app, sign):
    client, response, old = sign(app)
    assert response.status_code == 302, response.status_code
    new = _token_in_session(client)  # read before any page is rendered
    assert isinstance(new, str) and new and new != old
    assert _is_refusal(_watch(app, client, old))


def test_easyauth_a_sign_in_response_already_carries_the_new_token(easy_app):
    client = easy_app.test_client()
    client.get('/auth/login')  # a session of its own, signed in as nobody
    with client.session_transaction() as sess:
        sess[csrf.SESSION_KEY] = 'before-the-sign-in'
    client.get('/static/css/app.css', headers=EASY)  # a static file signs nobody in
    assert _token_in_session(client) == 'before-the-sign-in'
    response = client.get('/auth/login', headers=EASY)  # redirects; the sign-in ran first
    assert response.status_code == 302
    new = _token_in_session(client)
    assert new and new != 'before-the-sign-in'


def test_tabs_that_reload_together_after_a_logout_share_one_token(app):
    client, token = _signed_in(app)
    client.post('/auth/logout', data={'csrf_token': token})
    cookie = client.get_cookie('session').value
    tabs = [app.test_client() for _ in range(2)]
    for tab in tabs:  # both reload with the cookie the logout left, before either answer
        tab.set_cookie('session', cookie)
    tokens = [page_token(tab, '/auth/login') for tab in tabs]
    assert tokens[0] == tokens[1] != token
    for tab, tab_token in zip(tabs, tokens):  # each tab's form is accepted, none refused
        response = tab.post('/auth/login', data={'username': 'admin',
                                                 'password': ADMIN['password'],
                                                 'csrf_token': tab_token})
        assert response.status_code == 302, response.status_code


def test_deleting_ones_account_replaces_the_token(app):
    client, token = _signed_in(app)
    response = client.post('/settings/', data={'csrf_token': token, 'action': 'delete_account',
                                               'confirm_delete': 'DELETE'})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/auth/login')
    with client.session_transaction() as sess:
        assert '_user_id' not in sess
        assert sess.get(csrf.SESSION_KEY) not in (None, '', token)
    with app.app_context():
        assert User.count() == 0
    assert client.get('/settings/').status_code == 302  # signed out: to the login page


@pytest.mark.parametrize('form', [
    {'action': 'delete_account', 'confirm_delete': 'DELETE'},        # no token
    {'action': 'delete_account', 'confirm_delete': 'DELETE', 'csrf_token': 'stale'},
])
def test_a_refused_account_deletion_deletes_nothing_and_keeps_the_session(app, form):
    client, token = _signed_in(app)
    before = _snapshot(app)
    assert _is_refusal(client.post('/settings/', data=form))
    assert _snapshot(app) == before
    assert _token_in_session(client) == token
    with client.session_transaction() as sess:
        assert '_user_id' in sess


def test_an_unconfirmed_account_deletion_keeps_the_account_and_the_token(app):
    client, token = _signed_in(app)
    before = _snapshot(app)
    response = client.post('/settings/', data={'csrf_token': token, 'action': 'delete_account',
                                               'confirm_delete': 'delete'})
    assert response.status_code == 302
    assert _snapshot(app) == before
    assert _token_in_session(client) == token


def test_easyauth_deleting_ones_account_signs_out_at_azure(easy_app):
    client = easy_app.test_client()
    token = TOKEN_RE.search(client.get('/settings/', headers=EASY).get_data(as_text=True))[1]
    response = client.post('/settings/', headers={**EASY, 'X-CSRF-Token': token},
                           data={'action': 'delete_account', 'confirm_delete': 'DELETE'})
    assert response.status_code == 302
    assert response.headers['Location'] == '/.auth/logout'
    with client.session_transaction() as sess:
        assert '_user_id' not in sess
        assert sess.get(csrf.SESSION_KEY) not in (None, '', token)
