"""Reading an import upload: a ZIP or one CSV file of an export, the CSV files, the
format-3 marker and unescape.

Every refusal here raises ``ImportRefused`` naming the file (and, for a cell, the line and
the column); nothing is written by this module. ``services.import_service`` validates the
cells and plans the write.
"""
import csv
import io
import re
import zipfile
import zlib

from models.item import CATEGORIES
from services.export_service import (CHECKLIST_FILE, ESCAPE_TRIGGERS, FILE_COLUMNS,
                                     FORMAT_FILE, FORMAT_KEY, FORMAT_VERSION, GOALS_FILE,
                                     PROGRESS_FILE, SETTINGS_FILE, WATCHLIST_FILE)

# Bounds. A large export measured 1.04 MiB as CSV text and 31 KB zipped;
# both bounds leave about 30 times that as headroom.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
# The route caps the whole request at this, the file plus room for the form around it.
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
UPLOAD_TOO_LARGE = (f'Import refused, nothing written: the upload is larger than '
                    f'{MAX_UPLOAD_BYTES // (1024 * 1024)} MiB.')
_READ_CHUNK = 64 * 1024

# The columns a file must carry to name its rows at all.
_REQUIRED = {
    GOALS_FILE: ['name', 'category'],
    PROGRESS_FILE: ['name', 'category', 'row_type', 'row_key'],
    CHECKLIST_FILE: ['name', 'category', 'component_name'],
    WATCHLIST_FILE: ['name', 'category'],
    SETTINGS_FILE: ['key'],
    FORMAT_FILE: ['key', 'value'],
}


class ImportRefused(Exception):
    """The import wrote nothing; ``str(exc)`` is the message for the user."""


def _known_files() -> set:
    return {f'{cat}.csv' for cat in CATEGORIES} | set(FILE_COLUMNS) | {FORMAT_FILE}


# The first bytes of a ZIP: a local file header, or the end record of an empty archive.
_ZIP_SIGNATURES = (b'PK\x03\x04', b'PK\x05\x06')
# A browser's or file manager's duplicate name: "warframe (1).csv".
_COPY_SUFFIX = re.compile(r'(.+) \([0-9]+\)\.csv')


def read_upload(stream, filename=None) -> tuple:
    """(``{file name: text}``, ``bare``) for an upload. An upload starting with a ZIP
    signature, or named ``*.zip``, or with no name given (``None``), is read as a ZIP
    (``read_zip``). Any other upload is one CSV file of an export: its name says which
    file (``export_name``), and ``bare`` is True, so the caller reads it as that member of
    a format-3 ZIP (every cell unescaped)."""
    data = _read_bounded(stream)
    if (filename is None or data[:4] in _ZIP_SIGNATURES
            or filename.strip().lower().endswith('.zip')):
        return _read_zip_bytes(data), False
    fname = export_name(filename)
    return {fname: _decode(fname, data)}, True


def export_name(filename: str) -> str:
    """The export file a bare CSV upload is: its name's last path part (after a slash or
    a backslash), stripped of blanks, lower-cased, one copy suffix `` (N)`` removed.
    Refuses a name that is not one of the export's data files."""
    name = re.split(r'[/\\]', filename)[-1].strip().lower()
    copy = _COPY_SUFFIX.fullmatch(name)
    if copy:
        name = copy.group(1) + '.csv'
    accepted = sorted(_known_files() - {FORMAT_FILE})
    if name not in accepted:
        raise ImportRefused(
            f'Import refused, nothing written: {show(filename)} is not the name of a file '
            f'of an export. Upload the export ZIP, or one CSV file of it under its export '
            f'name: {", ".join(accepted)}.')
    return name


def read_zip(stream) -> dict:
    """{file name: text} for every known file; refuses a non-ZIP, a damaged or oversized
    ZIP, a repeated file and a file that is not UTF-8 (a UTF-8 BOM is accepted)."""
    return _read_zip_bytes(_read_bounded(stream))


def _read_bounded(stream) -> bytes:
    data = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportRefused(UPLOAD_TOO_LARGE)
    return data


def _decode(fname: str, raw: bytes) -> str:
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ImportRefused(f'Import refused, nothing written: {fname} is not UTF-8 text.')


def _read_zip_bytes(data: bytes) -> dict:
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
            texts[info.filename] = _decode(info.filename, raw)
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


def refuse(fname: str, line: int, column: str, reason: str):
    raise ImportRefused(f'Import refused, nothing written: {fname} line {line}, '
                        f'column {column}: {reason}')


def show(value: str) -> str:
    return repr(value if len(value) <= 40 else value[:40] + '…')


def unescape_cell(cell: str) -> str:
    """Reverse the export's formula escape: ``'`` followed by a trigger character loses the
    ``'``; any other cell (a lone ``'``, ``'x``) is returned as written."""
    if len(cell) > 1 and cell[0] == "'" and cell[1] in ESCAPE_TRIGGERS:
        return cell[1:]
    return cell


def csv_rows(fname: str, text: str, unescape: bool = False):
    """(header set, iterator of (line, {column: cell})); a row's line is where it starts.
    A short row reads its missing cells as empty; a long row and a blank row of cells
    are refused and skipped respectively. With ``unescape`` (a format-3 ZIP) every cell,
    header included, is unescaped before it is stripped or checked."""
    reader = csv.reader(io.StringIO(text, newline=''))
    try:
        header = next(reader, None)
    except csv.Error as exc:
        refuse(fname, 1, '-', f'not readable as CSV ({exc}).')
    if not header:
        refuse(fname, 1, '-', 'the file has no header row.')
    if unescape:
        header = [unescape_cell(h) for h in header]
    header = [h.strip() for h in header]
    seen = set()
    for h in header:
        if h and h in seen:  # blank header cells (a spreadsheet's trailing commas) are ignored
            refuse(fname, 1, h, 'the column appears twice in the header.')
        seen.add(h)
    for col in _REQUIRED.get(fname, ['name']):
        if col not in seen:
            refuse(fname, 1, col, 'the header lacks this column.')

    def body():
        prev = reader.line_num
        while True:
            try:
                cells = next(reader)
            except StopIteration:
                return
            except csv.Error as exc:
                refuse(fname, prev + 1, '-', f'not readable as CSV ({exc}).')
            line, prev = prev + 1, reader.line_num
            if unescape:
                cells = [unescape_cell(c) for c in cells]
            if not cells or all(not c.strip() for c in cells):
                continue
            if len(cells) > len(header):
                refuse(fname, line, f'#{len(header) + 1}',
                   'the row has more cells than the header.')
            cells = cells + [''] * (len(header) - len(cells))
            yield line, dict(zip(header, cells))
    return seen, body()


def read_format(text: str) -> None:
    """``format.csv`` holds exactly one row, ``format`` and ``3`` (blanks around stripped,
    as in every key cell); anything else refuses the import."""
    _, rows = csv_rows(FORMAT_FILE, text, unescape=True)
    first = None
    for line, row in rows:
        if first is not None:
            refuse(FORMAT_FILE, line, 'key', f'the file holds one row only (line {first}).')
        first = line
        if row['key'].strip() != FORMAT_KEY:
            refuse(FORMAT_FILE, line, 'key', f'{show(row["key"])} is not {FORMAT_KEY!r}.')
        if row['value'].strip() != str(FORMAT_VERSION):
            refuse(FORMAT_FILE, line, 'value', f'{show(row["value"])} is not '
                                               f'{FORMAT_VERSION}, the format this app reads.')
    if first is None:
        refuse(FORMAT_FILE, 2, 'key', 'the file has no format row.')
