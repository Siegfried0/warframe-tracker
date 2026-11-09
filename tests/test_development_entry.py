"""The development entries (``python app.py``, ``python run.py``) listen on loopback, with the
debugger off unless asked for, and never serve the debugger beyond loopback.

One function decides the host and debug mode for both entries: 127.0.0.1 unless
FLASK_RUN_HOST names another address (only with a usable SECRET_KEY), debug only for
FLASK_DEBUG ``1`` or ``true``, and debug with a non-loopback host refuses to start. No test
here starts a server: Flask's ``run`` is replaced by a recorder, or the entry refuses first.
"""
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import flask
import pytest

import app as app_module
import config

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / 'app'
KEY = 'an-owner-chosen-key'

# Axis 1: FLASK_DEBUG spellings -> debug on?
DEBUG_SPELLINGS = [
    pytest.param(None, False, id='unset'),
    pytest.param('', False, id='empty'),
    pytest.param(' ', False, id='blank'),
    pytest.param('1', True, id='one'),
    pytest.param(' 1 ', True, id='one-padded'),
    pytest.param('true', True, id='true'),
    pytest.param('TRUE', True, id='true-upper'),
    pytest.param('\tTrue\n', True, id='true-mixed-case-padded'),
    pytest.param('0', False, id='zero'),
    pytest.param('false', False, id='false'),
    pytest.param('yes', False, id='yes'),
    pytest.param('on', False, id='on'),
    pytest.param('2', False, id='two'),
    pytest.param('1 1', False, id='one-twice'),
]

# Axis 1: FLASK_RUN_HOST spellings -> (host served with a usable key, is loopback).
HOSTS = [
    pytest.param(None, '127.0.0.1', True, id='unset'),
    pytest.param('', '127.0.0.1', True, id='empty'),
    pytest.param(' \t', '127.0.0.1', True, id='blank'),
    pytest.param('127.0.0.1', '127.0.0.1', True, id='ipv4-loopback'),
    pytest.param(' 127.0.0.1 ', '127.0.0.1', True, id='ipv4-loopback-padded'),
    pytest.param('localhost', 'localhost', True, id='localhost'),
    pytest.param('LocalHost', 'LocalHost', True, id='localhost-mixed-case'),
    pytest.param('::1', '::1', True, id='ipv6-loopback'),
    pytest.param('0.0.0.0', '0.0.0.0', False, id='all-ipv4'),
    pytest.param('::', '::', False, id='all-ipv6'),
    pytest.param('192.168.1.20', '192.168.1.20', False, id='lan-address'),
    pytest.param(' 0.0.0.0\n', '0.0.0.0', False, id='all-ipv4-padded'),
    pytest.param('127.0.0.2', '127.0.0.2', False, id='other-127-address'),
]


def _env(host, debug):
    env = {}
    if host is not None:
        env['FLASK_RUN_HOST'] = host
    if debug is not None:
        env['FLASK_DEBUG'] = debug
    return env


@pytest.mark.parametrize('value, expected', DEBUG_SPELLINGS)
def test_debug_is_on_only_for_one_or_true(value, expected):
    run = app_module.development_run_settings(KEY, environ=_env(None, value))
    assert run.debug is expected
    assert run.host == '127.0.0.1'


@pytest.mark.parametrize('requested, served, loopback', HOSTS)
def test_with_a_key_the_requested_host_is_served_without_debug(requested, served, loopback):
    run = app_module.development_run_settings(KEY, environ=_env(requested, None))
    assert run == (served, None, False, None)


@pytest.mark.parametrize('requested, served, loopback', HOSTS)
def test_without_a_key_the_host_stays_loopback_and_an_ignored_host_is_named(
        requested, served, loopback):
    run = app_module.development_run_settings(None, environ=_env(requested, '1'))
    assert run.host == '127.0.0.1' and run.debug is True
    assert config.secret_key_is_usable(run.key)
    ignored = 'FLASK_RUN_HOST is set but was ignored' in run.warning
    assert ignored is (served != '127.0.0.1')


@pytest.mark.parametrize('requested, served, loopback', HOSTS)
def test_debug_beyond_loopback_refuses_naming_both_settings(requested, served, loopback):
    environ = _env(requested, 'true')
    if loopback:
        assert app_module.development_run_settings(KEY, environ=environ).host == served
        return
    with pytest.raises(app_module.DevelopmentRunRefused) as refused:
        app_module.development_run_settings(KEY, environ=environ)
    message = str(refused.value)
    assert 'FLASK_DEBUG' in message and 'FLASK_RUN_HOST' in message
    assert KEY not in message
    message.encode('ascii')


def test_the_settings_read_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv('FLASK_RUN_HOST', 'localhost')
    monkeypatch.setenv('FLASK_DEBUG', '1')
    assert app_module.development_run_settings(KEY) == ('localhost', None, True, None)


# --- both entries call the one decision -----------------------------------------------------

@pytest.fixture
def recorded_entry(monkeypatch, tmp_path):
    """Flask's server replaced by a recorder; the decision wrapped to count its calls."""
    monkeypatch.setattr(config.Config, 'SECRET_KEY', KEY)
    monkeypatch.setattr(config.Config, 'DATABASE_PATH', str(tmp_path / 'entry.db'))
    monkeypatch.setattr(config.Config, 'SYNC_INTERVAL_HOURS', 0)
    monkeypatch.setattr(config.Config, 'AUTO_SYNC_ON_STARTUP', False)
    monkeypatch.setenv('FLASK_RUN_HOST', 'localhost')
    monkeypatch.setenv('FLASK_DEBUG', 'TRUE')
    served = []
    monkeypatch.setattr(flask.Flask, 'run', lambda self, **kw: served.append(kw))
    return served


def test_python_app_py_serves_what_the_decision_says(recorded_entry):
    app_module.run_development_server()
    assert recorded_entry == [{'host': 'localhost', 'port': 5000, 'debug': True}]


def test_python_run_py_runs_the_same_entry_from_the_project_root(recorded_entry, monkeypatch):
    """run.py runs app/app.py as its __main__: the same decision, no copy of the rule."""
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(sys, 'argv', list(sys.argv))
    monkeypatch.setattr(sys, 'orig_argv', list(sys.orig_argv))
    monkeypatch.chdir(ROOT)
    runpy.run_path(str(ROOT / 'run.py'), run_name='__main__')
    assert recorded_entry == [{'host': 'localhost', 'port': 5000, 'debug': True}]
    assert Path.cwd() == APP_DIR


def test_neither_entry_passes_its_own_host_or_debug():
    for path in (ROOT / 'run.py', APP_DIR / 'app.py'):
        source = path.read_text(encoding='utf-8')
        assert '0.0.0.0' not in source.replace('gunicorn --bind=0.0.0.0', '')
        assert 'debug=True' not in source




# --- a fresh interpreter: the real `python app.py` / `python run.py` --------------------------
# A sitecustomize on PYTHONPATH replaces Flask's server with a printer before the entry runs,
# so no case here can start a server, even with the refusal broken. The printer also asks
# Werkzeug how its reloader (debug mode) would start the entry again, from the entry's
# working directory, and whether that restart would find the script.

RECORDER = '''
import json, os
import flask
from werkzeug import _reloader

def _recorded_run(self, host=None, port=None, debug=None, **kwargs):
    restart = _reloader._get_args_for_reloading()
    print('RUN ' + json.dumps({'host': host, 'port': port, 'debug': debug,
                               'restart_finds_script': os.path.isfile(restart[-1])}))

flask.Flask.run = _recorded_run
'''

ENTRIES = [
    pytest.param('app.py', APP_DIR, id='python-app-py'),
    pytest.param('run.py', ROOT, id='python-run-py'),
]


def _run_script(script, cwd, tmp_path, key=KEY, host=None, debug=None):
    double = tmp_path / 'double'
    double.mkdir()
    (double / 'sitecustomize.py').write_text(RECORDER, encoding='utf-8')
    env = {k: v for k, v in os.environ.items()
           if k not in ('SECRET_KEY', 'MSSQL_CONNECTION_STRING', 'EASYAUTH_ENABLED',
                        'FLASK_RUN_HOST', 'FLASK_DEBUG', 'PYTHONPATH')}
    env.update(_env(host, debug), PYTHONPATH=str(double), DATABASE_PATH=str(tmp_path / 'dev.db'),
               SYNC_INTERVAL_HOURS='0', AUTO_SYNC_ON_STARTUP='false', PYTHONIOENCODING='utf-8')
    if key is not None:
        env['SECRET_KEY'] = key
    result = subprocess.run([sys.executable, script], cwd=str(cwd), capture_output=True,
                            text=True, timeout=120, env=env)
    runs = [json.loads(line[4:]) for line in result.stdout.splitlines() if line.startswith('RUN ')]
    return result, runs


@pytest.mark.parametrize('script, cwd', ENTRIES)
def test_an_entry_asked_for_debug_beyond_loopback_exits_before_the_database(
        script, cwd, tmp_path):
    result, runs = _run_script(script, cwd, tmp_path, host='0.0.0.0', debug='1')
    assert result.returncode == 1, result.stderr
    assert runs == []
    assert 'FLASK_DEBUG' in result.stderr and 'FLASK_RUN_HOST' in result.stderr
    assert 'Traceback' not in result.stderr
    assert KEY not in result.stderr + result.stdout
    assert not (tmp_path / 'dev.db').exists()


@pytest.mark.parametrize('script, cwd', ENTRIES)
def test_an_entry_with_nothing_set_serves_loopback_without_debug(script, cwd, tmp_path):
    result, runs = _run_script(script, cwd, tmp_path)
    assert result.returncode == 0, result.stderr
    assert [{k: r[k] for k in ('host', 'port', 'debug')} for r in runs] == [
        {'host': '127.0.0.1', 'port': 5000, 'debug': False}]


@pytest.mark.parametrize('script, cwd', ENTRIES)
def test_an_entry_in_debug_on_loopback_can_restart_itself(script, cwd, tmp_path):
    result, runs = _run_script(script, cwd, tmp_path, host='localhost', debug='true')
    assert result.returncode == 0, result.stderr
    assert runs == [{'host': 'localhost', 'port': 5000, 'debug': True,
                     'restart_finds_script': True}]


@pytest.mark.parametrize('script, cwd', ENTRIES)
def test_an_entry_without_a_key_ignores_the_host_and_says_so(script, cwd, tmp_path):
    result, runs = _run_script(script, cwd, tmp_path, key=None, host='0.0.0.0')
    assert result.returncode == 0, result.stderr
    assert [r['host'] for r in runs] == ['127.0.0.1']
    assert result.stderr.count('FLASK_RUN_HOST is set but was ignored') == 1
