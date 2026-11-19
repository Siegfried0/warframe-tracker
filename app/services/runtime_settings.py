"""The admin's runtime settings: auto-sync on startup, the scheduled sync interval and the
session lifetime. Every decision about the three (the scheduler's start and jobs, the scheduled
check, the session lifetime) reads them here, never from ``Config``.

Each has three sources, in this order:

1. **A pin.** An environment variable that is set (``Config`` keeps its raw text, or None when
   it is unset), or a ``create_app`` override that is not None. ``init_app`` decides the pins
   once, before the database is opened. A pinned number is an ``int`` (not a ``bool``) within
   the bounds, or a text the spelling check accepts (blanks stripped, 1 to 4 ASCII digits); a
   pinned switch is a ``bool``, or a text that, blanks stripped and ASCII letters lower-cased,
   is ``true`` or ``false``. Any other pinned value, the empty text included, refuses the start
   with ``SettingError``: a mistyped ``0`` read as the default would start the sync the pin
   exists to prevent. A pin is never read from the database.
2. **A stored value**: the app-wide rows of ``settings`` (``user_id`` NULL) written for the
   admin, as normalised text (``true`` / ``false``, a whole number).
3. **The default**: ``false``, 24 hours, 30 days. A stored value that is not valid is read as
   the default and logged once per process, and stays stored until an admin saves.

A failed read of the settings raises from ``effective``: each caller decides what it means
(the scheduled check skips, the start treats the setting as off, the session uses the default).
"""
import logging
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from flask.sessions import SecureCookieSessionInterface
from itsdangerous import BadSignature

from config import SettingError
from services.backup_settings import parse_whole

log = logging.getLogger(__name__)


class Setting(NamedTuple):
    key: str        # the settings row's key
    env: str        # the environment variable (and ``Config`` / override name) that pins it
    kind: str       # 'switch' or 'number'
    low: int
    high: int
    default: object
    label: str
    unit: str


AUTO_SYNC = 'auto_sync_on_startup'
INTERVAL = 'sync_interval_hours'
LIFETIME = 'session_lifetime_days'

SETTINGS = {
    AUTO_SYNC: Setting(AUTO_SYNC, 'AUTO_SYNC_ON_STARTUP', 'switch', 0, 1, False,
                       'Sync on startup', ''),
    INTERVAL: Setting(INTERVAL, 'SYNC_INTERVAL_HOURS', 'number', 0, 8760, 24,
                      'Scheduled sync interval', 'hours'),
    LIFETIME: Setting(LIFETIME, 'SESSION_LIFETIME_DAYS', 'number', 1, 365, 30,
                      'Session lifetime', 'days'),
}
ORDER = (AUTO_SYNC, INTERVAL, LIFETIME)

EXTENSION = 'runtime_settings'
CACHE_SECONDS = 60          # the stored session lifetime is read at most once a minute
REMEMBER_COOKIE = 'remember_token'   # Flask-Login's default name of its remember cookie

_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_logged = set()   # (key, repr of the stored value) already logged in this process
_clock = time.monotonic   # the cache's clock (a test replaces it)


def parse_switch(raw):
    """True or False for a text that, blanks stripped and ASCII letters lower-cased, is
    ``true`` or ``false``; None for anything else."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().translate(_ASCII_LOWER)
    return {'true': True, 'false': False}.get(text)


def parse_value(setting: Setting, raw):
    """The value a stored text holds for the setting, or None when it holds none."""
    if setting.kind == 'switch':
        return parse_switch(raw)
    return parse_whole(raw, setting.low, setting.high)


def stored_text(setting: Setting, value) -> str:
    """The normalised text a value is stored as."""
    if setting.kind == 'switch':
        return 'true' if value else 'false'
    return str(int(value))


def _accepts(setting: Setting) -> str:
    if setting.kind == 'switch':
        return 'true or false (any letter case)'
    return f'a whole number from {setting.low} to {setting.high} (digits only)'


def _pinned(setting: Setting, raw):
    """The value a pin holds; raises SettingError for one the app cannot use."""
    if setting.kind == 'switch':
        value = raw if isinstance(raw, bool) else parse_switch(raw)
    elif isinstance(raw, int) and not isinstance(raw, bool):
        value = raw if setting.low <= raw <= setting.high else None
    else:
        value = parse_whole(raw, setting.low, setting.high)
    if value is None:
        raise SettingError(
            f'The app does not start: {setting.env} must be {_accepts(setting)}, or be unset, '
            f'not ' + repr(raw)[:60] + '.')
    return value


class _State:
    """One app's pins and its cache of the stored session lifetime."""

    def __init__(self, pins: dict):
        self.pins = pins               # key -> pinned value, only for the pinned settings
        self.lock = threading.Lock()
        self.lifetime = None           # (days, monotonic time read) or None
        self.lifetime_failing = False  # a failed read was logged and none succeeded since


def init_app(app) -> None:
    """Decide the pins from ``app.config`` (after the overrides) and install the session
    interface that applies the effective lifetime. Raises SettingError for a pin the app
    cannot use; opens nothing."""
    pins = {}
    for key in ORDER:
        setting = SETTINGS[key]
        raw = app.config.get(setting.env)
        if raw is not None:
            pins[key] = _pinned(setting, raw)
    app.extensions[EXTENSION] = _State(pins)
    app.session_interface = RuntimeSessionInterface()


def _state(app) -> _State:
    return app.extensions[EXTENSION]


def pinned(app, key: str) -> bool:
    return key in _state(app).pins


def _warn_once(key, raw, default):
    mark = (key, repr(raw))
    if mark not in _logged:
        _logged.add(mark)
        log.warning('Runtime setting %s holds %r, which is not valid: the default %r applies',
                    key, raw, default)


def read_stored(key: str, get=None):
    """(value, raw) of the stored row: value None when there is no row, a NULL value or a value
    that is not valid (logged once). A failed read raises."""
    if get is None:
        from services.auth_service import get_global_setting as get
    setting = SETTINGS[key]
    raw = get(key)
    if raw is None:
        return None, None
    value = parse_value(setting, raw)
    if value is None:
        _warn_once(key, raw, setting.default)
    return value, raw


def effective(app, key: str, get=None):
    """The value that applies: the pin, else the stored value, else the default. A failed
    read of an unpinned setting raises."""
    state = _state(app)
    if key in state.pins:
        return state.pins[key]
    value, _ = read_stored(key, get)
    return SETTINGS[key].default if value is None else value


# ── At the start ─────────────────────────────────────────────────────────────

class StartValues(NamedTuple):
    auto_sync: bool
    interval: int


def start_values(app, get=None) -> StartValues:
    """Auto-sync and the interval as the start decides them. An unpinned setting whose read
    fails counts as off (no start sync, no scheduled check), logged: the default 24 would start
    a sync an admin may have switched off, and a locked database must not stop the app."""
    values = {}
    for key, off in ((AUTO_SYNC, False), (INTERVAL, 0)):
        try:
            values[key] = effective(app, key, get)
        except Exception:  # noqa: BLE001 (any storage error: off, logged)
            log.exception('Runtime setting %s could not be read at the start: off until an '
                          'admin saves it or the next start', key)
            values[key] = off
    return StartValues(bool(values[AUTO_SYNC]), int(values[INTERVAL]))


# ── The session lifetime ─────────────────────────────────────────────────────

def session_lifetime(app, get=None, clock=None) -> timedelta:
    """The effective session lifetime. The stored value is cached for at most a minute per
    app; a failed read applies the default, is not cached and is logged once until a read
    succeeds. Never raises."""
    state = _state(app)
    if LIFETIME in state.pins:
        return timedelta(days=state.pins[LIFETIME])
    now = (clock or _clock)()
    cached = state.lifetime
    if cached is not None and 0 <= now - cached[1] < CACHE_SECONDS:
        return timedelta(days=cached[0])
    try:
        days = effective(app, LIFETIME, get)
    except Exception:  # noqa: BLE001 (a request never fails for this read)
        if not state.lifetime_failing:
            state.lifetime_failing = True
            log.exception('The session lifetime could not be read: the default %s days applies',
                          SETTINGS[LIFETIME].default)
        return timedelta(days=SETTINGS[LIFETIME].default)
    with state.lock:
        state.lifetime = (days, now)
        state.lifetime_failing = False
    return timedelta(days=days)


def forget_cached_lifetime(app) -> None:
    """Drop this app's cached session lifetime (after a save in this process), so the next
    request reads the stored value."""
    state = _state(app)
    with state.lock:
        state.lifetime = None


class RuntimeSessionInterface(SecureCookieSessionInterface):
    """Flask's signed-cookie session, with the effective lifetime as the cookie's maximum age
    (its idle time: Flask re-signs a permanent session at each request) and as a permanent
    cookie's expiry. A request that carries Flask-Login's remember cookie is marked so that the
    cookie signs nobody in and the response removes it: "remember me" is a permanent session."""

    def open_session(self, app, request):
        serializer = self.get_signing_serializer(app)
        if serializer is None:
            return None
        session = self.session_class()
        value = request.cookies.get(self.get_cookie_name(app))
        if value:
            max_age = int(session_lifetime(app).total_seconds())
            try:
                session = self.session_class(serializer.loads(value, max_age=max_age))
            except BadSignature:
                session = self.session_class()
        if app.config.get('REMEMBER_COOKIE_NAME', REMEMBER_COOKIE) in request.cookies:
            # Flask-Login then neither loads a user from it nor keeps it (its after-request
            # hook deletes the cookie and pops this mark before the session is saved).
            session['_remember'] = 'clear'
        return session

    def get_expiration_time(self, app, session):
        if session.permanent:
            return datetime.now(timezone.utc) + session_lifetime(app)
        return None
