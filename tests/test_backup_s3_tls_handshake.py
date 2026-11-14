"""The S3 client against a real TLS server on loopback, whose certificate a private certificate
authority signed: the client trusts it through BACKUP_S3_CA_BUNDLE only (never AWS_CA_BUNDLE,
never botocore's own certificates), and a completed handshake writes no TLS key log, whatever
SSLKEYLOGFILE holds. The server listens on 127.0.0.1 only; nothing reaches the network.
The certificates are made for each test with the openssl command (skipped where it is missing)."""
import http.server
import os
import shutil
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from services import backup_targets as bt
from test_backup_targets_s3 import _clean_environment

LISTING = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
           b'<Name>bucket</Name><Prefix></Prefix><KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys>'
           b'<IsTruncated>false</IsTruncated></ListBucketResult>')


def _openssl(*args, cwd):
    # Its own minimal configuration: an installed openssl may point at a file that is not there.
    (cwd / 'openssl.cnf').write_text('[req]\ndistinguished_name=dn\n[dn]\n')
    env = dict(os.environ, OPENSSL_CONF=str(cwd / 'openssl.cnf'))
    subprocess.run(['openssl', *args], cwd=cwd, check=True, capture_output=True, timeout=60,
                   env=env)


@pytest.fixture
def private_ca(tmp_path):
    """A private authority and a server certificate it signed for 127.0.0.1."""
    if not shutil.which('openssl'):
        pytest.skip('no openssl command here to make the certificates')
    folder = tmp_path / 'pki'
    folder.mkdir()
    (folder / 'ext.cnf').write_text('subjectAltName=IP:127.0.0.1\n'
                                    'basicConstraints=CA:FALSE\n')
    _openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key',
             '-out', 'ca.pem', '-days', '2', '-subj', '/CN=Test Home CA',
             '-addext', 'basicConstraints=critical,CA:TRUE',
             '-addext', 'keyUsage=critical,keyCertSign,cRLSign', cwd=folder)
    _openssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'server.key',
             '-out', 'server.csr', '-subj', '/CN=127.0.0.1', cwd=folder)
    _openssl('x509', '-req', '-in', 'server.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
             '-CAcreateserial', '-out', 'server.pem', '-days', '2', '-extfile', 'ext.cnf',
             cwd=folder)
    return folder


@pytest.fixture
def store(private_ca):
    """An HTTPS server on 127.0.0.1 answering every GET with an empty bucket listing."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/xml')
            self.send_header('Content-Length', str(len(LISTING)))
            self.end_headers()
            self.wfile.write(LISTING)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.handle_error = lambda *args: None       # a refused handshake is the client's answer
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(private_ca / 'server.pem', private_ca / 'server.key')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05},
                              daemon=True)
    thread.start()
    try:
        yield f'https://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


def _config(endpoint, bundle=None):
    config = {'BACKUP_S3_ENDPOINT': endpoint, 'BACKUP_S3_ACCESS_KEY': 'k',
              'BACKUP_S3_SECRET_KEY': 's', 'BACKUP_S3_REGION': 'us-east-1'}
    if bundle is not None:
        config['BACKUP_S3_CA_BUNDLE'] = bundle
    return config


@pytest.mark.parametrize('when', ['before', 'after'],
                         ids=['key log set before the client', 'key log set after the client'])
def test_a_store_behind_a_private_ca_is_reached_through_the_bundle_and_no_key_is_logged(
        tmp_path, monkeypatch, private_ca, store, when):
    pytest.importorskip('boto3')
    _clean_environment(monkeypatch)
    keylog = tmp_path / 'keys' / 'tls.log'
    keylog.parent.mkdir()
    if when == 'before':
        monkeypatch.setenv('SSLKEYLOGFILE', str(keylog))
    client = bt._s3_client(_config(store, f'  {private_ca / "ca.pem"}  '))
    if when == 'after':
        monkeypatch.setenv('SSLKEYLOGFILE', str(keylog))
    answer = client.list_objects_v2(Bucket='bucket')
    assert answer['ResponseMetadata']['HTTPStatusCode'] == 200
    assert answer.get('KeyCount') == 0
    assert not keylog.exists() and os.listdir(keylog.parent) == []
    assert os.environ['SSLKEYLOGFILE'] == str(keylog)


@pytest.mark.parametrize('aws_bundle', [False, True], ids=['no AWS_CA_BUNDLE', 'AWS_CA_BUNDLE set'])
def test_without_the_bundle_a_private_ca_store_fails_verification(
        monkeypatch, private_ca, store, aws_bundle):
    pytest.importorskip('boto3')
    from botocore.exceptions import SSLError
    _clean_environment(monkeypatch)
    if aws_bundle:      # never applies: the client verifies against botocore's certificates
        monkeypatch.setenv('AWS_CA_BUNDLE', str(private_ca / 'ca.pem'))
    client = bt._s3_client(_config(store))
    with pytest.raises(SSLError):
        client.list_objects_v2(Bucket='bucket')


def test_a_bundle_without_the_stores_authority_fails_verification(
        tmp_path, monkeypatch, private_ca, store):
    # The bundle replaces botocore's certificates; one that lacks the store's authority fails.
    pytest.importorskip('boto3')
    from botocore.exceptions import SSLError
    from botocore.httpsession import where
    _clean_environment(monkeypatch)
    public = tmp_path / 'public.pem'
    public.write_bytes(Path(where()).read_bytes())
    client = bt._s3_client(_config(store, str(public)))
    with pytest.raises(SSLError):
        client.list_objects_v2(Bucket='bucket')
