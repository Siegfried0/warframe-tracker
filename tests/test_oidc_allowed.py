"""OpenID Connect sign-in: who may sign in (the allowed email domains and provider groups).

The lists are checked at every OIDC sign-in, before the account is looked up or created; a
list set without an entry turns OIDC sign-in off. The fake provider of ``oidc_fixtures``
answers; nothing reaches the network.
"""
import logging

import pytest

from conftest import ADMIN, TokenClient
from oidc_fixtures import (  # noqa: F401  (the fixtures are found by name)
    _UNSET, CODE, OIDC, _make_app, callback, flashes, provider, signed_in_user_id, start,
    user_rows)
from services.oidc import SignInRefused
from services.oidc_allowed import NOT_ALLOWED_MESSAGE, allow_list, check_allowed

DOMAINS = 'OIDC_ALLOWED_EMAIL_DOMAINS'
GROUPS = 'OIDC_ALLOWED_GROUPS'
NOT_ALLOWED = 'Your account at the identity provider is not allowed to sign in to this app.'
LONE_SURROGATE = chr(0xD800)


def _passes(config, claims) -> bool:
    try:
        check_allowed(config, claims)
    except SignInRefused as refused:
        assert refused.message == NOT_ALLOWED_MESSAGE
        return False
    return True


# ── The domain rule ──────────────────────────────────────────────────────────

# (OIDC_ALLOWED_EMAIL_DOMAINS, the token's email, passes)
DOMAIN_ROWS = [
    pytest.param('example.com', 'a@example.com', True, id='exact'),
    pytest.param('example.com', 'a@EXAMPLE.COM', True, id='upper-case-email'),
    pytest.param('ExAmPlE.CoM', 'a@example.com', True, id='mixed-case-entry'),
    pytest.param('Example.com', 'A@eXample.Com', True, id='mixed-case-both'),
    pytest.param('  example.com  ', 'a@example.com', True, id='blanks-around-the-entry'),
    pytest.param('other.org , example.com ,x.net', 'a@example.com', True, id='blanks-in-list'),
    pytest.param('example.com', 'a@ example.com ', True, id='blanks-around-the-email-domain'),
    pytest.param('@example.com', 'a@example.com', True, id='entry-with-leading-at'),
    pytest.param(' @ example.com', 'a@example.com', True, id='entry-with-at-and-blanks'),
    pytest.param('@@example.com', 'a@example.com', False, id='entry-with-two-leading-at'),
    pytest.param('example.com', 'a@sub.example.com', False, id='subdomain'),
    pytest.param('sub.example.com', 'a@example.com', False, id='parent-domain'),
    pytest.param('example.com', 'a@evilexample.com', False, id='lookalike-suffix'),
    pytest.param('example.com', 'a@example.com.evil.net', False, id='lookalike-prefix'),
    pytest.param('example.com', 'a@exa mple.com', False, id='blank-inside-the-domain'),
    pytest.param('example.com', 'a@evil.net@example.com', True, id='two-at-last-counts'),
    pytest.param('evil.net', 'a@evil.net@example.com', False, id='two-at-first-ignored'),
    pytest.param('example.com', 'a.example.com', False, id='no-at'),
    pytest.param('example.com', 'example.com', False, id='bare-domain'),
    pytest.param('example.com', 'a@', False, id='nothing-after-the-at'),
    pytest.param('example.com', '@example.com', True, id='nothing-before-the-at'),
    pytest.param('example.com', '', False, id='empty'),
    pytest.param('example.com', '   ', False, id='blank'),
    pytest.param('example.com', 42, False, id='number'),
    pytest.param('example.com', ['a@example.com'], False, id='list'),
    pytest.param('example.com', {'a': 'example.com'}, False, id='object'),
    pytest.param('example.com', None, False, id='null'),
    pytest.param('example.com', True, False, id='boolean'),
    pytest.param('example.com', _UNSET, False, id='missing'),
    pytest.param('example.com', 'a@example.com' + LONE_SURROGATE, False,
                 id='lone-surrogate-in-the-domain'),
    pytest.param('example.com', LONE_SURROGATE + '@example.com', False,
                 id='lone-surrogate-in-the-local-part'),
    pytest.param('example.com', 'a@example.com.', False, id='trailing-dot-on-the-email'),
    pytest.param('example.com.', 'a@example.com', False, id='trailing-dot-on-the-entry'),
    pytest.param('example.com.', 'a@example.com.', True, id='trailing-dot-on-both'),
    pytest.param('bücher.example', 'a@BÜCHER.example', True, id='non-ascii-case'),
    pytest.param('bücher.example', 'a@xn--bcher-kva.example', False, id='punycode-vs-unicode'),
    pytest.param('xn--bcher-kva.example', 'a@xn--bcher-kva.example', True, id='punycode'),
    pytest.param('strasse.de', 'a@straße.de', False, id='sharp-s-is-not-ss'),
    pytest.param('テンノ.jp', 'a@テンノ.jp', True, id='japanese'),
]


@pytest.mark.parametrize('entries, email, passes', DOMAIN_ROWS)
def test_the_email_domain_must_equal_an_entry(entries, email, passes):
    claims = {} if email is _UNSET else {'email': email}
    assert _passes({DOMAINS: entries}, claims) is passes


# (email_verified, passes): present and not JSON true refuses; absent passes
VERIFIED_ROWS = [
    pytest.param(True, True, id='true'),
    pytest.param(_UNSET, True, id='absent'),
    pytest.param(False, False, id='false'),
    pytest.param('true', False, id='string-true'),
    pytest.param('True', False, id='string-True'),
    pytest.param(1, False, id='one'),
    pytest.param(None, False, id='null'),
    pytest.param([True], False, id='list'),
]


@pytest.mark.parametrize('verified, passes', VERIFIED_ROWS)
def test_email_verified_when_present_must_be_true(verified, passes):
    claims = {'email': 'a@example.com'}
    if verified is not _UNSET:
        claims['email_verified'] = verified
    assert _passes({DOMAINS: 'example.com'}, claims) is passes


def test_email_verified_is_not_read_without_a_domain_list():
    claims = {'email_verified': False, 'groups': ['tenno']}
    assert _passes({GROUPS: 'tenno'}, claims) is True


# ── The group rule ───────────────────────────────────────────────────────────

# (OIDC_ALLOWED_GROUPS, the token's groups claim, passes)
GROUP_ROWS = [
    pytest.param('tenno', ['tenno'], True, id='list-hit'),
    pytest.param('tenno', ['grineer', 'tenno'], True, id='list-hit-second'),
    pytest.param('admins, tenno', ['tenno'], True, id='hit-on-the-second-entry'),
    pytest.param('tenno', ['grineer', 'corpus'], False, id='list-miss'),
    pytest.param('tenno', 'tenno', True, id='single-string-hit'),
    pytest.param('tenno', 'grineer', False, id='single-string-miss'),
    pytest.param('tenno', 'grineer,tenno', False, id='single-string-is-not-split'),
    pytest.param('tenno', [], False, id='empty-list'),
    pytest.param('tenno', '', False, id='empty-string'),
    pytest.param('tenno', ['tenno', 5], False, id='list-with-a-number'),
    pytest.param('tenno', ['tenno', None], False, id='list-with-null'),
    pytest.param('tenno', ['tenno', ['x']], False, id='list-with-a-list'),
    pytest.param('tenno', ['tenno', 'x' + LONE_SURROGATE], False, id='list-with-lone-surrogate'),
    pytest.param('tenno', _UNSET, False, id='missing'),
    pytest.param('tenno', None, False, id='null'),
    pytest.param('tenno', 7, False, id='number'),
    pytest.param('tenno', {'tenno': True}, False, id='object'),
    pytest.param('tenno', True, False, id='boolean'),
    pytest.param('tenno', ['Tenno'], False, id='case-differs-in-the-claim'),
    pytest.param('Tenno', ['tenno'], False, id='case-differs-in-the-entry'),
    pytest.param('tenno', [' tenno'], False, id='claim-is-not-trimmed'),
    pytest.param('  tenno  ', ['tenno'], True, id='entry-is-trimmed'),
    pytest.param('/org/tenno', ['/org/tenno'], True, id='group-path'),
    pytest.param('/org/tenno', ['/org/tenno/sub'], False, id='group-sub-path'),
    pytest.param('3f2a-uuid', ['3f2a-uuid'], True, id='group-id'),
    pytest.param('@tenno', ['@tenno'], True, id='leading-at-kept-for-groups'),
]


@pytest.mark.parametrize('entries, held, passes', GROUP_ROWS)
def test_the_groups_claim_must_hold_an_entry_exactly(entries, held, passes):
    claims = {} if held is _UNSET else {'groups': held}
    assert _passes({GROUPS: entries}, claims) is passes


# ── Both lists, and neither ──────────────────────────────────────────────────

BOTH = {DOMAINS: 'example.com', GROUPS: 'tenno'}


@pytest.mark.parametrize('claims, passes', [
    pytest.param({'email': 'a@example.com', 'groups': ['tenno']}, True, id='both-pass'),
    pytest.param({'email': 'a@other.org', 'groups': ['tenno']}, False, id='domain-fails'),
    pytest.param({'email': 'a@example.com', 'groups': ['grineer']}, False, id='group-fails'),
    pytest.param({'email': 'a@other.org', 'groups': []}, False, id='both-fail'),
])
def test_with_both_lists_the_token_must_pass_both(claims, passes):
    assert _passes(BOTH, claims) is passes


@pytest.mark.parametrize('claims', [
    pytest.param({}, id='no-email-no-groups'),
    pytest.param({'email': 42, 'email_verified': False, 'groups': 5}, id='odd-claims'),
])
def test_with_neither_list_any_claims_pass(claims):
    assert _passes({DOMAINS: None, GROUPS: None}, claims) is True
    assert _passes({}, claims) is True


# ── Configuration ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('value, entries', [
    pytest.param(None, None, id='unset'),
    pytest.param('example.com', ('example.com',), id='one'),
    pytest.param(' A.com ,, @B.org ', ('a.com', 'b.org'), id='trimmed-lowered-at-dropped'),
    pytest.param('', (), id='empty'),
    pytest.param('   ', (), id='blank'),
    pytest.param(',', (), id='comma'),
    pytest.param(' , ', (), id='blank-comma-blank'),
    pytest.param('@', (), id='lone-at'),
    pytest.param(' @ , @ ', (), id='at-signs-only'),
])
def test_the_domain_list_spellings(value, entries):
    assert allow_list({DOMAINS: value}, DOMAINS) == entries


@pytest.mark.parametrize('value, entries', [
    pytest.param(None, None, id='unset'),
    pytest.param(' Tenno , /org/x ', ('Tenno', '/org/x'), id='trimmed-case-kept'),
    pytest.param('@', ('@',), id='lone-at-is-a-group'),
    pytest.param(' , ', (), id='blank-comma-blank'),
    pytest.param('', (), id='empty'),
])
def test_the_group_list_spellings(value, entries):
    assert allow_list({GROUPS: value}, GROUPS) == entries


def _app(tmp_path, **lists):
    return _make_app(tmp_path, **dict(OIDC, **lists))


def _client(application):
    return TokenClient(application, application.response_class, use_cookies=True)


# (lists, OIDC on, the names the startup warning gives)
CONFIG_ROWS = [
    pytest.param({}, True, [], id='unset'),
    pytest.param({DOMAINS: 'example.com'}, True, [], id='domains-set'),
    pytest.param({GROUPS: 'tenno'}, True, [], id='groups-set'),
    pytest.param(BOTH, True, [], id='both-set'),
    pytest.param({DOMAINS: ' , '}, False, [DOMAINS], id='domains-blank-comma-blank'),
    pytest.param({GROUPS: ','}, False, [GROUPS], id='groups-comma'),
    pytest.param({DOMAINS: '   '}, False, [DOMAINS], id='domains-blank'),
    pytest.param({GROUPS: ''}, False, [GROUPS], id='groups-empty'),
    pytest.param({DOMAINS: '@'}, False, [DOMAINS], id='domains-lone-at'),
    pytest.param({DOMAINS: 'example.com', GROUPS: ' , '}, False, [GROUPS],
                 id='domains-set-groups-empty'),
    pytest.param({DOMAINS: ',', GROUPS: ','}, False, [DOMAINS, GROUPS], id='both-empty'),
]


@pytest.mark.parametrize('lists, enabled, warned', CONFIG_ROWS)
def test_a_list_set_without_an_entry_turns_oidc_off_and_is_named(tmp_path, caplog, provider,
                                                                  lists, enabled, warned):
    with caplog.at_level(logging.WARNING):
        application = _app(tmp_path, **lists)
    warnings = [r.getMessage() for r in caplog.records if 'OIDC sign-in is off' in r.getMessage()]
    assert len(warnings) == len(warned)
    for name, line in zip(warned, warnings):
        assert line == f'OIDC sign-in is off: {name} is set but holds no entry.'
    client = _client(application)
    assert ('/auth/oidc/login' in client.get('/auth/setup').get_data(as_text=True)) is enabled
    client.post('/auth/setup', data=ADMIN)
    client.post('/auth/logout')
    assert ('/auth/oidc/login' in client.get('/auth/login').get_data(as_text=True)) is enabled
    if enabled:
        assert client.get('/auth/oidc/login').status_code == 302
    else:
        assert client.get('/auth/oidc/login').status_code == 404
        assert client.get('/auth/oidc/callback?code=x&state=y').status_code == 404
        assert provider.gets == [] and provider.posts == []


# ── Sign-in end to end ───────────────────────────────────────────────────────

GROUPED = {DOMAINS: 'example.test', GROUPS: 'tenno'}
PASSING = {'email': 'tenno@example.test', 'groups': ['tenno']}


@pytest.fixture
def allowed_client(tmp_path):
    """An OIDC app with both lists set, its local admin already made."""
    client = _client(_app(tmp_path, **GROUPED))
    assert client.post('/auth/setup', data=ADMIN).status_code == 302
    client.post('/auth/logout')
    with client.session_transaction() as sess:
        sess.pop('_flashes', None)
    return client


def _sign_in(client, provider, claims):
    provider.overrides = dict(claims)
    _, query = start(client, provider)
    return callback(client, code=CODE, state=query['state'])


def _assert_refused(client, response, before):
    assert response.headers['Location'].endswith('/auth/login')
    assert signed_in_user_id(client) is None
    assert user_rows(client.application) == before
    assert NOT_ALLOWED in flashes(client)


def test_a_passing_sign_in_creates_the_account(allowed_client, provider):
    _sign_in(allowed_client, provider, PASSING)
    assert signed_in_user_id(allowed_client) is not None
    assert [r['oidc_sub'] for r in user_rows(allowed_client.application)] == [None, 'sub-0001']


@pytest.mark.parametrize('claims, rule', [
    pytest.param({'email': 'tenno@other.test'}, 'domain', id='domain'),
    pytest.param({'email': _UNSET}, 'domain', id='no-email'),
    pytest.param({'email_verified': False}, 'verified', id='unverified'),
    pytest.param({'groups': ['grineer']}, 'groups', id='group'),
    pytest.param({'groups': _UNSET}, 'groups', id='no-groups'),
])
def test_a_new_sub_refused_writes_nothing_twice(allowed_client, provider, caplog, claims,
                                                rule):
    before = user_rows(allowed_client.application)
    for _ in range(2):
        with caplog.at_level(logging.WARNING):
            response = _sign_in(allowed_client, provider, dict(PASSING, **claims))
        _assert_refused(allowed_client, response, before)
        with allowed_client.session_transaction() as sess:
            sess.pop('_flashes', None)
    refusals = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith('OIDC sign-in refused: not allowed: ')]
    assert len(refusals) == 2
    assert all(line.startswith(f'OIDC sign-in refused: not allowed: {rule} (') for line in refusals)
    for value in ('tenno@', 'other.test', 'example.test', 'grineer', 'sub-0001'):
        assert value not in caplog.text


@pytest.mark.parametrize('change', [
    pytest.param({'groups': ['grineer']}, id='moved-out-of-the-group'),
    pytest.param({'email': 'tenno@other.test'}, id='email-moved-domain'),
    pytest.param({'email_verified': False}, id='email-no-longer-verified'),
])
def test_a_returning_account_that_no_longer_passes_is_refused_and_left_unchanged(
        allowed_client, provider, change):
    _sign_in(allowed_client, provider, PASSING)
    allowed_client.post('/auth/logout')
    from models.db import get_db
    with allowed_client.application.app_context():
        db = get_db()
        db.execute("UPDATE users SET last_login = '2026-01-01 00:00:00' WHERE oidc_sub = ?",
                   ('sub-0001',))
        db.commit()
    with allowed_client.session_transaction() as sess:
        sess.pop('_flashes', None)
    before = user_rows(allowed_client.application)
    response = _sign_in(allowed_client, provider, dict(PASSING, **change))
    _assert_refused(allowed_client, response, before)
    assert before[1]['last_login'].startswith('2026-01-01')


def test_a_tightened_list_refuses_an_account_made_before(tmp_path, provider):
    open_app = _app(tmp_path)
    client = _client(open_app)
    client.post('/auth/setup', data=ADMIN)
    client.post('/auth/logout')
    _sign_in(client, provider, {'email': 'tenno@other.test'})
    assert signed_in_user_id(client) is not None
    before = user_rows(open_app)

    tightened = _client(_app(tmp_path, **{DOMAINS: 'example.test'}))
    response = _sign_in(tightened, provider, {'email': 'tenno@other.test'})
    _assert_refused(tightened, response, before)


def test_the_setup_pages_first_oidc_account_must_pass(tmp_path, provider):
    client = _client(_app(tmp_path, **GROUPED))
    assert '/auth/oidc/login' in client.get('/auth/setup').get_data(as_text=True)
    response = _sign_in(client, provider, dict(PASSING, groups=['grineer']))
    assert response.headers['Location'].endswith('/auth/login')
    assert signed_in_user_id(client) is None
    assert user_rows(client.application) == []
    assert NOT_ALLOWED in flashes(client)

    _sign_in(client, provider, PASSING)
    rows = user_rows(client.application)
    assert [(r['role'], r['auth_type']) for r in rows] == [('admin', 'oidc')]


def test_the_check_runs_before_the_account_is_looked_up(allowed_client, provider,
                                                        monkeypatch):
    from models.user import User

    def looked_up(*args, **kwargs):
        raise AssertionError('the account was looked up before the allow lists')
    monkeypatch.setattr(User, 'get_oidc_account', looked_up)
    response = _sign_in(allowed_client, provider, dict(PASSING, groups=[]))
    assert response.headers['Location'].endswith('/auth/login')
    assert NOT_ALLOWED in flashes(allowed_client)
