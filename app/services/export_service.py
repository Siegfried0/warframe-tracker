"""Export of one user's tracker data as a ZIP of CSV files (format 2).

The export writes the caller's rows only: one ``<category>.csv`` per category (progress
flags, note, arcane ranks) plus ``farming_goals.csv``, ``farming_progress.csv``,
``farming_checklist.csv``, ``watchlist.csv`` and ``settings.csv``. Items are named by
``(name, category)``, never by id. The format's file and column lists live here; the
import (``services.import_service``) reads the same lists.
"""
import csv
import io
import zipfile

from models.db import get_db
from models.item import CATEGORIES
from models.progress import Progress
from services.auth_service import USER_SETTINGS

CATEGORY_COLUMNS = ['name', 'category', 'source', 'in_possession', 'mastered', 'subsumed',
                    'resurgence_watch', 'note', 'owned_rank', 'target_rank']
GOALS_FILE = 'farming_goals.csv'
PROGRESS_FILE = 'farming_progress.csv'
CHECKLIST_FILE = 'farming_checklist.csv'
WATCHLIST_FILE = 'watchlist.csv'
SETTINGS_FILE = 'settings.csv'
FILE_COLUMNS = {
    GOALS_FILE: ['name', 'category', 'pinned', 'display_mode', 'sort_order'],
    PROGRESS_FILE: ['name', 'category', 'row_type', 'row_key', 'acquired', 'quantity_owned',
                    'comp_state'],
    CHECKLIST_FILE: ['name', 'category', 'component_name', 'acquired'],
    WATCHLIST_FILE: ['name', 'category'],
    SETTINGS_FILE: ['key', 'value'],
}
SETTING_KEYS = [key for key, _, _ in USER_SETTINGS]


# ── Export ──────────────────────────────────────────────────────────────────

def export_user_data(user_id: int) -> bytes:
    """The caller's data as a format-2 ZIP."""
    db = get_db()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for cat in CATEGORIES:
            rows = Progress.get_all_for_user(user_id, category=cat)
            zf.writestr(f'{cat}.csv', _csv(CATEGORY_COLUMNS,
                                           ([r.get(c) for c in CATEGORY_COLUMNS] for r in rows)))
        goals = db.execute(
            '''SELECT i.name, i.category, fg.pinned, fg.display_mode, fg.sort_order
                 FROM farming_goals fg JOIN items i ON i.id = fg.item_id
                WHERE fg.user_id = ?
                ORDER BY i.category, i.name''', (user_id,)).fetchall()
        zf.writestr(GOALS_FILE, _csv(FILE_COLUMNS[GOALS_FILE], (list(r) for r in goals)))
        progress = db.execute(
            '''SELECT i.name, i.category, fp.row_type, fp.row_key, fp.acquired,
                      fp.quantity_owned, fp.comp_state
                 FROM farming_progress fp
                 JOIN farming_goals fg ON fg.id = fp.goal_id
                 JOIN items i ON i.id = fg.item_id
                WHERE fg.user_id = ? AND fp.user_id = ?
                ORDER BY i.category, i.name, fp.row_type, fp.row_key''',
            (user_id, user_id)).fetchall()
        zf.writestr(PROGRESS_FILE, _csv(FILE_COLUMNS[PROGRESS_FILE],
                                        (_export_progress_row(r) for r in progress)))
        checklist = db.execute(
            '''SELECT i.name, i.category, fc.component_name, fc.acquired
                 FROM farming_checklist fc JOIN items i ON i.id = fc.item_id
                WHERE fc.user_id = ?
                ORDER BY i.category, i.name, fc.component_name''', (user_id,)).fetchall()
        zf.writestr(CHECKLIST_FILE, _csv(FILE_COLUMNS[CHECKLIST_FILE],
                                         (list(r) for r in checklist)))
        watch = db.execute(
            '''SELECT i.name, i.category
                 FROM user_market_watchlist w JOIN items i ON i.id = w.item_id
                WHERE w.user_id = ?
                ORDER BY i.category, i.name''', (user_id,)).fetchall()
        zf.writestr(WATCHLIST_FILE, _csv(FILE_COLUMNS[WATCHLIST_FILE], (list(r) for r in watch)))
        marks = ', '.join('?' for _ in SETTING_KEYS)
        settings = db.execute(
            f'''SELECT "key", value FROM settings
                 WHERE user_id = ? AND "key" IN ({marks})
                 ORDER BY "key"''', [user_id, *SETTING_KEYS]).fetchall()
        zf.writestr(SETTINGS_FILE, _csv(FILE_COLUMNS[SETTINGS_FILE], (list(r) for r in settings)))
    return buf.getvalue()


def _export_progress_row(r) -> list:
    """A component row exports its normalised state (``pending``, ``blueprint`` or
    ``crafted``; a row stored before the three states with ``acquired = 1`` is ``crafted``)
    and ``acquired`` as its mirror, 1 for ``crafted``, else 0. An ingredient row carries no
    state: its ``comp_state`` is empty."""
    if r['row_type'] == 'component':
        state = r['comp_state']
        if state not in ('blueprint', 'crafted'):
            state = 'crafted' if r['acquired'] else 'pending'
        acquired = 1 if state == 'crafted' else 0
    else:
        state, acquired = None, r['acquired']
    return [r['name'], r['category'], r['row_type'], r['row_key'], acquired,
            r['quantity_owned'], state]


def _csv(columns: list, rows) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(['' if v is None else v for v in row])
    return out.getvalue()
