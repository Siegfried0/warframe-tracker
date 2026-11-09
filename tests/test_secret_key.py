"""The app does not start with a missing, blank or published SECRET_KEY.

Sessions and the form tokens are signed with the key; the value the app once fell back to is
published with the source, so a start with it (or with none) would let anyone forge a login.
``create_app()`` refuses such a key before it opens the database; the one exemption is the
development entry (``python app.py``), which then runs with a random key on 127.0.0.1 only.

The environment's key reaches the app through ``Config.SECRET_KEY`` (read when ``config`` is
imported), so the in-process tests set that attribute; the subprocess tests at the end start
a fresh interpreter with the real environment variable.
"""
import os
import subprocess
import sys
from pathlib import Path

import flask
import pytest

import app as app_module
import config
from config import PUBLISHED_DEFAULT_SECRET_KEY as PUBLISHED
from config import SecretKeyError

APP_DIR = Path(__file__).resolve().parent.parent / 'app'

# Axis 1 (input spellings): every key the app must refuse.
REFUSED = [
    pytest.param(None, id='unset'),
    pytest.param('', id='empty'),
    pytest.param(' ', id='one-space'),
    pytest.param('    ', id='spaces-only'),
    pytest.param('\t', id='tab-only'),
    pytest.param('\n', id='newline-only'),
    pytest.param(' \t\r\n ', id='mixed-blanks'),
    pytest.param(' 　', id='unicode-blanks'),
    pytest.param(PUBLISHED, id='published-default'),
    pytest.param(' ' + PUBLISHED, id='published-default-leading-space'),
    pytest.param(PUBLISHED + '\n', id='published-default-trailing-newline'),
    pytest.param('\t' + PUBLISHED + '  \r\n', id='published-default-both-sides'),
    pytest.param(b'', id='bytes-empty'),
    pytest.param(b'  ', id='bytes-blank'),
    pytest.param(PUBLISHED.encode(), id='bytes-published-default'),
    pytest.param(12345, id='not-a-string'),
]

# Every other non-blank key starts the app (no strength rule).
ACCEPTED = [
    pytest.param('x', id='one-character'),
    pytest.param(PUBLISHED.upper(), id='published-default-other-case'),
    pytest.param('Dev-Change-Me-In-Production', id='published-default-title-case'),
    pytest.param(PUBLISHED + '2', id='published-default-with-suffix'),
    pytest.param('dev change me in production', id='published-default-spaced'),
    pytest.param('a3f1' * 16, id='hex-64'),
    pytest.param(' padded-key ', id='key-with-surrounding-blanks'),
    pytest.param(b'\xff\x00\xfe', id='bytes-not-utf8'),
    pytest.param(b'bytes-key', id='bytes-key'),
]


def _base_overrides(tmp_path):
    return {
        'TESTING': True,
        'DATABASE_PATH': str(tmp_path / 'test.db'),
        'SYNC_INTERVAL_HOURS': 0,
        'AUTO_SYNC_ON_STARTUP': False,
    }


def _assert_refusal_message(exc_info, value):
    message = str(exc_info.value)
    assert 'SECRET_KEY' in message
    assert 'secrets.token_hex(32)' in message
    assert PUBLISHED not in message
    assert PUBLISHED.upper() not in message.upper()
    if isinstance(value, str) and value.strip():
        assert value.strip() not in message
    message.encode('ascii')  # readable on every console, Windows code pages included


@pytest.mark.parametrize('value', REFUSED)
def test_create_app_refuses_an_unusable_key_from_the_environment(value, tmp_path, monkeypatch):
    monkeypatch.setattr(config.Config, 'SECRET_KEY', value)
    with pytest.raises(SecretKeyError) as exc_info:
        app_module.create_app(_base_overrides(tmp_path))
    assert not (tmp_path / 'test.db').exists()
    _assert_refusal_message(exc_info, value)


@pytest.mark.parametrize('value', REFUSED)
def test_create_app_refuses_an_unusable_key_from_overrides_over_a_usable_environment(
        value, tmp_path, monkeypatch):
    monkeypatch.setattr(config.Config, 'SECRET_KEY', 'a-usable-environment-key')
    with pytest.raises(SecretKeyError) as exc_info:
        app_module.create_app({**_base_overrides(tmp_path), 'SECRET_KEY': value})
    assert not (tmp_path / 'test.db').exists()
    _assert_refusal_message(exc_info, value)


@pytest.mark.parametrize('value', ACCEPTED)
def test_create_app_starts_with_any_other_key_from_the_environment(value, tmp_path, monkeypatch):
    monkeypatch.setattr(config.Config, 'SECRET_KEY', value)
    flask_app = app_module.create_app(_base_overrides(tmp_path))
    assert flask_app.config['SECRET_KEY'] == value
    assert (tmp_path / 'test.db').exists()


@pytest.mark.parametrize('value', ACCEPTED)
def test_create_app_starts_with_a_usable_override_over_a_refused_environment(
        value, tmp_path, monkeypatch):
    monkeypatch.setattr(config.Config, 'SECRET_KEY', PUBLISHED)
    flask_app = app_module.create_app({**_base_overrides(tmp_path), 'SECRET_KEY': value})
    assert flask_app.config['SECRET_KEY'] == value


def test_the_config_has_no_default_key():
    """With SECRET_KEY unset the config holds no key at all: the refused value is a constant."""
    code = ('import os, sys; os.environ.pop("SECRET_KEY", None); sys.path.insert(0, sys.argv[1]); '
            'import config; print(repr(config.Config.SECRET_KEY))')
    result = subprocess.run([sys.executable, '-c', code, str(APP_DIR)], capture_output=True,
                            text=True, timeout=60, env=_child_env(None, 'unused.db'))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'None'


# --- the development entry (python app.py) ---------------------------------------------------

@pytest.mark.parametrize('value', REFUSED)
def test_development_entry_without_a_usable_key_uses_loopback_and_a_random_key(value):
    host, key, warning = app_module.development_run_settings(value)
    assert host == '127.0.0.1'
    assert isinstance(key, str) and len(key) == 64 and config.secret_key_is_usable(key)
    assert app_module.development_run_settings(value)[1] != key  # fresh for every process
    assert 'SECRET_KEY' in warning and '127.0.0.1' in warning
    assert PUBLISHED not in warning
    warning.encode('ascii')


@pytest.mark.parametrize('value', ACCEPTED)
def test_development_entry_with_a_usable_key_runs_as_before(value):
    assert app_module.development_run_settings(value) == ('0.0.0.0', None, None)


def _run_entry(monkeypatch, tmp_path, configured_key):
    """Run the development entry with Flask's server replaced by a recorder."""
    monkeypatch.setattr(config.Config, 'SECRET_KEY', configured_key)
    monkeypatch.setattr(config.Config, 'DATABASE_PATH', str(tmp_path / 'entry.db'))
    monkeypatch.setattr(config.Config, 'SYNC_INTERVAL_HOURS', 0)
    monkeypatch.setattr(config.Config, 'AUTO_SYNC_ON_STARTUP', False)
    calls = []
    monkeypatch.setattr(flask.Flask, 'run', lambda self, **kw: calls.append((self, kw)))
    app_module.run_development_server()
    assert len(calls) == 1
    return calls[0]


def test_development_server_without_a_key_serves_loopback_with_one_warning(
        monkeypatch, tmp_path, caplog):
    with caplog.at_level('WARNING'):
        flask_app, kwargs = _run_entry(monkeypatch, tmp_path, None)
    assert kwargs['host'] == '127.0.0.1'
    assert kwargs['port'] == 5000
    assert config.secret_key_is_usable(flask_app.config['SECRET_KEY'])
    warnings = [r for r in caplog.records if 'SECRET_KEY' in r.getMessage()]
    assert len(warnings) == 1
    assert flask_app.config['SECRET_KEY'] not in warnings[0].getMessage()


def test_development_server_with_a_key_serves_as_before_without_a_warning(
        monkeypatch, tmp_path, caplog):
    with caplog.at_level('WARNING'):
        flask_app, kwargs = _run_entry(monkeypatch, tmp_path, 'an-owner-chosen-key')
    assert kwargs == {'host': '0.0.0.0', 'port': 5000, 'debug': True}
    assert flask_app.config['SECRET_KEY'] == 'an-owner-chosen-key'
    assert not [r for r in caplog.records if 'SECRET_KEY' in r.getMessage()]


# --- a fresh interpreter: the real environment variable, the import Gunicorn does ----------

def _child_env(secret_key, db_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ('SECRET_KEY', 'MSSQL_CONNECTION_STRING', 'EASYAUTH_ENABLED')}
    env.update(DATABASE_PATH=str(db_path), SYNC_INTERVAL_HOURS='0', AUTO_SYNC_ON_STARTUP='false',
               PYTHONIOENCODING='utf-8')
    if secret_key is not None:
        env['SECRET_KEY'] = secret_key
    return env


def _import_app(secret_key, db_path):
    return subprocess.run(
        [sys.executable, '-c', 'import app; print(type(app.app).__name__)'],
        cwd=str(APP_DIR), capture_output=True, text=True, timeout=120,
        env=_child_env(secret_key, db_path))


@pytest.mark.parametrize('value', [
    pytest.param(None, id='unset'),
    pytest.param('', id='empty'),
    pytest.param('  \t', id='blank'),
    pytest.param(' ' + PUBLISHED + ' ', id='published-default-padded'),
])
def test_importing_the_app_without_a_usable_key_fails_before_the_database(value, tmp_path):
    db = tmp_path / 'gunicorn.db'
    result = _import_app(value, db)
    assert result.returncode != 0
    assert 'SecretKeyError' in result.stderr and 'SECRET_KEY' in result.stderr
    assert PUBLISHED not in result.stderr
    assert not db.exists()


def test_importing_the_app_with_a_key_gives_the_module_level_app(tmp_path):
    db = tmp_path / 'gunicorn.db'
    result = _import_app('a-deployment-key', db)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'Flask'
    assert db.exists()


def test_python_app_py_without_a_key_starts_on_loopback(tmp_path):
    """`python app.py` as a script (``__main__``), Flask's server replaced by a printer."""
    code = ('import runpy, flask; '
            'flask.Flask.run = lambda self, **kw: print("RUN", kw["host"], kw["port"]); '
            'runpy.run_path("app.py", run_name="__main__")')
    db = tmp_path / 'dev.db'
    result = subprocess.run([sys.executable, '-c', code], cwd=str(APP_DIR), capture_output=True,
                            text=True, timeout=120, env=_child_env(None, db))
    assert result.returncode == 0, result.stderr
    assert 'RUN 127.0.0.1 5000' in result.stdout
    assert result.stderr.count('SECRET_KEY is not set') == 1
