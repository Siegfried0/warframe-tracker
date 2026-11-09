"""OpenID Connect sign-in: the whole flow, every refusal, and the pending sign-in.

Each refusal is one row of a table and must end on the login page with one message, sign
nobody in, write nothing and log no token, code or secret.
"""
import hashlib
import logging
import time

import pytest
import requests

from conftest import ADMIN, TokenClient
from oidc_fixtures import (  # noqa: F401  (the fixtures are found by name)
    _UNSET, CLIENT_ID, CODE, DEACTIVATED, ISSUER, OIDC, REDIRECT_URI, REFUSED, SECRET, NotJson,
    FakeProvider, _assert_no_secret_logged, _claim, _discovery, _make_app, _now, _provider,
    _refused_without_write, b64url, callback, flashes, make_jwt, oidc_app, oidc_client, provider,
    seeded, signed_in_user_id, start, user_rows)


# ── The whole flow ───────────────────────────────────────────────────────────

def test_start_redirects_to_the_authorization_endpoint_with_state_nonce_and_pkce(
        seeded, provider):
    response, query = start(seeded, provider)
    location = response.headers['Location']
    assert location.startswith(provider.discovery['authorization_endpoint'] + '?')
    assert query['response_type'] == 'code'
    assert query['client_id'] == CLIENT_ID
    assert query['redirect_uri'] == REDIRECT_URI
    assert query['scope'] == 'openid email profile'
    assert query['code_challenge_method'] == 'S256'
    assert len(query['state']) >= 32 and len(query['nonce']) >= 32
    with seeded.session_transaction() as sess:
        pending = sess['_oidc_pending']
    assert pending['state'] == query['state'] and pending['nonce'] == query['nonce']
    expected = b64url(hashlib.sha256(pending['verifier'].encode('ascii')).digest())
    assert query['code_challenge'] == expected
    assert provider.gets[0][0] == ISSUER + '/.well-known/openid-configuration'
    assert provider.gets[0][1]['timeout'] > 0


def test_start_keeps_the_query_of_an_authorization_endpoint_that_has_one(seeded, provider):
    provider.discovery['authorization_endpoint'] += '?tenant=home'
    response, query = start(seeded, provider)
    assert '?tenant=home&' in response.headers['Location']
    assert query['tenant'] == 'home'


def test_first_sign_in_creates_the_account_and_the_next_finds_it(seeded, provider):
    _, query = start(seeded, provider)
    response = callback(seeded, code=CODE, state=query['state'])
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dashboard')
    rows = [r for r in user_rows(seeded.application) if r['auth_type'] == 'oidc']
    assert len(rows) == 1
    row = rows[0]
    assert row['username'] == 'tenno' and row['email'] == 'tenno@example.test'
    assert row['oidc_sub'] == 'sub-0001' and row['role'] == 'user'
    assert row['password_hash'] is None and row['last_login'] is not None
    assert signed_in_user_id(seeded) == str(row['id'])
    assert seeded.get('/dashboard').status_code == 200

    seeded.post('/auth/logout')
    assert signed_in_user_id(seeded) is None
    _, query = start(seeded, provider)
    provider.overrides = {'preferred_username': 'renamed', 'email': 'new@example.test'}
    callback(seeded, code=CODE, state=query['state'])
    assert signed_in_user_id(seeded) == str(row['id'])
    after = [r for r in user_rows(seeded.application) if r['auth_type'] == 'oidc']
    assert len(after) == 1 and after[0]['username'] == 'tenno'


def test_the_token_request_sends_the_code_verifier_and_basic_client_authentication(
        seeded, provider):
    _, query = start(seeded, provider)
    callback(seeded, code=CODE, state=query['state'])
    url, kwargs = provider.posts[0]
    assert url == provider.discovery['token_endpoint']
    assert kwargs['timeout'] > 0 and kwargs['allow_redirects'] is False
    data = kwargs['data']
    assert data['grant_type'] == 'authorization_code' and data['code'] == CODE
    assert data['redirect_uri'] == REDIRECT_URI
    challenge = b64url(hashlib.sha256(data['code_verifier'].encode('ascii')).digest())
    assert challenge == query['code_challenge']
    assert kwargs['auth'] == (CLIENT_ID, SECRET)
    assert 'client_secret' not in data


def test_the_token_request_posts_the_secret_when_the_provider_lists_only_post(
        seeded, provider):
    provider.discovery['token_endpoint_auth_methods_supported'] = ['client_secret_post']
    _, query = start(seeded, provider)
    callback(seeded, code=CODE, state=query['state'])
    kwargs = provider.posts[0][1]
    assert kwargs['auth'] is None
    assert kwargs['data']['client_id'] == CLIENT_ID
    assert kwargs['data']['client_secret'] == SECRET
    assert signed_in_user_id(seeded) is not None


def test_sign_in_replaces_the_session_csrf_token(seeded, provider):
    old = seeded.csrf_token()
    _, query = start(seeded, provider)
    callback(seeded, code=CODE, state=query['state'])
    with seeded.session_transaction() as sess:
        assert sess.get('_csrf_token') != old


def test_first_oidc_sign_in_on_an_empty_database_makes_the_admin(oidc_client, provider):
    _, query = start(oidc_client, provider)
    callback(oidc_client, code=CODE, state=query['state'])
    rows = user_rows(oidc_client.application)
    assert len(rows) == 1 and rows[0]['role'] == 'admin' and rows[0]['auth_type'] == 'oidc'

    oidc_client.post('/auth/logout')
    provider.sub = 'sub-0002'
    provider.overrides = {'preferred_username': 'second', 'email': 'second@example.test'}
    _, query = start(oidc_client, provider)
    callback(oidc_client, code=CODE, state=query['state'])
    roles = [r['role'] for r in user_rows(oidc_client.application)]
    assert roles == ['admin', 'user']


def test_a_provider_on_this_machine_may_use_plain_http(tmp_path, monkeypatch):
    from services import oidc
    fake = FakeProvider(issuer='http://127.0.0.1:8080/idp')
    monkeypatch.setattr(oidc.requests, 'get', fake.get)
    monkeypatch.setattr(oidc.requests, 'post', fake.post)
    flask_app = _make_app(tmp_path, **dict(OIDC, OIDC_ISSUER_URL='http://127.0.0.1:8080/idp'))
    client = TokenClient(flask_app, flask_app.response_class, use_cookies=True)
    _, query = start(client, fake)
    callback(client, code=CODE, state=query['state'])
    assert signed_in_user_id(client) is not None


# ── Refusals: one row each ───────────────────────────────────────────────────

CLAIM_REFUSALS = [
    pytest.param(_claim('iss', 'https://evil.example.test'), id='iss-other'),
    pytest.param(_claim('iss', ISSUER + '/'), id='iss-trailing-slash-differs-from-discovery'),
    pytest.param(_claim('iss', _UNSET), id='iss-missing'),
    pytest.param(_claim('aud', 'another-client'), id='aud-other'),
    pytest.param(_claim('aud', ['another-client']), id='aud-list-without-client'),
    pytest.param(_claim('aud', [CLIENT_ID, 'another-client']), id='aud-several-no-azp'),
    pytest.param(_claim('aud', []), id='aud-empty-list'),
    pytest.param(_claim('aud', 7), id='aud-number'),
    pytest.param(_claim('aud', _UNSET), id='aud-missing'),
    pytest.param(_claim('azp', 'another-client'), id='azp-other'),
    pytest.param(lambda p: p.overrides.update(exp=_now() - 120), id='exp-past'),
    pytest.param(lambda p: p.overrides.update(exp=str(_now() + 300)), id='exp-string'),
    pytest.param(_claim('exp', True), id='exp-bool'),
    pytest.param(_claim('exp', _UNSET), id='exp-missing'),
    pytest.param(lambda p: p.overrides.update(iat=_now() + 600), id='iat-future'),
    pytest.param(_claim('iat', _UNSET), id='iat-missing'),
    pytest.param(_claim('nonce', 'another-nonce'), id='nonce-other'),
    pytest.param(_claim('nonce', _UNSET), id='nonce-missing'),
    pytest.param(_claim('sub', ''), id='sub-empty'),
    pytest.param(_claim('sub', '   '), id='sub-blank'),
    pytest.param(_claim('sub', 12345), id='sub-number'),
    pytest.param(_claim('sub', _UNSET), id='sub-missing'),
    pytest.param(_claim('sub', 's' * 256), id='sub-256-chars'),
]

TRANSPORT_REFUSALS = [
    pytest.param(_provider('token_error', requests.Timeout('slow')), id='token-timeout'),
    pytest.param(_provider('token_error', requests.ConnectionError('down')),
                 id='token-connection-error'),
    pytest.param(_provider('token_status', 400), id='token-http-400'),
    pytest.param(_provider('token_status', 302), id='token-redirect'),
    pytest.param(_provider('token_body', NotJson()), id='token-not-json'),
    pytest.param(_provider('token_body', ['id_token']), id='token-json-list'),
    pytest.param(_provider('token_body', {'access_token': 'x'}), id='token-no-id-token'),
    pytest.param(_provider('token_body', {'id_token': 42}), id='id-token-number'),
    pytest.param(_provider('token_body', {'id_token': 'aaa.bbb'}), id='jwt-two-parts'),
    pytest.param(_provider('token_body', {'id_token': 'a.b.c.d'}), id='jwt-four-parts'),
    pytest.param(_provider('token_body', {'id_token': 'aaa..'}), id='jwt-empty-payload'),
    pytest.param(_provider('token_body', {'id_token': 'aaa.!!!!.'}), id='jwt-bad-base64'),
    pytest.param(_provider('token_body', {'id_token': 'aaa.' + b64url(b'not json') + '.'}),
                 id='jwt-payload-not-json'),
    pytest.param(_provider('token_body', {'id_token': 'aaa.' + b64url(b'\xff\xfe') + '.'}),
                 id='jwt-payload-not-utf8'),
    pytest.param(lambda p: p.__setattr__('token_body', {'id_token': make_jwt(['sub'])}),
                 id='jwt-payload-list'),
    pytest.param(_discovery('token_endpoint', 'http://id.example.test/token'),
                 id='token-endpoint-http-off-loopback'),
    pytest.param(_discovery('token_endpoint', _UNSET), id='discovery-no-token-endpoint'),
    pytest.param(_discovery('issuer', 'https://evil.example.test'),
                 id='discovery-other-issuer'),
]


@pytest.mark.parametrize('breaks', CLAIM_REFUSALS + TRANSPORT_REFUSALS)
def test_a_refused_callback_signs_nobody_in_and_writes_nothing(seeded, provider, caplog,
                                                                 breaks):
    _, query = start(seeded, provider)
    before = user_rows(seeded.application)
    breaks(provider)
    with caplog.at_level(logging.DEBUG):
        response = callback(seeded, code=CODE, state=query['state'])
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/auth/login')
    assert signed_in_user_id(seeded) is None
    assert user_rows(seeded.application) == before
    messages = flashes(seeded)
    assert len(messages) == 1 and REFUSED in messages[0]
    assert 'OIDC sign-in refused' in caplog.text
    _assert_no_secret_logged(caplog)


DISCOVERY_REFUSALS = [
    pytest.param(_provider('discovery_error', requests.Timeout('slow')), id='timeout'),
    pytest.param(_provider('discovery_error', requests.ConnectionError('down')),
                 id='connection-error'),
    pytest.param(_provider('discovery_status', 500), id='http-500'),
    pytest.param(_provider('discovery_status', 301), id='redirect'),
    pytest.param(_provider('discovery', NotJson()), id='not-json'),
    pytest.param(_provider('discovery', ['issuer']), id='json-list'),
    pytest.param(_discovery('issuer', _UNSET), id='no-issuer'),
    pytest.param(_discovery('issuer', 'https://evil.example.test'), id='other-issuer'),
    pytest.param(_discovery('authorization_endpoint', _UNSET), id='no-authorization-endpoint'),
    pytest.param(_discovery('authorization_endpoint', 'http://id.example.test/auth'),
                 id='authorization-endpoint-http-off-loopback'),
    pytest.param(_discovery('authorization_endpoint', 'javascript:alert(1)'),
                 id='authorization-endpoint-javascript'),
    pytest.param(_discovery('token_endpoint', 'ftp://id.example.test/token'),
                 id='token-endpoint-ftp'),
]


@pytest.mark.parametrize('breaks', DISCOVERY_REFUSALS)
def test_a_failed_discovery_at_the_start_ends_on_the_login_page(seeded, provider, caplog,
                                                                  breaks):
    breaks(provider)
    with caplog.at_level(logging.DEBUG):
        response = seeded.get('/auth/oidc/login')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/auth/login')
    assert any(REFUSED in m for m in flashes(seeded))
    with seeded.session_transaction() as sess:
        assert '_oidc_pending' not in sess
    _assert_no_secret_logged(caplog)


def test_an_issuer_off_loopback_without_https_is_refused_before_any_request(
        tmp_path, provider):
    flask_app = _make_app(tmp_path, **dict(OIDC, OIDC_ISSUER_URL='http://id.example.test'))
    client = TokenClient(flask_app, flask_app.response_class, use_cookies=True)
    response = client.get('/auth/oidc/login')
    assert response.headers['Location'].endswith('/auth/login')
    assert provider.gets == []


# ── The pending sign-in: state, expiry, replay ───────────────────────────────

def test_a_callback_without_a_pending_sign_in_is_refused(seeded, provider):
    before = user_rows(seeded.application)
    response = callback(seeded, code=CODE, state='made-up')
    _refused_without_write(seeded, response, before)
    assert provider.posts == []


@pytest.mark.parametrize('params', [
    pytest.param({'code': CODE}, id='state-missing'),
    pytest.param({'code': CODE, 'state': 'another-state'}, id='state-other'),
    pytest.param({'code': CODE, 'state': ''}, id='state-empty'),
    pytest.param({'state': None}, id='code-missing'),
    pytest.param({'error': 'access_denied', 'state': None}, id='provider-error'),
    pytest.param({'error': 'access_denied', 'code': CODE, 'state': None},
                 id='provider-error-with-code'),
])
def test_a_callback_with_a_wrong_state_an_error_or_no_code_is_refused(seeded, provider,
                                                                        params):
    _, query = start(seeded, provider)
    before = user_rows(seeded.application)
    params = {k: (query['state'] if k == 'state' and v is None else v)
              for k, v in params.items()}
    response = callback(seeded, **params)
    _refused_without_write(seeded, response, before)
    assert provider.posts == []
    with seeded.session_transaction() as sess:
        assert '_oidc_pending' not in sess  # used once, whatever the outcome


def test_a_callback_after_ten_minutes_is_refused(seeded, provider, monkeypatch):
    from services import oidc
    _, query = start(seeded, provider)
    before = user_rows(seeded.application)
    later = time.time() + 601
    monkeypatch.setattr(oidc.time, 'time', lambda: later)
    response = callback(seeded, code=CODE, state=query['state'])
    _refused_without_write(seeded, response, before)


def test_a_callback_just_inside_ten_minutes_is_accepted(seeded, provider, monkeypatch):
    from services import oidc
    _, query = start(seeded, provider)
    later = time.time() + 590
    provider.overrides = {'exp': int(later) + 300, 'iat': int(later)}
    monkeypatch.setattr(oidc.time, 'time', lambda: later)
    callback(seeded, code=CODE, state=query['state'])
    assert signed_in_user_id(seeded) is not None


def test_a_replayed_callback_is_refused(seeded, provider):
    _, query = start(seeded, provider)
    callback(seeded, code=CODE, state=query['state'])
    user_id = signed_in_user_id(seeded)
    seeded.post('/auth/logout')
    before = user_rows(seeded.application)
    response = callback(seeded, code=CODE, state=query['state'])
    _refused_without_write(seeded, response, before)
    assert user_id is not None and len(provider.posts) == 1


def test_only_the_last_of_two_starts_is_accepted(seeded, provider):
    _, first = start(seeded, provider)
    _, second = start(seeded, provider)
    before = user_rows(seeded.application)
    response = callback(seeded, code=CODE, state=first['state'])
    _refused_without_write(seeded, response, before)

    _, first = start(seeded, provider)
    _, second = start(seeded, provider)
    callback(seeded, code=CODE, state=second['state'])
    assert signed_in_user_id(seeded) is not None


def test_a_sign_out_drops_the_pending_sign_in(seeded, provider):
    _, query = start(seeded, provider)
    seeded.post('/auth/login', data={'username': ADMIN['username'],
                                     'password': ADMIN['password']})
    seeded.post('/auth/logout')
    before = user_rows(seeded.application)
    response = callback(seeded, code=CODE, state=query['state'])
    _refused_without_write(seeded, response, before)


def test_a_deactivated_account_is_refused_and_not_touched(seeded, provider):
    _, query = start(seeded, provider)
    callback(seeded, code=CODE, state=query['state'])
    seeded.post('/auth/logout')
    from models.user import User
    with seeded.application.app_context():
        User.get_oidc_account('sub-0001').set_active(False)
    before = user_rows(seeded.application)
    _, query = start(seeded, provider)
    response = callback(seeded, code=CODE, state=query['state'])
    assert response.headers['Location'].endswith('/auth/login')
    assert signed_in_user_id(seeded) is None
    assert user_rows(seeded.application) == before
    assert any(DEACTIVATED in m for m in flashes(seeded))
