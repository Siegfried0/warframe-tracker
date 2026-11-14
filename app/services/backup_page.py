"""The Settings page's backup section: what it shows, the save of one target's retention, and
the sentences that report a Back up now.

Targets are configured in the environment only; the page shows where each one writes (the
local folder; the S3 endpoint, bucket and prefix, never a key) and, for a configured target, a
form of its retention. A target counts as configured as the scheduler counts it: a local path
that is not blank, a bucket with both keys.

A save names one target and sends all six fields. Every field is checked first, by the same
checks the reader applies to stored values; one invalid field, a field (or the action) sent
twice, or a target that is missing, unknown or not configured saves nothing. A valid save writes
that target's six keys in one transaction (all or none) as normalised text, so the reader reads
back exactly what was saved. In MSSQL mode nothing is saved: the database's own backups apply.
"""
import logging

from services import backup_targets as targets
from services.backup_retention import GFS, INTERVAL_COUNT, STRATEGIES
from services.backup_service import (MSSQL_REASON, NONE_CONFIGURED, configured_targets,
                                     scheduled_targets)
from services.backup_settings import (BOUNDS, GFS_FIELDS, TARGETS, parse_number,
                                      parse_strategy, read_retention, setting_key)

log = logging.getLogger(__name__)

TARGET_NAMES = {'local': 'Local folder', 's3': 'S3 bucket'}
IN_TEXT = {'local': 'the local folder', 's3': 'the S3 bucket'}
FIELD_LABELS = {
    'strategy': 'Strategy',
    'min_days': 'Days between backups',
    'keep': 'Backups kept',
    'gfs_daily': 'Daily backups kept',
    'gfs_weekly': 'Weekly backups kept',
    'gfs_monthly': 'Monthly backups kept',
}
STRATEGY_LABELS = {INTERVAL_COUNT: 'Interval + count', GFS: 'Grandfather-father-son'}
ENV_VARS = {
    'local': ('BACKUP_LOCAL_PATH',),
    's3': ('BACKUP_S3_ENDPOINT', 'BACKUP_S3_BUCKET', 'BACKUP_S3_PREFIX',
           'BACKUP_S3_ACCESS_KEY', 'BACKUP_S3_SECRET_KEY'),
}
SAVE_FIELDS = ('target', 'strategy') + tuple(BOUNDS)
SINGLE_KEYS = {'action': 'Action', 'target': 'Target'}   # sent once, as the six fields are
WRITE_FAILED = 'the settings could not be written'
READ_FAILED = 'the settings could not be read'


# ── What the section shows ───────────────────────────────────────────────────

def endpoint_text(raw) -> str:
    """The S3 endpoint as shown: ``AWS default`` when unset; otherwise without its user
    information (``user:secret@``), query and fragment. Plain text work, so no spelling of the
    endpoint makes it fail."""
    text = (raw or '').strip()
    if not text:
        return 'AWS default'
    text = text.split('#', 1)[0].split('?', 1)[0]
    scheme, sep, rest = text.partition('://')
    if not sep:
        scheme, rest = '', text
    host, slash, path = rest.partition('/')
    host = host.rpartition('@')[2]
    return (scheme + sep) + host + slash + path


def _location(target, config) -> list:
    """(label, text) pairs of where the target writes, from the normalised settings."""
    if target == 'local':
        folder = targets.local_folder(config)
        return [('Folder', str(folder) if folder is not None else 'not set')]
    return [('Endpoint', endpoint_text(config.get('BACKUP_S3_ENDPOINT'))),
            ('Bucket', targets.s3_bucket(config) or 'not set'),
            ('Prefix', targets.s3_prefix(config) or '(the bucket root)')]


def _not_configured_reason(target, config):
    if target == 'local':
        return 'BACKUP_LOCAL_PATH is not set'
    if targets.s3_bucket(config) is None:
        return 'BACKUP_S3_BUCKET is not set'
    return targets.s3_keys_missing(config)


def backup_section(config, read=None) -> dict:
    """The section's content: one entry per target (local, then S3) and whether Back up now is
    offered. A configured target carries its retention as the reader reads it (a stored value
    that is not valid shows as the default the reader uses); a failed read shows no form."""
    read = read or read_retention
    mssql = config.get('DB_BACKEND') == 'mssql'
    usable = scheduled_targets(config)
    entries = []
    for target in TARGETS:
        entry = {'target': target, 'name': TARGET_NAMES[target],
                 'location': _location(target, config), 'env_vars': ENV_VARS[target],
                 'configured': target in usable, 'retention': None, 'error': None}
        if not entry['configured']:
            entry['reason'] = _not_configured_reason(target, config)
        elif not mssql:
            try:
                entry['retention'] = read(target)._asdict()
            except Exception:  # noqa: BLE001 (no form rather than invented values)
                log.exception('Backup settings of %s could not be read', target)
                entry['error'] = READ_FAILED
        entries.append(entry)
    return {'targets': entries, 'mssql': mssql, 'mssql_reason': MSSQL_REASON,
            'back_up_now': bool(configured_targets(config)),
            'none_configured': NONE_CONFIGURED, 'bounds': BOUNDS,
            'field_labels': FIELD_LABELS, 'strategy_labels': STRATEGY_LABELS}


# ── The save ─────────────────────────────────────────────────────────────────

def _range(field) -> str:
    low, high = BOUNDS[field]
    return f'{FIELD_LABELS[field]} must be a whole number from {low} to {high}'


def _refused(target, problems) -> tuple:
    where = f' of {IN_TEXT[target]}' if target in IN_TEXT else ''
    return False, f'Backup retention{where} not saved: ' + '; '.join(problems) + '.'


def check_retention_form(form, config) -> tuple:
    """(target, values, problems): the target named, the six values to store (text) and every
    reason the form cannot be saved. ``form`` is a multi-value form (``getlist``)."""
    problems = [f'{FIELD_LABELS.get(name) or SINGLE_KEYS[name]} was sent more than once'
                for name in ('action',) + SAVE_FIELDS if len(form.getlist(name)) > 1]
    target = form.get('target')
    if target not in TARGETS:
        problems.append('no known backup target was named (local or s3)')
    elif target not in scheduled_targets(config):
        problems.append(f'{IN_TEXT[target]} is not configured '
                        f'({_not_configured_reason(target, config)})')
    values = {}
    strategy = parse_strategy(form.get('strategy'))
    if strategy is None:
        problems.append('Strategy must be ' + ' or '.join(STRATEGIES))
    else:
        values['strategy'] = strategy
    for field in BOUNDS:
        number = parse_number(field, form.get(field))
        if number is None:
            problems.append(_range(field))
        else:
            values[field] = str(number)
    if all(field in values for field in GFS_FIELDS) \
            and not any(int(values[field]) for field in GFS_FIELDS):
        problems.append('at least one GFS count must be above 0')
    return target, values, problems


def save_retention(form, config, write=None) -> tuple:
    """Save one target's retention from the page's form. Returns (saved, message); nothing is
    written unless every field is valid, and then all six keys are written in one commit."""
    if config.get('DB_BACKEND') == 'mssql':
        return False, f'Backup retention not saved: {MSSQL_REASON}.'
    target, values, problems = check_retention_form(form, config)
    if problems:
        return _refused(target, problems)
    from models.db import get_db
    from services.auth_service import upsert_global_setting
    write = write or upsert_global_setting
    db = get_db()
    try:
        for field in ('strategy',) + tuple(BOUNDS):
            write(db, setting_key(target, field), values[field])
        db.commit()   # one commit: the six keys are saved together or not at all
    except Exception:  # noqa: BLE001 (logged; the page says so, never a 500)
        log.exception('Backup settings of %s could not be written', target)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            log.exception('Rolling back the backup settings of %s failed', target)
        return _refused(target, [WRITE_FAILED])
    return True, f'Backup retention of {IN_TEXT[target]} saved.'


# ── Back up now's result ─────────────────────────────────────────────────────

def _count(n, one, many) -> str:
    return f'{n} {one if n == 1 else many}'


def _sentence(name, result) -> tuple:
    if result.get('running'):
        return 'warning', f'{name}: skipped, {result.get("reason")}.'
    if result.get('ok') is False:
        return 'error', f'{name}: failed, {result.get("error") or result.get("reason")}.'
    if result.get('written'):
        text = f'{name}: backup {result["written"]} written'
        if result.get('retention_error'):
            text += f'; old backups were not cleaned up, {result["retention_error"]}'
            return 'warning', text + '.'
        text += f'; {_count(result.get("removed", 0), "old backup", "old backups")} removed'
        not_removed = result.get('not_removed', 0)
        if not_removed:
            text += f', {not_removed} could not be removed'
            return 'warning', text + '.'
        return 'success', text + '.'
    return 'info', f'{name}: skipped, {result.get("reason")}.'


def result_messages(result) -> list:
    """(flash category, sentence) per target of a Back up now's result, local first; one
    sentence when no target is configured."""
    if 'error' in result and not any(t in result for t in TARGETS):
        return [('error', f'{result["error"]}.')]
    return [_sentence(TARGET_NAMES[t], result[t]) for t in TARGETS if t in result]
