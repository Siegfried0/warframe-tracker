from functools import wraps

from flask import abort
from flask_login import current_user

# The Settings page's personal settings: (key, label, default). The export carries them.
USER_SETTINGS = [
    ('market_cache_hours', 'Market cache duration (hours)', '6'),
    ('buy_threshold_plat', 'Buy threshold (platinum)', ''),
    ('plat_per_hour',      'Platinum per hour (time-value)', ''),
]


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


def set_setting(user_id, key: str, value):
    from models.db import get_db
    db = get_db()
    db.execute(
        '''INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)
           ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value''',
        (user_id, key, str(value) if value is not None else None),
    )
    db.commit()


def get_global_setting(key: str, default=None):
    from models.db import get_db
    row = get_db().execute(
        'SELECT value FROM settings WHERE user_id IS NULL AND key = ?', (key,)
    ).fetchone()
    return row['value'] if row else default


def set_global_setting(key: str, value):
    from models.db import get_db
    db = get_db()
    db.execute(
        '''INSERT INTO settings (user_id, key, value) VALUES (NULL, ?, ?)
           ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value''',
        (key, str(value) if value is not None else None),
    )
    db.commit()
