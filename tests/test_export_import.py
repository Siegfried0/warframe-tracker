"""Export and import carry a user's tracker data (the Settings page's export and import).

Oracles: the round trip (Alice exported, imported into empty Bob, her progress read back
by item name and category); the format-1 import (flags and notes restored, everything
else untouched); the write path (only the caller's rows, catalogue items only for unknown
names, watchlist, settings and farming files ignored); the merge rules for ranks; and the
interface (routes, messages, page text). The refusal matrices are in
test_import_refusals.py.
"""
import io
from pathlib import Path

import pytest

from models.db import get_db
from models.item import CATEGORIES
from services import import_writer
from services.export_service import export_user_data
from services.import_service import ImportRefused, import_user_data
from services.auth_service import set_setting
from services.farming_service import add_farming_goal
from transfer_fixtures import (FORMAT1_HEADER, WEIRD, item_id, make_zip, nonzero_progress,
                               put_progress, read_user, read_zip, seeded,  # noqa: F401
                               seeded_template, snapshot)  # noqa: F401

APP = Path(__file__).resolve().parent.parent / 'app'
CATEGORY_FILES = {f'{cat}.csv' for cat in CATEGORIES}
UNTOUCHED = ['farming_goals', 'farming_progress', 'farming_checklist',
             'user_market_watchlist', 'settings']


@pytest.fixture
def sapp(seeded):
    """The app on this test's copy of the seeded database (transfer_fixtures.seed)."""
    return seeded[0]


@pytest.fixture
def users(seeded):
    return seeded[1]


def run_import(uid, files):
    return import_user_data(uid, make_zip(files))


def watch(uid, name, category):
    get_db().execute('INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
                     (uid, item_id(name, category)))
    get_db().commit()


def untouched_tables():
    view = snapshot(drop_updated_at=True)
    return {table: view[table] for table in UNTOUCHED}


# ── Round trip ──────────────────────────────────────────────────────────────

def test_export_then_import_into_an_empty_user_restores_every_table(sapp, users):
    with sapp.app_context():
        alice = read_user(users['alice'])
        data = export_user_data(users['alice'])
        import_user_data(users['bob'], io.BytesIO(data))
        bob = read_user(users['bob'])
        again = export_user_data(users['bob'])
    assert nonzero_progress(bob['progress']) == nonzero_progress(alice['progress'])
    assert alice['progress'][('Arcane Energize', 'arcane')][5:] == (3, 5)
    assert alice['progress'][(WEIRD, 'melee')][4] == 'ü\nline two'
    assert read_zip(again) == read_zip(data)


def test_export_writes_one_file_per_category_with_rank_columns(sapp, users):
    with sapp.app_context():
        files = read_zip(export_user_data(users['alice']))
    assert set(files) == CATEGORY_FILES
    for name in files:
        assert files[name].splitlines()[0] == FORMAT1_HEADER + ',owned_rank,target_rank'
    assert 'Arcane Energize,arcane,standard,1,0,0,0,,3,5' in files['arcane.csv']


def test_export_holds_no_other_users_rows(sapp, users):
    with sapp.app_context():
        files = read_zip(export_user_data(users['alice']))
    assert 'carol-only note' not in ''.join(files.values())
    assert 'Lex,secondary,standard,0,0,0,0,,,' in files['secondary.csv']


# ── Format 1 ────────────────────────────────────────────────────────────────

def test_a_format_1_zip_restores_flags_and_notes_and_leaves_the_rest(sapp, users):
    bob = users['bob']
    with sapp.app_context():
        put_progress(bob, 'Arcane Energize', 'arcane', owned_rank=4, target_rank=5)
        put_progress(bob, 'Excalibur', 'warframe', in_possession=0, note='old')
        watch(bob, 'Lex', 'secondary')
        set_setting(bob, 'plat_per_hour', '9')
        add_farming_goal(bob, item_id('Braton', 'primary'))
        before = untouched_tables()
        run_import(bob, {
            'warframe.csv': FORMAT1_HEADER + '\r\nExcalibur,warframe,standard,1,1,0,0,new note\r\n',
            'arcane.csv': FORMAT1_HEADER + '\r\nArcane Energize,arcane,,1,0,0,1,\r\n',
        })
        after = read_user(bob)
        assert untouched_tables() == before
    assert after['progress'][('Excalibur', 'warframe')][:5] == (1, 1, 0, 0, 'new note')
    assert after['progress'][('Arcane Energize', 'arcane')] == (1, 0, 0, 1, None, 4, 5)
    assert len(before['farming_goals']) == 1
    assert len(before['user_market_watchlist']) == 1 and len(before['settings']) == 1


def test_a_missing_flag_column_leaves_that_flag_alone(sapp, users):
    alice = users['alice']
    with sapp.app_context():
        run_import(alice, {'warframe.csv': 'name,mastered\nExcalibur,0\n'})
        row = read_user(alice)['progress'][('Excalibur', 'warframe')]
    assert row[:2] == (1, 0)          # in_possession kept, mastered written
    assert row[4] == 'keep, "forma" x2'


# ── Write path ──────────────────────────────────────────────────────────────

def test_an_import_leaves_other_users_rows_unchanged(sapp, users):
    with sapp.app_context():
        data = export_user_data(users['alice'])
        carol, alice = read_user(users['carol']), read_user(users['alice'])
        import_user_data(users['bob'], io.BytesIO(data))
        assert read_user(users['carol']) == carol
        assert read_user(users['alice']) == alice


def test_catalogue_items_are_added_only_for_unknown_names(sapp, users):
    with sapp.app_context():
        count = get_db().execute('SELECT COUNT(*) FROM items').fetchone()[0]
        run_import(users['bob'], {
            'warframe.csv': 'name,category\nExcalibur,warframe\nNew Frame,warframe\n',
            'primary.csv': 'name,in_possession\nNew Gun,1\n',
        })
        assert get_db().execute('SELECT COUNT(*) FROM items').fetchone()[0] == count + 2
        assert read_user(users['bob'])['progress'][('New Gun', 'primary')][0] == 1


@pytest.mark.parametrize('fname, text', [
    ('watchlist.csv', 'name,category\nLex,secondary\nNew Gun,primary\n'),
    ('settings.csv', 'key,value\nplat_per_hour,45\n'),
    ('farming_goals.csv', 'name,category,pinned\nFarm Only,warframe,1\n'),
    ('farming_progress.csv', 'name,category,row_type,row_key\nBraton,primary,x,y\n'),
    ('farming_checklist.csv', 'not,a,valid\nheader'),
])
def test_a_file_of_a_later_format_part_is_ignored_as_unknown(sapp, users, fname, text):
    bob = users['bob']
    with sapp.app_context():
        before = snapshot()
        result = run_import(bob, {fname: text})
        assert snapshot() == before
    assert result.counts == {'items': 0}


def test_a_rank_above_the_items_max_is_clamped(sapp, users):
    bob = users['bob']
    with sapp.app_context():
        run_import(bob, {'arcane.csv': 'name,owned_rank,target_rank\n'
                                       'Arcane Energize,9,5\nArcane New,3,\n'})
        view = read_user(bob)['progress']
    assert view[('Arcane Energize', 'arcane')][5:] == (5, 5)
    assert view[('Arcane New', 'arcane')][5:] == (0, None)  # no max rank known: 0


def test_a_failed_write_mid_import_writes_nothing(sapp, users, monkeypatch):
    real, calls = import_writer._upsert, []

    def fails_on_the_third_row(*args):
        calls.append(args)
        if len(calls) == 3:
            raise RuntimeError('disk full')
        return real(*args)
    monkeypatch.setattr(import_writer, '_upsert', fails_on_the_third_row)
    with sapp.app_context():
        data = export_user_data(users['alice'])
        before = snapshot()
        with pytest.raises(ImportRefused, match='Import failed, nothing written'):
            import_user_data(users['bob'], io.BytesIO(data))
        assert snapshot() == before
    assert len(calls) == 3


def test_the_same_zip_imported_twice_gives_the_same_state(sapp, users):
    with sapp.app_context():
        data = export_user_data(users['alice'])
        import_user_data(users['bob'], io.BytesIO(data))
        once = snapshot(drop_updated_at=True)
        import_user_data(users['bob'], io.BytesIO(data))
        assert snapshot(drop_updated_at=True) == once


# ── Interface ───────────────────────────────────────────────────────────────

def post_import(client, data: bytes):
    return client.post('/settings/import', data={'import_file': (io.BytesIO(data), 'x.zip')},
                       content_type='multipart/form-data', follow_redirects=True)


def test_the_export_route_downloads_the_callers_zip(admin_client):
    response = admin_client.get('/settings/export')
    assert response.status_code == 200
    assert response.mimetype == 'application/zip'
    assert 'attachment; filename=warframe_progress_' in response.headers['Content-Disposition']
    assert set(read_zip(response.data)) == CATEGORY_FILES


def test_the_import_route_reports_success_and_refusal(app, admin_client):
    ok = make_zip({'warframe.csv': 'name,mastered\nNew Frame,1\nOther Frame,0\n'}).getvalue()
    page = post_import(admin_client, ok).get_data(as_text=True)
    assert 'Import complete: 2 items.' in page
    bad = make_zip({'warframe.csv': 'name,mastered\nNew Frame,<b>x</b>\n'}).getvalue()
    page = post_import(admin_client, bad).get_data(as_text=True)
    assert 'Import refused, nothing written: warframe.csv line 2, column mastered:' in page
    assert '&lt;b&gt;' in page and '<b>x</b>' not in page
    page = admin_client.post('/settings/import', data={}, follow_redirects=True)
    assert 'No file uploaded.' in page.get_data(as_text=True)


@pytest.mark.parametrize('size, read_by_service', [(8 * 1024 * 1024 + 1, True),
                                                    (9 * 1024 * 1024, False)])
def test_the_import_route_refuses_an_oversized_upload(admin_client, monkeypatch, size,
                                                      read_by_service):
    import routes.settings
    if not read_by_service:  # past the request cap the upload never reaches the service
        monkeypatch.setattr(routes.settings, 'import_user_data', pytest.fail)
    page = post_import(admin_client, b'x' * size)
    assert page.status_code == 200
    assert 'Import refused, nothing written: the upload is larger than 8 MiB.' in \
        page.get_data(as_text=True)


def test_the_settings_page_says_what_the_export_carries(admin_client):
    page = ' '.join(admin_client.get('/settings/').get_data(as_text=True).split())
    for words in ('notes and arcane ranks', 'Your market watchlist, personal settings and '
                  'farming goals are not exported yet; an import leaves them as they are.'):
        assert words in page


# ── Structure ────────────────────────────────────────────────────────────────

NEW_SQL = ['services/export_service.py', 'services/import_service.py',
           'services/import_writer.py']


@pytest.mark.parametrize('path', NEW_SQL)
def test_new_sql_is_portable(path):
    text = (APP / path).read_text(encoding='utf-8').upper()
    for banned in ('ON CONFLICT', 'INSERT OR IGNORE', "DATETIME('NOW')"):
        assert banned not in text


def test_the_settings_route_holds_no_export_logic():
    text = (APP / 'routes/settings.py').read_text(encoding='utf-8')
    for word in ('import csv', 'import zipfile', 'get_db', 'Item.'):
        assert word not in text
