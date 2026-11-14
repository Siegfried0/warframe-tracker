"""Backing up each configured target and applying its retention afterwards, one backup at a
time, on a schedule. The clock is injected; every target folder is under the test's temporary
directory and S3 is botocore's Stubber, so nothing reaches the network."""
import os
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from models.db import get_db
from services import backup_service as bs
from services import backup_targets as bt
from services.auth_service import upsert_global_setting

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
D = timedelta(days=1)
KEYS = {'BACKUP_S3_ACCESS_KEY': 'test', 'BACKUP_S3_SECRET_KEY': 'test'}


def B(moment):
    return 'warframe_tracker_' + moment.strftime('%Y%m%dT%H%M%S') + 'Z.db'


@pytest.fixture
def folder(app, tmp_path):
    path = tmp_path / 'backups'
    path.mkdir()
    app.config['BACKUP_LOCAL_PATH'] = str(path)
    return path


def old_backups(folder, count):
    names = [B(NOW - (i + 1) * D) for i in range(count)]
    for name in names:
        (folder / name).write_bytes(b'old')
    return names


def settings(app, **values):
    with app.app_context():
        db = get_db()
        for key, value in values.items():
            upsert_global_setting(db, f'backup_local_{key}', value)
        db.commit()


def backup(app, force=True, now=NOW):
    with app.app_context():
        return bs.backup_now(app, force=force, now=now)


def test_no_target_configured_is_an_error(app):
    assert backup(app) == {'ok': False, 'error': 'No backup targets configured'}


def test_back_up_now_writes_and_then_keeps_the_seven_newest(app, folder):
    old_backups(folder, 8)
    (folder / 'notes.txt').write_bytes(b'mine')
    result = backup(app)['local']
    assert result == {'ok': True, 'written': str(folder / B(NOW)), 'removed': 2,
                      'not_removed': 0}
    kept = sorted(n for n in os.listdir(folder) if n != 'notes.txt')
    assert kept == sorted(B(NOW - i * D) for i in range(7))


def test_gfs_from_the_settings_is_applied_after_the_backup(app, folder):
    old_backups(folder, 20)
    settings(app, strategy='gfs', gfs_daily='3', gfs_weekly='0', gfs_monthly='0')
    assert backup(app)['local']['removed'] == 18
    assert sorted(os.listdir(folder)) == sorted([B(NOW), B(NOW - D), B(NOW - 2 * D)])


@pytest.mark.parametrize('age,written', [(D - timedelta(seconds=1), False), (D, True)],
                         ids=['newest younger than a day', 'newest a day old'])
def test_a_scheduled_backup_is_made_only_when_due(app, folder, age, written):
    (folder / B(NOW - age)).write_bytes(b'old')
    result = backup(app, force=False)['local']
    assert ('written' in result) is written
    if not written:
        assert result == {'ok': True, 'skipped': True, 'reason': 'no backup is due'}


def test_a_failed_backup_deletes_nothing(app, folder, monkeypatch):
    names = old_backups(folder, 9)
    monkeypatch.setattr(bt, 'copy_database',
                        lambda *a, **k: (_ for _ in ()).throw(OSError(28, 'disk full')))
    result = backup(app)['local']
    assert result['ok'] is False and 'the backup failed' in result['error']
    assert sorted(os.listdir(folder)) == sorted(names)


def test_a_skipped_backup_deletes_nothing(app, folder):
    names = old_backups(folder, 9)
    (folder / B(NOW)).write_bytes(b'same second')
    result = backup(app)['local']
    assert result == {'ok': True, 'skipped': True,
                      'reason': 'a backup with this name already exists'}
    assert len(os.listdir(folder)) == len(names) + 1


def test_back_up_now_twice_makes_two_backups_and_the_second_deletes_nothing_more(app, folder):
    backup(app)
    second = backup(app, now=NOW + timedelta(seconds=1))['local']
    assert second['removed'] == 0 and len(os.listdir(folder)) == 2


def test_a_listing_that_fails_after_the_backup_skips_retention_only(app, folder, monkeypatch):
    names = old_backups(folder, 9)
    monkeypatch.setattr(bt, 'list_local', lambda f: (_ for _ in ()).throw(OSError('denied')))
    result = backup(app)['local']
    assert result['written'] == str(folder / B(NOW))
    assert 'could not be listed' in result['retention_error']
    assert len(os.listdir(folder)) == len(names) + 1


def test_a_listing_that_fails_at_the_due_check_makes_no_backup(app, folder, monkeypatch):
    monkeypatch.setattr(bt, 'list_local', lambda f: (_ for _ in ()).throw(OSError('denied')))
    result = backup(app, force=False)['local']
    assert result['ok'] is False and result['skipped'] is True
    assert os.listdir(folder) == []


def test_a_failed_settings_read_skips_the_target(app, folder, monkeypatch):
    monkeypatch.setattr(bs, 'read_retention',
                        lambda target: (_ for _ in ()).throw(RuntimeError('database is locked')))
    result = backup(app)['local']
    assert result['ok'] is False and 'could not be read' in result['error']
    assert os.listdir(folder) == []


def test_the_new_backup_is_kept_when_a_name_ahead_of_the_clock_outranks_it(app, folder):
    ahead = B(NOW + timedelta(minutes=5))
    (folder / ahead).write_bytes(b'ahead')
    settings(app, keep='1')
    backup(app)
    assert sorted(os.listdir(folder)) == sorted([ahead, B(NOW)])


def test_a_backup_killed_before_retention_is_trimmed_by_the_next_one(app, folder):
    old_backups(folder, 8)
    (folder / B(NOW)).write_bytes(b'renamed, then killed')
    result = backup(app, now=NOW + timedelta(seconds=1))['local']
    assert result['removed'] == 3 and len(os.listdir(folder)) == 7


def test_retention_run_again_deletes_nothing_more(app, folder):
    old_backups(folder, 9)
    first = backup(app)['local']
    again = backup(app, now=NOW + timedelta(seconds=1))['local']
    assert (first['removed'], again['removed']) == (3, 1)
    assert len(os.listdir(folder)) == 7


def test_a_zero_byte_file_with_a_backup_name_counts_by_its_name(app, folder):
    (folder / B(NOW - timedelta(hours=1))).write_bytes(b'')
    assert backup(app, force=False)['local'] == {'ok': True, 'skipped': True,
                                                 'reason': 'no backup is due'}


def test_a_missing_folder_is_due_and_created_by_the_scheduled_backup(app, tmp_path):
    folder = tmp_path / 'not yet' / 'backups'
    app.config['BACKUP_LOCAL_PATH'] = str(folder) + os.sep + '  '
    assert 'written' in bs.scheduled_check(app, now=NOW)['local']
    assert os.listdir(folder) == [B(NOW)]


@pytest.mark.parametrize('raw', ['backups', os.path.join('.', 'backups'), '..'])
def test_a_relative_local_path_makes_no_backup_and_deletes_nothing(app, tmp_path, monkeypatch,
                                                                   raw):
    cwd = tmp_path / 'cwd' / 'here'
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    app.config['BACKUP_LOCAL_PATH'] = raw
    for force in (True, False):
        result = backup(app, force=force)['local']
        assert result == {'ok': False, 'error': 'BACKUP_LOCAL_PATH must be an absolute path'}
    assert os.listdir(cwd) == [] and os.listdir(cwd.parent) == ['here']


def test_a_backup_asked_for_while_one_runs_makes_none(app, folder, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_write = bt.write_local

    def slow(*args):
        entered.set()
        release.wait(10)
        return real_write(*args)
    monkeypatch.setattr(bt, 'write_local', slow)
    worker = threading.Thread(target=backup, args=(app,))
    worker.start()
    try:
        assert entered.wait(10)
        second = backup(app, now=NOW + timedelta(seconds=1))['local']
        scheduled = bs.scheduled_check(app, now=NOW + timedelta(seconds=2))['local']
    finally:
        release.set()
        worker.join(10)
    assert second == scheduled == {'ok': False, 'skipped': True, 'running': True,
                                   'reason': bs.RUNNING_REASON}
    assert os.listdir(folder) == [B(NOW)]
    assert not bs.is_running()


def test_mssql_mode_makes_no_file_backup_and_says_so_per_target(app, folder):
    app.config.update(DB_BACKEND='mssql', BACKUP_S3_BUCKET='bucket')
    result = backup(app)
    assert result == {t: {'ok': True, 'skipped': True, 'reason': bs.MSSQL_REASON}
                      for t in ('local', 's3')}
    assert os.listdir(folder) == []


def test_missing_boto3_fails_the_s3_target_only(app, folder, monkeypatch):
    app.config.update(BACKUP_S3_BUCKET='bucket', **KEYS)

    def no_boto3(config):
        raise ImportError('No module named boto3')
    monkeypatch.setattr(bt, '_s3_client', no_boto3)
    result = backup(app)
    assert result['s3'] == {'ok': False, 'error': 'boto3 is not installed (pip install boto3)'}
    assert result['local']['written']


def test_the_s3_target_backs_up_then_deletes_under_its_prefix(app, monkeypatch):
    boto3 = pytest.importorskip('boto3')
    from botocore.stub import ANY, Stubber
    client = boto3.session.Session().client(
        's3', region_name='us-east-1', endpoint_url='https://s3.example.invalid',
        aws_access_key_id='test', aws_secret_access_key='test')
    app.config.update(BACKUP_S3_BUCKET='bucket', BACKUP_S3_PREFIX='backups', **KEYS)
    monkeypatch.setattr(bt, '_s3_client', lambda config: client)
    old = [B(NOW - (i + 1) * D) for i in range(8)]
    request = {'Bucket': 'bucket', 'Prefix': 'backups/', 'Delimiter': '/'}
    with Stubber(client) as stub:
        stub.add_response('list_objects_v2', {
            'IsTruncated': False, 'Contents': [{'Key': 'backups/' + n} for n in old]}, request)
        stub.add_response('put_object', {}, {'Bucket': 'bucket', 'Key': 'backups/' + B(NOW),
                                             'Body': ANY})
        stub.add_response('list_objects_v2', {
            'IsTruncated': False,
            'Contents': [{'Key': 'backups/' + n} for n in old + [B(NOW)]]}, request)
        stub.add_response('delete_objects', {}, {'Bucket': 'bucket', 'Delete': {
            'Objects': [{'Key': 'backups/' + n} for n in old[6:]]}})
        result = backup(app, force=False)['s3']
        stub.assert_no_pending_responses()
    assert result == {'ok': True, 'written': 'backups/' + B(NOW), 'removed': 2,
                      'not_removed': 0}


def _s3_app(app, monkeypatch):
    boto3 = pytest.importorskip('boto3')
    client = boto3.session.Session().client(
        's3', region_name='us-east-1', endpoint_url='https://s3.example.invalid',
        aws_access_key_id='test', aws_secret_access_key='test')
    app.config.update(BACKUP_S3_BUCKET='bucket', BACKUP_S3_PREFIX='/backups/', **KEYS)
    monkeypatch.setattr(bt, '_s3_client', lambda config: client)
    return client, {'Bucket': 'bucket', 'Prefix': 'backups/', 'Delimiter': '/'}


def test_a_refused_s3_listing_at_the_due_check_uploads_nothing(app, monkeypatch):
    from botocore.stub import Stubber
    client, request = _s3_app(app, monkeypatch)
    with Stubber(client) as stub:
        stub.add_client_error('list_objects_v2', 'AccessDenied', http_status_code=403,
                              expected_params=request)
        result = backup(app, force=False)['s3']
        stub.assert_no_pending_responses()
    assert result['ok'] is False and result['skipped'] is True
    assert 'could not be listed' in result['error']


def test_a_forced_s3_backup_whose_retention_listing_fails_deletes_nothing(app, monkeypatch):
    from botocore.stub import ANY, Stubber
    client, request = _s3_app(app, monkeypatch)
    with Stubber(client) as stub:
        stub.add_response('put_object', {}, {'Bucket': 'bucket', 'Key': 'backups/' + B(NOW),
                                             'Body': ANY})
        stub.add_response('list_objects_v2', {'IsTruncated': True}, request)   # no token
        result = backup(app)['s3']
        stub.assert_no_pending_responses()
    assert result['written'] == 'backups/' + B(NOW) and result['removed'] == 0
    assert 'continuation token' in result['retention_error']


def test_an_s3_upload_that_times_out_fails_and_deletes_nothing(app, monkeypatch):
    from botocore.exceptions import ReadTimeoutError
    client, _ = _s3_app(app, monkeypatch)
    calls = []
    monkeypatch.setattr(client, 'list_objects_v2', lambda **k: calls.append(k) or {})
    monkeypatch.setattr(client, 'delete_objects', lambda **k: calls.append(k) or {})
    monkeypatch.setattr(client, 'put_object', mock.Mock(
        side_effect=ReadTimeoutError(endpoint_url='https://s3.example.invalid')))
    result = backup(app)['s3']
    assert result['ok'] is False and 'the backup failed' in result['error']
    assert calls == []


# ── The scheduler ────────────────────────────────────────────────────────────

@pytest.fixture
def scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone='UTC')
    yield sched
    if sched.running:
        sched.shutdown(wait=False)


def test_the_backup_check_runs_every_15_minutes_from_2_minutes_after_the_start(app, folder,
                                                                             scheduler):
    assert bs.add_jobs(scheduler, app, now=NOW) == 1
    job = scheduler.get_job('backup-check')
    assert job.func is bs.scheduled_check and job.args == (app,)
    assert job.trigger.start_date == NOW + timedelta(minutes=2)
    assert job.trigger.interval == timedelta(minutes=15)
    assert job.coalesce is True and job.max_instances == 1


@pytest.mark.parametrize('config', [{}, {'BACKUP_LOCAL_PATH': '  '},
                                    {'BACKUP_LOCAL_PATH': 'x', 'DB_BACKEND': 'mssql'}],
                         ids=['no target', 'blank path', 'mssql'])
def test_no_backup_job_without_a_target_or_in_mssql_mode(app, scheduler, config):
    app.config.update({'BACKUP_LOCAL_PATH': None, 'BACKUP_S3_BUCKET': None, **config})
    assert bs.add_jobs(scheduler, app, now=NOW) == 0
    assert scheduler.get_jobs() == []


@pytest.mark.parametrize('backend,started', [('sqlite', True), ('mssql', False)])
def test_a_backup_target_starts_the_scheduler_without_a_scheduled_sync(app, folder, monkeypatch,
                                                                       backend, started):
    import app as app_module
    from apscheduler.schedulers.background import BackgroundScheduler
    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self, *a, **k: None)
    app.config.update(DB_BACKEND=backend, AUTO_SYNC_ON_STARTUP=False, SYNC_INTERVAL_HOURS=0)
    app_module._start_scheduler(app)
    assert hasattr(app, 'scheduler') is started
    if started:
        assert {job.id for job in app.scheduler.get_jobs()} == {'backup-check'}
        del app.scheduler   # never started: nothing to shut down


def test_a_restart_within_min_days_makes_no_second_backup(app, folder):
    assert 'written' in bs.scheduled_check(app, now=NOW)['local']
    assert bs.scheduled_check(app, now=NOW + timedelta(minutes=15))['local']['skipped']
    assert os.listdir(folder) == [B(NOW)]


def test_a_scheduled_check_right_after_back_up_now_makes_no_second_backup(app, folder):
    backup(app)
    for minutes in (1, 14):
        assert bs.scheduled_check(app, now=NOW + timedelta(minutes=minutes))['local'] == {
            'ok': True, 'skipped': True, 'reason': bs.NOT_DUE}
    assert os.listdir(folder) == [B(NOW)]


# ── The S3 target's configuration (bucket and both keys) ─────────────────────

@pytest.mark.parametrize('keys', [{}, {'BACKUP_S3_ACCESS_KEY': 'test'},
                                  {'BACKUP_S3_ACCESS_KEY': ' ', 'BACKUP_S3_SECRET_KEY': 'x'},
                                  {'BACKUP_S3_ACCESS_KEY': 'x', 'BACKUP_S3_SECRET_KEY': '\t'}],
                         ids=['no keys', 'no secret key', 'blank access key', 'blank secret key'])
def test_a_bucket_without_both_keys_reports_it_and_makes_no_call(app, folder, monkeypatch,
                                                                 scheduler, keys):
    app.config.update(BACKUP_S3_BUCKET='bucket', **keys)
    made = []
    monkeypatch.setattr(bt, '_s3_client', lambda config: made.append(config))
    result = backup(app)
    assert result['s3'] == {'ok': False, 'error': bt.S3_KEYS_REASON}
    assert result['local']['written'] and made == []
    assert 's3' not in bs.scheduled_check(app, now=NOW + timedelta(seconds=1))
    app.config['BACKUP_LOCAL_PATH'] = None
    assert bs.add_jobs(scheduler, app, now=NOW) == 0 and made == []


@pytest.mark.parametrize('config,wanted', [
    ({'BACKUP_S3_BUCKET': ' bucket ', **KEYS}, True),
    ({'BACKUP_S3_BUCKET': '   ', **KEYS}, False),
    ({'BACKUP_S3_BUCKET': 'bucket'}, False),
    ({'BACKUP_LOCAL_PATH': 'relative'}, True),
    ({'BACKUP_LOCAL_PATH': '\t \n'}, False),
    ({'BACKUP_LOCAL_PATH': 'relative', 'DB_BACKEND': 'mssql'}, False),
], ids=['bucket and keys', 'blank bucket', 'bucket without keys', 'relative path counts',
        'blank path', 'mssql'])
def test_the_scheduler_counts_a_target_as_configured_by_its_settings(app, config, wanted):
    app.config.update({'BACKUP_LOCAL_PATH': None, 'BACKUP_S3_BUCKET': None, **config})
    assert bs.wants_scheduler(app.config) is wanted


# ── One target failing, the other working; retention interrupted or refused ──

def test_a_failed_s3_upload_leaves_the_local_target_backed_up_and_trimmed(app, folder,
                                                                         monkeypatch):
    from botocore.stub import ANY, Stubber
    old_backups(folder, 8)
    client, _ = _s3_app(app, monkeypatch)
    with Stubber(client) as stub:
        stub.add_client_error('put_object', 'AccessDenied', http_status_code=403,
                              expected_params={'Bucket': 'bucket',
                                               'Key': 'backups/' + B(NOW), 'Body': ANY})
        result = backup(app)
        stub.assert_no_pending_responses()     # no listing and no delete on S3
    assert result['s3']['ok'] is False and 'the backup failed' in result['s3']['error']
    assert result['local'] == {'ok': True, 'written': str(folder / B(NOW)), 'removed': 2,
                               'not_removed': 0}


def test_a_refused_delete_is_counted_and_the_others_are_removed(app, folder, monkeypatch):
    names = old_backups(folder, 9)          # 10 with the new one: three to remove
    refused = folder / names[-1]
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if os.fspath(path) == str(refused):
            raise PermissionError(13, 'in use')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, 'unlink', unlink)
    result = backup(app)['local']
    assert (result['removed'], result['not_removed']) == (2, 1)
    assert refused.exists() and len(os.listdir(folder)) == 8


def test_a_backup_killed_during_retention_is_finished_by_the_next_one(app, folder):
    names = old_backups(folder, 9)
    (folder / B(NOW)).write_bytes(b'written')
    (folder / names[-1]).unlink()          # killed after one of three deletes
    result = backup(app, now=NOW + timedelta(seconds=1))['local']
    assert result['removed'] == 3 and len(os.listdir(folder)) == 7


def test_retention_uses_the_settings_read_before_the_write(app, folder, monkeypatch):
    old_backups(folder, 9)
    settings(app, keep='3')
    real_write = bt.write_local

    def write_then_change(*args):
        settings(app, keep='1')            # saved while the backup runs: the next one's
        return real_write(*args)
    monkeypatch.setattr(bt, 'write_local', write_then_change)
    assert backup(app)['local']['removed'] == 7
    assert len(os.listdir(folder)) == 3


# ── Logging a failure that lasts; APScheduler missing ────────────────────────

def _failures(caplog):
    return [r for r in caplog.records
            if r.name == bs.__name__ and r.getMessage().startswith('Backup of local:')]


def test_a_lasting_failure_is_logged_once_per_reason_until_the_target_works(app, folder,
                                                                           monkeypatch,
                                                                           caplog):
    caplog.set_level('WARNING', logger=bs.__name__)
    monkeypatch.setattr(bs, '_reported', {})
    reason = ['denied']
    real_list = bt.list_local

    def listing(f):
        if reason[0]:
            raise OSError(reason[0])
        return real_list(f)
    monkeypatch.setattr(bt, 'list_local', listing)
    for i in range(3):
        bs.scheduled_check(app, now=NOW + i * timedelta(minutes=15))
    assert len(_failures(caplog)) == 1
    reason[0] = 'gone'
    bs.scheduled_check(app, now=NOW + timedelta(hours=1))
    assert len(_failures(caplog)) == 2
    assert 'could not be listed' in backup(app, force=False)['local']['error']
    assert len(_failures(caplog)) == 3          # Back up now always says it
    reason[0] = None
    assert 'written' in bs.scheduled_check(app, now=NOW + timedelta(hours=2))['local']
    reason[0] = 'gone'
    bs.scheduled_check(app, now=NOW + timedelta(days=2))
    assert len(_failures(caplog)) == 4          # after a success: logged again


def _no_create(monkeypatch):
    """A folder that lists but refuses new files (a read-only mount, a folder without write
    permission): ``open(path, 'xb')`` raises as the OS does, naming the path."""
    real_open = open

    def refusing(path, mode='r', *args, **kwargs):
        if mode == 'xb':
            raise PermissionError(13, 'Permission denied', str(path))
        return real_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(bt, 'open', refusing, raising=False)


def _no_rename(monkeypatch):
    """Windows refusing the rename while another handle (a virus scanner) holds the file:
    ``os.replace`` raises as the OS does, naming both paths."""
    def refusing(src, dst, *args, **kwargs):
        raise PermissionError(13, 'Access is denied', str(src), 5, str(dst))
    monkeypatch.setattr(bt.os, 'replace', refusing)


@pytest.mark.parametrize('refuse', [_no_create, _no_rename],
                         ids=['folder lists but refuses new files', 'rename refused'])
def test_a_lasting_write_failure_is_logged_once_across_scheduled_checks(app, folder,
                                                                       monkeypatch, caplog,
                                                                       refuse):
    caplog.set_level('WARNING', logger=bs.__name__)
    monkeypatch.setattr(bs, '_reported', {})
    refuse(monkeypatch)
    results = [bs.scheduled_check(app, now=NOW + i * timedelta(minutes=15))['local']
               for i in range(3)]
    assert all(r['ok'] is False and 'the backup failed' in r['error'] for r in results)
    assert len(_failures(caplog)) == 1          # one reason, one line: not one per check


def test_a_check_skipped_while_a_backup_runs_does_not_log_a_lasting_failure_again(app, folder,
                                                                                  monkeypatch,
                                                                                  caplog):
    caplog.set_level('WARNING', logger=bs.__name__)
    monkeypatch.setattr(bs, '_reported', {})

    def listing(f):
        raise OSError('denied')
    monkeypatch.setattr(bt, 'list_local', listing)
    bs.scheduled_check(app, now=NOW)
    assert bs._lock.acquire(blocking=False)
    try:
        assert bs.scheduled_check(app, now=NOW + timedelta(minutes=15))['local']['running']
    finally:
        bs._lock.release()
    bs.scheduled_check(app, now=NOW + timedelta(minutes=30))
    assert len(_failures(caplog)) == 1          # a skip is not a success: no second line


def test_missing_apscheduler_with_a_target_logs_one_warning_and_the_app_runs(app, folder,
                                                                           monkeypatch,
                                                                           caplog):
    import builtins

    import app as app_module
    real_import = builtins.__import__

    def no_apscheduler(name, *args, **kwargs):
        if name.startswith('apscheduler'):
            raise ImportError('No module named apscheduler')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_apscheduler)
    caplog.set_level('WARNING')
    app_module._start_scheduler(app)
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count(bs.NO_APSCHEDULER) == 1
    assert not any('scheduled sync' in m for m in messages)
    assert not hasattr(app, 'scheduler')


# ── Back up now on the Settings page is the admin's ──────────────────────────

@pytest.fixture
def calls(monkeypatch):
    import routes.settings as route
    made = []
    monkeypatch.setattr(route, 'backup_now', lambda app, force: made.append(force) or {})
    return made


def test_a_non_admin_back_up_now_is_refused_and_runs_nothing(app, admin_client, calls):
    from conftest import TokenClient
    from models.user import User
    with app.app_context():
        User.create('alice', password='pw-alice-1')
    user = TokenClient(app, app.response_class, use_cookies=True)
    assert user.post('/auth/login', data={'username': 'alice',
                                          'password': 'pw-alice-1'}).status_code == 302
    assert user.post('/settings/', data={'action': 'backup_now'}).status_code == 403
    assert calls == []


def test_an_admin_back_up_now_runs_the_backup(admin_client, calls):
    response = admin_client.post('/settings/', data={'action': 'backup_now'})
    assert response.status_code == 302 and calls == [True]
