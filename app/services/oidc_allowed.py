"""Who may sign in through OIDC: the optional allow lists, checked at every sign-in.

``OIDC_ALLOWED_EMAIL_DOMAINS`` and ``OIDC_ALLOWED_GROUPS`` are comma-separated lists read
from the configuration at each use. Unset: anyone the provider signs in. Set: the ID token's
claims must pass every list that is set, checked after the token's own claims and before the
account is looked up or created, so a returning account that no longer passes is refused too.
A list that is set but holds no entry turns OIDC sign-in off (``services.oidc.is_enabled``).

A refusal names the rule that refused (domain, verified, groups) in fixed text, never a
claim value.
"""
from services.oidc import SignInRefused, is_storable_text

DOMAINS_SETTING = 'OIDC_ALLOWED_EMAIL_DOMAINS'
GROUPS_SETTING = 'OIDC_ALLOWED_GROUPS'
ALLOW_LIST_NAMES = (DOMAINS_SETTING, GROUPS_SETTING)

NOT_ALLOWED_MESSAGE = ('Your account at the identity provider is not allowed to sign in to '
                       'this app.')


def allow_list(config, name):
    """The entries of an allow list, or ``None`` when the setting is unset: split at commas,
    blanks around each entry trimmed, empty entries dropped. Email domains lose one leading
    ``@`` and are lower-cased; group names are kept as written."""
    value = config.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        return ()
    entries = []
    for entry in value.split(','):
        entry = entry.strip()
        if name == DOMAINS_SETTING:
            entry = (entry[1:] if entry.startswith('@') else entry).strip().lower()
        if entry:
            entries.append(entry)
    return tuple(entries)


def empty_allow_lists(config) -> list:
    """The allow lists that are set but hold no entry (blank, ``" , "``): each turns OIDC
    sign-in off, so a typo never opens the app to everyone nor silently refuses everyone."""
    return [name for name in ALLOW_LIST_NAMES if allow_list(config, name) == ()]


def _refuse(rule: str):
    raise SignInRefused(f'not allowed: {rule}', NOT_ALLOWED_MESSAGE)


def check_allowed(config, claims) -> None:
    """Return when the claims pass every allow list that is set; raise ``SignInRefused``
    otherwise."""
    domains = allow_list(config, DOMAINS_SETTING)
    if domains is not None:
        email = claims.get('email')
        if not is_storable_text(email) or '@' not in email:
            _refuse('domain (the email is missing, not storable text or has no @)')
        if 'email_verified' in claims and claims['email_verified'] is not True:
            _refuse('verified (email_verified is present and not true)')
        # The part after the last @, blanks around it trimmed, in lower case, equal to an
        # entry: no subdomain, no suffix match, no other normalisation.
        if email.rpartition('@')[2].strip().lower() not in domains:
            _refuse('domain (the email domain is not in the list)')
    groups = allow_list(config, GROUPS_SETTING)
    if groups is not None:
        held = claims.get('groups')
        if isinstance(held, str):
            held = [held]
        if not isinstance(held, list) or not all(is_storable_text(g) for g in held):
            _refuse('groups (the claim is missing or not a list of strings)')
        # Compared exactly, case included: group ids and paths are the provider's.
        if not any(g in groups for g in held):
            _refuse('groups (no group of the claim is in the list)')
