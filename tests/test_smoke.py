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
    assert version == 8


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
