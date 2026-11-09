"""The page to return to after a sign-in is followed only when it is a path on this site.

Both sign-in paths take it: the local login form's ``next`` and the OIDC start (the login
page passes its ``next`` to the OIDC button). Anything else goes to the dashboard.
"""
import html
import random
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from conftest import ADMIN
from oidc_fixtures import (  # noqa: F401  (the fixtures are found by name)
    CODE, callback, oidc_app, oidc_client, provider, seeded, signed_in_user_id, start)

PENDING_KEY_NAME = '_oidc_pending'

# (the value of next, whether it is followed)
NEXT_SPELLINGS = [
    pytest.param('/collection/mods', True, id='path'),
    pytest.param('/market?tab=watch&sort=price', True, id='path-with-query'),
    pytest.param('/items/a%20b', True, id='path-with-encoded-blank'),
    pytest.param('/', True, id='root'),
    pytest.param('https://evil.test/', False, id='absolute'),
    pytest.param('HTTPS://evil.test/', False, id='absolute-upper-case'),
    pytest.param('http:evil.test', False, id='scheme-without-slashes'),
    pytest.param('//evil.test', False, id='scheme-relative'),
    pytest.param('///evil.test', False, id='three-slashes'),
    pytest.param('/\\evil.test', False, id='slash-backslash'),
    pytest.param('\\\\evil.test', False, id='two-backslashes'),
    pytest.param('/x\\y', False, id='backslash-inside'),
    pytest.param('javascript:alert(1)', False, id='javascript'),
    pytest.param('JaVaScRiPt:alert(1)', False, id='javascript-mixed-case'),
    pytest.param('data:text/html,x', False, id='data'),
    pytest.param(' /collection/mods', False, id='leading-blank'),
    pytest.param('\t//evil.test', False, id='leading-tab'),
    pytest.param('/\t/evil.test', False, id='tab-after-slash'),
    pytest.param('/\n/evil.test', False, id='newline-after-slash'),
    pytest.param('/%2Fevil.test', False, id='encoded-second-slash'),
    pytest.param('/%2fevil.test', False, id='encoded-second-slash-lower-case'),
    pytest.param('%2F%2Fevil.test', False, id='encoded-both-slashes'),
    pytest.param('/%5Cevil.test', False, id='encoded-backslash'),
    pytest.param('/%252F%252Fevil.test', False, id='double-encoded-slashes'),
    pytest.param('/%09/evil.test', False, id='encoded-tab'),
    pytest.param('/%0d%0aSet-Cookie:x=1', False, id='encoded-crlf'),
    pytest.param('evil.test', False, id='bare-host'),
    pytest.param('', False, id='empty'),
    # At most 200 characters, counted on the value as given (before percent-decoding).
    pytest.param('/' + 'a' * 199, True, id='at-the-limit'),
    pytest.param('/' + 'a' * 198, True, id='under-the-limit'),
    pytest.param('/' + 'a' * 200, False, id='one-over-the-limit'),
    pytest.param('/' + 'a' * 5000, False, id='very-long'),
    pytest.param('/' + '%20' * 66 + 'a', True, id='encoded-at-the-limit'),
    pytest.param('/' + '%61' * 67, False, id='encoded-over-the-limit-decoded-under'),
    pytest.param('/market?q=' + 'a' * 191, False, id='query-over-the-limit'),
]


def _assert_lands(response, value, followed):
    assert response.status_code == 302
    location = response.headers['Location']
    if followed:
        assert location == value
    else:
        assert location.endswith('/dashboard') and 'evil' not in location


@pytest.mark.parametrize('value, followed', NEXT_SPELLINGS)
def test_the_local_login_follows_only_a_path_on_this_site(seeded, value, followed):
    response = seeded.post('/auth/login', query_string={'next': value},
                           data={'username': ADMIN['username'], 'password': ADMIN['password']})
    _assert_lands(response, value, followed)
    assert signed_in_user_id(seeded) is not None


@pytest.mark.parametrize('value, followed', NEXT_SPELLINGS)
def test_the_oidc_sign_in_follows_only_a_path_on_this_site(seeded, provider, value,
                                                           followed):
    _, query = start(seeded, provider, path='/auth/oidc/login?' + _query(value))
    response = callback(seeded, code=CODE, state=query['state'])
    _assert_lands(response, value, followed)
    assert signed_in_user_id(seeded) is not None


@pytest.mark.parametrize('value, followed', NEXT_SPELLINGS)
def test_the_login_page_passes_only_a_safe_next_to_the_oidc_button(seeded, value, followed):
    page = seeded.get('/auth/login', query_string={'next': value}).get_data(as_text=True)
    href = _button_href(page)
    if followed:
        assert urlsplit(href).path == '/auth/oidc/login'
        assert parse_qs(urlsplit(href).query) == {'next': [value]}
    else:
        assert href == '/auth/oidc/login'


def _button_href(page):
    button = page[page.index('oidc-sign-in'):]
    start_at = button.index('href="') + len('href="')
    return html.unescape(button[start_at:button.index('"', start_at)])


def test_the_oidc_button_carries_next_through_to_the_return(seeded, provider):
    page = seeded.get('/auth/login?next=/collection/mods').get_data(as_text=True)
    href = _button_href(page)
    assert parse_qs(urlsplit(href).query) == {'next': ['/collection/mods']}
    _, query = start(seeded, provider, path=href)
    response = callback(seeded, code=CODE, state=query['state'])
    assert response.headers['Location'] == '/collection/mods'


def test_a_pending_return_page_is_checked_again_at_the_callback(seeded, provider):
    """The session is signed, so it cannot be changed from outside; the callback checks the
    stored page again all the same."""
    _, query = start(seeded, provider, path='/auth/oidc/login?next=%2Fcollection%2Fmods')
    with seeded.session_transaction() as sess:
        pending = dict(sess[PENDING_KEY_NAME], next='//evil.test')
        sess[PENDING_KEY_NAME] = pending
    response = callback(seeded, code=CODE, state=query['state'])
    assert response.headers['Location'].endswith('/dashboard')


def test_without_next_both_sign_ins_go_to_the_dashboard(seeded, provider):
    response = seeded.post('/auth/login', data={'username': ADMIN['username'],
                                                'password': ADMIN['password']})
    assert response.headers['Location'].endswith('/dashboard')
    seeded.post('/auth/logout')
    _, query = start(seeded, provider)
    response = callback(seeded, code=CODE, state=query['state'])
    assert response.headers['Location'].endswith('/dashboard')


@pytest.mark.parametrize('value', [None, 7, b'/x', ['/x']])
def test_a_value_that_is_not_text_is_no_return_page(value):
    from services.auth_service import safe_return_path
    assert safe_return_path(value) is None


def test_a_value_still_encoded_after_five_decodings_is_no_return_page():
    from services.auth_service import safe_return_path
    value = '/a%2Fb'
    for _ in range(5):
        value = value.replace('%', '%25')
    assert safe_return_path(value) is None
    assert safe_return_path('/a%25252Fb') == '/a%25252Fb'


def _query(value):
    return urlencode({'next': value})


# Browsers keep a cookie of at most 4096 bytes (name, value and attributes); Werkzeug warns
# from 4093. A path of 199 characters outside the Basic Multilingual Plane is the worst
# case at the limit: each takes 12 bytes in the session's JSON (two \uXXXX escapes), and
# random ones leave the session's compression little to save.
COOKIE_LIMIT = 4093


def _random_text(low, high, length, seed=47):
    rng = random.Random(seed)
    return ''.join(chr(rng.randint(low, high)) for _ in range(length))


@pytest.mark.parametrize('value', [
    pytest.param('/' + 'a' * 199, id='ascii-at-the-limit'),
    pytest.param('/' + '\U0001F600' * 199, id='astral-repeated-at-the-limit'),
    pytest.param('/' + _random_text(0x10000, 0x10FFFF, 199), id='astral-random-at-the-limit'),
    pytest.param('/' + _random_text(0x0800, 0xD7FF, 199), id='bmp-random-at-the-limit'),
    pytest.param('/' + 'é' * 199, id='latin-at-the-limit'),
    pytest.param('/' + '"' * 199, id='quotes-at-the-limit'),
])
def test_a_very_long_next_keeps_the_oidc_session_cookie_within_browser_limits(
        seeded, provider, recwarn, value):
    response = seeded.get('/auth/oidc/login?' + _query(value))
    cookies = [h for h in response.headers.getlist('Set-Cookie') if h.startswith('session=')]
    assert len(cookies) == 1
    assert len(cookies[0]) <= COOKIE_LIMIT, len(cookies[0])
    assert not [w for w in recwarn if 'cookie' in str(w.message).lower()]
    with seeded.session_transaction() as sess:
        assert sess[PENDING_KEY_NAME]['next'] == value   # kept whole, not cut


@pytest.mark.parametrize('value', [
    pytest.param('/' + 'a' * 200, id='ascii-over-the-limit'),
    pytest.param('/' + '\U0001F600' * 5000, id='astral-far-over-the-limit'),
])
def test_a_next_over_the_limit_is_not_kept_and_the_sign_in_goes_to_the_dashboard(
        seeded, provider, value):
    response, query = start(seeded, provider, path='/auth/oidc/login?' + _query(value))
    assert len(response.headers['Set-Cookie']) <= COOKIE_LIMIT
    with seeded.session_transaction() as sess:
        assert sess[PENDING_KEY_NAME]['next'] == '/dashboard'
    response = callback(seeded, code=CODE, state=query['state'])
    assert response.headers['Location'].endswith('/dashboard')
    assert signed_in_user_id(seeded) is not None


def test_the_return_page_limit_is_200_characters():
    from services.auth_service import RETURN_PATH_MAX, safe_return_path
    assert RETURN_PATH_MAX == 200
    assert safe_return_path('/' + 'b' * 199) == '/' + 'b' * 199
    assert safe_return_path('/' + 'b' * 200) is None
