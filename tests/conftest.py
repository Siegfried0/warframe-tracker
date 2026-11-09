"""Shared fixtures: every test gets its own app on its own temporary database, offline.

The app's modules import as top-level names from app/ (``from models.db import ...``), and
importing ``app`` runs ``create_app()`` at import time. So app/ goes first on sys.path, and
DATABASE_PATH names a throwaway database (and SECRET_KEY a random key) before the first
import: no test ever opens the user's database.
"""
import os
import secrets
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

APP_DIR = Path(__file__).resolve().parent.parent / 'app'

# The temp root carries the prefix `warframe-tests-`: the run-start sweep removes a root with
# that prefix that a killed run left behind.
_IMPORT_ROOT = Path(tempfile.mkdtemp(prefix='warframe-tests-'))
_IMPORT_DB = _IMPORT_ROOT / 'import.db'
os.environ['DATABASE_PATH'] = str(_IMPORT_DB)
os.environ['SYNC_INTERVAL_HOURS'] = '0'
os.environ['AUTO_SYNC_ON_STARTUP'] = 'false'
# The app refuses to start without a usable SECRET_KEY (importing it starts it): a random one
# for this run, whatever the shell running the tests has set.
os.environ['SECRET_KEY'] = secrets.token_hex(32)
os.environ.pop('MSSQL_CONNECTION_STRING', None)
os.environ.pop('EASYAUTH_ENABLED', None)
os.environ.pop('SESSION_COOKIE_SAMESITE', None)
sys.path.insert(0, str(APP_DIR))

import app as app_module  # noqa: E402  (sys.path and the environment must be set first)

ADMIN = {'username': 'admin', 'password': 'correct-horse-9', 'confirm': 'correct-horse-9'}


def pytest_unconfigure(config):
    shutil.rmtree(_IMPORT_ROOT, ignore_errors=True)

_LOOPBACK = {'127.0.0.1', '::1', 'localhost'}
_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in _LOOPBACK:
        raise RuntimeError(f'tests must not reach the network (tried {address!r})')
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Refuse every non-loopback connection: external services are mocked, never called."""
    monkeypatch.setattr(socket.socket, 'connect', _guarded_connect)


@pytest.fixture(autouse=True)
def image_cache_root(tmp_path, monkeypatch):
    """Point the picture cache at a temporary folder: no test reads, writes or deletes the
    pictures the app downloaded into its own static folder."""
    from services import image_cache
    root = tmp_path / 'images'
    monkeypatch.setattr(image_cache, 'cache_root', lambda: root)
    return root


@pytest.fixture
def app(tmp_path):
    """A fresh app on an empty database of its own (migrations applied, no scheduler)."""
    flask_app = app_module.create_app({
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'test.db'),
        'SYNC_INTERVAL_HOURS': 0,
        'AUTO_SYNC_ON_STARTUP': False,
        'SECRET_KEY': 'test-only',
    })
    yield flask_app
    scheduler = getattr(flask_app, 'scheduler', None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)


class TokenClient(FlaskClient):
    """A test client that sends the session's CSRF token on every unsafe request, in the
    X-CSRF-Token header, as the app's own pages do (the hidden field or app.js). A request
    that sets the header itself keeps it. Tests of the refusal use ``app.test_client()``,
    which sends no token."""

    def open(self, *args, **kwargs):
        method = str(kwargs.get('method') or 'GET').upper()
        plain = not args or isinstance(args[0], str)
        if plain and method not in ('GET', 'HEAD', 'OPTIONS'):
            headers = Headers(kwargs.pop('headers', None) or {})
            if 'X-CSRF-Token' not in headers:
                headers['X-CSRF-Token'] = self.csrf_token()
            kwargs['headers'] = headers
        return super().open(*args, **kwargs)

    def csrf_token(self) -> str:
        """The session's token, put into the session first if it has none yet (as the first
        page the browser loads would)."""
        from services import csrf
        with self.session_transaction() as sess:
            token = sess.get(csrf.SESSION_KEY)
            if not token:
                token = secrets.token_urlsafe(32)
                sess[csrf.SESSION_KEY] = token
        return token


@pytest.fixture
def client(app):
    """The app's client, sending the session's CSRF token like the app's pages."""
    return TokenClient(app, app.response_class, use_cookies=True)


@pytest.fixture
def admin_client(client):
    """A client logged in as the admin the first-run wizard creates."""
    response = client.post('/auth/setup', data=ADMIN)
    assert response.status_code == 302, response.status_code
    return client
