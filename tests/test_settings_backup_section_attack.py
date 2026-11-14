"""Attacks on the Settings page's backup section with the real components: a database another
connection holds locked while a save writes, Back up now run twice into a real folder, the
action key sent twice, and a save that does not reach other uncommitted work."""
import sqlite3

import pytest

from test_settings_backup_section import (VALID, _flashes, _rows, _save,  # noqa: F401
                                          _stored, admin_client, local)


def test_a_save_while_another_connection_holds_the_database_locked_saves_nothing(
        local, admin_client):
    _save(admin_client, strategy='interval_count', min_days='2')
    before = _rows(local)
    holder = sqlite3.connect(local.config['DATABASE_PATH'], timeout=0.1)
    holder.isolation_level = None
    holder.execute('BEGIN EXCLUSIVE')
    try:
        response = _save(admin_client, strategy='gfs', min_days='9')
    finally:
        holder.execute('ROLLBACK')
        holder.close()
    assert response.status_code == 200
    messages = _flashes(response)
    assert messages == ['Backup retention of the local folder not saved: the settings could '
                        'not be written.'], messages
    assert 'locked' not in response.get_data(as_text=True)
    assert _rows(local) == before


def test_back_up_now_twice_into_a_real_folder_reports_a_sentence_each_time(
        local, admin_client):
    first = admin_client.post('/settings/', data={'action': 'backup_now'},
                              follow_redirects=True)
    second = admin_client.post('/settings/', data={'action': 'backup_now'},
                               follow_redirects=True)
    for response in (first, second):
        assert response.status_code == 200
        messages = _flashes(response)
        assert len(messages) == 1, messages
        assert messages[0].startswith('Local folder: '), messages
        assert '{' not in messages[0] and '..' not in messages[0], messages
    assert 'written' in _flashes(first)[0]


@pytest.mark.parametrize('actions', [
    pytest.param(['save_backup_retention', 'backup_now'], id='save-then-backup-now'),
    pytest.param(['save_backup_retention', 'save_backup_retention'], id='save-twice'),
])
def test_an_action_sent_more_than_once_saves_nothing(local, admin_client, actions):
    before = _rows(local)
    data = [('action', a) for a in actions] + [('target', 'local')] + \
        [(k, v) for k, v in VALID.items()]
    from werkzeug.datastructures import MultiDict
    admin_client.post('/settings/', data=MultiDict(data), follow_redirects=True)
    assert _rows(local) == before
