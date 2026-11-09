"""Shared fixtures: every test gets its own app on its own temporary database, offline.

The app's modules import as top-level names from app/ (``from models.db import ...``), and
importing ``app`` runs ``create_app()`` at import time. So app/ goes first on sys.path, and
DATABASE_PATH names a throwaway database before the first import: no test ever opens the
user's database.
"""
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / 'app'

# The temp root carries the prefix `warframe-tests-`: the run-start sweep removes a root with
# that prefix that a killed run left behind.
_IMPORT_ROOT = Path(tempfile.mkdtemp(prefix='warframe-tests-'))
_IMPORT_DB = _IMPORT_ROOT / 'import.db'
os.environ['DATABASE_PATH'] = str(_IMPORT_DB)
os.environ['SYNC_INTERVAL_HOURS'] = '0'
os.environ['AUTO_SYNC_ON_STARTUP'] = 'false'
os.environ.pop('MSSQL_CONNECTION_STRING', None)
os.environ.pop('EASYAUTH_ENABLED', None)
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


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_client(client):
    """A client logged in as the admin the first-run wizard creates."""
    response = client.post('/auth/setup', data=ADMIN)
    assert response.status_code == 302, response.status_code
    return client
