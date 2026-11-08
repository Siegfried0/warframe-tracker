"""Export and import carry a user's tracker data (the Settings page's export and import).

Oracles: the round trip (Alice exported, imported into empty Bob, her progress, farming
goals, goal progress, checklist, watchlist and settings read back by item name and category
or setting key); the format-1 import and a ZIP of category files only (what they carry
restored, everything else untouched); the write path (only the caller's rows, catalogue
items only for unknown names, the global settings never written); the merge rules for
ranks, component states, watchlist and settings; and the interface (routes, messages, page
text). The refusal matrices are in test_import_refusals.py.
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
from transfer_fixtures import (FORMAT1_HEADER, WEIRD, csv_text, item_id, make_zip,
                               nonzero_progress,
                               put_progress, read_user, read_zip, seeded,  # noqa: F401
                               seeded_template, snapshot, watch)  # noqa: F401

APP = Path(__file__).resolve().parent.parent / 'app'
CATEGORY_FILES = {f'{cat}.csv' for cat in CATEGORIES}
FARMING_FILES = {
    'farming_goals.csv': 'name,category,pinned,display_mode,sort_order',
    'farming_progress.csv': 'name,category,row_type,row_key,acquired,quantity_owned,comp_state',
    'farming_checklist.csv': 'name,category,component_name,acquired',
}
FARMING_VIEWS = ('goals', 'goal_rows', 'checklist')
EXPORT_FILES = CATEGORY_FILES | set(FARMING_FILES) | {'watchlist.csv', 'settings.csv'}
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
    for view in FARMING_VIEWS:
        assert bob[view] == alice[view], view
    for table in ('watchlist', 'settings'):
        assert bob[table] == alice[table], table
    assert alice['watchlist'] == {('Braton', 'primary'), (WEIRD, 'melee')}
    assert alice['settings'] == {'market_cache_hours': '12', 'plat_per_hour': '30',
                                 'buy_threshold_plat': None}
    assert alice['progress'][('Arcane Energize', 'arcane')][5:] == (3, 5)
    assert alice['progress'][(WEIRD, 'melee')][4] == 'ü\nline two'
    assert alice['goals'] == {('Excalibur', 'warframe'): (1, 'list', 2),
                              (WEIRD, 'melee'): (0, 'tree', 3)}
    assert alice['goal_rows'] == {
        ('Excalibur', 'warframe', 'component', 'Blueprint'): (0, 0, 'blueprint'),
        ('Excalibur', 'warframe', 'component', 'Neuroptics'): (1, 0, 'crafted'),
        ('Excalibur', 'warframe', 'component', 'Chassis'): (0, 0, None),
        ('Excalibur', 'warframe', 'ingredient', 'Morphics'): (0, 3, None)}
    assert alice['checklist'] == {('Braton', 'primary', 'Barrel'): 1,
                                  ('Braton', 'primary', 'Stock'): 0}
    assert read_zip(again) == read_zip(data)


def test_export_writes_every_file_of_the_format_with_its_header(sapp, users):
    with sapp.app_context():
        files = read_zip(export_user_data(users['alice']))
    assert set(files) == EXPORT_FILES
    for name in CATEGORY_FILES:
        assert files[name].splitlines()[0] == FORMAT1_HEADER + ',owned_rank,target_rank'
    for name, header in FARMING_FILES.items():
        assert files[name].splitlines()[0] == header
    assert files['watchlist.csv'].splitlines()[0] == 'name,category'
    assert files['settings.csv'] == ('key,value\r\nbuy_threshold_plat,\r\n'
                                     'market_cache_hours,12\r\nplat_per_hour,30\r\n')
    assert 'Arcane Energize,arcane,standard,1,0,0,0,,3,5' in files['arcane.csv']
    assert 'Excalibur,warframe,1,list,2\r\n' in files['farming_goals.csv']
    progress = files['farming_progress.csv']
    assert 'Excalibur,warframe,component,Blueprint,0,0,blueprint\r\n' in progress
    assert 'Excalibur,warframe,component,Neuroptics,1,0,crafted\r\n' in progress
    assert 'Excalibur,warframe,component,Chassis,0,0,pending\r\n' in progress
    assert 'Excalibur,warframe,ingredient,Morphics,0,3,\r\n' in progress
    assert 'Braton,primary,Barrel,1\r\n' in files['farming_checklist.csv']


@pytest.mark.parametrize('stored, exported', [
    ((1, None), 'crafted,1'), ((0, None), 'pending,0'), ((1, 'blueprint'), 'blueprint,0'),
    ((0, 'crafted'), 'crafted,1'), ((1, 'pending'), 'crafted,1'),
])
def test_a_component_exports_its_normalised_state_and_acquired_as_its_mirror(
        sapp, users, stored, exported):
    acquired, state = stored
    with sapp.app_context():
        db = get_db()
        db.execute('UPDATE farming_progress SET acquired = ?, comp_state = ? '
                   "WHERE row_key = 'Chassis'", (acquired, state))
        db.commit()
        files = read_zip(export_user_data(users['alice']))
    state_out, acquired_out = exported.split(',')
    assert (f'Excalibur,warframe,component,Chassis,{acquired_out},0,{state_out}\r\n'
            in files['farming_progress.csv'])


def test_export_holds_no_other_users_rows(sapp, users):
    with sapp.app_context():
        files = read_zip(export_user_data(users['alice']))
    text = ''.join(files.values())
    for carol_value in ('carol-only note', 'Receiver', ',99,', '777'):
        assert carol_value not in text
    assert 'Lex,secondary,standard,0,0,0,0,,,' in files['secondary.csv']
    assert 'Lex' not in files['farming_goals.csv']
    assert 'Lex' not in files['watchlist.csv']
    assert files['watchlist.csv'] == ('name,category\r\n'
                                      '"Odd, ""quoted""\nname Ñoño ☃",melee\r\n'
                                      'Braton,primary\r\n')


def test_export_carries_only_the_settings_pages_keys(sapp, users):
    alice = users['alice']
    with sapp.app_context():
        set_setting(alice, 'theme', 'dark')
        get_db().execute('INSERT INTO settings (user_id, "key", value) VALUES (NULL, ?, ?)',
                         ('sync_interval_hours', '24'))
        get_db().commit()
        text = read_zip(export_user_data(alice))['settings.csv']
    assert 'theme' not in text and 'sync_interval_hours' not in text


@pytest.mark.parametrize('stored', ['abc', '2.5', '-3'])
def test_a_stored_setting_that_is_not_a_whole_number_exports_as_stored_and_is_refused(
        sapp, users, stored):
    """The Preferences form stores any text; the export never rewrites what the user typed,
    and the import refuses a value the app cannot use, naming the cell."""
    alice, bob = users['alice'], users['bob']
    with sapp.app_context():
        set_setting(alice, 'plat_per_hour', stored)
        data = export_user_data(alice)
        assert f'plat_per_hour,{stored}\r\n' in read_zip(data)['settings.csv']
        before = snapshot()
        with pytest.raises(ImportRefused) as refused:
            import_user_data(bob, io.BytesIO(data))
        assert snapshot() == before
    assert 'settings.csv line 4, column value:' in str(refused.value)


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
    assert [g['user_id'] for g in before['farming_goals']].count(bob) == 1
    assert after['watchlist'] == {('Lex', 'secondary')}
    assert after['settings'] == {'plat_per_hour': '9'}


def test_a_zip_of_category_files_only_leaves_farming_watchlist_and_settings(sapp, users):
    alice = users['alice']
    with sapp.app_context():
        files = read_zip(export_user_data(alice))
        only_categories = {n: t for n, t in files.items() if n in CATEGORY_FILES}
        only_categories['warframe.csv'] = only_categories['warframe.csv'].replace(
            'Excalibur,warframe,standard,1,1', 'Excalibur,warframe,standard,0,1')
        before = read_user(alice)
        result = run_import(alice, only_categories)
        after = read_user(alice)
    assert after['progress'][('Excalibur', 'warframe')][0] == 0
    for view in (*FARMING_VIEWS, 'watchlist', 'settings'):
        assert after[view] == before[view], view
    assert [result.counts[k] for k in ('goals', 'progress', 'checklist', 'watchlist',
                                       'settings')] == [0, 0, 0, 0, 0]


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
            'farming_goals.csv': 'name,category\nNew Gun,primary\nNew Mod,mod\n',
            'farming_checklist.csv': 'name,category,component_name\nNew Frame,warframe,Systems\n',
            'watchlist.csv': 'name,category\nNew Frame,warframe\nNew Gun,primary\n'
                             'New Pistol,secondary\nLex,secondary\n',
        })
        assert get_db().execute('SELECT COUNT(*) FROM items').fetchone()[0] == count + 4
        bob = read_user(users['bob'])
    assert bob['progress'][('New Gun', 'primary')][0] == 1
    assert set(bob['goals']) == {('New Gun', 'primary'), ('New Mod', 'mod')}
    assert bob['checklist'] == {('New Frame', 'warframe', 'Systems'): 0}
    assert bob['watchlist'] == {('New Frame', 'warframe'), ('New Gun', 'primary'),
                                ('New Pistol', 'secondary'), ('Lex', 'secondary')}


def test_an_import_merges_farming_rows_and_keeps_rows_it_does_not_name(sapp, users):
    alice = users['alice']
    with sapp.app_context():
        result = run_import(alice, {
            'farming_goals.csv': 'name,category,pinned\nExcalibur,warframe,0\nLex,secondary,1\n',
            'farming_progress.csv': 'name,category,row_type,row_key,quantity_owned\n'
                                    'Excalibur,warframe,ingredient,Morphics,8\n'
                                    'Lex,secondary,component,Barrel,0\n',
            'farming_checklist.csv': 'name,category,component_name,acquired\n'
                                     'Braton,primary,Stock,1\n',
        })
        view = read_user(alice)
    assert view['goals'] == {('Excalibur', 'warframe'): (0, 'list', 2),
                             (WEIRD, 'melee'): (0, 'tree', 3),
                             ('Lex', 'secondary'): (1, 'tree', 0)}
    rows = view['goal_rows']
    assert rows[('Excalibur', 'warframe', 'ingredient', 'Morphics')] == (0, 8, None)
    assert rows[('Excalibur', 'warframe', 'component', 'Blueprint')] == (0, 0, 'blueprint')
    assert rows[('Lex', 'secondary', 'component', 'Barrel')] == (0, 0, None)
    assert view['checklist'] == {('Braton', 'primary', 'Barrel'): 1,
                                 ('Braton', 'primary', 'Stock'): 1}
    assert (result.counts['goals'], result.counts['progress'], result.counts['checklist']) \
        == (2, 2, 1)


def test_an_import_merges_and_keeps_rows_it_does_not_name(sapp, users):
    alice = users['alice']
    with sapp.app_context():
        run_import(alice, {
            'watchlist.csv': 'name,category\nLex,secondary\nBraton,primary\n',
            'settings.csv': 'key,value\nplat_per_hour,45\nbuy_threshold_plat,7\n',
        })
        view = read_user(alice)
    assert view['watchlist'] == {('Braton', 'primary'), (WEIRD, 'melee'), ('Lex', 'secondary')}
    assert view['settings'] == {'market_cache_hours': '12', 'plat_per_hour': '45',
                                'buy_threshold_plat': '7'}


@pytest.mark.parametrize('text, expected', [
    ('key,value\nplat_per_hour,\nmarket_cache_hours, 3 \n',
     {'plat_per_hour': None, 'market_cache_hours': '3', 'buy_threshold_plat': None}),
    ('key\nplat_per_hour\nbuy_threshold_plat\n',
     {'plat_per_hour': '30', 'market_cache_hours': '12', 'buy_threshold_plat': None}),
    ('key,value\nPlat_Per_Hour,5\ntheme,dark\nmarket_cache_hours ,4\n',
     {'plat_per_hour': '30', 'market_cache_hours': '4', 'buy_threshold_plat': None}),
], ids=['empty value is null, blanks stripped', 'no value column: existing kept',
        'wrong case and other keys ignored'])
def test_setting_values_and_keys_import_as_the_format_says(sapp, users, text, expected):
    alice = users['alice']
    with sapp.app_context():
        run_import(alice, {'settings.csv': text})
        assert read_user(alice)['settings'] == expected


def test_a_setting_without_a_value_column_is_added_empty(sapp, users):
    bob = users['bob']
    with sapp.app_context():
        result = run_import(bob, {'settings.csv': 'key\nplat_per_hour\n'})
        assert read_user(bob)['settings'] == {'plat_per_hour': None}
    assert result.counts['settings'] == 1


def test_an_import_never_writes_the_global_settings(sapp, users):
    with sapp.app_context():
        get_db().execute('INSERT INTO settings (user_id, "key", value) VALUES (NULL, ?, ?)',
                         ('plat_per_hour', '1'))
        get_db().commit()
        run_import(users['bob'], {'settings.csv': 'key,value\nplat_per_hour,45\n'})
        rows = get_db().execute('SELECT user_id, value FROM settings WHERE "key" = ?',
                                ('plat_per_hour',)).fetchall()
    assert sorted((tuple(r) for r in rows), key=str) == sorted([
        (None, '1'), (users['alice'], '30'), (users['bob'], '45'), (users['carol'], '777')],
        key=str)


def test_a_rank_above_the_items_max_is_clamped(sapp, users):
    bob = users['bob']
    with sapp.app_context():
        run_import(bob, {'arcane.csv': 'name,owned_rank,target_rank\n'
                                       'Arcane Energize,9,5\nArcane New,3,\n'})
        view = read_user(bob)['progress']
    assert view[('Arcane Energize', 'arcane')][5:] == (5, 5)
    assert view[('Arcane New', 'arcane')][5:] == (0, None)  # no max rank known: 0


@pytest.mark.parametrize('failing_table', ['user_progress', 'farming_goals',
                                           'farming_progress', 'farming_checklist'])
def test_a_failed_write_mid_import_writes_nothing(sapp, users, monkeypatch, failing_table):
    real, calls = import_writer._upsert, []

    def fails_on_the_second_row_of_the_table(db, table, *args, **kwargs):
        if table == failing_table:
            calls.append(table)
            if len(calls) == 2:
                raise RuntimeError('disk full')
        return real(db, table, *args, **kwargs)
    monkeypatch.setattr(import_writer, '_upsert', fails_on_the_second_row_of_the_table)
    with sapp.app_context():
        data = export_user_data(users['alice'])
        before = snapshot()
        with pytest.raises(ImportRefused, match='Import failed, nothing written'):
            import_user_data(users['bob'], io.BytesIO(data))
        assert snapshot() == before
    assert len(calls) == 2


def test_the_same_zip_imported_twice_gives_the_same_state(sapp, users):
    with sapp.app_context():
        data = export_user_data(users['alice'])
        import_user_data(users['bob'], io.BytesIO(data))
        once = snapshot(drop_updated_at=True)
        import_user_data(users['bob'], io.BytesIO(data))
        assert snapshot(drop_updated_at=True) == once


# ── Component state ─────────────────────────────────────────────────────────

PROGRESS_HEAD = 'name,category,row_type,row_key,acquired'


@pytest.mark.parametrize('file_text, key, expected', [
    # comp_state present decides, acquired follows it whatever the file's cell says
    (PROGRESS_HEAD + ',comp_state\nExcalibur,warframe,component,Blueprint,0,crafted\n',
     'Blueprint', (1, 0, 'crafted')),
    (PROGRESS_HEAD + ',comp_state\nExcalibur,warframe,component,Neuroptics,1,\n',
     'Neuroptics', (0, 0, None)),
    (PROGRESS_HEAD + ',comp_state\nExcalibur,warframe,component,Neuroptics,1,blueprint\n',
     'Neuroptics', (0, 0, 'blueprint')),
    (PROGRESS_HEAD + ',comp_state\nExcalibur,warframe,component,Blueprint,1,pending\n',
     'Blueprint', (0, 0, None)),
    # comp_state absent: acquired 0 never downgrades a blueprint; 1 means crafted
    (PROGRESS_HEAD + '\nExcalibur,warframe,component,Blueprint,0\n', 'Blueprint',
     (0, 0, 'blueprint')),
    (PROGRESS_HEAD + '\nExcalibur,warframe,component,Blueprint,1\n', 'Blueprint',
     (1, 0, 'crafted')),
    (PROGRESS_HEAD + '\nExcalibur,warframe,component,Neuroptics,0\n', 'Neuroptics',
     (0, 0, None)),
    (PROGRESS_HEAD + '\nExcalibur,warframe,component,Systems,1\n', 'Systems',
     (1, 0, 'crafted')),
    # both absent: untouched on an existing row, pending on a created one
    ('name,category,row_type,row_key\nExcalibur,warframe,component,Blueprint\n', 'Blueprint',
     (0, 0, 'blueprint')),
    ('name,category,row_type,row_key\nExcalibur,warframe,component,Neuroptics\n',
     'Neuroptics', (1, 0, 'crafted')),
    ('name,category,row_type,row_key\nExcalibur,warframe,component,Systems\n', 'Systems',
     (0, 0, None)),
    # an ingredient row keeps no state
    (PROGRESS_HEAD + ',quantity_owned,comp_state\nExcalibur,warframe,ingredient,Morphics,1,7,\n',
     'Morphics', (1, 7, None)),
])
def test_component_state_follows_the_state_rules(sapp, users, file_text, key, expected):
    alice = users['alice']
    with sapp.app_context():
        run_import(alice, {'farming_progress.csv': file_text})
        rows = read_user(alice)['goal_rows']
    row_type = 'ingredient' if key == 'Morphics' else 'component'
    assert rows[('Excalibur', 'warframe', row_type, key)] == expected


def test_progress_for_a_goal_only_among_the_users_goals_is_accepted(sapp, users):
    alice = users['alice']
    with sapp.app_context():
        run_import(alice, {'farming_progress.csv': csv_text(
            PROGRESS_HEAD.split(',') + ['comp_state'],
            [WEIRD, 'melee', 'component', 'Blade', '1', 'crafted'])})
        rows = read_user(alice)['goal_rows']
    assert rows[(WEIRD, 'melee', 'component', 'Blade')] == (1, 0, 'crafted')


def test_progress_for_a_goal_created_by_the_same_import_is_accepted(sapp, users):
    bob = users['bob']
    with sapp.app_context():
        run_import(bob, {
            'farming_goals.csv': 'name,category\nLex,secondary\n',
            'farming_progress.csv': PROGRESS_HEAD + ',comp_state\n'
                                    'Lex,secondary,component,Barrel,0,blueprint\n'})
        view = read_user(bob)
    assert view['goal_rows'] == {('Lex', 'secondary', 'component', 'Barrel'):
                                 (0, 0, 'blueprint')}


# ── Interface ───────────────────────────────────────────────────────────────

def post_import(client, data: bytes):
    return client.post('/settings/import', data={'import_file': (io.BytesIO(data), 'x.zip')},
                       content_type='multipart/form-data', follow_redirects=True)


def test_the_export_route_downloads_the_callers_zip(admin_client):
    response = admin_client.get('/settings/export')
    assert response.status_code == 200
    assert response.mimetype == 'application/zip'
    assert 'attachment; filename=warframe_progress_' in response.headers['Content-Disposition']
    assert set(read_zip(response.data)) == EXPORT_FILES


def test_the_import_route_reports_success_and_refusal(app, admin_client):
    ok = make_zip({'warframe.csv': 'name,mastered\nNew Frame,1\nOther Frame,0\n',
                   'watchlist.csv': 'name,category\nNew Frame,warframe\n',
                   'settings.csv': 'key,value\nplat_per_hour,5\nfoo,1\n'}).getvalue()
    page = post_import(admin_client, ok).get_data(as_text=True)
    assert ('Import complete: 2 items, 0 goals, 0 goal progress rows, 0 checklist rows, '
            '1 watchlist entries, 1 settings. 1 unknown setting key ignored.' in page)
    farming = make_zip({'farming_goals.csv': 'name,category\nNew Frame,warframe\n',
                        'farming_checklist.csv': 'name,category,component_name\n'
                                                 'New Frame,warframe,Systems\n',
                        'settings.csv': 'key,value\nfoo,1\nbar,2\n'}).getvalue()
    page = post_import(admin_client, farming).get_data(as_text=True)
    assert ('Import complete: 0 items, 1 goals, 0 goal progress rows, 1 checklist rows, '
            '0 watchlist entries, 0 settings. 2 unknown setting keys ignored.' in page)
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
    for words in ('notes and arcane ranks', 'your farming goals with their component and '
                  'resource progress, the farming checklist, your market watchlist and your '
                  'personal settings.'):
        assert words in page
    assert 'not exported yet' not in page


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
