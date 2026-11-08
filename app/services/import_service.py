"""Import of a user's tracker data from an export ZIP (formats 1 and 2).

The import reads and checks every row of every known file first; any invalid cell
refuses the whole import (``ImportRefused``, naming file, line and column) and nothing is
written. Then ``services.import_writer`` writes everything in one transaction, merging: a
row overwrites the caller's row for the same key or is added; rows the file does not name
stay. A file or column the ZIP lacks leaves that data untouched, so every format-1 export
still imports.
"""
import csv
import io
import re
import zipfile
import zlib
from dataclasses import dataclass, field

from models.db import get_db
from models.item import CATEGORIES
from services.export_service import (CHECKLIST_FILE, FILE_COLUMNS, GOALS_FILE, PROGRESS_FILE,
                                     SETTING_KEYS, SETTINGS_FILE, WATCHLIST_FILE)
from services.import_writer import write_plan

# Bounds. A large export measured 1.04 MiB as CSV text and 31 KB zipped;
# both bounds leave about 30 times that as headroom.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
# The route caps the whole request at this, the file plus room for the form around it.
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
UPLOAD_TOO_LARGE = (f'Import refused, nothing written: the upload is larger than '
                    f'{MAX_UPLOAD_BYTES // (1024 * 1024)} MiB.')
_INT64_MAX = 2 ** 63 - 1
_UINT = re.compile(r'[0-9]+')
_READ_CHUNK = 64 * 1024

# The columns a file must carry to name its rows at all.
_REQUIRED = {
    GOALS_FILE: ['name', 'category'],
    PROGRESS_FILE: ['name', 'category', 'row_type', 'row_key'],
    CHECKLIST_FILE: ['name', 'category', 'component_name'],
    WATCHLIST_FILE: ['name', 'category'],
    SETTINGS_FILE: ['key'],
}
_FLAGS = ['in_possession', 'mastered', 'subsumed', 'resurgence_watch']
_RANKS = ['owned_rank', 'target_rank']
_COMP_STATES = ('pending', 'blueprint', 'crafted')


class ImportRefused(Exception):
    """The import wrote nothing; ``str(exc)`` is the message for the user."""


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


# ── Import: reading ─────────────────────────────────────────────────────────

def _known_files() -> set:
    return {f'{cat}.csv' for cat in CATEGORIES} | set(FILE_COLUMNS)


def _read_zip(stream) -> dict:
    """{file name: text} for every known file; refuses a non-ZIP, a damaged or oversized
    ZIP, a repeated file and a file that is not UTF-8 (a UTF-8 BOM is accepted)."""
    data = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportRefused(UPLOAD_TOO_LARGE)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError):
        raise ImportRefused('Import refused, nothing written: the file is not a ZIP archive.')
    known = _known_files()
    texts, budget = {}, MAX_UNCOMPRESSED_BYTES
    with zf:
        for info in zf.infolist():
            if info.is_dir() or info.filename not in known:
                continue
            if info.filename in texts:
                raise ImportRefused(f'Import refused, nothing written: {info.filename} '
                                    f'appears twice in the ZIP.')
            raw = _read_member(zf, info, budget)
            budget -= len(raw)
            try:
                texts[info.filename] = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                raise ImportRefused(f'Import refused, nothing written: {info.filename} is '
                                    f'not UTF-8 text.')
    return texts


def _read_member(zf, info, budget: int) -> bytes:
    """The member's bytes, read in chunks so a ZIP bomb stops at the bound."""
    parts, size = [], 0
    try:
        with zf.open(info) as fh:
            while True:
                chunk = fh.read(_READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > budget:
                    raise ImportRefused(
                        f'Import refused, nothing written: the ZIP unpacks to more than '
                        f'{MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MiB.')
                parts.append(chunk)
    except ImportRefused:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError,
            NotImplementedError, ValueError):
        raise ImportRefused(f'Import refused, nothing written: {info.filename} in the ZIP is '
                            f'damaged or cannot be read.')
    return b''.join(parts)


class _Invalid(Exception):
    def __init__(self, column: str, reason: str):
        super().__init__(reason)
        self.column, self.reason = column, reason


def _refuse(fname: str, line: int, column: str, reason: str):
    raise ImportRefused(f'Import refused, nothing written: {fname} line {line}, '
                        f'column {column}: {reason}')


def _show(value: str) -> str:
    return repr(value if len(value) <= 40 else value[:40] + '…')


def _rows(fname: str, text: str):
    """(header set, iterator of (line, {column: cell})); a row's line is where it starts.
    A short row reads its missing cells as empty; a long row and a blank row of cells
    are refused and skipped respectively."""
    reader = csv.reader(io.StringIO(text, newline=''))
    try:
        header = next(reader, None)
    except csv.Error as exc:
        _refuse(fname, 1, '-', f'not readable as CSV ({exc}).')
    if not header:
        _refuse(fname, 1, '-', 'the file has no header row.')
    header = [h.strip() for h in header]
    seen = set()
    for h in header:
        if h and h in seen:  # blank header cells (a spreadsheet's trailing commas) are ignored
            _refuse(fname, 1, h, 'the column appears twice in the header.')
        seen.add(h)
    for col in _REQUIRED.get(fname, ['name']):
        if col not in seen:
            _refuse(fname, 1, col, 'the header lacks this column.')

    def body():
        prev = reader.line_num
        while True:
            try:
                cells = next(reader)
            except StopIteration:
                return
            except csv.Error as exc:
                _refuse(fname, prev + 1, '-', f'not readable as CSV ({exc}).')
            line, prev = prev + 1, reader.line_num
            if not cells or all(not c.strip() for c in cells):
                continue
            if len(cells) > len(header):
                _refuse(fname, line, f'#{len(header) + 1}',
                        'the row has more cells than the header.')
            cells = cells + [''] * (len(header) - len(cells))
            yield line, dict(zip(header, cells))
    return seen, body()


def _flag(cell: str, column: str) -> int:
    s = cell.strip()
    if s == '':
        return 0
    if s in ('0', '1'):
        return int(s)
    raise _Invalid(column, f'{_show(cell)} is not 0 or 1.')


def _uint(cell: str, column: str, empty):
    s = cell.strip()
    if s == '':
        return empty
    if not _UINT.fullmatch(s) or int(s) > _INT64_MAX:
        raise _Invalid(column, f'{_show(cell)} is not a whole number from 0 to {_INT64_MAX}.')
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
        raise _Invalid('category', f'{_show(s)} is not {file_cat!r}, the file\'s category.')
    if s not in CATEGORIES:
        raise _Invalid('category', f'{_show(s)} is not one of the app\'s categories.')
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


def _parse(texts: dict) -> _Plan:
    plan = _Plan()
    for fname in sorted(texts):
        cols, rows = _rows(fname, texts[fname])
        target, seen = _target(plan, fname), {}
        for line, row in rows:
            try:
                key, values = _parse_row(plan, fname, cols, row)
            except _Invalid as bad:
                _refuse(fname, line, bad.column, bad.reason)
            if key is None:
                continue
            if key in seen:
                key_col = 'key' if fname == SETTINGS_FILE else 'name'
                _refuse(fname, line, key_col, f'the same row key already appears on line '
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
            raise _Invalid('display_mode', f'{_show(row["display_mode"])} is not tree or list.')
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
        raise _Invalid('row_type', f'{_show(row["row_type"])} is not component or ingredient.')
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
                               f'{_show(row["comp_state"])} is not pending, blueprint or crafted.')
            values['comp_state'] = state
    return (name, cat, row_type, _name(row, 'row_key')), values


def import_user_data(user_id: int, stream) -> ImportResult:
    """Validate the whole upload, then write it for ``user_id`` in one transaction.
    Raises ``ImportRefused`` (nothing written) on any invalid input or failed write."""
    plan = _parse(_read_zip(stream))
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
            _refuse(PROGRESS_FILE, line, 'name', f'{_show(name)} ({cat}) has no farming goal '
                                                 f'in the file or in your goals.')
