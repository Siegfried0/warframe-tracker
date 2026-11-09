import re
from functools import wraps
from urllib.parse import unquote

from flask import abort
from flask_login import current_user

# The Settings page's personal settings: (key, label, default). The export carries them.
USER_SETTINGS = [
    ('market_cache_hours', 'Market cache duration (hours)', '6'),
    ('buy_threshold_plat', 'Buy threshold (platinum)', ''),
    ('plat_per_hour',      'Platinum per hour (time-value)', ''),
]

# A personal setting is a whole number from 0 to this bound (SQLite's signed 64-bit range),
# the same values the import accepts for a setting, so a saved value always re-imports.
SETTING_VALUE_MAX = 2 ** 63 - 1
_SETTING_DIGITS = len(str(SETTING_VALUE_MAX))
_WHOLE = re.compile(r'[0-9]+')

# The longest page to return to after a sign-in, in characters of the value as given (before
# percent-decoding, so an encoded spelling counts at its encoded length). A longer one goes
# to the dashboard, never cut: a cut path is another page. At this length even a path of
# characters outside the Basic Multilingual Plane (12 bytes each in the session's JSON)
# keeps the pending OIDC sign-in within the browsers' 4 KB cookie limit.
RETURN_PATH_MAX = 200


def safe_return_path(value):
    """``value`` when it is a path on this site of at most ``RETURN_PATH_MAX`` characters,
    else None (the caller goes to the dashboard).

    A path starts with one ``/``, so anything with a scheme (``https://evil``, ``javascript:``
    in any case) or a leading blank is refused. Also refused: a scheme-relative ``//evil``,
    any backslash (browsers read ``/\\evil`` as ``//evil``) and any control character
    (browsers drop tabs and newlines, so ``/\\t/evil`` would become ``//evil``). The same
    checks apply to the value percent-decoded, repeatedly, so an encoded spelling
    (``/%2F%2Fevil``, ``/%5Cevil``) is refused too.
    """
    if not isinstance(value, str) or len(value) > RETURN_PATH_MAX:
        return None
    seen = value
    for _ in range(_DECODE_ROUNDS):
        if not _plain_path(seen):
            return None
        decoded = unquote(seen)
        if decoded == seen:
            return value
        seen = decoded
    return None  # still encoded after that many rounds: not a plain path


_DECODE_ROUNDS = 5


def _plain_path(value: str) -> bool:
    if not value.startswith('/') or value.startswith('//'):
        return False
    return '\\' not in value and not any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def sign_out(session) -> None:
    """End the signed-in session (logout, and the deletion of one's own account): the CSRF
    token is replaced at once, a pending OIDC sign-in is dropped and the user is logged
    out. The caller sends the browser on (in EasyAuth mode to Azure's sign-out)."""
    # Imported here: services.csrf reaches this module through the export service.
    from flask_login import logout_user
    from services import csrf, oidc
    csrf.rotate_token()
    oidc.forget_pending(session)
    logout_user()


def admin_required(f):
    """Route decorator — 403 if the current user is not an admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def get_setting(user_id, key: str, default=None):
    from models.db import get_db
    row = get_db().execute(
        'SELECT value FROM settings WHERE user_id = ? AND key = ?',
        (user_id, key),
    ).fetchone()
    return row['value'] if row else default


def parse_setting_value(value):
    """A setting value as a whole number from 0 to SETTING_VALUE_MAX (surrounding blanks
    stripped), or None for anything else: NULL, empty, text, a decimal, a negative."""
    if value is None:
        return None
    s = str(value).strip()
    if not _WHOLE.fullmatch(s):
        return None
    # Leading zeros go and the length is checked before int(): int() refuses a digit string
    # past the interpreter's digit limit (zeros included), and no number with more digits
    # than the bound fits.
    digits = s.lstrip('0') or '0'
    if len(digits) > _SETTING_DIGITS or int(digits) > SETTING_VALUE_MAX:
        return None
    return int(digits)


def get_setting_int(user_id, key: str):
    """A personal setting read as a whole number. A missing row, a NULL or a stored value
    that is not a whole number in range reads as the key's default (None when it has none),
    so a cleared field or a hand-edited value never breaks a page that uses it."""
    default = parse_setting_value(dict((k, d) for k, _, d in USER_SETTINGS).get(key))
    value = parse_setting_value(get_setting(user_id, key))
    return default if value is None else value


def save_preferences(user_id, form) -> list:
    """Store the Settings page's personal settings from ``form``. Each field is empty
    (stored as NULL) or a whole number from 0 to SETTING_VALUE_MAX, stored as its digits.
    Returns the labels of the invalid fields; if there is any, nothing is written."""
    values, invalid = {}, []
    for key, label, _ in USER_SETTINGS:
        raw = (form.get(key) or '').strip()
        if raw == '':
            values[key] = None
            continue
        number = parse_setting_value(raw)
        if number is None:
            invalid.append(label)
        else:
            values[key] = number
    if invalid:
        return invalid
    from models.db import get_db
    db = get_db()
    try:
        for key, value in values.items():
            _upsert_setting(db, user_id, key, value)
        db.commit()  # one commit: the fields are saved all or none
    except Exception:
        db.rollback()
        raise
    return []


def _upsert_setting(db, user_id, key: str, value):
    db.execute(
        '''INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)
           ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value''',
        (user_id, key, str(value) if value is not None else None),
    )


def set_setting(user_id, key: str, value):
    from models.db import get_db
    db = get_db()
    _upsert_setting(db, user_id, key, value)
    db.commit()


def get_global_setting(key: str, default=None):
    from models.db import get_db
    row = get_db().execute(
        'SELECT value FROM settings WHERE user_id IS NULL AND key = ?', (key,)
    ).fetchone()
    return row['value'] if row else default


def upsert_global_setting(db, key: str, value):
    """Write one app-wide setting (``user_id`` NULL) without committing: the row's value is
    updated if the key exists, else a row is inserted. No ``ON CONFLICT``: SQLite's UNIQUE
    (user_id, key) treats every NULL user_id as distinct, so the conflict never fires and
    each call would add a row; MSSQL has no such clause. Both backends run these statements."""
    text = str(value) if value is not None else None
    row = db.execute(
        'SELECT id FROM settings WHERE user_id IS NULL AND key = ?', (key,)
    ).fetchone()
    if row:
        db.execute(
            'UPDATE settings SET value = ? WHERE user_id IS NULL AND key = ?', (text, key)
        )
    else:
        db.execute(
            'INSERT INTO settings (user_id, key, value) VALUES (NULL, ?, ?)', (key, text)
        )


def set_global_setting(key: str, value):
    from models.db import get_db
    db = get_db()
    try:
        upsert_global_setting(db, key, value)
        db.commit()
    except Exception:
        db.rollback()
        raise
