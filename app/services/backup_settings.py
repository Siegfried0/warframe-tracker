"""A backup target's retention, read from the app-wide settings (``user_id`` NULL).

Keys ``backup_<target>_<field>`` (target ``local`` or ``s3``). An absent key, or a row whose
value is NULL (the settings reader does not tell the two apart), takes its default without a
log line. A stored value that is not valid is read as its default and logged, never raised:
a number is valid when its text, blanks stripped, is 1 to 4 ASCII digits within its bounds
(``+7``, ``7.0``, ``1_000``, ``1e3``, a minus, other digits and an empty text are not); the
strategy is compared after stripping blanks and lower-casing ASCII letters. GFS counts that
are all 0 are read as their defaults. Each invalid stored text is logged once per process,
not at every scheduled check. A failed read of the settings raises: the caller skips that
target rather than apply defaults it did not read.
"""
import logging
import re
import string

from services.backup_retention import STRATEGIES, Retention

log = logging.getLogger(__name__)

TARGETS = ('local', 's3')
DEFAULTS = Retention()
BOUNDS = {
    'min_days': (0, 365),
    'keep': (1, 1000),
    'gfs_daily': (0, 1000),
    'gfs_weekly': (0, 1000),
    'gfs_monthly': (0, 1000),
}
GFS_FIELDS = ('gfs_daily', 'gfs_weekly', 'gfs_monthly')
_NUMBER = re.compile(r'[0-9]{1,4}')
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_logged = set()   # (key, repr of the stored value) already logged in this process


def setting_key(target: str, field: str) -> str:
    return f'backup_{target}_{field}'


def _warn_once(key, raw, message, *args):
    mark = (key, repr(raw))
    if mark not in _logged:
        _logged.add(mark)
        log.warning(message, *args)


def _invalid(key, raw, default):
    _warn_once(key, raw, 'Backup setting %s holds %r, which is not valid: the default %r '
               'applies', key, raw, default)
    return default


def _strategy(key, raw):
    if raw is None:
        return DEFAULTS.strategy
    text = raw.strip().translate(_ASCII_LOWER) if isinstance(raw, str) else None
    return text if text in STRATEGIES else _invalid(key, raw, DEFAULTS.strategy)


def _number(key, raw, field):
    default = getattr(DEFAULTS, field)
    if raw is None:
        return default
    low, high = BOUNDS[field]
    text = raw.strip() if isinstance(raw, str) else ''
    if _NUMBER.fullmatch(text) and low <= int(text) <= high:
        return int(text)
    return _invalid(key, raw, default)


def read_retention(target: str, get=None) -> Retention:
    """The target's retention. ``get(key)`` reads one global setting and returns None when it
    is absent or NULL (``get_global_setting``); any error it raises is raised."""
    if get is None:
        from services.auth_service import get_global_setting as get
    key = setting_key(target, 'strategy')
    values = {'strategy': _strategy(key, get(key))}
    for field in BOUNDS:
        key = setting_key(target, field)
        values[field] = _number(key, get(key), field)
    if not any(values[field] for field in GFS_FIELDS):
        _warn_once(setting_key(target, 'gfs_*'), 0,
                   'Backup settings of %s keep no GFS backup at all: the defaults apply',
                   target)
        for field in GFS_FIELDS:
            values[field] = getattr(DEFAULTS, field)
    return Retention(**values)
