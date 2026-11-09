"""The fake identity provider and the readers the OIDC sign-in tests share.

The provider's two requests (discovery, token) are mocked in ``services.oidc``; ID tokens are
unsigned JWTs built here. Nothing reaches the network. A test module imports the fixtures
(``provider``, ``oidc_app``, ``oidc_client``, ``seeded``) by name.
"""
import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

import app as app_module
from conftest import ADMIN, TokenClient

ISSUER = 'https://id.example.test/realms/home'
CLIENT_ID = 'tracker-client'
SECRET = 'client-secret-do-not-log-7f3a'
REDIRECT_URI = 'https://tracker.example.test/auth/oidc/callback'
CODE = 'auth-code-do-not-log-91b2'
OIDC = {
    'OIDC_CLIENT_ID': CLIENT_ID,
    'OIDC_CLIENT_SECRET': SECRET,
    'OIDC_ISSUER_URL': ISSUER,
    'OIDC_REDIRECT_URI': REDIRECT_URI,
}
OIDC_OFF = {name: None for name in OIDC}
REFUSED = 'Sign-in with your identity provider did not complete'
DEACTIVATED = 'This account has been deactivated'
_UNSET = object()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def make_jwt(claims) -> str:
    """An unsigned JWT ('alg': 'none') carrying ``claims`` (any JSON value)."""
    header = b64url(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())
    payload = b64url(json.dumps(claims).encode('utf-8'))
    return f'{header}.{payload}.'


class NotJson:
    """A response body that is not JSON."""


class RawBody:
    """A response body given as bytes, parsed by a real ``requests.Response``: whatever its
    ``json()`` raises on them is what the sign-in sees."""

    def __init__(self, content: bytes):
        self.content = content


def _real_response(status_code, content: bytes):
    response = requests.Response()
    response.status_code = status_code
    response._content = content
    response.encoding = None
    return response


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, NotJson):
            raise ValueError('not JSON')
        return self._body


def _response(status_code, body):
    if isinstance(body, RawBody):
        return _real_response(status_code, body.content)
    return FakeResponse(status_code, body)


class FakeProvider:
    """The identity provider's two endpoints. Tests change its fields to misbehave."""

    def __init__(self, issuer=ISSUER):
        self.discovery = {
            'issuer': issuer,
            'authorization_endpoint': issuer + '/protocol/openid-connect/auth',
            'token_endpoint': issuer + '/protocol/openid-connect/token',
        }
        self.discovery_status = 200
        self.discovery_error = None
        self.token_status = 200
        self.token_error = None
        self.token_body = None      # None: {'id_token': make_jwt(self.claims())}
        self.overrides = {}         # claim -> value, or _UNSET to drop the claim
        self.nonce = None           # read from the authorization URL by start()
        self.sub = 'sub-0001'
        self.gets, self.posts = [], []

    def claims(self):
        now = int(time.time())
        claims = {'iss': self.discovery['issuer'], 'aud': CLIENT_ID, 'sub': self.sub,
                  'exp': now + 300, 'iat': now, 'nonce': self.nonce,
                  'preferred_username': 'tenno', 'email': 'tenno@example.test'}
        for key, value in self.overrides.items():
            if value is _UNSET:
                claims.pop(key, None)
            else:
                claims[key] = value
        return claims

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if self.discovery_error:
            raise self.discovery_error
        return _response(self.discovery_status, self.discovery)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.token_error:
            raise self.token_error
        body = self.token_body if self.token_body is not None else {
            'id_token': make_jwt(self.claims()), 'access_token': 'access-do-not-log',
            'token_type': 'Bearer'}
        return _response(self.token_status, body)


@pytest.fixture
def provider(monkeypatch):
    from services import oidc
    fake = FakeProvider()
    monkeypatch.setattr(oidc.requests, 'get', fake.get)
    monkeypatch.setattr(oidc.requests, 'post', fake.post)
    return fake


def _make_app(tmp_path, **overrides):
    config = {'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'test.db'),
              'SYNC_INTERVAL_HOURS': 0, 'AUTO_SYNC_ON_STARTUP': False,
              'SECRET_KEY': 'test-only', 'EASYAUTH_ENABLED': False}
    config.update(overrides)
    return app_module.create_app(config)


@pytest.fixture
def oidc_app(tmp_path):
    return _make_app(tmp_path, **OIDC)


@pytest.fixture
def oidc_client(oidc_app):
    return TokenClient(oidc_app, oidc_app.response_class, use_cookies=True)


@pytest.fixture
def seeded(oidc_client):
    """An OIDC-enabled app whose database already has the local admin."""
    assert oidc_client.post('/auth/setup', data=ADMIN).status_code == 302
    oidc_client.post('/auth/logout')
    with oidc_client.session_transaction() as sess:
        sess.pop('_flashes', None)  # the setup's and the logout's messages
    return oidc_client


def start(client, provider, path='/auth/oidc/login'):
    response = client.get(path)
    assert response.status_code == 302, response.status_code
    query = parse_qs(urlsplit(response.headers['Location']).query)
    provider.nonce = query['nonce'][0]
    return response, {k: v[0] for k, v in query.items()}


def callback(client, **params):
    return client.get('/auth/oidc/callback', query_string=params)


def signed_in_user_id(client):
    with client.session_transaction() as sess:
        return sess.get('_user_id')


def flashes(client):
    with client.session_transaction() as sess:
        return [message for _, message in sess.get('_flashes', [])]


def user_rows(app):
    from models.db import get_db
    with app.app_context():
        return [dict(r) for r in get_db().execute(
            'SELECT id, username, email, role, auth_type, oidc_sub, is_active, password_hash, '
            'last_login FROM users ORDER BY id').fetchall()]



def _claim(key, value):
    def apply(provider):
        provider.overrides[key] = value
    return apply


def _provider(attr, value):
    def apply(provider):
        setattr(provider, attr, value)
    return apply


def _discovery(key, value):
    def apply(provider):
        if value is _UNSET:
            provider.discovery.pop(key)
        else:
            provider.discovery[key] = value
    return apply


def _now():
    return int(time.time())



def _assert_no_secret_logged(caplog):
    text = caplog.text
    for secret in (SECRET, CODE, 'access-do-not-log', 'eyJ'):
        assert secret not in text



def _refused_without_write(client, response, before):
    assert response.headers['Location'].endswith('/auth/login')
    assert signed_in_user_id(client) is None
    assert user_rows(client.application) == before
    assert any(REFUSED in m for m in flashes(client))



def _sign_in(client, provider, **overrides):
    provider.overrides = overrides
    _, query = start(client, provider)
    callback(client, code=CODE, state=query['state'])


def _oidc_rows(app):
    return [r for r in user_rows(app) if r['auth_type'] == 'oidc']
