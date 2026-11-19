"""The Settings page's runtime section (admins only): auto-sync on startup, the scheduled sync
interval and the session lifetime, each with its value and its source, and their one save.

What the section shows: per setting the value that applies and where it comes from (saved
here, the default, or set by its environment variable: shown read-only with the sentence that
says where to change it). A stored value that is not valid is shown as the default it is read
as, with a note; it stays stored until a save. When the settings cannot be read the section
shows no form, since a form of invented values would overwrite what is stored on its next save;
the pinned settings still show.

The save (``action=save_runtime_settings``) carries every editable field of the section and,
per field, a "use the default" box ``<field>_default``:

- the switch is a checkbox: absent is off, exactly ``on`` is on, any other text is refused;
- a number is checked by the same spelling check the reader applies to stored values and is
  stored normalised (``0024`` as ``24``); a number field missing from the form is refused;
- a "use the default" box is exactly ``on`` or absent; it removes the stored row. A value sent
  with it is refused: for the switch a ticked box, for a number a text that is not empty;
- a field (or a box, or the action) sent more than once is refused, and so is a pinned
  setting's field or box: it is set by its environment variable.

Every field is checked first; one problem saves nothing and the message names every refused
field. A valid save reads the interval before it and writes every row in one commit on the same
connection, or nothing when the read or a write fails. A save that wrote or removed the interval
and leaves it above 0 adds the scheduled check in this process when it has none; when the
interval was 0 before, the page says that other processes start their check at their next start.
"""
import logging

from services import runtime_settings as rs

log = logging.getLogger(__name__)

ACTION = 'save_runtime_settings'
DEFAULT_SUFFIX = '_default'
BOX_ON = 'on'
WRITE_FAILED = 'the settings could not be written'
READ_FAILED = 'the settings could not be read'
NOT_SAVED = 'Runtime settings not saved: '

PIN_SENTENCE = 'Set by the environment variable {env}; remove it there to set it here.'
APPLIES = {
    rs.AUTO_SYNC: 'Applies from the next start of the app.',
    rs.INTERVAL: 'Applies from the next scheduled check (within 15 minutes) in every worker '
                 'process; 0 switches the scheduled sync off.',
    rs.LIFETIME: 'Applies from the next request (within a minute) to every session: a session '
                 'unused for longer ends, and every sign-in ends three lifetimes after it was '
                 'made.',
}
SAVED = 'Runtime settings saved.'
AT_NEXT_START = 'Sync on startup applies from the next start of the app.'
SWITCHED_ON = ('The scheduled sync is on: this process checks shortly (in about 2 minutes); '
               'other worker processes start their check at their next start.')
NO_SCHEDULER = ('The scheduled sync does not run in this process: its scheduler could not be '
                'started (APScheduler is not installed, or it failed; see the log). Other '
                'worker processes start their check at their next start.')


def default_box(key: str) -> str:
    """The name of a setting's "use the default" box."""
    return key + DEFAULT_SUFFIX


def _value_text(setting, value) -> str:
    if setting.kind == 'switch':
        return 'on' if value else 'off'
    return f'{value} {setting.unit}'


# ── What the section shows ───────────────────────────────────────────────────

def _entry(app, key, get) -> dict:
    """One setting as shown; reading the stored row may raise."""
    setting = rs.SETTINGS[key]
    entry = {'key': key, 'setting': setting, 'box': default_box(key), 'invalid_stored': False,
             'stored': False}
    if rs.pinned(app, key):
        entry.update(value=rs.effective(app, key), source='pinned',
                     pin_sentence=PIN_SENTENCE.format(env=setting.env))
        return entry
    value, raw = rs.read_stored(key, get)
    entry['stored'] = raw is not None
    if value is None:
        entry.update(value=setting.default, source='default',
                     invalid_stored=raw is not None)
    else:
        entry.update(value=value, source='saved')
    return entry


def runtime_section(app, get=None) -> dict:
    """The section's content: one entry per setting, in the page's order, and whether the form
    is shown (some setting is not pinned and the settings could be read)."""
    try:
        entries = [_entry(app, key, get) for key in rs.ORDER]
        error = None
    except Exception:  # noqa: BLE001 (no form rather than invented values)
        log.exception('The runtime settings could not be read for the Settings page')
        entries = [_entry(app, key, get) for key in rs.ORDER if rs.pinned(app, key)]
        error = READ_FAILED
    for entry in entries:
        setting = entry['setting']
        entry['text'] = _value_text(setting, entry['value'])
        entry['default_text'] = _value_text(setting, setting.default)
        entry['applies'] = APPLIES[entry['key']]
    editable = error is None and any(e['source'] != 'pinned' for e in entries)
    return {'entries': entries, 'error': error, 'editable': editable, 'action': ACTION}


# ── The save ─────────────────────────────────────────────────────────────────

def _range(setting) -> str:
    return (f'{setting.label} must be a whole number from {setting.low} to {setting.high} '
            f'{setting.unit}')


def check_runtime_form(form, app) -> tuple:
    """(writes, deletes, problems): the stored text to write by key, the keys whose rows are
    removed, and every reason the form cannot be saved. ``form`` is a multi-value form."""
    problems = []
    if len(form.getlist('action')) > 1:
        problems.append('Action was sent more than once')
    writes, deletes = {}, []
    for key in rs.ORDER:
        setting = rs.SETTINGS[key]
        box = default_box(key)
        values, boxes = form.getlist(key), form.getlist(box)
        if len(values) > 1:
            problems.append(f'{setting.label} was sent more than once')
        if len(boxes) > 1:
            problems.append(f'"Use the default" of {setting.label} was sent more than once')
        if len(values) > 1 or len(boxes) > 1:
            continue
        raw = values[0] if values else None
        if rs.pinned(app, key):
            if values or boxes:
                problems.append(f'{setting.label} is set by the environment variable '
                                f'{setting.env} and cannot be saved here')
            continue
        reset = False
        if boxes:
            if boxes[0] != BOX_ON:
                problems.append(f'"Use the default" of {setting.label} must be ticked or not '
                                f'ticked')
                continue
            reset = True
        if setting.kind == 'switch':
            if raw is not None and raw != BOX_ON:
                problems.append(f'{setting.label} must be ticked or not ticked')
            elif reset and raw is not None:
                problems.append(f'{setting.label}: a value and "use the default" were both '
                                f'sent')
            elif reset:
                deletes.append(key)
            else:
                writes[key] = rs.stored_text(setting, raw == BOX_ON)
            continue
        if raw is None:
            problems.append(f'{setting.label} was not sent')
        elif reset and raw.strip():
            problems.append(f'{setting.label}: a value and "use the default" were both sent')
        elif reset:
            deletes.append(key)
        else:
            value = rs.parse_value(setting, raw)
            if value is None:
                problems.append(_range(setting))
            else:
                writes[key] = rs.stored_text(setting, value)
    if not problems and not writes and not deletes:
        problems.append('nothing to save: every setting is set by its environment variable')
    return writes, deletes, problems


def _interval_after(writes, deletes):
    """The effective interval a save leaves when it wrote or removed the interval's row
    (the interval is then not pinned), else None."""
    if rs.INTERVAL in writes:
        return int(writes[rs.INTERVAL])
    if rs.INTERVAL in deletes:
        return rs.SETTINGS[rs.INTERVAL].default
    return None


def save_runtime(form, app, user_id, write=None, delete=None, ensure=None) -> list:
    """Save the section's form. Returns the (flash category, sentence) list to show. Nothing is
    written unless every field is valid; then the interval before the save is read and every
    row written on one connection, in one commit, or nothing when any of it fails."""
    writes, deletes, problems = check_runtime_form(form, app)
    if problems:
        return [('error', NOT_SAVED + '; '.join(problems) + '.')]
    from models.db import get_db
    from services.auth_service import delete_global_setting, upsert_global_setting
    write = write or upsert_global_setting
    delete = delete or delete_global_setting
    db = get_db()

    def read_here(key):
        row = db.execute('SELECT value FROM settings WHERE user_id IS NULL AND key = ?',
                         (key,)).fetchone()
        return row['value'] if row else None

    try:
        before = rs.effective(app, rs.INTERVAL, read_here)
        for key, text in writes.items():
            write(db, key, text)
        for key in deletes:
            delete(db, key)
        db.commit()   # one commit: the section's rows are saved together or not at all
    except Exception:  # noqa: BLE001 (logged; the page says so, never a 500)
        log.exception('The runtime settings could not be written')
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            log.exception('Rolling back the runtime settings failed')
        return [('error', NOT_SAVED + WRITE_FAILED + '.')]
    rs.forget_cached_lifetime(app)
    log.info('Runtime settings saved by user %s: %s; set to the default: %s', user_id,
             writes, deletes)
    messages = [('success', SAVED)]
    if rs.AUTO_SYNC in writes or rs.AUTO_SYNC in deletes:
        messages.append(('info', AT_NEXT_START))
    after = _interval_after(writes, deletes)
    if after is not None and after > 0:
        if ensure is None:
            from services.sync_schedule import ensure_check_job as ensure
        try:
            outcome = ensure(app)
        except Exception:  # noqa: BLE001 (the rows are saved; the page says the check is off)
            log.exception('The scheduled check could not be added after a save')
            outcome = 'unavailable'
        if outcome == 'unavailable':
            messages.append(('warning', NO_SCHEDULER))
        elif before == 0:
            messages.append(('info', SWITCHED_ON))
    return messages
