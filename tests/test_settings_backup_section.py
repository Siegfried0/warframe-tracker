"""The Settings page's backup section is the admin's: it shows each target's location (never a
key) and its retention, saves one target's retention at a time (all six fields checked by the
reader's own checks, written together or not at all), and reports Back up now as one sentence
per target.

Every test runs on its own temporary database; a configured local target is a temporary folder
and no backup is ever made (Back up now is replaced by a stub returning a result).
"""
import html
import os
import re
import sqlite3

import pytest
from werkzeug.datastructures import MultiDict

from conftest import TokenClient
from models.db import get_db
from services import auth_service, backup_page
from services.backup_retention import Retention
from services.backup_settings import read_retention, setting_key

VALID = {'strategy': 'gfs', 'min_days': '3', 'keep': '10',
         'gfs_daily': '5', 'gfs_weekly': '2', 'gfs_monthly': '12'}
FIELDS = ('strategy', 'min_days', 'keep', 'gfs_daily', 'gfs_weekly', 'gfs_monthly')
BOUNDS = {'min_days': (0, 365), 'keep': (1, 1000), 'gfs_daily': (0, 1000),
          'gfs_weekly': (0, 1000), 'gfs_monthly': (0, 1000)}
LABELS = backup_page.FIELD_LABELS
SECRET = 'very-secret-key-Zq9'
ACCESS = 'access-key-Ax7'


@pytest.fixture
def admin_client(admin_client):
    """The admin, with the first-run wizard's welcome already shown."""
    admin_client.get('/settings/')
    return admin_client


@pytest.fixture
def local(app, tmp_path):
    app.config['BACKUP_LOCAL_PATH'] = str(tmp_path / 'backups')
    return app


@pytest.fixture
def s3(app):
    app.config.update(BACKUP_S3_BUCKET='bucket-one', BACKUP_S3_PREFIX='/nightly',
                      BACKUP_S3_ACCESS_KEY=ACCESS, BACKUP_S3_SECRET_KEY=SECRET,
                      BACKUP_S3_ENDPOINT='https://user:pw@s3.example.invalid/path?token=x#f')
    return app


def _user_client(app, name='alice', role='user'):
    from models.user import User
    with app.app_context():
        User.create(name, password='pw-' + name + '-1', role=role)
    client = TokenClient(app, app.response_class, use_cookies=True)
    response = client.post('/auth/login', data={'username': name, 'password': 'pw-' + name + '-1'})
    assert response.status_code == 302
    return client


def _rows(app):
    with app.app_context():
        return sorted(tuple(r) for r in get_db().execute(
            "SELECT key, value FROM settings WHERE user_id IS NULL AND key LIKE 'backup_%' "
            'ORDER BY key, id').fetchall())


def _stored(app, target):
    with app.app_context():
        return {f: auth_service.get_global_setting(setting_key(target, f)) for f in FIELDS}


def _save(client, target='local', **fields):
    data = {'action': 'save_backup_retention', 'target': target, **VALID, **fields}
    data = {k: v for k, v in data.items() if v is not None}
    return client.post('/settings/', data=data, follow_redirects=True)


def _flashes(response):
    text = response.get_data(as_text=True)
    return [html.unescape(' '.join(m.split())) for m in re.findall(
        r'<div class="card-panel [^"]*white-text" style="margin-bottom:1rem">(.*?)</div>',
        text, re.S)]


def _section(response):
    text = response.get_data(as_text=True)
    found = re.search(r'<div class="card blue-grey darken-2" id="backup-card">(.*?)'
                      r'<!-- Account info -->', text, re.S)
    return found.group(1) if found else None


# ── Who sees it ─────────────────────────────────────────────────────────────

def test_an_admin_sees_the_backup_section_and_a_user_does_not(local, admin_client):
    assert _section(admin_client.get('/settings/')) is not None
    user = _user_client(local)
    page = user.get('/settings/').get_data(as_text=True)
    assert 'id="backup-card"' not in page and 'Back Up Now' not in page
    assert 'save_backup_retention' not in page and 'settings_backup.js' not in page


@pytest.mark.parametrize('action', ['save_backup_retention', 'backup_now'])
def test_a_non_admin_backup_post_is_refused_and_changes_nothing(local, admin_client, action,
                                                                monkeypatch):
    import routes.settings as route
    called = []
    monkeypatch.setattr(route, 'backup_now', lambda *a, **k: called.append(1) or {})
    user = _user_client(local)
    response = user.post('/settings/', data={'action': action, 'target': 'local', **VALID})
    assert response.status_code == 403
    assert _rows(local) == [] and called == []


def test_a_save_with_a_wrong_csrf_token_is_refused(local, admin_client):
    response = admin_client.post('/settings/', headers={'X-CSRF-Token': 'wrong'},
                                 data={'action': 'save_backup_retention', 'target': 'local',
                                       **VALID})
    assert response.status_code in (400, 403)
    assert _rows(local) == []


@pytest.mark.parametrize('action', ['true', '1', 'on', '', 'Save_Backup_Retention'])
def test_a_boolean_like_or_unknown_action_saves_nothing(local, admin_client, action):
    response = admin_client.post('/settings/', data={'action': action, 'target': 'local',
                                                     **VALID})
    assert response.status_code == 302
    assert _rows(local) == []


# ── What it shows ───────────────────────────────────────────────────────────

def test_no_target_configured_says_how_and_offers_no_back_up_now(app, admin_client):
    section = _section(admin_client.get('/settings/'))
    assert 'Back Up Now' not in section and '<form' not in section
    assert 'No backup targets configured' in section
    for name in ('BACKUP_LOCAL_PATH', 'BACKUP_S3_BUCKET', 'BACKUP_S3_ACCESS_KEY',
                 'BACKUP_S3_SECRET_KEY', 'BACKUP_S3_ENDPOINT', 'BACKUP_S3_PREFIX'):
        assert f'<code>{name}</code>' in section
    assert section.count('README') >= 2


def test_a_configured_local_target_shows_its_folder_and_the_default_retention(
        local, admin_client, tmp_path):
    section = _section(admin_client.get('/settings/'))
    assert html.escape(str(tmp_path / 'backups')) in section
    form = re.search(r'<section class="backup-target" id="backup-local">(.*?)</section>',
                     section, re.S).group(1)
    assert 'name="target" value="local"' in form
    assert re.search(r'value="interval_count" checked', form)
    defaults = Retention()
    for field in BOUNDS:
        assert re.search(rf'name="{field}" type="text" inputmode="numeric"\s+'
                         rf'value="{getattr(defaults, field)}"', form), field
        low, high = BOUNDS[field]
        assert f'{LABELS[field]}\n                  ({low}&ndash;{high})' in form
    assert 'Back Up Now' in section
    s3_part = re.search(r'id="backup-s3">(.*?)</section>', section, re.S).group(1)
    assert '<form' not in s3_part and 'BACKUP_S3_BUCKET is not set' in s3_part


def test_the_s3_location_shows_endpoint_bucket_and_prefix_and_never_a_key(s3, admin_client):
    section = _section(admin_client.get('/settings/'))
    part = re.search(r'id="backup-s3">(.*?)</section>', section, re.S).group(1)
    assert '<dd>https://s3.example.invalid/path</dd>' in part
    assert '<dd>bucket-one</dd>' in part and '<dd>nightly/</dd>' in part
    assert 'name="target" value="s3"' in part
    page = admin_client.get('/settings/').get_data(as_text=True)
    for leaked in (SECRET, ACCESS, 'user:pw', 'pw@', 'token=x', '#f'):
        assert leaked not in page


@pytest.mark.parametrize('raw, shown', [
    (None, 'AWS default'), ('', 'AWS default'), ('   ', 'AWS default'),
    ('https://s3.example.invalid', 'https://s3.example.invalid'),
    ('https://u:p@h.invalid:9000/x', 'https://h.invalid:9000/x'),
    ('https://a@b@h.invalid', 'https://h.invalid'),
    ('u:p@h.invalid', 'h.invalid'),
    ('http://[::1', 'http://[::1'),
    ('https://h.invalid/?q=1#frag', 'https://h.invalid/'),
])
def test_the_endpoint_is_shown_without_user_information_query_or_fragment(raw, shown):
    assert backup_page.endpoint_text(raw) == shown


def test_a_bucket_without_keys_is_not_configured_but_back_up_now_reports_it(app, admin_client):
    app.config['BACKUP_S3_BUCKET'] = 'bucket-one'
    section = _section(admin_client.get('/settings/'))
    part = re.search(r'id="backup-s3">(.*?)</section>', section, re.S).group(1)
    assert '<form' not in part and 'BACKUP_S3_ACCESS_KEY and BACKUP_S3_SECRET_KEY' in part
    assert 'Back Up Now' in section


@pytest.mark.parametrize('path, shown', [
    ('/srv/<script>alert(1)</script>&"\'',
     '/srv/&lt;script&gt;alert(1)&lt;/script&gt;&amp;&#34;&#39;'),
    ('/' + 'very-long-folder-name-' * 40, '/' + 'very-long-folder-name-' * 40),
])
def test_a_location_is_escaped(app, admin_client, path, shown):
    app.config['BACKUP_LOCAL_PATH'] = path
    section = _section(admin_client.get('/settings/'))
    assert '<script>alert' not in section
    shown = shown.replace('/', os.sep)        # the folder as this platform spells it
    assert f'<dd>{shown}</dd>' in section


def test_a_relative_local_path_is_shown_with_its_form(
        app, admin_client):
    app.config['BACKUP_LOCAL_PATH'] = ' backups '
    section = _section(admin_client.get('/settings/'))
    assert '<dd>backups</dd>' in section and 'name="target" value="local"' in section


def test_both_strategies_fields_are_shown_and_none_is_disabled(local, admin_client):
    section = _section(admin_client.get('/settings/'))
    assert 'disabled' not in section and ' hidden' not in section
    assert '<fieldset data-strategy="interval_count">' in section
    assert '<fieldset data-strategy="gfs">' in section
    assert '<legend>Interval + count</legend>' in section
    assert '<legend>Grandfather-father-son</legend>' in section
    ids = re.findall(r'<input id="([^"]+)"', section)
    assert all(f'<label for="{i}"' in section for i in ids) and len(ids) == 5
    assert section.count('name="csrf_token"') == section.count('<form') == 2


def test_a_stored_value_out_of_bounds_shows_the_default_and_stays_stored(local, admin_client):
    with local.app_context():
        auth_service.set_global_setting(setting_key('local', 'keep'), '0')
        auth_service.set_global_setting(setting_key('local', 'strategy'), 'weekly')
    section = _section(admin_client.get('/settings/'))
    assert re.search(r'name="keep" type="text" inputmode="numeric"\s+value="7"', section)
    assert re.search(r'value="interval_count" checked', section)
    assert _stored(local, 'local')['keep'] == '0'
    assert _stored(local, 'local')['strategy'] == 'weekly'


def test_a_failed_settings_read_shows_no_form_and_keeps_back_up_now(local, admin_client,
                                                                   monkeypatch):
    def broken(target):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(backup_page, 'read_retention', broken)
    section = _section(admin_client.get('/settings/'))
    assert 'the settings could not be read' in section
    assert 'save_backup_retention' not in section and 'Back Up Now' in section
    assert 'database is locked' not in section


def test_mssql_mode_shows_no_form_and_refuses_a_save(tmp_path):
    config = {'DB_BACKEND': 'mssql', 'BACKUP_LOCAL_PATH': str(tmp_path)}
    section = backup_page.backup_section(config, read=lambda t: pytest.fail('read'))
    assert [e['retention'] for e in section['targets']] == [None, None]
    assert section['mssql'] is True
    saved, message = backup_page.save_retention(
        MultiDict({'target': 'local', **VALID}), config,
        write=lambda *a: pytest.fail('written'))
    assert saved is False and 'MSSQL' in message and 'own backups apply' in message


# ── The save ────────────────────────────────────────────────────────────────

def test_a_valid_save_writes_the_six_keys_of_that_target_only(local, s3, admin_client, caplog):
    response = _save(admin_client)
    assert _flashes(response) == ['Backup retention of the local folder saved.']
    assert _stored(local, 'local') == VALID
    assert all(v is None for v in _stored(local, 's3').values())
    caplog.set_level('WARNING')
    with local.app_context():
        assert read_retention('local') == Retention('gfs', 3, 10, 5, 2, 12)
    assert not caplog.records       # the reader reads back what the page wrote, unlogged


def _number_cases(field):
    low, high = BOUNDS[field]
    valid = [(str(low), str(low)), (str(high), str(high)), (f' {high} ', str(high)),
             (f'\t{low}\n', str(low)), (f'0{low + 1}', str(low + 1)),
             (f'{low + 1:04d}', str(low + 1))]
    invalid = ['', '   ', str(low - 1), str(high + 1), '-1', '+7', '7.0', '1e3', '1_000',
               '１２', '٣', '7 7', '0x10', '18446744073709551617', '9' * 5000, '00000',
               '7\x00', 'seven', '½', 'true']
    if low == 0:
        invalid.remove(str(low - 1))
    return [(field, text, stored) for text, stored in valid] + \
        [(field, text, None) for text in invalid]


NUMBER_CASES = [case for field in BOUNDS for case in _number_cases(field)]


@pytest.mark.parametrize('field, text, stored', NUMBER_CASES)
def test_each_number_field_saves_only_a_whole_number_within_its_bounds(
        local, admin_client, field, text, stored):
    fields = {field: text}
    if field.startswith('gfs_'):     # the others above 0: only this field decides
        fields.update({f: '1' for f in BOUNDS if f.startswith('gfs_') and f != field})
    response = _save(admin_client, **fields)
    if stored is None:
        low, high = BOUNDS[field]
        assert _rows(local) == []
        message = _flashes(response)[0]
        assert message.startswith('Backup retention of the local folder not saved: ')
        assert f'{LABELS[field]} must be a whole number from {low} to {high}' in message
    else:
        assert _stored(local, 'local')[field] == stored


@pytest.mark.parametrize('field', list(BOUNDS))
def test_a_lone_surrogate_is_not_a_number(field):
    form = MultiDict({'target': 'local', **VALID, field: '\ud800'})
    _, values, problems = backup_page.check_retention_form(form, {'BACKUP_LOCAL_PATH': '/b'})
    assert field not in values and len(problems) == 1


def test_a_form_body_that_is_not_utf8_saves_nothing(local, admin_client):
    body = '&'.join(f'{k}={v}' for k, v in {'action': 'save_backup_retention',
                                              'target': 'local', **VALID}.items())
    response = admin_client.post('/settings/', data=body.replace('keep=10', 'keep=%ED%A0%80'),
                                 content_type='application/x-www-form-urlencoded',
                                 follow_redirects=True)
    assert 'Backups kept must be a whole number' in _flashes(response)[0]
    assert _rows(local) == []


@pytest.mark.parametrize('field', list(BOUNDS))
def test_a_missing_number_field_saves_nothing_and_names_it(local, admin_client, field):
    low, high = BOUNDS[field]
    message = _flashes(_save(admin_client, **{field: None}))[0]
    assert f'{LABELS[field]} must be a whole number from {low} to {high}' in message
    assert _rows(local) == []


@pytest.mark.parametrize('text, stored', [
    ('gfs', 'gfs'), ('GFS', 'gfs'), (' gfs ', 'gfs'), ('Interval_Count', 'interval_count'),
    ('interval_count', 'interval_count'), ('\tINTERVAL_COUNT\n', 'interval_count'),
    ('', None), ('gfs2', None), ('interval count', None), ('ｇｆｓ', None),
    ('İnterval_count', None), ('weekly', None), ('true', None), (None, None),
])
def test_the_strategy_saves_only_a_known_strategy_in_lower_case(local, admin_client, text,
                                                                stored):
    response = _save(admin_client, strategy=text)
    if stored is None:
        assert _rows(local) == []
        assert 'Strategy must be interval_count or gfs' in _flashes(response)[0]
    else:
        assert _stored(local, 'local')['strategy'] == stored


@pytest.mark.parametrize('strategy', ['gfs', 'interval_count'])
def test_all_three_gfs_counts_zero_are_refused_whatever_the_strategy(local, admin_client,
                                                                     strategy):
    response = _save(admin_client, strategy=strategy, gfs_daily='0', gfs_weekly='00',
                     gfs_monthly=' 0 ')
    assert 'at least one GFS count must be above 0' in _flashes(response)[0]
    assert _rows(local) == []


def test_every_invalid_field_is_named_in_one_message(local, admin_client):
    message = _flashes(_save(admin_client, strategy='x', min_days='366', keep='0',
                             gfs_daily='-1'))[0]
    for part in ('Strategy must be', LABELS['min_days'], LABELS['keep'], LABELS['gfs_daily']):
        assert part in message
    assert _rows(local) == []


def test_a_strategy_switch_is_saved_and_read_back(local, admin_client):
    _save(admin_client, strategy='gfs')
    _save(admin_client, strategy='interval_count', min_days='0', keep='1')
    with local.app_context():
        assert read_retention('local') == Retention('interval_count', 0, 1, 5, 2, 12)
    section = _section(admin_client.get('/settings/'))
    assert re.search(r'value="interval_count" checked', section)


@pytest.mark.parametrize('target, why', [
    (None, 'no known backup target was named'),
    ('', 'no known backup target was named'),
    ('ftp', 'no known backup target was named'),
    ('LOCAL', 'no known backup target was named'),
    (' local', 'no known backup target was named'),
    ('s3', 'the S3 bucket is not configured (BACKUP_S3_BUCKET is not set)'),
])
def test_a_missing_unknown_or_unconfigured_target_saves_nothing(local, admin_client, target,
                                                                why):
    assert why in _flashes(_save(admin_client, target=target))[0]
    assert _rows(local) == []


def test_a_bucket_without_keys_cannot_be_saved(app, admin_client):
    app.config['BACKUP_S3_BUCKET'] = 'bucket-one'
    message = _flashes(_save(admin_client, target='s3'))[0]
    assert 'the S3 bucket is not configured' in message and 'must both be set' in message
    assert _rows(app) == []


@pytest.mark.parametrize('field', ['action', 'target'] + list(FIELDS))
def test_a_field_sent_twice_saves_nothing(local, admin_client, field):
    data = MultiDict({'action': 'save_backup_retention', 'target': 'local', **VALID})
    data.add(field, data[field])
    response = admin_client.post('/settings/', data=data, follow_redirects=True)
    label = LABELS.get(field) or {'action': 'Action', 'target': 'Target'}[field]
    assert f'{label} was sent more than once' in _flashes(response)[0]
    assert _rows(local) == []


def test_the_same_save_twice_leaves_the_same_state(local, admin_client):
    _save(admin_client)
    first = _rows(local)
    _save(admin_client)
    assert _rows(local) == first and len(first) == 6


def test_a_write_that_fails_part_way_saves_nothing_and_says_so(local, admin_client,
                                                               monkeypatch):
    _save(admin_client, strategy='interval_count', min_days='2')
    before = _rows(local)
    real = auth_service.upsert_global_setting
    calls = []

    def locked_on_the_fourth(db, key, value):
        calls.append(key)
        if len(calls) == 4:
            raise sqlite3.OperationalError('database is locked')
        real(db, key, value)
    monkeypatch.setattr(auth_service, 'upsert_global_setting', locked_on_the_fourth)
    response = _save(admin_client, strategy='gfs', min_days='9')
    assert response.status_code == 200
    message = _flashes(response)[0]
    assert message == ('Backup retention of the local folder not saved: the settings could '
                       'not be written.')
    assert 'database is locked' not in response.get_data(as_text=True)
    assert len(calls) == 4 and _rows(local) == before


def test_two_admins_saving_one_target_the_last_save_wins_whole(local, admin_client):
    other = _user_client(local, 'bea', role='admin')
    _save(admin_client, strategy='gfs', gfs_daily='9')
    _save(other, strategy='interval_count', min_days='4', keep='20', gfs_daily='1')
    assert _stored(local, 'local') == {**VALID, 'strategy': 'interval_count', 'min_days': '4',
                                       'keep': '20', 'gfs_daily': '1'}


def test_a_save_while_a_backup_runs_is_saved(local, admin_client):
    from services import backup_service
    assert backup_service._lock.acquire(blocking=False)
    try:
        assert 'saved' in _flashes(_save(admin_client))[0]
    finally:
        backup_service._lock.release()
    assert _stored(local, 'local') == VALID


# ── Back up now's result ────────────────────────────────────────────────────

RESULTS = [
    ({'local': {'ok': True, 'written': 'warframe_tracker_20261114T120000Z.db', 'removed': 0,
                'not_removed': 0}},
     [('success', 'Local folder: backup warframe_tracker_20261114T120000Z.db written; '
                  '0 old backups removed.')]),
    ({'local': {'ok': True, 'written': 'b.db', 'removed': 1, 'not_removed': 0},
      's3': {'ok': True, 'written': 'b.db', 'removed': 3, 'not_removed': 2}},
     [('success', 'Local folder: backup b.db written; 1 old backup removed.'),
      ('warning', 'S3 bucket: backup b.db written; 3 old backups removed, 2 could not be '
                  'removed.')]),
    ({'s3': {'ok': True, 'written': 'b.db', 'removed': 0, 'not_removed': 0,
             'retention_error': 'the backups could not be listed: AccessDenied'}},
     [('warning', 'S3 bucket: backup b.db written; old backups were not cleaned up, the '
                  'backups could not be listed: AccessDenied.')]),
    ({'local': {'ok': True, 'skipped': True, 'reason': 'a backup with this name already exists'}},
     [('info', 'Local folder: skipped, a backup with this name already exists.')]),
    ({'local': {'ok': False, 'skipped': True, 'running': True,
                'reason': 'a backup is already running'},
      's3': {'ok': False, 'skipped': True, 'running': True,
             'reason': 'a backup is already running'}},
     [('warning', 'Local folder: skipped, a backup is already running.'),
      ('warning', 'S3 bucket: skipped, a backup is already running.')]),
    ({'local': {'ok': False, 'error': 'BACKUP_LOCAL_PATH must be an absolute path'}},
     [('error', 'Local folder: failed, BACKUP_LOCAL_PATH must be an absolute path.')]),
    ({'local': {'ok': True, 'skipped': True,
                'reason': 'the database runs on MSSQL: its own backups apply, no file backup '
                          'is made'}},
     [('info', 'Local folder: skipped, the database runs on MSSQL: its own backups apply, no '
               'file backup is made.')]),
    ({'ok': False, 'error': 'No backup targets configured'},
     [('error', 'No backup targets configured.')]),
]


@pytest.mark.parametrize('result, expected', RESULTS)
def test_back_up_now_reports_one_sentence_per_target(result, expected):
    assert backup_page.result_messages(result) == expected


def test_back_up_now_flashes_escaped_sentences_not_json(local, admin_client, monkeypatch):
    import routes.settings as route
    result = {'local': {'ok': False, 'error': 'the backup failed: <b>disk</b> & "full"'},
              's3': {'ok': True, 'written': 'b.db', 'removed': 2, 'not_removed': 0}}
    monkeypatch.setattr(route, 'backup_now', lambda app, force: result)
    for _ in range(2):        # twice: each press reports its own result
        response = admin_client.post('/settings/', data={'action': 'backup_now'},
                                     follow_redirects=True)
        text = response.get_data(as_text=True)
        assert '<b>disk</b>' not in text and '&lt;b&gt;disk&lt;/b&gt;' in text
        assert '{' not in ' '.join(_flashes(response))
        assert _flashes(response) == [
            'Local folder: failed, the backup failed: <b>disk</b> & "full".',
            'S3 bucket: backup b.db written; 2 old backups removed.']
