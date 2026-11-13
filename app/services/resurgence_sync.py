"""Prime Resurgence: the current stock, read from the wiki once per sync, and its freshness.

Source: the official wiki's "Prime Resurgence" page, read as wikitext through the MediaWiki
API the catalogue sync already calls. The page lists every rotation as one row of a table
inside the section named ``primeresurgenceoffer``; the active row's status cell holds the
checked box, every other row the crossed one. A row holds a start-date and an end-date table
(newest date first), the packs (tables marked ``data-tableid="CollectedPrimes"``, one entry
per ``data-rowid`` row, the item name in its first cell) and a relic table.

The parser accepts only that shape: anything else is an unreadable page, which changes
nothing (the last stock and its end time stay) and is reported in the sync result; it never
takes a partial stock. A rotation changes at 18:00 UTC (the page says so), so the end date is
read as that time; a page without a readable end date makes the stock expire a configured
number of days after the read (``RESURGENCE_FALLBACK_DAYS``).

Sync writes catalogue rows only: ``items.resurgence_available`` and two app-wide settings.
Freshness (``resurgence_status``) decides whether any of it is shown: once the end time has
passed, or when no stock was ever read, nothing counts as available.
"""
import html
import logging
import re
from datetime import date, datetime, time, timedelta, timezone

from flask import current_app

from models.db import get_db
from services import wiki_sync_requests
from services.auth_service import get_global_setting, upsert_global_setting

log = logging.getLogger(__name__)

PAGE_TITLE = 'Prime Resurgence'

# App-wide settings (``settings`` rows with user_id NULL); values are UTC times written
# as ``YYYY-MM-DDTHH:MM:SSZ``.
SETTING_ENDS_AT = 'resurgence_ends_at'   # when the stored stock stops counting
SETTING_READ_AT = 'resurgence_read_at'   # when the stock was last read from the page

ROTATION_CHANGE_UTC = time(18, 0)        # the page: "changes at 18:00 UTC"
DEFAULT_FALLBACK_DAYS = 7

NOTICE_MISSING = ('No Prime Resurgence data has been read yet, so Prime Resurgence '
                  'availability is not shown. The next sync reads it.')
NOTICE_EXPIRED = ('The Prime Resurgence data is out of date (the rotation it lists has '
                  'ended), so Prime Resurgence availability is not shown. The next sync '
                  'reads the current rotation.')

VAULTED_NOTE = 'Vaulted: obtainable by trade or through Prime Resurgence'
IN_RESURGENCE_NOTE = 'in Prime Resurgence now'

_TIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
_SECTION = re.compile(
    r'<section\s+begin\s*=\s*"?primeresurgenceoffer"?\s*/>(.*?)'
    r'<section\s+end\s*=\s*"?primeresurgenceoffer"?\s*/>',
    re.S | re.I,
)
_ROW_SEPARATOR = re.compile(r'\|-(?!\|)[^|]*')     # "|-" with attributes; not a tabber "|-|"
_DATE = re.compile(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)')
_TEMPLATE = re.compile(r'\{\{([^{}]*)\}\}')
_LINK = re.compile(r'\[\[([^\[\]]*)\]\]')
_TAG = re.compile(r'<[^<>]*>')
_COUNT_SUFFIX = re.compile(r'\s*\(\d+x\)\s*$', re.I)
_ACTIVE, _INACTIVE = '☑', '☒'        # the checked and the crossed ballot box


class PageUnreadable(Exception):
    """The page does not have the shape the parser expects; nothing is written."""


# ── Time values ──────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def parse_utc(value) -> datetime | None:
    """A stored time value as an aware UTC datetime, or None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), _TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Parsing ──────────────────────────────────────────────────────────────────

def _split_rows(table: str) -> list[list[tuple[int, str]]]:
    """The rows of a wikitext table, each a list of (depth, stripped line).

    Depth 1 is the table itself; a nested table's lines are deeper. Rows are split only at
    depth-1 row separators, so nested tables and tabber headers stay inside their row.
    Raises PageUnreadable when the text is not one balanced table.
    """
    rows: list[list[tuple[int, str]]] = [[]]
    depth = 0
    seen_table = False
    for raw in table.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('{|'):
            if depth == 0:
                if seen_table:
                    raise PageUnreadable('more than one table in the stock section')
                seen_table = True
            depth += 1
            if depth == 1:
                continue
        elif depth == 0:
            raise PageUnreadable('text outside the stock table')
        elif line.startswith('|}'):
            depth -= 1
            if depth == 0:
                continue
        elif depth == 1 and _ROW_SEPARATOR.fullmatch(line):
            rows.append([])
            continue
        rows[-1].append((depth, line))
    if not seen_table or depth != 0:
        raise PageUnreadable('the stock table is missing or not closed')
    return rows


def _nested_tables(row: list[tuple[int, str]]) -> list[list[str]]:
    """The tables nested directly in a row, in order, each as its lines (opening line first)."""
    tables: list[list[str]] = []
    depth = 1
    for _, line in row:
        if line.startswith('{|'):
            depth += 1
            if depth == 2:
                tables.append([])
        if depth >= 2:
            tables[-1].append(line)
        if line.startswith('|}'):
            depth -= 1
    return tables


def _row_status(row: list[tuple[int, str]]) -> str | None:
    """'active', 'inactive' or None (a row without a status cell: the header)."""
    cells = [line for depth, line in row
             if depth == 1 and line.startswith('|') and not line.startswith(('|-', '|}'))]
    if not cells:
        return None
    last = cells[-1]
    has_active, has_inactive = _ACTIVE in last, _INACTIVE in last
    if has_active == has_inactive:
        raise PageUnreadable('a rotation row without exactly one status mark')
    return 'active' if has_active else 'inactive'


def _first_date(table: list[str]) -> date | None:
    for line in table:
        found = _DATE.search(line)
        if found:
            try:
                return date(*(int(g) for g in found.groups()))
            except ValueError:
                return None
    return None


def entry_names(cell: str) -> list[str]:
    """The names one stock entry's name cell can mean, best first.

    ``{{WF|Ash Prime}}`` gives the template's first argument; ``[[Ash Prime|Ash]]`` the link
    target and then its label (a target with a section anchor is a general page, so only its
    label counts); HTML tags and entities are removed and a count such as "(2x)" dropped.
    """
    text = html.unescape(cell)
    candidates: list[str] = []
    for template in _TEMPLATE.findall(text):
        parts = template.split('|')
        if len(parts) > 1 and '=' not in parts[1]:
            candidates.append(parts[1])
    for link in _LINK.findall(text):
        target, _, label = link.partition('|')
        if '#' not in target:
            candidates.append(target)
        if label:
            candidates.append(label)
    if not candidates:
        candidates.append(text)
    names = []
    for candidate in candidates:
        name = _COUNT_SUFFIX.sub('', _TAG.sub('', candidate))
        name = ' '.join(name.split())
        if name and name not in names:
            names.append(name)
    return names


def parse_stock(wikitext: str) -> tuple[list[list[str]], date | None]:
    """The active rotation's stock entries (each a list of candidate names) and its end date.

    Raises PageUnreadable unless the page has the stock section with exactly one active
    rotation holding at least one pack entry. The end date is None when the active row has
    no readable end-date table.
    """
    if not isinstance(wikitext, str):
        raise PageUnreadable('the page has no text')
    section = _SECTION.search(wikitext)
    if not section:
        raise PageUnreadable('the page has no stock section')
    rows = _split_rows(section.group(1))
    active = []
    for row in rows:
        if _row_status(row) == 'active':
            active.append(row)
    if len(active) != 1:
        raise PageUnreadable(f'{len(active)} active rotations instead of one')
    tables = _nested_tables(active[0])
    date_tables = [t for t in tables if 'data-tableid' not in t[0]]
    end_date = _first_date(date_tables[1]) if len(date_tables) >= 2 else None

    entries: list[list[str]] = []
    for table in tables:
        if 'data-tableid="CollectedPrimes"' not in table[0].replace("'", '"'):
            continue
        for i, line in enumerate(table):
            if not (line.startswith('|-') and 'data-rowid' in line):
                continue
            cell = table[i + 1] if i + 1 < len(table) else ''
            if not cell.startswith('|') or cell.startswith('|-') or cell.startswith('|}'):
                raise PageUnreadable('a stock entry without a name cell')
            names = entry_names(cell[1:])
            if names and names not in entries:
                entries.append(names)
    if not entries:
        raise PageUnreadable('the active rotation lists no stock')
    return entries, end_date


# ── Matching and writing ─────────────────────────────────────────────────────

def _key(name: str) -> str:
    return ' '.join(name.split()).casefold()


def match_entries(db, entries: list[list[str]]) -> tuple[set[int], list[str]]:
    """The catalogue item ids the entries name, and the entries that name none.

    A name matches an item whose name is the same apart from case and spacing; a name
    without "Prime" at its end also tries the name with " Prime" added (everything
    Prime Resurgence sells is a Prime). An entry takes its first candidate that matches.
    """
    catalogue: dict[str, list[int]] = {}
    for row in db.execute('SELECT id, name FROM items').fetchall():
        catalogue.setdefault(_key(row['name']), []).append(row['id'])
    ids: set[int] = set()
    unmatched: list[str] = []
    for names in entries:
        hit = None
        for name in names:
            key = _key(name)
            hit = catalogue.get(key)
            if not hit and not key.endswith('prime'):
                hit = catalogue.get(key + ' prime')
            if hit:
                break
        if hit:
            ids.update(hit)
        else:
            unmatched.append(names[0])
    return ids, unmatched


def _fallback_days() -> int:
    days = current_app.config.get('RESURGENCE_FALLBACK_DAYS', DEFAULT_FALLBACK_DAYS)
    try:
        days = int(days)
    except (TypeError, ValueError):
        return DEFAULT_FALLBACK_DAYS
    return days if days > 0 else DEFAULT_FALLBACK_DAYS


def _fetch_wikitext() -> str:
    return wiki_sync_requests._fetch_wiki_module(PAGE_TITLE)  # the sync's MediaWiki page fetch


def sync_resurgence(now: datetime | None = None) -> dict:
    """Read the page and store the active rotation's stock; never raises.

    Success sets ``resurgence_available`` for the matched items and clears it for every
    other item, and stores the end time and the read time, in one transaction. A failed
    fetch, an unreadable page or a failed write changes nothing and returns
    ``{'ok': False, 'error': ...}``.
    """
    now = now or _utc_now()
    try:
        entries, end_date = parse_stock(_fetch_wikitext())
    except PageUnreadable as exc:
        log.warning('Prime Resurgence page unreadable: %s', exc)
        return {'ok': False, 'error': f'page unreadable: {exc}'}
    except Exception as exc:  # the fetch: timeout, HTTP error, invalid JSON, missing page
        log.warning('Prime Resurgence fetch failed: %s', exc)
        return {'ok': False, 'error': f'fetch failed: {exc}'}

    if end_date is not None:
        ends_at = datetime.combine(end_date, ROTATION_CHANGE_UTC, tzinfo=timezone.utc)
    else:
        ends_at = now + timedelta(days=_fallback_days())

    db = get_db()
    try:
        ids, unmatched = match_entries(db, entries)
        db.execute('UPDATE items SET resurgence_available = 0 WHERE resurgence_available <> 0')
        id_list = sorted(ids)
        for start in range(0, len(id_list), 500):
            chunk = id_list[start:start + 500]
            placeholders = ','.join('?' for _ in chunk)
            db.execute(
                f'UPDATE items SET resurgence_available = 1 WHERE id IN ({placeholders})',
                chunk,
            )
        upsert_global_setting(db, SETTING_ENDS_AT, format_utc(ends_at))
        upsert_global_setting(db, SETTING_READ_AT, format_utc(now))
        db.commit()
    except Exception as exc:
        db.rollback()
        log.warning('Prime Resurgence write failed: %s', exc)
        return {'ok': False, 'error': f'write failed: {exc}'}
    return {
        'ok': True,
        'available': len(ids),
        'entries': len(entries),
        'unmatched': unmatched,
        'ends_at': format_utc(ends_at),
        'end_from_page': end_date is not None,
    }


# ── Freshness ────────────────────────────────────────────────────────────────

def resurgence_status(now: datetime | None = None) -> dict:
    """Whether the stored stock counts: ``current`` is True only before its end time.

    ``state`` is 'current', 'expired' or 'missing' (never read, or a stored end time that
    does not parse); ``notice`` is the text to show instead of availability, None when
    current.
    """
    now = now or _utc_now()
    ends_at = parse_utc(get_global_setting(SETTING_ENDS_AT))
    if ends_at is None:
        state, notice = 'missing', NOTICE_MISSING
    elif now >= ends_at:
        state, notice = 'expired', NOTICE_EXPIRED
    else:
        state, notice = 'current', None
    return {'current': state == 'current', 'state': state, 'notice': notice,
            'ends_at': format_utc(ends_at) if ends_at else None}


def vaulted_note(in_resurgence_now: bool) -> str:
    """The planner's tooltip for a vaulted component."""
    return f'{VAULTED_NOTE} ({IN_RESURGENCE_NOTE})' if in_resurgence_now else VAULTED_NOTE
