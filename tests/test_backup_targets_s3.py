"""The S3 prefix as a backup target: its configuration, the client seam, listing, upload and
delete. S3 is botocore's Stubber on a client whose endpoint does not exist, so nothing reaches
the network."""
import sqlite3
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

from services import backup_targets as bt

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
NAME = 'warframe_tracker_20261114T120000Z.db'


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'live.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE t (v TEXT)')
    conn.execute("INSERT INTO t VALUES ('committed')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def folder(tmp_path):
    return tmp_path / 'backups'


# ── Configuration ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw,expected', [
    ('warframe-tracker/', 'warframe-tracker/'), ('warframe-tracker', 'warframe-tracker/'),
    ('  /warframe-tracker//  ', 'warframe-tracker/'), ('a/b', 'a/b/'), ('', ''), ('/', ''),
    (None, ''), ('   ', ''), ('//a', 'a/'), ('///', ''), ('a//', 'a/'),
], ids=['slash', 'no slash', 'blanks and slashes', 'nested', 'empty', 'only a slash', 'unset',
        'blanks', 'two leading slashes', 'only slashes', 'two trailing slashes'])
def test_the_s3_prefix_ends_in_exactly_one_slash_or_is_the_bucket_root(raw, expected):
    assert bt.s3_prefix({'BACKUP_S3_PREFIX': raw}) == expected


def test_the_s3_client_gives_up_on_an_unreachable_endpoint_within_about_a_minute(tmp_path,
                                                                               monkeypatch):
    pytest.importorskip('boto3')
    monkeypatch.setenv('AWS_CONFIG_FILE', str(tmp_path / 'none'))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(tmp_path / 'none'))
    client = bt._s3_client({'BACKUP_S3_ENDPOINT': ' https://s3.example.invalid ',
                            'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'})
    config = client.meta.config
    assert (config.connect_timeout, config.read_timeout) == (10, 30)
    # retries={'max_attempts': 2}: botocore stores it as the first try plus two retries.
    assert config.retries['total_max_attempts'] == 3
    assert client.meta.endpoint_url == 'https://s3.example.invalid'


@pytest.mark.parametrize('keys', [
    {}, {'BACKUP_S3_ACCESS_KEY': 'k'}, {'BACKUP_S3_SECRET_KEY': 's'},
    {'BACKUP_S3_ACCESS_KEY': '  ', 'BACKUP_S3_SECRET_KEY': 's'},
    {'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': ''},
], ids=['neither', 'no secret key', 'no access key', 'blank access key', 'empty secret key'])
def test_a_bucket_without_both_keys_makes_no_client_and_looks_up_no_other_credentials(
        keys, monkeypatch):
    # boto3 made unimportable: a lookup of AWS_* or ~/.aws would need it first.
    monkeypatch.setitem(sys.modules, 'boto3', None)
    config = {'BACKUP_S3_BUCKET': 'b', **keys}
    assert bt.s3_keys_missing(config) == bt.S3_KEYS_REASON
    with pytest.raises(ValueError, match='must both be set'):
        bt._s3_client(config)


def test_both_keys_set_is_configured():
    assert bt.s3_keys_missing({'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'}) is None


def test_boto3_missing_fails_the_s3_client_only(database, folder, monkeypatch):
    monkeypatch.setitem(sys.modules, 'boto3', None)
    with pytest.raises(ImportError):
        bt._s3_client({'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'})
    assert bt.write_local(folder, database, NOW) == str(folder / NAME)

# ── The S3 prefix (botocore's Stubber) ───────────────────────────────────────

@pytest.fixture
def s3():
    boto3 = pytest.importorskip('boto3')
    from botocore.stub import Stubber
    client = boto3.session.Session().client(
        's3', region_name='us-east-1', endpoint_url='https://s3.example.invalid',
        aws_access_key_id='test', aws_secret_access_key='test')
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()


def listing(prefix, token=None):
    request = {'Bucket': 'b', 'Prefix': prefix, 'Delimiter': '/'}
    if token:
        request['ContinuationToken'] = token
    return request


def test_the_listing_follows_every_page_under_the_prefix(s3):
    client, stub = s3
    stub.add_response('list_objects_v2', {
        'IsTruncated': True, 'NextContinuationToken': 't1',
        'Contents': [{'Key': 'p/' + NAME}], 'CommonPrefixes': [{'Prefix': 'p/old/'}]},
        listing('p/'))
    stub.add_response('list_objects_v2', {
        'IsTruncated': False, 'Contents': [{'Key': 'p/notes.txt'}]}, listing('p/', 't1'))
    assert bt.list_s3(client, 'b', 'p/') == [NAME, 'notes.txt']


def test_a_truncated_page_without_a_token_fails_the_listing(s3):
    client, stub = s3
    stub.add_response('list_objects_v2', {'IsTruncated': True, 'Contents': []}, listing('p/'))
    with pytest.raises(RuntimeError, match='continuation token'):
        bt.list_s3(client, 'b', 'p/')


def test_a_refused_listing_raises(s3):
    client, stub = s3
    stub.add_client_error('list_objects_v2', 'AccessDenied', http_status_code=403)
    with pytest.raises(Exception, match='AccessDenied'):
        bt.list_s3(client, 'b', 'p/')


def test_the_upload_is_a_copy_staged_in_a_folder_that_is_removed(s3, database, monkeypatch):
    from botocore.stub import ANY
    client, stub = s3
    staged = []
    real_copy = bt.copy_database

    def recording(source, dest, **kwargs):
        staged.append(dest)
        return real_copy(source, dest, **kwargs)
    monkeypatch.setattr(bt, 'copy_database', recording)
    stub.add_response('put_object', {}, {'Bucket': 'b', 'Key': 'p/' + NAME, 'Body': ANY})
    # One PutObject, never a managed (multipart) upload that could leave a part behind.
    monkeypatch.setattr(client, 'upload_file', mock.Mock(side_effect=AssertionError('multipart')),
                        raising=False)
    assert bt.write_s3(client, 'b', 'p/', database, NOW) == 'p/' + NAME
    assert staged and not staged[0].parent.exists()
    assert staged[0].parent.name.startswith('warframe_tracker_backup_')


def test_a_refused_upload_raises_and_removes_the_staging_folder(s3, database, monkeypatch):
    client, stub = s3
    staged = []
    monkeypatch.setattr(bt, 'copy_database',
                        lambda source, dest, **k: (staged.append(dest), dest.write_bytes(b'x')))
    stub.add_client_error('put_object', 'AccessDenied', http_status_code=403)
    with pytest.raises(Exception):
        bt.write_s3(client, 'b', 'p/', database, NOW)
    assert not staged[0].parent.exists()


def test_deletes_go_in_batches_and_count_each_failure(s3):
    client, stub = s3
    names = [f'n{i:04d}' for i in range(1001)]
    stub.add_response('delete_objects', {
        'Deleted': [{'Key': 'p/n0000'}],
        'Errors': [{'Key': 'p/n0001', 'Code': 'AccessDenied', 'Message': 'no'}]},
        {'Bucket': 'b', 'Delete': {'Objects': [{'Key': 'p/' + n} for n in names[:1000]]}})
    stub.add_client_error('delete_objects', 'SlowDown', http_status_code=503)
    assert bt.delete_s3(client, 'b', 'p/', names) == (999, 2)


@pytest.mark.parametrize('count,batches', [(1000, [1000]), (2000, [1000, 1000]),
                                           (2001, [1000, 1000, 1])],
                         ids=['exactly one batch', 'exactly two batches', 'one key over'])
def test_deletes_split_at_exactly_1000_keys(s3, count, batches):
    client, stub = s3
    names = [f'n{i:04d}' for i in range(count)]
    start = 0
    for size in batches:
        batch = names[start:start + size]
        stub.add_response('delete_objects', {'Deleted': [{'Key': 'p/' + n} for n in batch]},
                          {'Bucket': 'b', 'Delete': {'Objects': [{'Key': 'p/' + n} for n in batch]}})
        start += size
    assert bt.delete_s3(client, 'b', 'p/', names) == (count, 0)


@pytest.mark.parametrize('variable,value', [
    ('AWS_ENDPOINT_URL', 'https://elsewhere.example.invalid'),
    ('AWS_ENDPOINT_URL_S3', 'https://elsewhere.example.invalid'),
    ('AWS_PROFILE', 'no-such-profile'),
], ids=['generic endpoint', 's3 endpoint', 'missing profile'])
def test_the_s3_client_ignores_the_ambient_aws_configuration(tmp_path, monkeypatch, variable,
                                                             value):
    # The target is configured by BACKUP_S3_* alone: an AWS_* variable meant for another tool
    # must neither send the database elsewhere nor stop the backup.
    pytest.importorskip('boto3')
    monkeypatch.setenv('AWS_CONFIG_FILE', str(tmp_path / 'none'))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(tmp_path / 'none'))
    monkeypatch.setenv(variable, value)
    client = bt._s3_client({'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'})
    assert 'elsewhere' not in client.meta.endpoint_url


def test_the_s3_client_ignores_a_profile_in_the_aws_config_file(tmp_path, monkeypatch):
    # A config file another tool uses, with a profile that is selected and one named default.
    pytest.importorskip('boto3')
    settings = ('endpoint_url = https://elsewhere.example.invalid\n'
                'ca_bundle = ' + str(tmp_path / 'no-such-bundle.pem') + '\n'
                's3 =\n    addressing_style = virtual\n')
    aws_config = tmp_path / 'config'
    aws_config.write_text('[default]\n' + settings + '[profile other]\n' + settings)
    monkeypatch.setenv('AWS_CONFIG_FILE', str(aws_config))
    monkeypatch.setenv('AWS_PROFILE', 'other')
    client = bt._s3_client({'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'})
    assert 'elsewhere' not in client.meta.endpoint_url
    assert client.meta.config.s3 is None or 'addressing_style' not in client.meta.config.s3


def test_the_s3_client_uses_the_keys_without_surrounding_blanks(tmp_path, monkeypatch):
    pytest.importorskip('boto3')
    monkeypatch.setenv('AWS_CONFIG_FILE', str(tmp_path / 'none'))
    client = bt._s3_client({'BACKUP_S3_ACCESS_KEY': ' k \n', 'BACKUP_S3_SECRET_KEY': '\ts '})
    credentials = client._request_signer._credentials
    assert (credentials.access_key, credentials.secret_key) == ('k', 's')


def test_deleting_nothing_makes_no_request(s3):
    client, _ = s3
    assert bt.delete_s3(client, 'b', 'p/', []) == (0, 0)
