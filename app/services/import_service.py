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

_FLAGS = ['in_possession', 'mastered', 'subsumed', 'resurgence_watch']
_RANKS = ['owned_rank', 'target_rank']


class ImportRefused(Exception):
    """The import wrote nothing; ``str(exc)`` is the message for the user."""


@dataclass
class ImportResult:
    counts: dict = field(default_factory=dict)

    @property
    def message(self) -> str:
        return f"Import complete: {self.counts.get('items', 0)} items."


# ── Import: reading ─────────────────────────────────────────────────────────

def _known_files() -> set:
    return {f'{cat}.csv' for cat in CATEGORIES}


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
    if 'name' not in seen:
        _refuse(fname, 1, 'name', 'the header lacks this column.')

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


def _name(row: dict) -> str:
    s = row.get('name', '').strip()
    if not s:
        raise _Invalid('name', 'the cell is empty.')
    return s


def _category(row: dict, file_cat: str) -> str:
    s = row.get('category', '').strip()
    if s in ('', file_cat):
        return file_cat
    raise _Invalid('category', f'{_show(s)} is not {file_cat!r}, the file\'s category.')


@dataclass
class _Plan:
    """Everything the import will write, validated; keys use (name, category)."""
    items: dict = field(default_factory=dict)       # (name, cat) -> {column: value}


def _parse(texts: dict) -> _Plan:
    plan = _Plan()
    for fname in sorted(texts):
        cols, rows = _rows(fname, texts[fname])
        seen = {}
        for line, row in rows:
            try:
                key, values = _parse_row(fname, cols, row)
            except _Invalid as bad:
                _refuse(fname, line, bad.column, bad.reason)
            if key in seen:
                _refuse(fname, line, 'name', f'the same row key already appears on line '
                                             f'{seen[key]}.')
            seen[key] = line
            plan.items[key] = values
    return plan


def _parse_row(fname: str, cols: set, row: dict):
    """(key, values) for one row; values hold only the columns the file carries."""
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


def import_user_data(user_id: int, stream) -> ImportResult:
    """Validate the whole upload, then write it for ``user_id`` in one transaction.
    Raises ``ImportRefused`` (nothing written) on any invalid input or failed write."""
    plan = _parse(_read_zip(stream))
    db = get_db()
    try:
        counts = write_plan(db, user_id, plan)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise ImportRefused(f'Import failed, nothing written: the database refused a write '
                            f'({type(exc).__name__}).') from exc
    return ImportResult(counts=counts)
