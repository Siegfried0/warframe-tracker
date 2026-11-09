"""OpenID Connect sign-in: the authorization code flow with PKCE, on ``requests`` alone.

The flow, in two requests to the provider:

1. ``start_sign_in`` reads the provider's discovery document, keeps a pending sign-in in the
   signed session (``state``, ``nonce``, the PKCE verifier and the time it started) and returns the authorization URL the browser is sent to.
2. ``finish_sign_in`` takes the pending sign-in out of the session (it is used once), checks
   the callback's ``state``, exchanges the code at the token endpoint and accepts the ID token
   from that response by its claims. The signature is not verified: the token comes straight
   from the provider's token endpoint over TLS (OpenID Connect Core 1.0, 3.1.3.7), so it must
   never be accepted from the browser.

Every failure raises ``SignInRefused`` with a reason for the log. The reason is fixed text
(and at most an HTTP status or the names of missing fields): never a token, a code, the client
secret or a claim's value. Nothing is written before every check has passed.
"""
import base64
import binascii
import hashlib
import ipaddress
import json
import secrets
import time
from urllib.parse import quote_plus, urlencode, urlsplit

import requests

from models.db import INTEGRITY_ERRORS, get_db
from models.user import User

# The four settings OIDC needs, in the order the startup warning names them.
SETTING_NAMES = ('OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET', 'OIDC_ISSUER_URL', 'OIDC_REDIRECT_URI')

PENDING_KEY = '_oidc_pending'
PENDING_LIFETIME_SECONDS = 600     # a sign-in must come back from the provider within this
CLOCK_LEEWAY_SECONDS = 60          # tolerated clock difference for exp and iat
REQUEST_TIMEOUT_SECONDS = 10       # every request to the provider
SCOPE = 'openid email profile'

USERNAME_MAX = 64                  # a username taken from the token is cut to this length
EMAIL_MAX = 254
SUB_MAX = 255                      # OpenID Connect Core: sub is at most 255 ASCII characters

REFUSED_MESSAGE = ('Sign-in with your identity provider did not complete. Nobody was signed '
                   'in. Try again, or ask the administrator.')
DEACTIVATED_MESSAGE = 'This account has been deactivated. Ask the administrator.'

_LOOPBACK_NAMES = {'localhost'}


class SignInRefused(Exception):
    """The sign-in ends on the login page with ``message``; ``reason`` is for the log only."""

    def __init__(self, reason: str, message: str = REFUSED_MESSAGE):
        super().__init__(reason)
        self.reason = reason
        self.message = message


# ── Configuration ────────────────────────────────────────────────────────────

def missing_settings(config) -> list:
    """The names of the OIDC settings that are unset or blank."""
    return [name for name in SETTING_NAMES if not _setting(config, name)]


def is_enabled(config) -> bool:
    """OIDC is on when all four settings are set and EasyAuth is off."""
    return not config.get('EASYAUTH_ENABLED') and not missing_settings(config)


def warn_if_partial(app) -> None:
    """At startup: a partial OIDC configuration leaves OIDC off; say which names are missing
    (never a value)."""
    missing = missing_settings(app.config)
    if 0 < len(missing) < len(SETTING_NAMES):
        app.logger.warning('OIDC sign-in is off: %s not set.', ', '.join(missing))


def _setting(config, name) -> str:
    value = config.get(name)
    return value.strip() if isinstance(value, str) else ''


# ── Transport ────────────────────────────────────────────────────────────────

def url_allowed(url) -> bool:
    """An https URL with a host, or http to a loopback host (a provider on this machine)."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if not host:
        return False
    scheme = parts.scheme.lower()
    if scheme == 'https':
        return True
    return scheme == 'http' and _is_loopback(host)


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _json_response(response, what: str) -> dict:
    if response.status_code != 200:
        raise SignInRefused(f'{what} answered HTTP {response.status_code}')
    try:
        body = response.json()
    except ValueError:
        raise SignInRefused(f'{what} did not answer JSON') from None
    if not isinstance(body, dict):
        raise SignInRefused(f'{what} did not answer a JSON object')
    return body


def _issuer_matches(configured: str, given) -> bool:
    """The provider's issuer equals the configured one; one trailing slash is not significant."""
    return isinstance(given, str) and given.rstrip('/') == configured.rstrip('/')


def discover(config) -> dict:
    """The provider's discovery document, checked: its issuer is the configured one and the
    issuer and both endpoints are allowed URLs."""
    issuer = _setting(config, 'OIDC_ISSUER_URL')
    if not url_allowed(issuer):
        raise SignInRefused('the issuer URL is not https (or http to a loopback host)')
    url = issuer.rstrip('/') + '/.well-known/openid-configuration'
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False,
                                headers={'Accept': 'application/json'})
    except requests.RequestException as err:
        raise SignInRefused(f'discovery request failed ({type(err).__name__})') from None
    document = _json_response(response, 'discovery')
    if not _issuer_matches(issuer, document.get('issuer')):
        raise SignInRefused('discovery names another issuer')
    missing = [key for key in ('authorization_endpoint', 'token_endpoint')
               if not isinstance(document.get(key), str) or not document.get(key)]
    if missing:
        raise SignInRefused('discovery lacks ' + ', '.join(missing))
    for key in ('authorization_endpoint', 'token_endpoint'):
        if not url_allowed(document[key]):
            raise SignInRefused(f'{key} is not https (or http to a loopback host)')
    return document


# ── Start ────────────────────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def start_sign_in(config, session) -> str:
    """Keep a new pending sign-in in ``session`` (replacing any earlier one) and return the
    provider's authorization URL."""
    document = discover(config)
    verifier = secrets.token_urlsafe(48)
    pending = {
        'state': secrets.token_urlsafe(32),
        'nonce': secrets.token_urlsafe(32),
        'verifier': verifier,
        'started': int(time.time()),
    }
    session[PENDING_KEY] = pending
    query = urlencode({
        'response_type': 'code',
        'client_id': _setting(config, 'OIDC_CLIENT_ID'),
        'redirect_uri': _setting(config, 'OIDC_REDIRECT_URI'),
        'scope': SCOPE,
        'state': pending['state'],
        'nonce': pending['nonce'],
        'code_challenge': _b64url(hashlib.sha256(verifier.encode('ascii')).digest()),
        'code_challenge_method': 'S256',
    })
    endpoint = document['authorization_endpoint']
    return endpoint + ('&' if '?' in endpoint else '?') + query


def forget_pending(session) -> None:
    """Drop the pending sign-in (at sign-out): a callback afterwards is refused."""
    session.pop(PENDING_KEY, None)


# ── Callback ─────────────────────────────────────────────────────────────────

def finish_sign_in(config, session, args):
    """Check the callback, exchange the code and return the user to sign in.

    The pending sign-in is taken out of the session first, so it is used once whatever the
    outcome. Raises ``SignInRefused``; nothing is written unless every check passed.
    """
    pending = session.pop(PENDING_KEY, None)
    if not _pending_valid(pending):
        raise SignInRefused('no pending sign-in, or it expired')
    state = args.get('state')
    if not isinstance(state, str) or not secrets.compare_digest(
            state.encode('utf-8'), pending['state'].encode('utf-8')):
        raise SignInRefused('the state does not match the pending sign-in')
    if args.get('error'):
        raise SignInRefused('the provider answered with an error')
    code = args.get('code')
    if not code:
        raise SignInRefused('the callback has no code')

    document = discover(config)
    id_token = _exchange_code(config, document, code, pending['verifier'])
    claims = decode_id_token(id_token)
    sub = check_claims(config, document, claims, pending['nonce'])
    return _account_for(sub, claims)


def _pending_valid(pending) -> bool:
    if not isinstance(pending, dict):
        return False
    if not all(isinstance(pending.get(k), str) and pending.get(k)
               for k in ('state', 'nonce', 'verifier')):
        return False
    started = pending.get('started')
    if not isinstance(started, int) or isinstance(started, bool):
        return False
    age = time.time() - started
    return -CLOCK_LEEWAY_SECONDS <= age <= PENDING_LIFETIME_SECONDS


def _token_auth_method(document) -> str:
    """client_secret_basic, unless the provider lists client_secret_post and not basic."""
    methods = document.get('token_endpoint_auth_methods_supported')
    if isinstance(methods, list) and 'client_secret_post' in methods \
            and 'client_secret_basic' not in methods:
        return 'client_secret_post'
    return 'client_secret_basic'


def _exchange_code(config, document, code, verifier) -> str:
    client_id = _setting(config, 'OIDC_CLIENT_ID')
    secret = _setting(config, 'OIDC_CLIENT_SECRET')
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': _setting(config, 'OIDC_REDIRECT_URI'),
        'code_verifier': verifier,
    }
    auth = None
    if _token_auth_method(document) == 'client_secret_post':
        data['client_id'] = client_id
        data['client_secret'] = secret
    else:
        # RFC 6749, 2.3.1: both values form-encoded before they go into the Basic header.
        auth = (quote_plus(client_id), quote_plus(secret))
    try:
        response = requests.post(document['token_endpoint'], data=data, auth=auth,
                                 timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False,
                                 headers={'Accept': 'application/json'})
    except requests.RequestException as err:
        raise SignInRefused(f'token request failed ({type(err).__name__})') from None
    body = _json_response(response, 'the token endpoint')
    id_token = body.get('id_token')
    if not isinstance(id_token, str) or not id_token:
        raise SignInRefused('the token response has no id_token')
    return id_token


def decode_id_token(id_token: str) -> dict:
    """The claims of a JWT (header.payload.signature), read without checking the signature."""
    parts = id_token.split('.')
    if len(parts) != 3 or not parts[1]:
        raise SignInRefused('the id_token is not a JWT')
    payload = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
        claims = json.loads(raw.decode('utf-8'))
    except (binascii.Error, ValueError, UnicodeDecodeError, RecursionError):
        raise SignInRefused('the id_token payload is not base64url JSON') from None
    if not isinstance(claims, dict):
        raise SignInRefused('the id_token payload is not a JSON object')
    return claims


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and value == value and abs(value) != float('inf')


def check_claims(config, document, claims, nonce, now=None) -> str:
    """Accept the ID token's claims or raise; returns the ``sub``."""
    now = time.time() if now is None else now
    client_id = _setting(config, 'OIDC_CLIENT_ID')
    if not isinstance(claims.get('iss'), str) or claims['iss'] != document['issuer']:
        raise SignInRefused('the id_token names another issuer')
    aud = claims.get('aud')
    if isinstance(aud, str):
        audiences = [aud]
    elif isinstance(aud, list) and aud and all(isinstance(a, str) for a in aud):
        audiences = aud
    else:
        raise SignInRefused('the id_token has no valid aud')
    if client_id not in audiences:
        raise SignInRefused('the id_token is for another client')
    azp = claims.get('azp')
    if len(audiences) > 1 and azp is None:
        raise SignInRefused('the id_token has several audiences and no azp')
    if azp is not None and azp != client_id:
        raise SignInRefused('the id_token azp is another client')
    exp, iat = claims.get('exp'), claims.get('iat')
    if not _is_number(exp) or not _is_number(iat):
        raise SignInRefused('the id_token exp or iat is not a number')
    if exp <= now - CLOCK_LEEWAY_SECONDS:
        raise SignInRefused('the id_token has expired')
    if iat > now + CLOCK_LEEWAY_SECONDS:
        raise SignInRefused('the id_token was issued in the future')
    token_nonce = claims.get('nonce')
    if not isinstance(token_nonce, str) or not secrets.compare_digest(
            token_nonce.encode('utf-8'), nonce.encode('utf-8')):
        raise SignInRefused('the id_token nonce does not match')
    sub = claims.get('sub')
    if not isinstance(sub, str) or not sub.strip() or len(sub) > SUB_MAX:
        raise SignInRefused('the id_token sub is not a non-empty string of at most 255 characters')
    return sub


# ── Accounts ─────────────────────────────────────────────────────────────────

def _claim_text(claims, key, limit):
    value = claims.get(key)
    if not isinstance(value, str):
        return None
    value = ' '.join(value.split())  # no control characters or runs of blanks in a name
    return value[:limit] if value else None


def _account_for(sub: str, claims: dict):
    """The OIDC account of ``sub``, created on its first sign-in. Never another row."""
    user = User.get_oidc_account(sub)
    if user is None:
        user = _create_account(sub, claims)
    if not user.is_active:
        raise SignInRefused('the account is deactivated', DEACTIVATED_MESSAGE)
    return user


def _create_account(sub: str, claims: dict):
    """Insert the OIDC account of ``sub``. A username, email or sub another row holds fails
    the insert on its unique column: the sign-in is refused and nothing is written."""
    username = (_claim_text(claims, 'preferred_username', USERNAME_MAX)
                or _claim_text(claims, 'email', USERNAME_MAX)
                or sub.strip()[:USERNAME_MAX])
    email = _claim_text(claims, 'email', EMAIL_MAX)
    try:
        User.create_oidc_account(username, email, sub)
    except INTEGRITY_ERRORS:
        get_db().rollback()
        raise SignInRefused('another account holds the username, email or sub') from None
    user = User.get_oidc_account(sub)
    if user is None:
        raise SignInRefused('the account could not be created')
    return user
