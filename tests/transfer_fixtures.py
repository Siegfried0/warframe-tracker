"""Shared data and readers for the export / import tests.

`seed(app)` builds a catalogue and three users: Alice (progress with notes and arcane
ranks), Bob (empty, the import target) and Carol (rows of her own that no import into Bob may touch). The
`seeded` fixture gives each test its own copy of one database seeded once per session
(seeding per test cost most of the matrices' run time). The readers return each table
keyed by item name and category, never by id, so two users' data compare directly.
"""
import csv
import io
import shutil
import zipfile

import pytest

import app as app_module

from models.db import get_db
from models.item import Item
from models.user import User

WEIRD = 'Odd, "quoted"\nname Ñoño ☃'
FORMAT1_HEADER = 'name,category,source,in_possession,mastered,subsumed,resurgence_watch,note'

# (name, category, max rank)
CATALOGUE = [
    ('Excalibur', 'warframe', None),
    ('Braton', 'primary', None),
    ('Lex', 'secondary', None),
    (WEIRD, 'melee', None),
    ('Arcane Energize', 'arcane', 5),
    ('Morphics', 'resource', None),
]


def item_id(name, category):
    return get_db().execute('SELECT id FROM items WHERE name = ? AND category = ?',
                            (name, category)).fetchone()[0]


def put_progress(uid, name, category, **fields):
    cols = ['user_id', 'item_id', *fields]
    marks = ', '.join('?' for _ in cols)
    db = get_db()
    db.execute(f'INSERT INTO user_progress ({", ".join(cols)}) VALUES ({marks})',
               [uid, item_id(name, category), *fields.values()])
    db.commit()


def seed(app):
    """Return {'alice': id, 'bob': id, 'carol': id}; runs inside an app context."""
    for name, category, max_rank in CATALOGUE:
        Item.upsert(name, category, source='standard', mod_max_rank=max_rank)
    users = {n: User.create(n, password=f'pw-{n}-123').id for n in ('alice', 'bob', 'carol')}
    alice, carol = users['alice'], users['carol']

    put_progress(alice, 'Excalibur', 'warframe', in_possession=1, mastered=1,
                 note='keep, "forma" x2')
    put_progress(alice, 'Braton', 'primary', resurgence_watch=1)
    put_progress(alice, WEIRD, 'melee', mastered=1, subsumed=0, note='ü\nline two')
    put_progress(alice, 'Arcane Energize', 'arcane', in_possession=1, owned_rank=3,
                 target_rank=5)

    put_progress(carol, 'Lex', 'secondary', in_possession=1, note='carol-only note')
    return users


def _make_app(path):
    return app_module.create_app({
        'TESTING': True, 'DATABASE_PATH': str(path), 'SYNC_INTERVAL_HOURS': 0,
        'AUTO_SYNC_ON_STARTUP': False, 'SECRET_KEY': 'test-only',
    })


def _stop(flask_app):
    scheduler = getattr(flask_app, 'scheduler', None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)


@pytest.fixture(scope='session')
def seeded_template(tmp_path_factory):
    """(path, users) of a database seeded once; tests only ever read it to copy it."""
    path = tmp_path_factory.mktemp('transfer-seed') / 'seed.db'
    flask_app = _make_app(path)
    with flask_app.app_context():
        users = seed(flask_app)
        get_db().execute('PRAGMA wal_checkpoint(TRUNCATE)')
    _stop(flask_app)
    return path, users


@pytest.fixture
def seeded(seeded_template, tmp_path):
    """(app, users): a fresh app on this test's own copy of the seeded database."""
    src, users = seeded_template
    dst = tmp_path / 'test.db'
    shutil.copyfile(src, dst)
    flask_app = _make_app(dst)
    yield flask_app, users
    _stop(flask_app)


# ── Readers: one dict per table, keyed by names ─────────────────────────────

def _rows(sql, uid):
    return [dict(r) for r in get_db().execute(sql, (uid,)).fetchall()]


def read_user(uid) -> dict:
    """The progress a user owns, keyed by (name, category)."""
    progress = {(r['name'], r['category']): (r['in_possession'], r['mastered'], r['subsumed'],
                                             r['resurgence_watch'], r['note'], r['owned_rank'],
                                             r['target_rank'])
                for r in _rows('''SELECT i.name, i.category, p.* FROM user_progress p
                                  JOIN items i ON i.id = p.item_id WHERE p.user_id = ?''', uid)}
    return {'progress': progress}


def nonzero_progress(view: dict) -> dict:
    """Progress rows that record anything: an export names every catalogue item, so an
    import adds all-default rows for the items the source user never touched."""
    return {k: v for k, v in view.items() if any(x not in (0, None) for x in v)}


_TABLES = ['items', 'user_progress', 'farming_goals', 'farming_progress',
           'farming_checklist', 'user_market_watchlist', 'settings']


def snapshot(drop_updated_at=False) -> dict:
    """Every row of every table an import can write (the farming tables too: an import of
    this format never writes them), in id order."""
    out = {}
    for table in _TABLES:
        rows = [dict(r) for r in get_db().execute(f'SELECT * FROM {table} ORDER BY id')]
        if drop_updated_at:
            for r in rows:
                r.pop('updated_at', None)
        out[table] = rows
    return out


# ── Building uploads ────────────────────────────────────────────────────────

def make_zip(files: dict) -> io.BytesIO:
    """A ZIP of {name: str or bytes}; str is written as UTF-8."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content.encode('utf-8') if isinstance(content, str) else content)
    buf.seek(0)
    return buf


def csv_text(header: list, *rows) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


def read_zip(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n).decode('utf-8') for n in zf.namelist()}
