"""The Wiki Sync page: the last-sync line it is given, Run Sync Now's question within the hour
after a successful sync (decided by the server), and the answer while a sync runs. The real
sync is replaced: nothing reaches the network."""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

import routes.sync
from models.db import get_db
from services import sync_schedule as ss

FMT = '%Y-%m-%dT%H:%M:%SZ'


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime(FMT)


@pytest.fixture
def runs(monkeypatch):
    """The route's sync, replaced by one that records through the real runner."""
    calls = []

    def fake_run_sync(app):
        calls.append(app)
        return ss.run_recorded(lambda: {'ok': True, 'counts': {}, 'total': 0})

    monkeypatch.setattr(routes.sync, 'run_sync', fake_run_sync)
    return calls


def store(app, **values):
    """Store the values; a timedelta means that long before now, taken when the test runs
    (a time taken when the table is collected would age while the suite runs)."""
    with app.app_context():
        db = get_db()
        for key, value in values.items():
            if isinstance(value, timedelta):
                value = (datetime.now(timezone.utc) - value).strftime(FMT)
            ss.upsert_global_setting(db, key, value)
        db.commit()


def post(client, body=None, raw=None):
    if raw is not None:
        return client.post('/sync/run', data=raw, content_type='application/json')
    if body is None:
        return client.post('/sync/run')
    return client.post('/sync/run', json=body)


# ── The confirmation ─────────────────────────────────────────────────────────

def test_within_the_hour_after_a_success_nothing_starts_without_confirm(app, admin_client, runs):
    finished = ago(minutes=10)
    store(app, sync_finished_at=finished, sync_result='ok')
    response = post(admin_client, {})
    answer = response.get_json()
    assert response.status_code == 200 and runs == []
    assert answer['ok'] is False and answer['needs_confirmation'] is True
    assert answer['finished_at'] == finished and answer['minutes_ago'] in (9, 10)
    assert answer['last_sync']['state'] == 'ok'


@pytest.mark.parametrize('body,raw,confirmed', [
    ({'confirm': True}, None, True),
    ({'confirm': 'true'}, None, False),
    ({'confirm': 1}, None, False),
    ({'confirm': 'yes'}, None, False),
    ({'confirm': None}, None, False),
    ({}, None, False),
    ([True], None, False),
    (None, 'confirm=true', False),
    (None, 'true', False),
    (None, None, False),
], ids=['json true', 'string true', 'one', 'yes', 'null', 'missing', 'a list',
        'not json', 'bare true', 'no body'])
def test_only_json_true_confirms(app, admin_client, runs, body, raw, confirmed):
    store(app, sync_finished_at=ago(minutes=5), sync_result='ok')
    answer = post(admin_client, body, raw).get_json()
    assert (len(runs) == 1) is confirmed
    assert answer.get('needs_confirmation', False) is (not confirmed)


@pytest.mark.parametrize('values,asks', [
    ({'sync_finished_at': timedelta(minutes=59), 'sync_result': 'ok'}, True),
    ({'sync_finished_at': timedelta(minutes=61), 'sync_result': 'ok'}, False),
    ({'sync_finished_at': timedelta(minutes=1), 'sync_result': 'failed'}, False),
    ({}, False),
    ({'sync_started_at': timedelta(minutes=1)}, False),
    ({'sync_finished_at': 'garbage', 'sync_result': 'ok'}, False),
], ids=['ok 59 min ago', 'ok 61 min ago', 'failed', 'never', 'unfinished', 'unreadable'])
def test_run_sync_now_runs_at_once_after_a_failure_or_the_hour(app, admin_client, runs, values,
                                                               asks):
    store(app, **values)
    answer = post(admin_client, {}).get_json()
    assert answer.get('needs_confirmation', False) is asks
    assert len(runs) == (0 if asks else 1)


def test_the_confirmation_answered_twice_runs_two_syncs_in_turn(app, admin_client, runs):
    store(app, sync_finished_at=ago(minutes=5), sync_result='ok')
    first = post(admin_client, {'confirm': True}).get_json()
    second = post(admin_client, {'confirm': True}).get_json()
    assert first['ok'] is True and second['ok'] is True and len(runs) == 2
    # without confirm, the fresh record asks again
    assert post(admin_client, {}).get_json()['needs_confirmation'] is True


def test_a_sync_run_from_the_page_returns_the_new_last_sync(app, admin_client, runs):
    answer = post(admin_client, {}).get_json()
    assert answer['ok'] is True
    line = answer['last_sync']
    assert line['state'] == 'ok' and line['running'] is False
    assert line['finished_at'] is not None and line['unfinished_started_at'] is None


# ── One sync at a time ───────────────────────────────────────────────────────

@pytest.mark.parametrize('body', [{}, {'confirm': True}], ids=['plain', 'confirmed'])
def test_while_a_sync_runs_the_page_gets_409_running(app, admin_client, runs, body):
    assert ss._lock.acquire(blocking=False)
    try:
        response = post(admin_client, body)
    finally:
        ss._lock.release()
    answer = response.get_json()
    assert response.status_code == 409 and runs == []
    assert answer['ok'] is False and answer['running'] is True
    assert answer['last_sync']['running'] is True


def test_a_sync_that_starts_between_the_check_and_the_run_still_answers_409(
        app, admin_client, monkeypatch):
    monkeypatch.setattr(routes.sync, 'run_sync',
                        lambda app: {'ok': False, 'running': True, 'error': ss.RUNNING_ERROR})
    response = post(admin_client, {})
    assert response.status_code == 409 and response.get_json()['running'] is True


# ── The page ─────────────────────────────────────────────────────────────────

def page_state(admin_client):
    html = admin_client.get('/sync/').get_data(as_text=True)
    found = re.search(r'var LAST_SYNC = (.*);', html)
    assert found, 'the page embeds the last sync'
    return html, json.loads(found.group(1))


@pytest.mark.parametrize('values,state,unfinished', [
    ({}, 'never', False),
    ({'sync_finished_at': timedelta(hours=3), 'sync_result': 'ok'}, 'ok', False),
    ({'sync_finished_at': timedelta(hours=3), 'sync_result': 'failed'}, 'failed', False),
    ({'sync_finished_at': '', 'sync_result': 'ok'}, 'unknown', False),
    ({'sync_finished_at': timedelta(hours=3), 'sync_result': 'perhaps'}, 'unknown', False),
    ({'sync_started_at': timedelta(minutes=30)}, 'never', True),
    ({'sync_started_at': timedelta(minutes=30), 'sync_finished_at': timedelta(hours=3),
      'sync_result': 'ok'}, 'ok', True),
    # a sync that started and finished a little ahead of this clock did finish
    ({'sync_started_at': timedelta(minutes=-1), 'sync_finished_at': timedelta(minutes=-3),
      'sync_result': 'ok'}, 'ok', False),
], ids=['never', 'ok', 'failed', 'empty finish', 'odd result', 'started only',
        'started after the finish', 'started and finished just ahead'])
def test_the_page_shows_each_last_sync_state(app, admin_client, values, state, unfinished):
    store(app, **values)
    html, line = page_state(admin_client)
    assert line['state'] == state
    assert (line['unfinished_started_at'] is not None) is unfinished
    assert 'id="last-sync"' in html and 'aria-live="polite"' in html


@pytest.mark.parametrize('field', ['sync_finished_at', 'sync_result', 'sync_started_at'])
def test_a_hostile_stored_value_never_reaches_the_page(app, admin_client, field):
    hostile = '</script><img src=x onerror=alert(1)>'
    store(app, **{field: hostile})
    html, line = page_state(admin_client)
    assert hostile not in html and 'onerror' not in html
    assert line['state'] in ('unknown', 'never')


def test_the_last_sync_line_is_drawn_as_text_not_html(admin_client):
    html = admin_client.get('/sync/').get_data(as_text=True)
    assert "$('#last-sync').text(line)" in html
    assert "$('#last-sync').html(" not in html
