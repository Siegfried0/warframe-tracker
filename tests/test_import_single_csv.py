"""The import takes one CSV file of an export on its own, not only the export ZIP.

The upload's content decides ZIP or CSV; a bare CSV's upload name says which export file it
is (last path part, blanks stripped, any case, one copy suffix " (N)" removed) and it is
read as that member of a current export ZIP: unescaped, validated, one transaction, the
rest of the user's data untouched. Axis 1 (input spellings): the upload name matrix and
the content's first bytes. Write path: every refusal leaves every table as it was.
Interface: the route passes the upload's name; the Settings page accepts .zip and .csv.
"""
import csv
import io

import pytest

from models.db import get_db
from models.item import Item
from services.export_service import export_user_data
from services.import_service import ImportRefused, import_user_data
from services.import_reader import MAX_UPLOAD_BYTES
from transfer_fixtures import (item_id, make_zip, put_progress, read_user, read_zip,  # noqa: F401
                               seeded, seeded_template, snapshot)  # noqa: F401

NAME_REFUSED = 'is not the name of a file of an export'

# What each accepted export file is fed in the name matrix, and how its effect is read.
CONTENT = {
    'warframe.csv': ('name,mastered\nExcalibur,1\n',
                     lambda u: u['progress'][('Excalibur', 'warframe')][1] == 1),
    'farming_goals.csv': ('name,category\nExcalibur,warframe\n',
                          lambda u: ('Excalibur', 'warframe') in u['goals']),
    'settings.csv': ('key,value\nplat_per_hour,5\n',
                     lambda u: u['settings'].get('plat_per_hour') == '5'),
}


@pytest.fixture
def sapp(seeded):
    """The app on this test's copy of the seeded database (transfer_fixtures.seed)."""
    return seeded[0]


@pytest.fixture
def users(seeded):
    return seeded[1]


def run(app, uid, data, filename):
    """Import ``data`` as an upload named ``filename``; on refusal assert that nothing
    changed and return the message, else the result."""
    if isinstance(data, str):
        data = data.encode('utf-8')
    with app.app_context():
        before = snapshot()
        try:
            return import_user_data(uid, io.BytesIO(data), filename)
        except ImportRefused as exc:
            assert snapshot() == before
            return str(exc)


def imported(app, uid, check):
    with app.app_context():
        return check(read_user(uid))


# ── Axis 1: the upload name ─────────────────────────────────────────────────

@pytest.mark.parametrize('filename, export_file', [
    ('warframe.csv', 'warframe.csv'),
    ('Warframe.CSV', 'warframe.csv'),
    ('C:\\x\\warframe.csv', 'warframe.csv'),  # abs-path-ok: an upload name under test
    ('a/b/farming_goals.csv', 'farming_goals.csv'),
    (' settings.csv ', 'settings.csv'),
    ('warframe (1).csv', 'warframe.csv'),
    ('WARFRAME (12).CSV', 'warframe.csv'),
    ('x/y\\settings (3).csv', 'settings.csv'),
])
def test_a_bare_csv_is_the_export_file_its_upload_name_says(sapp, users, filename,
                                                           export_file):
    text, check = CONTENT[export_file]
    result = run(sapp, users['bob'], text, filename)
    assert not isinstance(result, str), result
    assert imported(sapp, users['bob'], check)


@pytest.mark.parametrize('filename', [
    'warframes.csv', 'format.csv', '', '   ', 'notes.txt', 'warframe.csv.txt',
    'warframe (1) (2).csv', 'warframe(1).csv', 'warframe (x).csv', 'warframe (1).CSV.txt',
    'warframe.csv/', 'warframe', '.csv', 'warframe.tsv',
])
def test_a_bare_csv_under_any_other_name_is_refused_listing_the_accepted_names(
        sapp, users, filename):
    message = run(sapp, users['bob'], 'name,mastered\nExcalibur,1\n', filename)
    assert isinstance(message, str), message
    assert message.startswith('Import refused, nothing written:')
    assert NAME_REFUSED in message
    for accepted in ('warframe.csv', 'arcane.csv', 'farming_goals.csv',
                     'farming_progress.csv', 'farming_checklist.csv', 'watchlist.csv',
                     'settings.csv'):
        assert accepted in message
    assert 'format.csv' not in message.split('export name: ')[1]
    assert 'ZIP' in message


@pytest.mark.parametrize('filename', ['export.zip', 'EXPORT.ZIP', ' a/export.Zip '])
def test_a_non_zip_upload_named_zip_is_refused_as_not_a_zip(sapp, users, filename):
    message = run(sapp, users['bob'], 'name,mastered\nExcalibur,1\n', filename)
    assert 'the file is not a ZIP archive' in message


# ── Axis 1: the content's first bytes ───────────────────────────────────────

@pytest.mark.parametrize('filename', ['warframe.csv', 'notes.txt', '', 'export.zip'])
def test_an_upload_starting_with_a_zip_signature_is_read_as_a_zip(sapp, users, filename):
    data = make_zip({'secondary.csv': 'name,in_possession\nLex,1\n'}).getvalue()
    result = run(sapp, users['bob'], data, filename)
    assert not isinstance(result, str), result
    assert imported(sapp, users['bob'], lambda u: u['progress'][('Lex', 'secondary')][0] == 1)
    assert imported(sapp, users['bob'], lambda u: ('Excalibur', 'warframe') not in u['progress'])


def test_an_empty_archive_named_csv_is_read_as_an_empty_zip(sapp, users):
    data = make_zip({}).getvalue()
    assert data[:4] == b'PK\x05\x06'
    result = run(sapp, users['bob'], data, 'warframe.csv')
    assert result.counts == {'items': 0, 'goals': 0, 'progress': 0, 'checklist': 0,
                             'watchlist': 0, 'settings': 0}


def test_a_bare_csv_with_a_bom_imports(sapp, users):
    run(sapp, users['bob'], b'\xef\xbb\xbfname,in_possession\r\nLex,1\r\n', 'secondary.csv')
    assert imported(sapp, users['bob'], lambda u: u['progress'][('Lex', 'secondary')][0] == 1)


def test_an_empty_bare_csv_is_refused_naming_the_file(sapp, users):
    assert 'warframe.csv line 1' in run(sapp, users['bob'], b'', 'warframe.csv')


def test_a_zip_upload_without_a_name_is_read_as_a_zip_as_before(sapp, users):
    assert 'not a ZIP archive' in run(sapp, users['bob'], 'name,mastered\nExcalibur,1\n', None)


# ── Format 3 unescape ───────────────────────────────────────────────────────

@pytest.mark.parametrize('cell, stored', [
    ("'=1+1", '=1+1'), ("'-dash", '-dash'), ("'@x", '@x'), ("''=x", "'=x"),
    ("'x", "'x"), ("'", "'"), ('plain', 'plain'),
])
def test_a_bare_csvs_cells_are_unescaped_as_in_a_current_export(sapp, users, cell, stored):
    text = 'name,note\n' + f'Excalibur,{cell}\n'
    run(sapp, users['bob'], text, 'warframe.csv')
    assert imported(sapp, users['bob'],
                    lambda u: u['progress'][('Excalibur', 'warframe')][4]) == stored


def test_a_bare_csvs_escaped_header_cell_is_unescaped(sapp, users):
    # A header cell is unescaped too, so "'=x" names column "=x", an unknown column.
    run(sapp, users['bob'], "name,mastered,'=x\nExcalibur,1,y\n", 'warframe.csv')
    assert imported(sapp, users['bob'], lambda u: u['progress'][('Excalibur', 'warframe')][1])


# ── Round trip ──────────────────────────────────────────────────────────────

def _flip_mastered(text: str, name: str) -> str:
    rows = list(csv.reader(io.StringIO(text, newline='')))
    col = rows[0].index('mastered')
    for row in rows[1:]:
        if row[0] == name:
            row[col] = '0' if row[col] == '1' else '1'
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()


def test_one_file_of_an_export_edited_and_uploaded_alone_changes_only_its_rows(sapp, users):
    alice = users['alice']
    with sapp.app_context():
        Item.upsert('Volt', 'warframe', source='standard')
        put_progress(alice, 'Volt', 'warframe', in_possession=1, note='-dash note')
        db = get_db()
        db.execute('UPDATE user_progress SET note = ? WHERE user_id = ? AND item_id = ?',
                   ('=1+1 formula', alice, item_id('Excalibur', 'warframe')))
        db.commit()
        files = read_zip(export_user_data(alice))
        before = read_user(alice)
        tables = snapshot(drop_updated_at=True)
    assert "'=1+1 formula" in files['warframe.csv'] and "'-dash note" in files['warframe.csv']
    edited = _flip_mastered(files['warframe.csv'], 'Excalibur')

    result = run(sapp, alice, edited, 'warframe (1).csv')
    assert not isinstance(result, str), result

    with sapp.app_context():
        after = read_user(alice)
        tables_after = snapshot(drop_updated_at=True)
    want = dict(before['progress'])
    want[('Excalibur', 'warframe')] = (1, 0, 0, 0, '=1+1 formula', None, None)
    assert after['progress'] == want
    assert after['progress'][('Volt', 'warframe')][4] == '-dash note'
    for view in ('goals', 'goal_rows', 'checklist', 'watchlist', 'settings'):
        assert after[view] == before[view]
    for table in ('items', 'farming_goals', 'farming_progress', 'farming_checklist',
                  'user_market_watchlist', 'settings'):
        assert tables_after[table] == tables[table]
    changed = [a for a, b in zip(tables_after['user_progress'], tables['user_progress'])
               if a != b]
    assert len(tables_after['user_progress']) == len(tables['user_progress'])
    assert [(r['item_id'], r['mastered']) for r in changed] == [
        (r['item_id'], 0) for r in changed]
    assert len(changed) == 1


# ── Refusals on a bare file, one per family ─────────────────────────────────

@pytest.mark.parametrize('filename, data, words', [
    ('warframe.csv', 'name,mastered\nExcalibur,2\n',
     'warframe.csv line 2, column mastered:'),
    ('Warframe (2).csv', 'name,mastered\nExcalibur,yes\n',
     'warframe.csv line 2, column mastered:'),
    ('watchlist.csv', 'name,category\nLex,secondary\nLex,secondary\n',
     'watchlist.csv line 3, column name: the same row key already appears on line 2'),
    ('melee.csv', 'name,mastered\nExcalibur,1\n'.encode('latin-1') + 'Ñ,1\n'.encode('latin-1'),
     'melee.csv is not UTF-8'),
    ('warframe.csv', b'n' * (MAX_UPLOAD_BYTES + 1), 'larger than 8 MiB'),
    ('settings.csv', 'value\n5\n', 'settings.csv line 1, column key: the header lacks'),
    ('FARMING_PROGRESS.csv', 'name,category,row_type,row_key,quantity_owned\n'
                             'Lex,secondary,ingredient,Morphics,4\n',
     'farming_progress.csv line 2, column name:'),
    ('primary.csv', 'name,category\nBraton,secondary\n', 'primary.csv line 2, column category'),
])
def test_a_bare_csvs_refusals_name_the_file_by_its_export_name(sapp, users, filename, data,
                                                               words):
    message = run(sapp, users['bob'], data, filename)
    assert isinstance(message, str), message
    assert message.startswith('Import refused, nothing written:')
    assert words in message


def test_a_bare_progress_file_imports_when_its_goal_is_among_the_users_goals(sapp, users):
    alice = users['alice']
    result = run(sapp, alice, 'name,category,row_type,row_key,quantity_owned\n'
                             'Excalibur,warframe,ingredient,Morphics,7\n',
                 'farming_progress.csv')
    assert not isinstance(result, str), result
    assert imported(sapp, alice, lambda u: u['goal_rows'][
        ('Excalibur', 'warframe', 'ingredient', 'Morphics')][1]) == 7


# ── Interface ───────────────────────────────────────────────────────────────

def post_import(client, data: bytes, filename: str):
    return client.post('/settings/import',
                       data={'import_file': (io.BytesIO(data), filename)},
                       content_type='multipart/form-data', follow_redirects=True)


def test_the_import_route_passes_the_uploads_name(admin_client):
    page = post_import(admin_client, b'name,mastered\nNew Frame,1\n',
                       'warframe (1).csv').get_data(as_text=True)
    assert 'Import complete: 1 items, 0 goals' in page
    page = post_import(admin_client, b'name,mastered\nNew Frame,1\n',
                       'my-edit.csv').get_data(as_text=True)
    assert 'Import refused, nothing written:' in page
    assert NAME_REFUSED in page
    page = post_import(admin_client, b'name,mastered\nNew Frame,1\n',
                       'export.zip').get_data(as_text=True)
    assert 'the file is not a ZIP archive' in page


def test_the_settings_page_accepts_a_zip_or_one_csv_file(admin_client):
    page = admin_client.get('/settings/').get_data(as_text=True)
    assert 'accept=".zip,.csv"' in page
    assert 'placeholder="Choose a ZIP or one CSV file of an export…"' in page
    assert 'one CSV file of an export' in page
