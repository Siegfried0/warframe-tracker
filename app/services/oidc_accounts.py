"""The OIDC account of a signed-in ``sub``: found, or created on its first sign-in.

Only the row with ``auth_type = 'oidc'`` and that ``sub`` is ever matched, never a local or
EasyAuth row, and a new account never takes what another row holds (its username gets a
variant, a held email is stored as NULL). The claims were checked by ``services.oidc``.
"""
import secrets

from models.db import INTEGRITY_ERRORS, get_db
from models.user import User
from services.oidc import DEACTIVATED_MESSAGE, SignInRefused, is_storable_text

USERNAME_MAX = 64                  # a username taken from the token is cut to this length
EMAIL_MAX = 254


def _claim_text(claims, key, limit):
    """A display claim (name, email) as text of at most ``limit`` characters, or None when
    it is absent, blank or not storable text (then the next fallback is taken)."""
    value = claims.get(key)
    if not is_storable_text(value):
        return None
    value = ' '.join(value.split())  # no control characters or runs of blanks in a name
    return value[:limit] if value else None


def account_for(sub: str, claims: dict):
    """The OIDC account of ``sub``, created on its first sign-in. Never another row."""
    user = User.get_oidc_account(sub)
    if user is None:
        user = _create_account(sub, claims)
    if not user.is_active:
        raise SignInRefused('the account is deactivated', DEACTIVATED_MESSAGE)
    return user


def _create_account(sub: str, claims: dict):
    """Insert the OIDC account of ``sub``, never taking what another row holds: a taken
    username gets a variant, a taken email is stored as NULL, and a ``sub`` another kind of
    account holds (the column is unique) refuses the sign-in.

    An insert that still fails on a unique column is told apart by re-reading the ``sub``
    rather than by the backend's message: when an OIDC row of this ``sub`` exists now, a
    parallel first sign-in made it and it is used; otherwise (a name or email taken in the
    meantime, a second NULL email on MSSQL) the sign-in is refused, nothing written."""
    if User.oidc_sub_held(sub, other_than='oidc'):
        raise SignInRefused('another kind of account holds this sub')
    base = (_claim_text(claims, 'preferred_username', USERNAME_MAX)
            or _claim_text(claims, 'email', USERNAME_MAX)
            or sub.strip()[:USERNAME_MAX])
    email = _claim_text(claims, 'email', EMAIL_MAX)
    if email is not None and User.email_taken(email):
        email = None  # email is not an identity here; the account never takes another's
    username = unique_username(base)
    try:
        User.create_oidc_account(username, email, sub)
    except INTEGRITY_ERRORS:
        get_db().rollback()
        user = User.get_oidc_account(sub)
        if user is None:
            raise SignInRefused('the insert failed on a unique column') from None
        return user
    user = User.get_oidc_account(sub)
    if user is None:
        raise SignInRefused('the account could not be created')
    return user


USERNAME_NUMBERED_MAX = 99         # name-2 ... name-99, then name-<8 hex>
USERNAME_RANDOM_TRIES = 20


def unique_username(base: str) -> str:
    """``base`` when no account has it, else ``base-2`` ... ``base-99``, then
    ``base-<8 hex>``: never an existing name, and never longer than ``USERNAME_MAX`` (the
    base is cut to make room for the suffix). Raises ``SignInRefused`` when every try is
    taken."""
    if not User.get_by_username(base):
        return base
    suffixes = [f'-{n}' for n in range(2, USERNAME_NUMBERED_MAX + 1)]
    suffixes += [f'-{secrets.token_hex(4)}' for _ in range(USERNAME_RANDOM_TRIES)]
    for suffix in suffixes:
        candidate = base[:USERNAME_MAX - len(suffix)] + suffix
        if not User.get_by_username(candidate):
            return candidate
    raise SignInRefused('no free username variant')
