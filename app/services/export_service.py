"""Export of one user's tracker data as a ZIP of CSV files (format 2).

The export writes the caller's rows only: one ``<category>.csv`` per category (progress
flags, note, arcane ranks). Items are named by ``(name, category)``, never by id.
"""
import csv
import io
import zipfile

from models.item import CATEGORIES
from models.progress import Progress

CATEGORY_COLUMNS = ['name', 'category', 'source', 'in_possession', 'mastered', 'subsumed',
                    'resurgence_watch', 'note', 'owned_rank', 'target_rank']


# ── Export ──────────────────────────────────────────────────────────────────

def export_user_data(user_id: int) -> bytes:
    """The caller's data as a format-2 ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for cat in CATEGORIES:
            rows = Progress.get_all_for_user(user_id, category=cat)
            zf.writestr(f'{cat}.csv', _csv(CATEGORY_COLUMNS,
                                           ([r.get(c) for c in CATEGORY_COLUMNS] for r in rows)))
    return buf.getvalue()


def _csv(columns: list, rows) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(['' if v is None else v for v in row])
    return out.getvalue()
