"""Import of a user's tracker data from an export ZIP (formats 1, 2 and 3), or from one
CSV file of an export.

The import reads and checks every row of every known file first; any invalid cell
refuses the whole import (``ImportRefused``, naming file, line and column) and nothing is
written. Then ``services.import_writer`` writes everything in one transaction, merging: a
row overwrites the caller's row for the same key or is added; rows the file does not name
stay. A file or column the ZIP lacks leaves that data untouched, so every format-1 export
still imports.

A ZIP holding ``format.csv`` (one row ``format, 3``) is format 3: every cell of every file
that starts with ``'`` followed by one of the export's escape triggers loses that first
``'`` before anything else reads it, which reverses the export's formula escape exactly.
A ZIP without the marker (formats 1 and 2) is read as written, nothing unescaped.

One CSV file uploaded alone (not a ZIP) is the export file its upload name says
(``import_reader.export_name``) and is read as that member of a format-3 ZIP: unescaped,
validated and written the same way; the user's other data is untouched.
"""
import re
from dataclasses import dataclass, field

from models.db import get_db
from models.item import CATEGORIES
from services.export_service import (CHECKLIST_FILE, FORMAT_FILE, GOALS_FILE, PROGRESS_FILE,
                                     SETTING_KEYS, SETTINGS_FILE, WATCHLIST_FILE)
# The bounds and ImportRefused are the route's interface to the import; they live in the
# reader and are re-exported here.
from services.import_reader import (MAX_REQUEST_BYTES, UPLOAD_TOO_LARGE,  # noqa: F401
                                    ImportRefused, csv_rows, read_format, read_upload, refuse,
                                    show)
from services.import_writer import write_plan

_INT64_MAX = 2 ** 63 - 1
_UINT = re.compile(r'[0-9]+')

_FLAGS = ['in_possession', 'mastered', 'subsumed', 'resurgence_watch']
_RANKS = ['owned_rank', 'target_rank']
_COMP_STATES = ('pending', 'blueprint', 'crafted')


@dataclass
class ImportResult:
    counts: dict = field(default_factory=dict)
    ignored_settings: int = 0

    @property
    def message(self) -> str:
        c = self.counts
        text = (f"Import complete: {c.get('items', 0)} items, {c.get('goals', 0)} goals, "
                f"{c.get('progress', 0)} goal progress rows, {c.get('checklist', 0)} "
                f"checklist rows, {c.get('watchlist', 0)} watchlist entries, "
                f"{c.get('settings', 0)} settings.")
        if self.ignored_settings:
            noun = 'key' if self.ignored_settings == 1 else 'keys'
            text += f' {self.ignored_settings} unknown setting {noun} ignored.'
        return text


class _Invalid(Exception):
    def __init__(self, column: str, reason: str):
        super().__init__(reason)
        self.column, self.reason = column, reason


def _flag(cell: str, column: str) -> int:
    s = cell.strip()
    if s == '':
        return 0
    if s in ('0', '1'):
        return int(s)
    raise _Invalid(column, f'{show(cell)} is not 0 or 1.')


def _uint(cell: str, column: str, empty):
    s = cell.strip()
    if s == '':
        return empty
    if not _UINT.fullmatch(s) or int(s) > _INT64_MAX:
        raise _Invalid(column, f'{show(cell)} is not a whole number from 0 to {_INT64_MAX}.')
    return int(s)


def _name(row: dict, column: str = 'name') -> str:
    s = row.get(column, '').strip()
    if not s:
        raise _Invalid(column, 'the cell is empty.')
    return s


def _category(row: dict, file_cat=None) -> str:
    """In a ``<category>.csv`` the cell is empty or the file's own category; in any other
    file it is exactly one of the app's categories."""
    s = row.get('category', '').strip()
    if file_cat is not None:
        if s in ('', file_cat):
            return file_cat
        raise _Invalid('category', f'{show(s)} is not {file_cat!r}, the file\'s category.')
    if s not in CATEGORIES:
        raise _Invalid('category', f'{show(s)} is not one of the app\'s categories.')
    return s


@dataclass
class _Plan:
    """Everything the import will write, validated; keys use (name, category)."""
    items: dict = field(default_factory=dict)       # (name, cat) -> {column: value}
    goals: dict = field(default_factory=dict)       # (name, cat) -> {column: value}
    progress: dict = field(default_factory=dict)    # (name, cat, type, key) -> {...}
    checklist: dict = field(default_factory=dict)   # (name, cat, component) -> {...}
    progress_lines: dict = field(default_factory=dict)  # progress key -> its line
    watchlist: dict = field(default_factory=dict)   # (name, cat) -> {}
    settings: dict = field(default_factory=dict)    # key -> {'value': int or None} or {}
    ignored_settings: int = 0


def _parse(texts: dict, bare: bool = False) -> _Plan:
    """``bare``: one CSV file of an export, read as a member of a format-3 ZIP."""
    plan = _Plan()
    texts = dict(texts)
    marked = FORMAT_FILE in texts
    if marked:
        read_format(texts.pop(FORMAT_FILE))
    for fname in sorted(texts):
        cols, rows = csv_rows(fname, texts[fname], unescape=marked or bare)
        target, seen = _target(plan, fname), {}
        for line, row in rows:
            try:
                key, values = _parse_row(plan, fname, cols, row)
            except _Invalid as bad:
                refuse(fname, line, bad.column, bad.reason)
            if key is None:
                continue
            if key in seen:
                key_col = 'key' if fname == SETTINGS_FILE else 'name'
                refuse(fname, line, key_col, f'the same row key already appears on line '
                                             f'{seen[key]}.')
            seen[key] = line
            target[key] = values
            if fname == PROGRESS_FILE:
                plan.progress_lines[key] = line
    return plan


def _target(plan: _Plan, fname: str) -> dict:
    return {GOALS_FILE: plan.goals, PROGRESS_FILE: plan.progress,
            CHECKLIST_FILE: plan.checklist, WATCHLIST_FILE: plan.watchlist,
            SETTINGS_FILE: plan.settings}.get(fname, plan.items)


def _parse_row(plan: _Plan, fname: str, cols: set, row: dict):
    """(key, values) for one row; values hold only the columns the file carries."""
    if fname == GOALS_FILE:
        return _parse_goal_row(cols, row)
    if fname == PROGRESS_FILE:
        return _parse_progress_row(cols, row)
    if fname == CHECKLIST_FILE:
        values = {}
        if 'acquired' in cols:
            values['acquired'] = _flag(row['acquired'], 'acquired')
        return (_name(row), _category(row), _name(row, 'component_name')), values
    if fname == SETTINGS_FILE:
        key = _name(row, 'key')
        if key not in SETTING_KEYS:
            plan.ignored_settings += 1
            return None, None
        return key, ({'value': _uint(row['value'], 'value', None)} if 'value' in cols else {})
    if fname == WATCHLIST_FILE:
        return (_name(row), _category(row)), {}
    file_cat = fname[:-len('.csv')]
    values = {}
    for col in _FLAGS:
        if col in cols:
            values[col] = _flag(row[col], col)
    if 'note' in cols:
        values['note'] = row['note'] or None
    for col in _RANKS:
        if col in cols:
            values[col] = _uint(row[col], col, None)
    return (_name(row), _category(row, file_cat)), values


def _parse_goal_row(cols: set, row: dict):
    values = {}
    if 'pinned' in cols:
        values['pinned'] = _flag(row['pinned'], 'pinned')
    if 'display_mode' in cols:
        mode = row['display_mode'].strip()
        if mode == '':
            mode = 'tree'
        if mode not in ('tree', 'list'):
            raise _Invalid('display_mode', f'{show(row["display_mode"])} is not tree or list.')
        values['display_mode'] = mode
    if 'sort_order' in cols:
        values['sort_order'] = _uint(row['sort_order'], 'sort_order', 0)
    return (_name(row), _category(row)), values


def _parse_progress_row(cols: set, row: dict):
    """A component row's ``comp_state`` is empty (pending), ``pending``, ``blueprint`` or
    ``crafted``, exact lower case; an ingredient row's is empty."""
    name, cat = _name(row), _category(row)
    row_type = row['row_type'].strip()
    if row_type not in ('component', 'ingredient'):
        raise _Invalid('row_type', f'{show(row["row_type"])} is not component or ingredient.')
    values = {}
    if 'acquired' in cols:
        values['acquired'] = _flag(row['acquired'], 'acquired')
    if 'quantity_owned' in cols:
        values['quantity_owned'] = _uint(row['quantity_owned'], 'quantity_owned', 0)
    if 'comp_state' in cols:
        state = row['comp_state'].strip()
        if row_type == 'ingredient':
            if state:
                raise _Invalid('comp_state', 'an ingredient row carries no state.')
        else:
            if state == '':
                state = 'pending'
            if state not in _COMP_STATES:
                raise _Invalid('comp_state',
                               f'{show(row["comp_state"])} is not pending, blueprint or crafted.')
            values['comp_state'] = state
    return (name, cat, row_type, _name(row, 'row_key')), values


def import_user_data(user_id: int, stream, filename=None) -> ImportResult:
    """Validate the whole upload, then write it for ``user_id`` in one transaction.
    ``filename`` is the upload's file name: it says which file a bare CSV is (``None``: the
    upload is a ZIP). Raises ``ImportRefused`` (nothing written) on any invalid input or
    failed write."""
    plan = _parse(*read_upload(stream, filename))
    db = get_db()
    _check_progress_goals(db, user_id, plan)
    try:
        counts = write_plan(db, user_id, plan)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise ImportRefused(f'Import failed, nothing written: the database refused a write '
                            f'({type(exc).__name__}).') from exc
    return ImportResult(counts=counts, ignored_settings=plan.ignored_settings)


def _check_progress_goals(db, user_id: int, plan: _Plan):
    """A progress row whose goal item has no goal, in the file or among the caller's goals,
    is invalid. Read only: runs before the first write."""
    for key, line in plan.progress_lines.items():
        name, cat = key[0], key[1]
        if (name, cat) in plan.goals:
            continue
        found = db.execute(
            '''SELECT 1 FROM farming_goals fg JOIN items i ON i.id = fg.item_id
                WHERE fg.user_id = ? AND i.name = ? AND i.category = ?''',
            (user_id, name, cat)).fetchone()
        if found is None:
            refuse(PROGRESS_FILE, line, 'name', f'{show(name)} ({cat}) has no farming goal '
                                                f'in the file or in your goals.')
