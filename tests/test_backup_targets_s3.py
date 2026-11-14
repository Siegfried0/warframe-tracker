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


# ── The AWS environment (botocore's session variable table) ─────────────────
# The table is read from the installed botocore, never kept by hand here: a variable a later
# botocore adds gets its own row without a change to this file.

try:
    from botocore.configprovider import BOTOCORE_DEFAUT_SESSION_VARIABLES as SESSION_VARIABLES
    from botocore.configprovider import DEFAULT_S3_CONFIG_VARS as S3_VARIABLES
except ImportError:                       # boto3 not installed: the rows below are skipped
    SESSION_VARIABLES, S3_VARIABLES = {}, {}


def _env_names(entry):
    env = entry[1]
    return [] if env is None else [env] if isinstance(env, str) else list(env)


TABLES = list(SESSION_VARIABLES.values()) + list(S3_VARIABLES.values())
TABLE_ENV = sorted({name for entry in TABLES for name in _env_names(entry)})
PROXY_ENV = ['HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'no_proxy']


def _entry(name):
    return next(e for e in TABLES if name in _env_names(e))


def _hostile_value(name, tmp_path):
    """A value each variable would act on if it were read: one that would move, slow, weaken
    or break the client."""
    named = {
        'AWS_CA_BUNDLE': str(tmp_path / 'no-such-bundle.pem'),
        'AWS_DEFAULT_REGION': 'ap-south-1',
        'AWS_MAX_ATTEMPTS': '0',
        'AWS_RETRY_MODE': 'adaptive',
        'AWS_DEFAULTS_MODE': 'in-region',
        'AWS_PROFILE': 'no-such-profile',
        'AWS_DEFAULT_PROFILE': 'no-such-profile',
    }
    if name in named:
        return named[name]
    if name == 'AWS_DATA_PATH':
        # A service model botocore would load before its own: a broken one fails the client.
        model = tmp_path / 'models' / 's3' / '2006-03-01'
        model.mkdir(parents=True, exist_ok=True)
        (model / 'service-2.json').write_text('{ not json')
        return str(tmp_path / 'models')
    if name in ('AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE'):
        path = tmp_path / ('hostile-' + name.lower())
        path.write_text('[default]\nregion = ap-south-1\nmax_attempts = 0\n'
                        'ca_bundle = ' + str(tmp_path / 'no-such-bundle.pem') + '\n'
                        'use_fips_endpoint = true\nuse_dualstack_endpoint = true\n'
                        '[profile no-such-profile]\nregion = ap-south-1\n')
        return str(path)
    converter = _entry(name)[3]
    if converter is int:
        return '0'
    if getattr(converter, '__name__', '') == 'ensure_boolean':
        return 'true'
    return 'hostile-value'


def _clean_environment(monkeypatch):
    for name in TABLE_ENV + PROXY_ENV + ['REQUESTS_CA_BUNDLE']:
        monkeypatch.delenv(name, raising=False)


def _assert_botocore_defaults(client, region):
    """The client a clean environment gives: the region from BACKUP_S3_REGION (unset: botocore's
    S3 default), AWS's plain endpoint for it (no FIPS, no dual-stack), our timeouts and retries,
    botocore's default retry mode, and TLS verified against botocore's own certificates."""
    from botocore.httpsession import get_cert_path, where
    assert client.meta.region_name == (region or 'us-east-1')
    host = 's3.amazonaws.com' if region is None else f's3.{region}.amazonaws.com'
    assert client.meta.endpoint_url == f'https://{host}'
    # retries={'max_attempts': 2}: botocore stores it as the first try plus two retries.
    assert client.meta.config.retries == {'total_max_attempts': 3,
                                          'mode': SESSION_VARIABLES['retry_mode'][2]}
    assert (client.meta.config.connect_timeout, client.meta.config.read_timeout) == (10, 30)
    s3_defaults = {name: e[2] for name, e in S3_VARIABLES.items() if e[2] is not None}
    assert client.meta.config.s3 == (s3_defaults or None)
    http = client._endpoint.http_session
    assert http._verify is True
    assert get_cert_path(http._verify) == where()


REGION_SPELLINGS = [
    pytest.param(None, None, id='region unset'),
    pytest.param('', None, id='region empty'),
    pytest.param(' \t\n', None, id='region blanks only'),
    pytest.param('eu-central-1', 'eu-central-1', id='region eu-central-1'),
    pytest.param(' eu-central-1 ', 'eu-central-1', id='region with surrounding blanks'),
    pytest.param('no-such-region', 'no-such-region', id='nonsense region passed through'),
]
ENVIRONMENTS = (
    [pytest.param('all hostile', id='every table variable hostile'),
     pytest.param('all empty', id='every table variable empty'),
     pytest.param('none', id='no table variable set')]
    + [pytest.param(name, id=f'{name} hostile') for name in TABLE_ENV]
)


@pytest.mark.parametrize('environment', ENVIRONMENTS)
@pytest.mark.parametrize('raw_region,region', REGION_SPELLINGS)
def test_the_s3_client_ignores_every_variable_of_botocores_session_table(
        tmp_path, monkeypatch, environment, raw_region, region):
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    # A CA bundle from the environment, the one requests reads included, is never used.
    monkeypatch.setenv('REQUESTS_CA_BUNDLE', str(tmp_path / 'no-such-requests-bundle.pem'))
    for name in TABLE_ENV:
        if environment in ('all hostile', name):
            monkeypatch.setenv(name, _hostile_value(name, tmp_path))
        elif environment == 'all empty':
            monkeypatch.setenv(name, '')
    config = {'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'}
    if raw_region is not None:
        config['BACKUP_S3_REGION'] = raw_region
    sessions = _record_sessions(monkeypatch)
    _assert_botocore_defaults(bt._s3_client(config), region)
    # Many settings leave no trace on a client that makes no call (the metadata service, CSM,
    # the user agent): the session the client was built from must hold the default of each.
    import os
    overridden = {'config_file': os.devnull, 'credentials_file': os.devnull}
    [session] = sessions
    for name, entry in SESSION_VARIABLES.items():
        assert session.get_config_variable(name) == overridden.get(name, entry[2]), name


def _record_sessions(monkeypatch):
    """Every botocore session made while the test runs, so its settings can be read back."""
    import botocore.session
    made = []

    class Recorded(botocore.session.Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(botocore.session, 'Session', Recorded)
    return made


def test_the_session_table_is_read_from_botocore_and_holds_the_variables_the_rule_names():
    # The rows above come from botocore's own table: were its name or shape to change, they
    # would silently vanish. The variables the rule names must be among them.
    pytest.importorskip('boto3')
    assert all(len(entry) == 4 for entry in TABLES)
    assert {'AWS_S3_USE_ARN_REGION', 'AWS_S3_US_EAST_1_REGIONAL_ENDPOINT', 'AWS_CA_BUNDLE',
            'AWS_DEFAULT_REGION', 'AWS_DATA_PATH', 'AWS_MAX_ATTEMPTS',
            'AWS_RETRY_MODE', 'AWS_DEFAULTS_MODE', 'AWS_USE_FIPS_ENDPOINT',
            'AWS_USE_DUALSTACK_ENDPOINT', 'AWS_PROFILE', 'AWS_CONFIG_FILE',
            'AWS_SHARED_CREDENTIALS_FILE'} <= set(TABLE_ENV)


def test_two_clients_in_one_process_see_the_same_settings(tmp_path, monkeypatch):
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    for name in TABLE_ENV:
        monkeypatch.setenv(name, _hostile_value(name, tmp_path))
    config = {'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's',
              'BACKUP_S3_REGION': 'eu-central-1'}
    first, second = bt._s3_client(config), bt._s3_client(config)
    assert first is not second
    for client in (first, second):
        _assert_botocore_defaults(client, 'eu-central-1')


@pytest.mark.parametrize('variables,proxied', [
    ({'HTTPS_PROXY': 'http://127.0.0.1:9'}, True),
    ({'https_proxy': 'http://127.0.0.1:9'}, True),
    ({'HTTPS_PROXY': 'http://127.0.0.1:9', 'NO_PROXY': 's3.amazonaws.com'}, False),
    ({}, False),
], ids=['HTTPS_PROXY', 'https_proxy', 'NO_PROXY bypasses it', 'no proxy'])
def test_the_proxy_variables_still_apply(monkeypatch, variables, proxied):
    # The machine's network settings, not another tool's: an operator behind a proxy needs them.
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    client = bt._s3_client({'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'})
    proxies = client._endpoint.http_session._proxy_config._proxies
    assert proxies == ({'https': 'http://127.0.0.1:9'} if proxied else {})


@pytest.mark.parametrize('raw,expected', [
    (None, None), ('', None), ('   ', None), ('eu-central-1', 'eu-central-1'),
    (' eu-central-1 ', 'eu-central-1'), ('\teu-west-2\n', 'eu-west-2'),
])
def test_the_s3_region_is_stripped_and_blank_means_botocores_default(raw, expected):
    assert bt.s3_region({} if raw is None else {'BACKUP_S3_REGION': raw}) == expected


def test_the_app_configuration_reads_the_s3_region_from_the_environment(monkeypatch):
    # A separate copy of the module: reloading `config` itself would replace the `Config` class
    # other modules and tests already hold.
    import importlib.util

    import config as app_config
    monkeypatch.setenv('BACKUP_S3_REGION', ' eu-central-1 ')
    spec = importlib.util.spec_from_file_location('config_copy_for_region', app_config.__file__)
    copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy)
    assert copy.Config.BACKUP_S3_REGION == ' eu-central-1 '


# ── Files under ~/.aws (botocore's customer data folder, ~/.aws/models) ───────
# botocore searches ~/.aws/models before its own data, whatever the environment says: a file
# another tool left there would change or break the backup client. The folder's location is
# fixed when botocore is imported (from the home folder), so the test moves it to tmp_path.

def _home_models(tmp_path, monkeypatch):
    import botocore.loaders
    folder = tmp_path / 'home' / '.aws' / 'models'
    folder.mkdir(parents=True)
    monkeypatch.setattr(botocore.loaders.Loader, 'CUSTOMER_DATA_PATH', str(folder))
    return folder


def _broken_s3_model(folder):
    model = folder / 's3' / '2006-03-01'
    model.mkdir(parents=True)
    (model / 'service-2.json').write_text('{ not json')


def _endpoints_elsewhere(folder):
    """botocore's own endpoints table, copied, with every S3 host renamed: a file of the shape
    another tool (or a hostile one) could leave in ~/.aws/models."""
    import json

    import botocore.loaders
    builtin = botocore.loaders.Loader(include_default_search_paths=False,
                                      extra_search_paths=[botocore.loaders.Loader.BUILTIN_DATA_PATH])
    endpoints = builtin.load_data('endpoints')
    for partition in endpoints['partitions']:
        s3 = partition['services'].get('s3')
        if s3:
            s3.setdefault('defaults', {})['hostname'] = 'elsewhere.example.invalid'
            for endpoint in s3.get('endpoints', {}).values():
                endpoint['hostname'] = 'elsewhere.example.invalid'
    (folder / 'endpoints.json').write_text(json.dumps(endpoints))


@pytest.mark.parametrize('plant', [_broken_s3_model, _endpoints_elsewhere],
                         ids=['a broken s3 model in ~/.aws/models',
                              'endpoints in ~/.aws/models naming another host'])
@pytest.mark.parametrize('raw_region,region', [(None, None), ('eu-central-1', 'eu-central-1')],
                         ids=['region unset', 'region eu-central-1'])
def test_the_s3_client_ignores_the_aws_models_folder_in_the_home_folder(
        tmp_path, monkeypatch, plant, raw_region, region):
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    plant(_home_models(tmp_path, monkeypatch))
    config = {'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'}
    if raw_region is not None:
        config['BACKUP_S3_REGION'] = raw_region
    client = bt._s3_client(config)
    _assert_botocore_defaults(client, region)
    # The request itself, not only the client's record of its endpoint (nothing is sent: the
    # handler answers before the HTTP layer).
    from botocore.awsrequest import AWSResponse
    sent = []

    def answer(request, **kwargs):
        sent.append(request.url)
        return AWSResponse(request.url, 500, {}, None)
    client.meta.events.register('before-send', answer)
    with pytest.raises(Exception):
        client.list_objects_v2(Bucket='b', Prefix='p/', Delimiter='/')
    assert sent and all('elsewhere' not in url for url in sent)


@pytest.mark.parametrize('variable', ['AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN',
                                      'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'])
def test_the_s3_client_signs_with_the_backup_keys_alone(monkeypatch, variable):
    # Credential variables are outside botocore's session table: the explicit keys win, and
    # no session token from the environment is attached to them.
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    monkeypatch.setenv(variable, 'from-the-environment')
    client = bt._s3_client({'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'})
    credentials = client._request_signer._credentials
    assert (credentials.access_key, credentials.secret_key, credentials.token) == ('k', 's', None)


@pytest.mark.parametrize('raw', ['eu central 1', 'eu-central-1.elsewhere.example.invalid',
                                 'eu-central-1/x', '-eu', 'é'])
def test_a_region_botocore_refuses_fails_the_s3_target_only(tmp_path, monkeypatch, raw):
    # Not validated by the app (passed through), but never a host or a path: botocore refuses it
    # when the client is built, and the backup reports the S3 target as failed.
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    from services import backup_service

    class App:
        config = {'BACKUP_S3_ACCESS_KEY': 'AKIDEXAMPLEKEY', 'BACKUP_S3_SECRET_KEY': 'SECRETEXAMPLE',
                  'BACKUP_S3_BUCKET': 'b', 'BACKUP_S3_REGION': raw}
    monkeypatch.setattr(backup_service, 'read_retention', lambda target: None)
    result = backup_service.backup_target(App, 's3', True, NOW)
    assert result['ok'] is False
    assert 'SECRETEXAMPLE' not in result['error'] and 'AKIDEXAMPLEKEY' not in result['error']


@pytest.mark.parametrize('variables', [
    {'AWS_ENDPOINT_URL': 'https://elsewhere.example.invalid'},
    {'AWS_ENDPOINT_URL_S3': 'https://elsewhere.example.invalid'},
    {'AWS_ENDPOINT_URL_S3': 'https://elsewhere.example.invalid',
     'AWS_IGNORE_CONFIGURED_ENDPOINT_URLS': 'false'},
], ids=['generic endpoint', 's3 endpoint', 's3 endpoint, configured endpoints not ignored'])
@pytest.mark.parametrize('endpoint,region', [
    (None, 'eu-central-1'), ('https://store.example.invalid', 'eu-central-1'),
    ('https://store.example.invalid', None),
], ids=['aws region', 'own endpoint and region', 'own endpoint'])
def test_an_endpoint_from_the_environment_never_replaces_the_configured_target(
        monkeypatch, variables, endpoint, region):
    # The endpoint and the region come from the backup settings alone, whichever of them is set.
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    config = {'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'}
    if endpoint:
        config['BACKUP_S3_ENDPOINT'] = endpoint
    if region:
        config['BACKUP_S3_REGION'] = region
    client = bt._s3_client(config)
    assert client.meta.endpoint_url == (endpoint or f'https://s3.{region}.amazonaws.com')
    assert client.meta.region_name == (region or 'us-east-1')
