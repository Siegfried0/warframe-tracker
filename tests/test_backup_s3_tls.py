"""The S3 client's TLS: verified against BACKUP_S3_CA_BUNDLE when set (else botocore's own
certificates), and no TLS context of the client writes a key log, whatever SSLKEYLOGFILE holds.
Every client here makes no call: nothing reaches the network."""
import os
import sys

import pytest

from services import backup_targets as bt
from test_backup_targets_s3 import _clean_environment


# ── BACKUP_S3_CA_BUNDLE (a private certificate authority) ───────────────────
# Each spelling builds a client that makes no call and reads its verify setting back;
# AWS_CA_BUNDLE names another real file throughout, so it would show if it applied.

def _pem(tmp_path, name='ca.pem'):
    """A real PEM bundle (a copy of botocore's own), so a TLS context can load it."""
    from botocore.httpsession import where
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(where(), 'rb') as source:
        path.write_bytes(source.read())
    return path


def _link(tmp_path, target):
    link = tmp_path / 'link.pem'
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'this system made no link here: {exc}')
    return str(link)


def _folder(tmp_path):
    path = tmp_path / 'certs'
    path.mkdir()
    return str(path)


DEFAULT = 'botocore default'
ABSOLUTE, NOT_A_FILE = bt.CA_BUNDLE_ABSOLUTE_REASON, bt.CA_BUNDLE_NOT_A_FILE_REASON
# (the raw value, made in tmp_path; what the client verifies against, or the refusal)
CA_SPELLINGS = [
    pytest.param(lambda t: None, DEFAULT, id='unset'),
    pytest.param(lambda t: '', DEFAULT, id='empty'),
    pytest.param(lambda t: ' \t\n', DEFAULT, id='blanks only'),
    pytest.param(lambda t: str(_pem(t)), 'file', id='an absolute file'),
    pytest.param(lambda t: f'  {_pem(t)}\t\n', 'file', id='surrounding blanks'),
    pytest.param(lambda t: str(_pem(t, 'my certs/ca bundle.pem')), 'file', id='blanks inside'),
    pytest.param(lambda t: str(_pem(t, 'zertifikäte/ca€.pem')), 'file', id='non-ASCII inside'),
    pytest.param(lambda t: _link(t, _pem(t)), 'file', id='a link to a file'),
    pytest.param(lambda t: 'ca.pem', ABSOLUTE, id='relative'),
    pytest.param(lambda t: '\\ca.pem', ABSOLUTE, id='rooted without a drive'),
    pytest.param(lambda t: 'C:ca.pem', ABSOLUTE, id='a drive without a root'),
    pytest.param(lambda t: 'false', ABSOLUTE, id='false is a path'),
    pytest.param(lambda t: '0', ABSOLUTE, id='0 is a path'),
    pytest.param(lambda t: 'no', ABSOLUTE, id='no is a path'),
    pytest.param(_folder, NOT_A_FILE, id='an absolute folder'),
    pytest.param(lambda t: str(t / 'missing.pem'), NOT_A_FILE, id='a missing file'),
    pytest.param(lambda t: _link(t, t / 'never-there.pem'), NOT_A_FILE,
                 id='a link that leads nowhere'),
    pytest.param(lambda t: str(t / 'nul\x00.pem'), NOT_A_FILE, id='a NUL inside'),
]


@pytest.mark.parametrize('make,expected', CA_SPELLINGS)
def test_the_s3_ca_bundle_is_the_clients_verify_setting_or_fails_before_boto3(
        tmp_path, monkeypatch, make, expected):
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    monkeypatch.setenv('AWS_CA_BUNDLE', str(_pem(tmp_path, 'aws/other.pem')))
    raw = make(tmp_path)
    config = {'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'}
    if raw is not None:
        config['BACKUP_S3_CA_BUNDLE'] = raw
    if expected in (ABSOLUTE, NOT_A_FILE):
        # boto3 made unimportable: the refusal comes first, and names the setting only.
        monkeypatch.setitem(sys.modules, 'boto3', None)
        with pytest.raises(ValueError) as refused:
            bt._s3_client(config)
        assert str(refused.value) == expected
        assert raw.strip() not in str(refused.value)
        return
    from botocore.httpsession import get_cert_path, where
    # Two clients in one process see the same setting (nothing is kept between them).
    for client in (bt._s3_client(config), bt._s3_client(config)):
        http = client._endpoint.http_session
        if expected == DEFAULT:
            assert http._verify is True
            assert get_cert_path(http._verify) == where()
            assert http._proxy_config.settings['proxy_ca_bundle'] == where()
        else:
            assert http._verify == raw.strip()
            assert http._proxy_config.settings['proxy_ca_bundle'] == raw.strip()


def test_the_app_configuration_reads_the_s3_ca_bundle_from_the_environment(monkeypatch):
    import importlib.util

    import config as app_config
    monkeypatch.setenv('BACKUP_S3_CA_BUNDLE', ' /etc/ssl/home-ca.pem ')
    spec = importlib.util.spec_from_file_location('config_copy_for_ca', app_config.__file__)
    copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy)
    assert copy.Config.BACKUP_S3_CA_BUNDLE == ' /etc/ssl/home-ca.pem '


@pytest.mark.parametrize('make,reason', [
    pytest.param(lambda t: 'ca.pem', ABSOLUTE, id='relative'),
    pytest.param(lambda t: str(t / 'missing.pem'), NOT_A_FILE, id='a missing file'),
    pytest.param(_folder, NOT_A_FILE, id='an absolute folder'),
])
def test_a_bad_ca_bundle_fails_the_s3_target_only_and_the_local_backup_is_made(
        app, tmp_path, monkeypatch, make, reason):
    from datetime import datetime, timezone

    from services import backup_service
    folder = tmp_path / 'backups'
    app.config.update(BACKUP_LOCAL_PATH=str(folder), BACKUP_S3_BUCKET='bucket',
                      BACKUP_S3_ACCESS_KEY='test', BACKUP_S3_SECRET_KEY='test',
                      BACKUP_S3_CA_BUNDLE=make(tmp_path))
    # boto3 made unimportable: a client built (and so any S3 call, a delete included) would
    # report it missing instead.
    monkeypatch.setitem(sys.modules, 'boto3', None)
    with app.app_context():
        result = backup_service.backup_now(
            app, force=True, now=datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc))
    assert result['s3'] == {'ok': False, 'error': reason}
    assert result['local']['ok'] and result['local']['written']
    assert os.listdir(folder) == ['warframe_tracker_20261114T120000Z.db']


# ── No TLS key log (SSLKEYLOGFILE) ───────────────────────────────────────────
# botocore's own context factory sets every context's key log from SSLKEYLOGFILE when it builds
# it: at client construction (the pool) and at a proxy's first request (each proxy manager's
# pool, and the context of the link to an HTTPS proxy). Each row reads every one back from a
# client that makes no call; the proxy hosts do not exist and are never contacted.

PROXIES = ('https://proxy.invalid:3128', 'http://proxy2.invalid:3128')


def _contexts(client):
    http = client._endpoint.http_session
    found = [('pool', http._manager.connection_pool_kw['ssl_context'])]
    for url in PROXIES:
        manager = http._get_proxy_manager(url)
        found.append((url + ' pool', manager.connection_pool_kw['ssl_context']))
        if url.startswith('https'):
            found.append((url + ' link', manager.proxy_config.ssl_context))
    found.append(('a context built later', http._get_ssl_context()))
    return found


KEYLOG_SPELLINGS = {
    'unset': None,
    'empty': '',
    'a new file': 'new.log',
    'an existing file': 'existing.log',
    'blanks inside': 'with blanks/key log.txt',
}


@pytest.mark.parametrize('bundle', [False, True], ids=['no CA bundle', 'CA bundle'])
@pytest.mark.parametrize('when', ['before', 'after'],
                         ids=['set before the client', 'set after the client'])
@pytest.mark.parametrize('kind', list(KEYLOG_SPELLINGS))
def test_no_tls_context_of_the_s3_client_writes_a_key_log(tmp_path, monkeypatch, kind, when,
                                                          bundle):
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    keys = tmp_path / 'keys'
    (keys / 'with blanks').mkdir(parents=True)
    (keys / 'existing.log').write_bytes(b'old')
    name = KEYLOG_SPELLINGS[kind]
    value = name if not name else str(keys / name)
    config = {'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'}
    if bundle:
        config['BACKUP_S3_CA_BUNDLE'] = str(_pem(tmp_path))
    if value is not None and when == 'before':
        monkeypatch.setenv('SSLKEYLOGFILE', value)
    client = bt._s3_client(config)
    if value is not None and when == 'after':
        monkeypatch.setenv('SSLKEYLOGFILE', value)
    contexts = _contexts(client)
    assert len(contexts) == 5
    for where, context in contexts:
        assert context.keylog_filename is None, where
    # Nothing written: no new file, the existing one unchanged.
    assert sorted(p.name for p in keys.rglob('*')) == ['existing.log', 'with blanks']
    assert (keys / 'existing.log').read_bytes() == b'old'
    # The app never changes the environment.
    assert os.environ.get('SSLKEYLOGFILE') == value
    # The verify setting is the bundle's, or botocore's default.
    assert client._endpoint.http_session._verify == config.get('BACKUP_S3_CA_BUNDLE', True)


def test_the_https_proxy_link_is_verified_against_the_bundle(tmp_path, monkeypatch):
    import ssl
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    client = bt._s3_client({'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's',
                            'BACKUP_S3_CA_BUNDLE': str(_pem(tmp_path))})
    http = client._endpoint.http_session
    named = http._get_proxy_manager('https://proxy.invalid:3128').proxy_config.ssl_context
    numeric = http._get_proxy_manager('https://10.0.0.1:3128').proxy_config.ssl_context
    for context in (named, numeric):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.cert_store_stats()['x509_ca'] > 0
    # botocore checks the host name of a named proxy itself (never of an IP address).
    assert (named.check_hostname, numeric.check_hostname) == (True, False)


def _botocore_factory_copy():
    """botocore's own context factory, from a fresh copy of its module (the app replaces the
    name in the loaded one)."""
    import importlib.util

    import botocore.httpsession
    spec = importlib.util.spec_from_file_location('botocore_httpsession_copy',
                                                  botocore.httpsession.__file__)
    copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy)
    return copy.create_urllib3_context


TLS_SETTINGS = ['protocol', 'options', 'verify_mode', 'check_hostname', 'post_handshake_auth',
                'minimum_version', 'maximum_version', 'verify_flags',
                'hostname_checks_common_name']


def test_the_apps_tls_context_has_botocores_settings_without_the_key_log():
    # A botocore upgrade that changes its settings fails here; it never silently weakens TLS.
    pytest.importorskip('boto3')
    theirs, ours = _botocore_factory_copy()(), bt._tls_context()
    for name in TLS_SETTINGS:
        assert getattr(ours, name) == getattr(theirs, name), name
    assert [c['name'] for c in ours.get_ciphers()] == [c['name'] for c in theirs.get_ciphers()]
    assert ours.keylog_filename is None


def test_the_s3_client_replaces_botocores_context_factory(monkeypatch):
    pytest.importorskip('boto3')
    import botocore.httpsession
    _clean_environment(monkeypatch)
    bt._s3_client({'BACKUP_S3_ACCESS_KEY': 'k', 'BACKUP_S3_SECRET_KEY': 's'})
    assert botocore.httpsession.create_urllib3_context is bt._tls_context
