"""Smoke tests: the app starts on an empty database, migrates, and serves its pages."""
import pytest

from models.db import get_connection

PAGES = [
    '/dashboard',
    '/collection/warframe',
    '/collection/mod',
    '/collection/arcane',
    '/farming/',
    '/market/',
    '/settings/',
    '/admin/',
    '/sync/',
]


def test_empty_database_migrates_to_the_current_schema(app):
    with app.app_context():
        db = get_connection()
        try:
            version = db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
        finally:
            db.close()
    assert version == 11


def test_first_run_sends_every_visitor_to_the_setup_wizard(client):
    assert client.get('/auth/setup').status_code == 200
    response = client.get('/auth/login')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/auth/setup')


def test_pages_need_a_login(client):
    response = client.get('/dashboard')
    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


@pytest.mark.parametrize('path', PAGES)
def test_every_page_renders_for_the_admin(admin_client, path):
    assert admin_client.get(path).status_code == 200


def test_the_wizard_runs_once(admin_client):
    response = admin_client.get('/auth/setup')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dashboard')


def test_no_test_reaches_the_network():
    import socket

    sock = socket.socket()
    try:
        with pytest.raises(RuntimeError, match='must not reach the network'):
            sock.connect(('192.0.2.1', 80))
    finally:
        sock.close()


# The name lookups: a refused host raises before the real resolver runs, so no DNS query
# leaves. The guard's saved real function is swapped for a recorder, so no row looks anything up.
REFUSED_HOSTS = [
    pytest.param('wiki.warframe.com', id='a name'),
    pytest.param('WIKI.warframe.com', id='a name in capitals'),
    pytest.param(b'wiki.warframe.com', id='a name as bytes'),
    pytest.param('192.0.2.1', id='an IPv4 literal outside loopback'),
    pytest.param('128.0.0.1', id='an IPv4 literal just outside 127/8'),
    pytest.param('2001:db8::1', id='an IPv6 literal outside loopback'),
    pytest.param('example.invalid', id='an invalid name'),
    pytest.param('wikí.example', id='a non-ASCII name'),
    pytest.param(b'\xffwiki', id='undecodable bytes'),
    pytest.param('localhost..', id='localhost with two trailing dots'),
    pytest.param('localhost.example', id='a name starting with localhost'),
    pytest.param(' localhost', id='localhost with a blank'),
]
ALLOWED_HOSTS = [
    pytest.param(None, id='None'),
    pytest.param('', id='empty'),
    pytest.param('localhost', id='localhost'),
    pytest.param('LOCALHOST', id='localhost in capitals'),
    pytest.param('localhost.', id='localhost with a trailing dot'),
    pytest.param('127.0.0.1', id='127.0.0.1'),
    pytest.param('127.0.0.2', id='another address in 127/8'),
    pytest.param('::1', id='::1'),
    pytest.param(b'localhost', id='localhost as bytes'),
]
RESOLVERS = ['getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr', 'getnameinfo']


def _lookup_args(resolver, host):
    # getaddrinfo takes a port too; getnameinfo takes a sockaddr and flags (0: look the name up).
    if resolver == 'getaddrinfo':
        return (host, 80)
    if resolver == 'getnameinfo':
        return ((host, 80), 0)
    return (host,)


def _recorders(monkeypatch):
    import socket

    import conftest
    calls = []
    for name in RESOLVERS:
        # No guard in place: stop here, before any row could make a real lookup.
        assert getattr(socket, name) is not conftest._real_resolvers[name], f'{name} unguarded'
        def record(host, *args, _name=name, **kwargs):
            calls.append((_name, host))
            return 'resolved'
        monkeypatch.setitem(conftest._real_resolvers, name, record)
    return calls


@pytest.mark.parametrize('resolver', RESOLVERS)
@pytest.mark.parametrize('host', REFUSED_HOSTS)
def test_no_test_looks_up_a_name_outside_loopback(monkeypatch, resolver, host):
    import socket
    calls = _recorders(monkeypatch)
    for _ in range(2):              # refused every time: the guard keeps no state
        with pytest.raises(RuntimeError, match=r'must not reach the network \(looked up'):
            getattr(socket, resolver)(*_lookup_args(resolver, host))
    assert calls == []


@pytest.mark.parametrize('resolver', RESOLVERS)
@pytest.mark.parametrize('host', ALLOWED_HOSTS)
def test_a_loopback_lookup_reaches_the_real_resolver(monkeypatch, resolver, host):
    import socket
    calls = _recorders(monkeypatch)
    args = _lookup_args(resolver, host)
    assert getattr(socket, resolver)(*args) == 'resolved'
    assert calls == [(resolver, args[0])]


@pytest.mark.parametrize('host', REFUSED_HOSTS)
def test_no_test_connects_outside_loopback_by_any_spelling(host):
    import socket
    sock = socket.socket()
    try:
        with pytest.raises(RuntimeError, match='must not reach the network'):
            sock.connect((host, 80))
    finally:
        sock.close()


def test_a_connection_by_name_is_refused_at_its_lookup(monkeypatch):
    # socket.create_connection (and urllib3) look getaddrinfo up when called: the patch reaches it.
    import socket
    calls = _recorders(monkeypatch)
    with pytest.raises(RuntimeError, match=r'looked up'):
        socket.create_connection(('wiki.warframe.com', 443), timeout=1)
    assert calls == []


def test_a_thread_a_test_starts_is_guarded_too(monkeypatch):
    import socket
    import threading
    calls, errors = _recorders(monkeypatch), []

    def lookup():
        try:
            socket.getaddrinfo('wiki.warframe.com', 443)
        except RuntimeError as exc:
            errors.append(exc)
    thread = threading.Thread(target=lookup)
    thread.start()
    thread.join(10)
    assert len(errors) == 1 and calls == []


def test_no_test_reverse_looks_up_an_address_outside_loopback(monkeypatch):
    # getnameinfo asks the resolver for an address's name (a DNS PTR query) unless told to
    # answer numerically: it is a name lookup like gethostbyaddr.
    import _socket
    import socket
    # Unguarded: stop here, before the row could make a real lookup.
    assert socket.getnameinfo is not _socket.getnameinfo, 'getnameinfo unguarded'
    with pytest.raises(RuntimeError, match=r'must not reach the network'):
        socket.getnameinfo(('192.0.2.1', 80), 0)


@pytest.mark.parametrize('sockaddr', [
    pytest.param(('192.0.2.1', 80, 0, 0), id='a four-element sockaddr'),
    pytest.param(('2001:db8::1', 80, 0, 0), id='an IPv6 sockaddr'),
    pytest.param((), id='an empty sockaddr'),
    pytest.param('192.0.2.1', id='an address that is no sockaddr'),
])
def test_a_reverse_lookup_of_any_sockaddr_shape_outside_loopback_is_refused(monkeypatch, sockaddr):
    import socket
    calls = _recorders(monkeypatch)
    with pytest.raises(RuntimeError, match=r'must not reach the network \(looked up'):
        socket.getnameinfo(sockaddr, 0)
    assert calls == []
