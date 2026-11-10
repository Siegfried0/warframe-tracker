"""The desktop nav bar keeps every item's name when its labels collapse to icons.

From 993 px the top nav shares one row with the brand logo; where the labels do not fit the
items show their icons only. What pytest can see: every top-nav link and the logout control
keep their label text in the page (the accessible name; the icon beside it is hidden from
assistive technology) and carry it as a title tooltip, and the username sits in its own
element that the stylesheet cuts with an ellipsis while the control's title holds the whole
name. Admin and non-admin, local logout form and EasyAuth link, short, long and wide names.
"""
from html.parser import HTMLParser

import pytest

import app as app_module
from conftest import TokenClient

LONG = 'averyveryverylongusernamewith32c'   # 32 characters, no spaces
WIDE = 'W' * 32
ADMIN_LABELS = ['Dashboard', 'Collection', 'Farming', 'Market', 'Admin', 'Sync', 'Settings']
USER_LABELS = ['Dashboard', 'Collection', 'Farming', 'Market', 'Settings']
_VOID = {'use', 'input', 'br', 'img', 'meta', 'hr'}  # no end tag (use: always closed here)


class _TopNav(HTMLParser):
    """Collects each control (a link or button) of the ul.top-nav: its title, its whole text
    without aria-hidden subtrees, and the text of its span.nav-user."""

    def __init__(self):
        super().__init__()
        self.depth_ul = 0
        self.controls = []
        self.current = None
        self.hidden = 0
        self.in_user = 0
        self.stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get('class') or '').split()
        if tag == 'ul' and 'top-nav' in classes:
            self.depth_ul = 1
            self.stack = []
            return
        if not self.depth_ul:
            return
        if tag in _VOID:
            return
        self.stack.append(tag)
        if tag in ('a', 'button') and self.current is None:
            self.current = {'tag': tag, 'title': a.get('title'), 'text': '', 'user': None,
                            'depth': len(self.stack)}
        elif self.current is not None:
            if a.get('aria-hidden') == 'true' or self.hidden:
                self.hidden += 1
            if tag == 'span' and 'nav-user' in classes:
                self.in_user = len(self.stack)
                self.current['user'] = ''

    def handle_endtag(self, tag):
        if not self.depth_ul:
            return
        if tag == 'ul' and not self.stack:
            self.depth_ul = 0
            return
        if tag in _VOID:
            return
        if self.current is not None and len(self.stack) > self.current['depth']:
            if self.hidden:
                self.hidden -= 1
            if self.in_user == len(self.stack):
                self.in_user = 0
        elif self.current is not None and len(self.stack) == self.current['depth']:
            self.controls.append(self.current)
            self.current = None
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.current is not None and not self.hidden:
            self.current['text'] += data
            if self.in_user:
                self.current['user'] += data


def _top_nav(html):
    parser = _TopNav()
    parser.feed(html)
    return [{**c, 'text': ' '.join(c['text'].split())} for c in parser.controls]


def _local_client(app, username, role):
    from models.user import User
    with app.app_context():
        User.create(username=username, password='correct-horse-9', role=role)
    client = TokenClient(app, app.response_class, use_cookies=True)
    response = client.post('/auth/login', data={'username': username,
                                                'password': 'correct-horse-9'})
    assert response.status_code == 302, response.status_code
    return client


@pytest.fixture
def easy_app(tmp_path):
    return app_module.create_app({
        'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'easy.db'), 'SYNC_INTERVAL_HOURS': 0,
        'AUTO_SYNC_ON_STARTUP': False, 'SECRET_KEY': 'test-only', 'EASYAUTH_ENABLED': True,
    })


def _easy_headers(name, sub):
    return {'X-MS-CLIENT-PRINCIPAL-NAME': name, 'X-MS-CLIENT-PRINCIPAL-ID': sub}


def _assert_nav(controls, labels, username, logout_tag):
    *items, logout = controls
    assert [c['text'] for c in items] == labels
    assert [c['title'] for c in items] == labels
    assert all(c['tag'] == 'a' for c in items)
    assert logout['tag'] == logout_tag
    assert logout['text'] == username
    assert logout['user'] == username
    assert logout['title'] == f'Log out {username}'


@pytest.mark.parametrize('username', ['al', LONG, WIDE])
@pytest.mark.parametrize('role, labels', [('admin', ADMIN_LABELS), ('user', USER_LABELS)])
@pytest.mark.parametrize('page', ['/dashboard', '/collection/', '/settings/'])
def test_every_local_top_nav_control_keeps_its_name_and_title(app, role, labels, username,
                                                              page):
    client = _local_client(app, username, role)
    response = client.get(page, follow_redirects=True)
    assert response.status_code == 200
    _assert_nav(_top_nav(response.get_data(as_text=True)), labels, username, 'button')


@pytest.mark.parametrize('username, role, labels', [
    ('erin@example.org', 'admin', ADMIN_LABELS),
    (LONG + '@example.org', 'admin', ADMIN_LABELS),
    ('finn@example.org', 'user', USER_LABELS),
])
def test_every_easyauth_top_nav_control_keeps_its_name_and_title(easy_app, username, role,
                                                                 labels):
    client = easy_app.test_client()
    if role == 'user':  # the first EasyAuth account is the admin; this one comes second
        assert client.get('/dashboard', headers=_easy_headers('first@example.org',
                                                              'sub-first')).status_code == 200
        client = easy_app.test_client()
    response = client.get('/dashboard', headers=_easy_headers(username, 'sub-' + username))
    assert response.status_code == 200
    _assert_nav(_top_nav(response.get_data(as_text=True)), labels, username, 'a')


def test_the_username_markup_is_escaped_in_text_and_title(app):
    client = _local_client(app, 'x"<b>y', 'user')
    html = client.get('/dashboard').get_data(as_text=True)
    assert 'title="Log out x&#34;&lt;b&gt;y"' in html
    assert '<b>y' not in html
    _assert_nav(_top_nav(html), USER_LABELS, 'x"<b>y', 'button')
