import os
from pathlib import Path

BASE_DIR = Path(__file__).parent  # app/

# The key that older versions used when none was set. It is published with the source, so a
# session or form token signed with it can be forged by anyone: the app refuses to start with it.
PUBLISHED_DEFAULT_SECRET_KEY = 'dev-change-me-in-production'

SECRET_KEY_HELP = (
    'SECRET_KEY must be set to a long random value; it signs the login sessions and the form '
    'tokens. Make one with: python -c "import secrets; print(secrets.token_hex(32))" '
    'and set it in the environment before starting the app.'
)


class SecretKeyError(RuntimeError):
    """The app was started without a usable SECRET_KEY. The message never holds a key value."""


def secret_key_is_usable(value) -> bool:
    """A key is usable when it is a non-blank string (or bytes) other than the published default.

    Surrounding whitespace is ignored for the comparison; the default in another letter case is
    a different key and is accepted.
    """
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode('utf-8')
        except UnicodeDecodeError:
            return len(value) > 0  # raw random bytes: nothing published can match them
    if not isinstance(value, str):
        return False
    trimmed = value.strip()
    return trimmed != '' and trimmed != PUBLISHED_DEFAULT_SECRET_KEY


def require_usable_secret_key(value) -> None:
    """Raise SecretKeyError when the key is missing, blank or the published default."""
    if not secret_key_is_usable(value):
        raise SecretKeyError(
            'The app does not start: SECRET_KEY is missing, blank or the value published with '
            'the source code. ' + SECRET_KEY_HELP
        )


SAMESITE_VALUES = ('Strict', 'Lax', 'None')
SAMESITE_DEFAULT = 'Lax'


class SettingError(RuntimeError):
    """A setting holds a value the app cannot use: it does not start (never a 500 per page)."""


def session_cookie_samesite(value) -> str:
    """The session cookie's SameSite value in the spelling the cookie needs: ``Strict``,
    ``Lax`` or ``None`` in any letter case, surrounding blanks ignored. Unset, empty or blank
    means the default ``Lax``. Raises SettingError for any other value (``off``, ``yes``)."""
    if value is None:
        return SAMESITE_DEFAULT
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed == '':
            return SAMESITE_DEFAULT
        for known in SAMESITE_VALUES:
            if trimmed.lower() == known.lower():
                return known
    raise SettingError(
        'The app does not start: SESSION_COOKIE_SAMESITE must be Strict, Lax or None (any '
        'letter case; empty means the default, Lax), not ' + repr(value)[:60] + '.'
    )


class Config:
    # No default: create_app() refuses to start without a usable key.
    SECRET_KEY = os.environ.get('SECRET_KEY')
    DATABASE_PATH = os.environ.get('DATABASE_PATH', str(BASE_DIR / 'warframe_tracker.db'))

    SESSION_LIFETIME_DAYS = int(os.environ.get('SESSION_LIFETIME_DAYS', 30))

    # The session cookie is not sent on cross-site posts (defence in depth beside the CSRF
    # token); 'Strict', 'Lax' or 'None' (the last needs HTTPS). create_app() normalises it
    # (session_cookie_samesite) and refuses to start with any other value.
    SESSION_COOKIE_SAMESITE = os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax')

    MARKET_CACHE_HOURS = int(os.environ.get('MARKET_CACHE_HOURS', 6))

    AUTO_SYNC_ON_STARTUP = os.environ.get('AUTO_SYNC_ON_STARTUP', 'false').lower() == 'true'
    SYNC_INTERVAL_HOURS = int(os.environ.get('SYNC_INTERVAL_HOURS', 24))

    # Prime Resurgence stock read from a page without a readable end date counts for this
    # many days after the sync that read it (a value below 1 means the default, 7)
    RESURGENCE_FALLBACK_DAYS = int(os.environ.get('RESURGENCE_FALLBACK_DAYS', 7))

    # OIDC — all optional
    OIDC_CLIENT_ID = os.environ.get('OIDC_CLIENT_ID')
    OIDC_CLIENT_SECRET = os.environ.get('OIDC_CLIENT_SECRET')
    OIDC_ISSUER_URL = os.environ.get('OIDC_ISSUER_URL')
    OIDC_REDIRECT_URI = os.environ.get('OIDC_REDIRECT_URI')
    # Who may sign in through OIDC (comma-separated; unset = anyone the provider signs in;
    # set but without an entry = OIDC sign-in off)
    OIDC_ALLOWED_EMAIL_DOMAINS = os.environ.get('OIDC_ALLOWED_EMAIL_DOMAINS')
    OIDC_ALLOWED_GROUPS = os.environ.get('OIDC_ALLOWED_GROUPS')

    # Backup — local
    BACKUP_LOCAL_PATH = os.environ.get('BACKUP_LOCAL_PATH')

    # Backup — S3-compatible (all optional)
    BACKUP_S3_ENDPOINT = os.environ.get('BACKUP_S3_ENDPOINT')
    BACKUP_S3_BUCKET = os.environ.get('BACKUP_S3_BUCKET')
    BACKUP_S3_PREFIX = os.environ.get('BACKUP_S3_PREFIX', 'warframe-tracker/')
    BACKUP_S3_ACCESS_KEY = os.environ.get('BACKUP_S3_ACCESS_KEY')
    BACKUP_S3_SECRET_KEY = os.environ.get('BACKUP_S3_SECRET_KEY')

    # Azure EasyAuth — opt-in
    EASYAUTH_ENABLED = os.environ.get('EASYAUTH_ENABLED', 'false').lower() == 'true'

    # Database backend — set MSSQL_CONNECTION_STRING to switch from SQLite
    MSSQL_CONNECTION_STRING = os.environ.get('MSSQL_CONNECTION_STRING')
    DB_BACKEND = 'mssql' if os.environ.get('MSSQL_CONNECTION_STRING') else 'sqlite'
