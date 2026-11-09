"""OpenID Connect sign-in: accounts, claim spellings, configuration and the pages.

An OIDC sign-in only ever matches an OIDC row with the same sub; a username, email or sub
another row holds refuses it.
"""
import logging
import time

import pytest

from conftest import ADMIN, TokenClient
from oidc_fixtures import (  # noqa: F401  (the fixtures are found by name)
    _UNSET, CLIENT_ID, CODE, ISSUER, OIDC, OIDC_OFF, SECRET, FakeProvider, _make_app,
    _oidc_rows, _refused_without_write, _sign_in, callback, oidc_app, oidc_client, provider,
    seeded, signed_in_user_id, start, user_rows)


# ── Accounts: only an OIDC row with the same sub ─────────────────────────────

def _holder(application, **fields):
    from models.user import User
    with application.app_context():
        User.create(**fields)


@pytest.mark.parametrize('holder, overrides', [
    pytest.param({'username': 'tenno', 'password': 'x' * 8}, {}, id='local-username'),
    pytest.param({'username': 'holder', 'password': 'x' * 8, 'email': 'tenno@example.test'},
                 {}, id='local-email'),
    pytest.param({'username': 'sub-0001', 'password': 'x' * 8},
                 {'preferred_username': _UNSET, 'email': _UNSET}, id='local-named-like-sub'),
    pytest.param({'username': 'azure-user', 'auth_type': 'easyauth', 'oidc_sub': 'sub-0001'},
                 {}, id='easyauth-same-sub'),
])
def test_an_account_another_row_holds_refuses_the_sign_in(seeded, provider, holder,
                                                           overrides):
    """A username, email or sub another row holds: no other row is matched, none is made."""
    _holder(seeded.application, **holder)
    before = user_rows(seeded.application)
    _, query = start(seeded, provider)
    for key, value in overrides.items():
        provider.overrides[key] = value
    response = callback(seeded, code=CODE, state=query['state'])
    _refused_without_write(seeded, response, before)


USERNAME_SPELLINGS = [
    pytest.param({'preferred_username': 'Ünïcødé-テンノ'}, 'Ünïcødé-テンノ', id='non-ascii'),
    pytest.param({'preferred_username': 'x' * 300}, 'x' * 64, id='very-long-cut-to-64'),
    pytest.param({'preferred_username': '  spaced\tname\n '}, 'spaced name', id='blanks'),
    pytest.param({'preferred_username': '   '}, 'tenno@example.test', id='blank-then-email'),
    pytest.param({'preferred_username': 7}, 'tenno@example.test', id='number-then-email'),
    pytest.param({'preferred_username': _UNSET}, 'tenno@example.test', id='missing-then-email'),
    pytest.param({'preferred_username': _UNSET, 'email': _UNSET}, 'sub-0001', id='then-sub'),
]


@pytest.mark.parametrize('overrides, username', USERNAME_SPELLINGS)
def test_the_username_comes_from_the_token(seeded, provider, overrides, username):
    _sign_in(seeded, provider, **overrides)
    assert [r['username'] for r in _oidc_rows(seeded.application)] == [username]


@pytest.mark.parametrize('email, stored', [
    pytest.param('é@example.test', 'é@example.test', id='non-ascii'),
    pytest.param('a' * 250 + '@example.test', ('a' * 250 + '@example.test')[:254],
                 id='very-long-cut-to-254'),
    pytest.param(['x@example.test'], None, id='list'),
    pytest.param('', None, id='empty'),
])
def test_the_email_comes_from_the_token(seeded, provider, email, stored):
    _sign_in(seeded, provider, email=email, preferred_username='p')
    assert [r['email'] for r in _oidc_rows(seeded.application)] == [stored]


def test_two_oidc_accounts_made_on_an_empty_database_give_one_admin(oidc_app):
    from models.user import User
    with oidc_app.app_context():
        User.create_oidc_account('one', None, 'sub-a')
        User.create_oidc_account('two', None, 'sub-b')
    assert [r['role'] for r in user_rows(oidc_app)] == ['admin', 'user']


def test_password_reset_is_refused_for_an_oidc_account(seeded, provider):
    _sign_in(seeded, provider)
    oidc_id = _oidc_rows(seeded.application)[0]['id']
    seeded.post('/auth/logout')
    seeded.post('/auth/login', data={'username': ADMIN['username'],
                                     'password': ADMIN['password']})
    response = seeded.post(f'/admin/user/{oidc_id}/reset-password',
                           json={'password': 'new-password-1'})
    assert response.status_code == 403
    assert _oidc_rows(seeded.application)[0]['password_hash'] is None


# ── Claim spellings accepted ─────────────────────────────────────────────────

@pytest.mark.parametrize('overrides', [
    pytest.param(lambda now: {'aud': [CLIENT_ID]}, id='aud-list-of-one'),
    pytest.param(lambda now: {'aud': [CLIENT_ID, 'api'], 'azp': CLIENT_ID}, id='aud-several-with-azp'),
    pytest.param(lambda now: {'azp': CLIENT_ID}, id='azp-equal'),
    pytest.param(lambda now: {'exp': now + 300.5, 'iat': now}, id='float-times'),
    pytest.param(lambda now: {'exp': int(now) - 30}, id='exp-within-leeway'),
    pytest.param(lambda now: {'iat': int(now) + 30}, id='iat-within-leeway'),
    pytest.param(lambda now: {'sub': 's' * 255}, id='sub-255-chars'),
])
def test_claim_spellings_that_are_accepted(seeded, provider, overrides):
    # Times are taken when the test runs, not when the table is collected.
    _sign_in(seeded, provider, **overrides(time.time()))
    assert signed_in_user_id(seeded) is not None


@pytest.mark.parametrize('configured, provider_issuer', [
    pytest.param(ISSUER + '/', ISSUER, id='configured-with-slash'),
    pytest.param(ISSUER, ISSUER + '/', id='provider-with-slash'),
])
def test_the_issuer_matches_with_or_without_a_trailing_slash(tmp_path, monkeypatch,
                                                             configured, provider_issuer):
    from services import oidc
    fake = FakeProvider(issuer=provider_issuer)
    monkeypatch.setattr(oidc.requests, 'get', fake.get)
    monkeypatch.setattr(oidc.requests, 'post', fake.post)
    flask_app = _make_app(tmp_path, **dict(OIDC, OIDC_ISSUER_URL=configured))
    client = TokenClient(flask_app, flask_app.response_class, use_cookies=True)
    _, query = start(client, fake)
    callback(client, code=CODE, state=query['state'])
    assert signed_in_user_id(client) is not None
    assert fake.gets[0][0] == ISSUER + '/.well-known/openid-configuration'


@pytest.mark.parametrize('url, allowed', [
    ('https://id.example.test', True),
    ('HTTPS://id.example.test/x', True),
    ('http://localhost:8080', True),
    ('http://127.0.0.1', True),
    ('http://127.8.9.10/idp', True),
    ('http://[::1]:9000', True),
    ('http://id.example.test', False),
    ('http://10.0.0.5', False),
    ('http://localhost.evil.test', False),
    ('ftp://id.example.test', False),
    ('https://', False),
    ('https:///path', False),
    ('javascript:alert(1)', False),
    ('http://[::1', False),
    ('', False),
    (None, False),
])
def test_which_provider_urls_are_allowed(url, allowed):
    from services.oidc import url_allowed
    assert url_allowed(url) is allowed


# ── Configuration and the pages ──────────────────────────────────────────────

def test_with_oidc_on_the_login_and_setup_pages_offer_it(oidc_client):
    page = oidc_client.get('/auth/setup').get_data(as_text=True)
    assert '/auth/oidc/login' in page
    oidc_client.post('/auth/setup', data=ADMIN)
    oidc_client.post('/auth/logout')
    page = oidc_client.get('/auth/login').get_data(as_text=True)
    assert 'Sign in with OpenID Connect' in page and '/auth/oidc/login' in page


def test_with_oidc_off_no_page_offers_it_and_its_routes_are_404(client):
    page = client.get('/auth/setup').get_data(as_text=True)
    assert '/auth/oidc/login' not in page
    client.post('/auth/setup', data=ADMIN)
    client.post('/auth/logout')
    assert '/auth/oidc/login' not in client.get('/auth/login').get_data(as_text=True)
    assert client.get('/auth/oidc/login').status_code == 404
    assert client.get('/auth/oidc/callback?code=x&state=y').status_code == 404


@pytest.mark.parametrize('present', [
    pytest.param(('OIDC_CLIENT_ID',), id='one'),
    pytest.param(('OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET', 'OIDC_ISSUER_URL'), id='three'),
])
def test_a_partial_configuration_leaves_oidc_off_and_names_the_missing(tmp_path, caplog,
                                                                       present):
    settings = {name: (OIDC[name] if name in present else None) for name in OIDC}
    with caplog.at_level(logging.WARNING):
        flask_app = _make_app(tmp_path, **settings)
    missing = [name for name in OIDC if name not in present]
    warning = [r.getMessage() for r in caplog.records if 'OIDC sign-in is off' in r.getMessage()]
    assert len(warning) == 1
    assert all(name in warning[0] for name in missing)
    assert SECRET not in caplog.text and ISSUER not in caplog.text and CLIENT_ID not in caplog.text
    client = TokenClient(flask_app, flask_app.response_class, use_cookies=True)
    assert client.get('/auth/oidc/login').status_code == 404


def test_a_blank_setting_counts_as_missing(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        flask_app = _make_app(tmp_path, **dict(OIDC, OIDC_CLIENT_SECRET='   '))
    assert 'OIDC_CLIENT_SECRET' in caplog.text
    client = TokenClient(flask_app, flask_app.response_class, use_cookies=True)
    assert client.get('/auth/oidc/login').status_code == 404


def test_no_warning_when_oidc_is_not_configured_at_all(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        _make_app(tmp_path, **OIDC_OFF)
    assert 'OIDC' not in caplog.text


def test_with_easyauth_the_oidc_routes_go_to_azure_and_no_button_shows(tmp_path, provider):
    flask_app = _make_app(tmp_path, **dict(OIDC, EASYAUTH_ENABLED=True))
    client = TokenClient(flask_app, flask_app.response_class, use_cookies=True)
    for path in ('/auth/oidc/login', '/auth/oidc/callback?code=x&state=y'):
        response = client.get(path)
        assert response.status_code == 302 and response.headers['Location'] == '/.auth/login'
    from services import oidc
    assert oidc.is_enabled(flask_app.config) is False
    with flask_app.test_request_context('/'):
        flask_app.update_template_context(context := {})
        assert context['oidc_enabled'] is False
    assert provider.gets == []


def test_a_signed_in_user_starting_oidc_goes_to_the_dashboard(seeded, provider):
    seeded.post('/auth/login', data={'username': ADMIN['username'],
                                     'password': ADMIN['password']})
    response = seeded.get('/auth/oidc/login')
    assert response.headers['Location'].endswith('/dashboard')
    assert provider.gets == []
