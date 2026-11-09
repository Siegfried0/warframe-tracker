"""CSRF protection: every request whose method is not GET, HEAD or OPTIONS must carry the
session's token (form field ``csrf_token`` or header ``X-CSRF-Token``), or it is refused
with 400 before the route runs and before anything is written.

The route table is read from the app's URL map, not from a list: a new unsafe route is in
the table the moment it exists. Refusals go through ``app.test_client()`` (no token); the
conftest ``client`` sends the session's token as the app's pages do.
"""
import io
import re
import zipfile

import pytest

import app as app_module
from models.db import get_db
from models.item import Item
from models.user import User
from services import csrf

ADMIN = {'username': 'admin', 'password': 'correct-horse-9', 'confirm': 'correct-horse-9'}
SAFE = {'GET', 'HEAD', 'OPTIONS'}
TOKEN_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


def _unsafe_rules():
    rows = []
    for rule in app_module.app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        for method in sorted(rule.methods - SAFE):
            rows.append((rule.endpoint, method))
    return sorted(rows)


UNSAFE = _unsafe_rules()


def _path(app, endpoint):
    rule = next(r for r in app.url_map.iter_rules() if r.endpoint == endpoint)
    with app.test_request_context():
        from flask import url_for
        return url_for(endpoint, **{arg: 1 for arg in rule.arguments})


def _snapshot(app):
    """Every row of every table: a refused request leaves all of it as it was."""
    with app.app_context():
        db = get_db()
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {t: sorted(tuple(r) for r in db.execute(f'SELECT * FROM "{t}"'))
                for t in tables}


def _signed_in(app):
    """A plain client (it sends no token by itself) signed in as the first-run admin and
    an item in the database; returns (client, its token)."""
    raw = app.test_client()
    page = raw.get('/auth/setup')
    token = TOKEN_RE.search(page.get_data(as_text=True)).group(1)
    response = raw.post('/auth/setup', data={**ADMIN, 'csrf_token': token})
    assert response.status_code == 302, response.status_code
    with app.app_context():
        Item.upsert('Braton', 'primary', source='standard')
    return raw, page_token(raw)


def page_token(client, path='/settings/'):
    response = client.get(path)
    match = TOKEN_RE.search(response.get_data(as_text=True))
    assert match, f'{path} has no csrf-token meta tag'
    return match.group(1)


def _is_refusal(response):
    if response.status_code != 400:
        return False
    if response.is_json:
        return response.get_json() == {'error': csrf.REFUSED}
    return csrf.REFUSED in response.get_data(as_text=True)


@pytest.fixture
def no_sync(monkeypatch):
    """The sync route's real run would reach the network: stub it."""
    import routes.sync
    monkeypatch.setattr(routes.sync, 'run_sync', lambda app: {'ok': True, 'stub': True})


def test_the_table_holds_every_unsafe_route():
    endpoints = {e for e, _ in UNSAFE}
    # A sample the app must have; the table itself comes from the URL map.
    assert {'auth.login', 'auth.logout', 'collection.toggle', 'settings.import_data',
            'admin.create_user', 'sync.run', 'market.watch', 'farming.goals_add'} <= endpoints
    assert len(UNSAFE) >= 30


BAD_TOKENS = ['missing', 'empty', 'wrong', 'other_session', 'query_string']


def _session_token(client):
    """The session's current token (a page view would create one if a sign-out removed it)."""
    with client.session_transaction() as sess:
        return sess.get(csrf.SESSION_KEY)


@pytest.mark.parametrize('endpoint,method', UNSAFE)
def test_every_unsafe_route_refuses_without_the_sessions_token_and_passes_with_it(
        app, no_sync, endpoint, method):
    client, token = _signed_in(app)
    other = app.test_client()
    other_token = page_token(other, '/auth/login')
    assert other_token != token
    path = _path(app, endpoint)
    wrong = []
    for case in BAD_TOKENS:
        kwargs = {'method': method}
        if case == 'empty':
            kwargs['headers'] = {'X-CSRF-Token': ''}
            kwargs['data'] = {'csrf_token': ''}
        elif case == 'wrong':
            kwargs['headers'] = {'X-CSRF-Token': 'x' * 43}
        elif case == 'other_session':
            kwargs['data'] = {'csrf_token': other_token}
        elif case == 'query_string':
            kwargs['query_string'] = {'csrf_token': token}
        before = _snapshot(app)
        response = client.open(path, **kwargs)
        if not _is_refusal(response):
            wrong.append(f'{case}: {response.status_code}')
        elif _snapshot(app) != before:
            wrong.append(f'{case}: refused but changed the database')
    # The session's token passes, in the header and in the form field. A request that
    # signs out (logout) removes it: the next case reads the session's new one.
    for where in ('header', 'field'):
        token = _session_token(client) or page_token(client, '/auth/login')
        if where == 'field':
            response = client.open(path, method=method, data={'csrf_token': token})
        else:
            response = client.open(path, method=method, headers={'X-CSRF-Token': token})
        if _is_refusal(response) or response.status_code >= 500:
            wrong.append(f'valid {where}: {response.status_code}')
    assert not wrong, f'{method} {path}: {wrong}'


def test_a_method_the_route_does_not_serve_is_checked_too(app):
    client, token = _signed_in(app)
    page = _path(app, 'dashboard.index')
    for method in ('PUT', 'PATCH', 'DELETE'):
        assert _is_refusal(client.open(page, method=method)), method
        assert client.open(page, method=method,
                           headers={'X-CSRF-Token': token}).status_code == 405, method


# Axis 1: spellings of the token. Each is refused in the field and in the header.
def _spellings(token):
    return {
        'whitespace': f' {token} ',
        'trailing newline': token + '\n',
        'wrong case': token.swapcase(),
        'truncated': token[:-1],
        'extra byte': token + 'A',
        'doubled': token + token,
        'non-ascii': token[:-1] + 'é',
        'very long': 'A' * 8000,
        'prefix of nothing': token[:1],
    }


@pytest.mark.parametrize('where', ['field', 'header'])
def test_every_other_spelling_of_the_token_is_refused(app, where):
    client, token = _signed_in(app)
    wrong = []
    for name, spelling in _spellings(token).items():
        if where == 'field':
            response = client.post('/market/watch', data={'csrf_token': spelling})
        else:
            try:
                response = client.post('/market/watch', headers={'X-CSRF-Token': spelling})
            except ValueError:
                continue  # a header the HTTP layer itself cannot carry (a newline)
        if not _is_refusal(response):
            wrong.append(f'{name}: {response.status_code}')
    assert not wrong, wrong


def test_the_header_decides_when_field_and_header_disagree(app):
    client, token = _signed_in(app)
    wrong_header = client.post('/settings/', data={'csrf_token': token, 'action': 'none'},
                               headers={'X-CSRF-Token': 'wrong'})
    assert _is_refusal(wrong_header)
    right_header = client.post('/settings/', data={'csrf_token': 'wrong', 'action': 'none'},
                               headers={'X-CSRF-Token': token})
    assert right_header.status_code == 302


def test_a_body_that_is_neither_form_nor_json_carries_no_field(app):
    """A cross-site text/plain form can spell ``csrf_token=...`` but is never read as one."""
    client, token = _signed_in(app)
    response = client.post('/settings/', data=f'csrf_token={token}&action=x',
                           content_type='text/plain')
    assert _is_refusal(response)


# The refusal's shape: JSON for a JSON or AJAX request, HTML otherwise; never 500 or a redirect.
@pytest.mark.parametrize('kwargs,shape', [
    ({'json': {'item_id': 1}}, 'json'),
    ({'headers': {'X-Requested-With': 'XMLHttpRequest'}}, 'json'),
    ({'headers': {'Accept': 'application/json'}}, 'json'),
    ({'data': {'item_id': '1'}}, 'html'),
    ({'headers': {'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'}}, 'html'),
    ({}, 'html'),
])
def test_a_refusal_is_json_for_json_requests_and_a_reload_page_otherwise(app, kwargs, shape):
    client, _ = _signed_in(app)
    response = client.post('/market/watch', **kwargs)
    assert response.status_code == 400
    assert 'Location' not in response.headers
    if shape == 'json':
        assert response.is_json and response.get_json() == {'error': csrf.REFUSED}
    else:
        assert response.mimetype == 'text/html'
        body = response.get_data(as_text=True)
        assert 'Request refused' in body and 'Reload the page and try again' in body


def test_a_refusal_comes_before_the_route_and_its_sign_in_redirect(app):
    """Anonymous, no cookie at all, first request a POST: refused, not sent to log in."""
    client = app.test_client()
    client.post('/auth/setup', data={**ADMIN, 'csrf_token': 'nothing-yet'})
    with app.app_context():
        assert User.count() == 0
    response = client.post('/collection/toggle', json={'item_id': 1, 'field': 'mastered'})
    assert _is_refusal(response)


def test_the_first_run_form_works_after_its_page_was_loaded(app):
    client = app.test_client()
    token = page_token(client, '/auth/setup')
    assert client.post('/auth/setup', data={**ADMIN, 'csrf_token': token}).status_code == 302
    with app.app_context():
        assert User.count() == 1


def test_a_session_from_before_the_token_is_refused_once_then_works(app):
    client, _ = _signed_in(app)
    with client.session_transaction() as sess:
        sess.pop(csrf.SESSION_KEY)
    assert _is_refusal(client.post('/market/watch', data={'csrf_token': 'anything'}))
    token = page_token(client)
    assert client.post('/settings/', data={'csrf_token': token,
                                           'action': 'none'}).status_code == 302


def _item_id(app):
    with app.app_context():
        return get_db().execute("SELECT id FROM items WHERE name = 'Braton'").fetchone()[0]


# Spot checks: a refused write leaves its rows; the same request with the token writes them.
def _toggle(app, client, token):
    headers = {'X-CSRF-Token': token} if token else {}
    return client.post('/collection/toggle', headers=headers,
                       json={'item_id': _item_id(app), 'field': 'mastered'})


def _goal(app, client, token):
    headers = {'X-CSRF-Token': token} if token else {}
    return client.post('/farming/goals/add', headers=headers, json={'item_id': _item_id(app)})


def _watch(app, client, token):
    headers = {'X-CSRF-Token': token} if token else {}
    return client.post('/market/watch', headers=headers, json={'item_id': _item_id(app)})


def _create_user(app, client, token):
    data = {'username': 'carol', 'password': 'pw-carol-1234', 'role': 'user'}
    if token:
        data['csrf_token'] = token
    return client.post('/admin/user/create', data=data)


def _save_settings(app, client, token):
    data = {'action': 'save_preferences', 'market_cache_hours': '12'}
    if token:
        data['csrf_token'] = token
    return client.post('/settings/', data=data)


def _import(app, client, token):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        archive.writestr('settings.csv', 'key,value\nplat_per_hour,55\n')
    data = {'import_file': (io.BytesIO(buf.getvalue()), 'export.zip')}
    if token:
        data['csrf_token'] = token
    return client.post('/settings/import', data=data, content_type='multipart/form-data')


@pytest.mark.parametrize('write', [_toggle, _goal, _watch, _create_user, _save_settings,
                                   _import], ids=lambda f: f.__name__.strip('_'))
def test_a_refused_write_changes_nothing_and_the_same_write_with_the_token_does(app, write):
    client, token = _signed_in(app)
    before = _snapshot(app)
    for bad in (None, 'wrong-token'):
        assert _is_refusal(write(app, client, bad)), bad
        assert _snapshot(app) == before, bad
    response = write(app, client, token)
    assert response.status_code in (200, 302), response.status_code
    assert _snapshot(app) != before


# Axis 3: repetition. Axis 7: two tabs of one session.
def test_one_token_serves_many_requests_and_every_tab_of_the_session(app):
    client, token = _signed_in(app)
    tokens = {page_token(client, p) for p in ('/settings/', '/dashboard', '/collection/warframe',
                                              '/admin/')}
    assert tokens == {token}
    for _ in range(5):  # a double (or fivefold) submit is not refused by the check
        assert not _is_refusal(_watch(app, client, token))


def test_the_token_changes_at_login_and_the_old_one_is_refused(app):
    _signed_in(app)
    client = app.test_client()
    before_login = page_token(client, '/auth/login')
    response = client.post('/auth/login', data={'username': 'admin',
                                                'password': ADMIN['password'],
                                                'csrf_token': before_login})
    assert response.status_code == 302
    after_login = page_token(client)
    assert after_login != before_login
    assert _is_refusal(_watch(app, client, before_login))
    assert not _is_refusal(_watch(app, client, after_login))


def test_register_replaces_the_token(app):
    _signed_in(app)
    client = app.test_client()
    before = page_token(client, '/auth/register')
    response = client.post('/auth/register', data={'username': 'dora',
                                                   'password': 'pw-dora-1234',
                                                   'confirm': 'pw-dora-1234',
                                                   'csrf_token': before})
    assert response.status_code == 302
    assert page_token(client) != before


def test_the_token_changes_at_logout_and_the_old_one_is_refused(app):
    client, token = _signed_in(app)
    response = client.post('/auth/logout', data={'csrf_token': token})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/auth/login')
    with client.session_transaction() as sess:
        assert '_user_id' not in sess
    assert _is_refusal(client.post('/auth/login', data={'username': 'admin',
                                                        'password': ADMIN['password'],
                                                        'csrf_token': token}))
    assert page_token(client, '/auth/login') != token


def test_a_logout_in_one_tab_makes_the_other_tabs_next_post_fail_once(app):
    client, token = _signed_in(app)
    client.post('/auth/logout', data={'csrf_token': token})  # tab 1
    assert _is_refusal(_watch(app, client, token))            # tab 2, its old page
    # tab 2 reloads: signed out, the login page has a fresh token that works
    fresh = page_token(client, '/auth/login')
    response = client.post('/auth/login', data={'username': 'admin',
                                                'password': ADMIN['password'],
                                                'csrf_token': fresh})
    assert response.status_code == 302


def test_a_get_to_logout_leaves_the_user_signed_in(app):
    client, _ = _signed_in(app)
    response = client.get('/auth/logout')
    assert response.status_code == 405
    with client.session_transaction() as sess:
        assert '_user_id' in sess
    assert client.get('/settings/').status_code == 200


# Every page with a form or a POST script holds the token.
PAGES = ['/dashboard', '/collection/warframe', '/farming/', '/market/', '/settings/', '/admin/',
         '/sync/']
FORM_RE = re.compile(r'<form\b[^>]*\bmethod="post"[^>]*>(.*?)</form>', re.S | re.I)


@pytest.mark.parametrize('path', PAGES)
def test_every_signed_in_page_holds_the_token_in_its_meta_and_each_post_form(app, path):
    client, token = _signed_in(app)
    body = client.get(path).get_data(as_text=True)
    assert f'<meta name="csrf-token" content="{token}">' in body
    forms = FORM_RE.findall(body)
    assert forms, 'the navigation has the logout form'
    field = f'<input type="hidden" name="csrf_token" value="{token}">'
    assert all(field in form for form in forms), path


@pytest.mark.parametrize('path', ['/auth/setup', '/auth/login', '/auth/register'])
def test_every_sign_in_page_form_holds_the_token(app, path):
    client = app.test_client()
    if path != '/auth/setup':
        _signed_in(app)
    body = client.get(path).get_data(as_text=True)
    token = TOKEN_RE.search(body).group(1)
    forms = FORM_RE.findall(body)
    assert len(forms) == 1
    assert f'<input type="hidden" name="csrf_token" value="{token}">' in forms[0]


def test_the_logout_control_is_a_post_form_in_the_navbar_and_the_sidenav(app):
    client, _ = _signed_in(app)
    body = client.get('/dashboard').get_data(as_text=True)
    assert 'href="/auth/logout"' not in body
    assert body.count('<form method="post" action="/auth/logout" class="logout-form">') == 2
    assert body.count('<button type="submit"') >= 2


def test_the_session_cookie_is_samesite_lax(app):
    client = app.test_client()
    response = client.get('/auth/setup')
    cookie = response.headers.get('Set-Cookie', '')
    assert 'session=' in cookie
    assert 'SameSite=Lax' in cookie


def test_samesite_follows_the_config(tmp_path):
    flask_app = app_module.create_app({
        'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'strict.db'),
        'SYNC_INTERVAL_HOURS': 0, 'AUTO_SYNC_ON_STARTUP': False, 'SECRET_KEY': 'test-only',
        'SESSION_COOKIE_SAMESITE': 'Strict',
    })
    cookie = flask_app.test_client().get('/auth/setup').headers.get('Set-Cookie', '')
    assert 'SameSite=Strict' in cookie


def test_a_form_too_large_to_read_the_token_from_is_refused_unread(app):
    client, token = _signed_in(app)
    big = b'x' * (9 * 1024 * 1024)  # 9 MiB, above the 8 MiB upload limit
    response = client.post('/settings/import', content_type='multipart/form-data',
                           data={'csrf_token': token,
                                 'import_file': (io.BytesIO(big), 'export.zip')})
    assert response.status_code == 413
    assert '8 MiB' in response.get_data(as_text=True)


def test_a_too_large_upload_sent_with_the_header_keeps_the_import_message(app):
    """With the token in the header the check reads no body; the import route's own limit
    and message stay as they were."""
    client, token = _signed_in(app)
    big = b'x' * (9 * 1024 * 1024)
    response = client.post('/settings/import', content_type='multipart/form-data',
                           headers={'X-CSRF-Token': token},
                           data={'import_file': (io.BytesIO(big), 'export.zip')})
    assert response.status_code == 302
    assert 'larger than 8 MiB' in client.get('/settings/').get_data(as_text=True)


# Axis 8: in EasyAuth mode the check runs before EasyAuth's own hook writes a user.
EASY = {'X-MS-CLIENT-PRINCIPAL-NAME': 'erin@example.org', 'X-MS-CLIENT-PRINCIPAL-ID': 'sub-erin'}


@pytest.fixture
def easy_app(tmp_path):
    return app_module.create_app({
        'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'easy.db'), 'SYNC_INTERVAL_HOURS': 0,
        'AUTO_SYNC_ON_STARTUP': False, 'SECRET_KEY': 'test-only', 'EASYAUTH_ENABLED': True,
    })


def test_easyauth_a_refused_post_signs_nobody_in_and_creates_no_user(easy_app):
    client = easy_app.test_client()
    assert _is_refusal(client.post('/market/watch', headers=EASY, json={'item_id': 1}))
    with easy_app.app_context():
        assert User.count() == 0


def test_easyauth_the_same_user_keeps_the_token_and_logout_goes_to_azure(easy_app):
    client = easy_app.test_client()
    body = client.get('/settings/', headers=EASY).get_data(as_text=True)
    token = TOKEN_RE.search(body).group(1)
    assert 'href="/.auth/logout"' in body and 'class="logout-form"' not in body
    again = client.get('/dashboard', headers=EASY).get_data(as_text=True)
    assert TOKEN_RE.search(again).group(1) == token
    with easy_app.app_context():
        Item.upsert('Braton', 'primary', source='standard')
    response = client.post('/market/watch', headers={**EASY, 'X-CSRF-Token': token},
                           json={'item_id': _item_id(easy_app)})
    assert response.status_code == 200, response.status_code
    response = client.post('/auth/logout', headers={**EASY, 'X-CSRF-Token': token})
    assert response.status_code == 302
    assert response.headers['Location'] == '/.auth/logout'


def test_easyauth_another_principal_on_the_session_gets_a_new_token(easy_app):
    client = easy_app.test_client()
    token = TOKEN_RE.search(client.get('/settings/', headers=EASY).get_data(as_text=True))[1]
    other = {'X-MS-CLIENT-PRINCIPAL-NAME': 'finn@example.org',
             'X-MS-CLIENT-PRINCIPAL-ID': 'sub-finn'}
    body = client.get('/settings/', headers=other).get_data(as_text=True)
    assert TOKEN_RE.search(body)[1] != token
