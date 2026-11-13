"""Resource rows grant no mastery, and the export still carries a user's progress on them.

Oracles: the game's rule that a resource is no mastery item (0 mastery points, never on the
mastery track), so the resource sync writes every row with ``grants_mastery`` 0 and the
dashboard's mastery summary and "level these now" list leave resources out; the export /
import format, unchanged, so ``resource.csv`` keeps every resource row with the user's
progress and an export written while resource rows still carried the flag imports and
round-trips; and the dashboard's "N / M items mastered", whose M counts what N can count:
mastery items only. The sync's only source, WFCD, is replaced by fixed data (no network).
"""
import csv
import io

import pytest

from models.db import get_db
from models.item import Item
from models.progress import Progress
from models.user import User
from services import wiki_sync_gear, wiki_sync_mods, wiki_sync_requests
from services.export_service import export_user_data
from services.import_service import import_user_data
from transfer_fixtures import make_zip, read_zip

RESOURCES_JSON = [{'name': 'Ferrite'}, {'name': 'Alloy Plate'}, {'name': 'Morphics'}]


def _fixed_wfcd(monkeypatch, data=None):
    files = {'Resources.json': RESOURCES_JSON if data is None else data}
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd',
                        lambda name: [dict(i) for i in files.get(name, [])])


def _flag(name, category='resource'):
    return get_db().execute('SELECT grants_mastery FROM items WHERE name = ? AND category = ?',
                            (name, category)).fetchone()[0]


def _id(name, category):
    return get_db().execute('SELECT id FROM items WHERE name = ? AND category = ?',
                            (name, category)).fetchone()[0]


def _progress(uid, name, category, **fields):
    cols = ['user_id', 'item_id', *fields]
    marks = ', '.join('?' for _ in cols)
    get_db().execute(f'INSERT INTO user_progress ({", ".join(cols)}) VALUES ({marks})',
                     [uid, _id(name, category), *fields.values()])
    get_db().commit()


def _seed_before_the_fix(uid):
    """A catalogue as a sync before the fix left it: the resource rows carry
    grants_mastery 1 (Item.upsert's default). The user mastered Excalibur and Ferrite,
    and holds Braton and Alloy Plate unmastered."""
    Item.upsert('Excalibur', 'warframe', source='standard')
    Item.upsert('Braton', 'primary', source='standard')
    Item.upsert('Serration', 'mod', source='standard', grants_mastery=False, mod_max_rank=10)
    Item.upsert('Arcane Energize', 'arcane', source='standard', grants_mastery=False,
                mod_max_rank=5)
    Item.upsert('Ferrite', 'resource', source='standard')
    Item.upsert('Alloy Plate', 'resource', source='standard')
    _progress(uid, 'Excalibur', 'warframe', in_possession=1, mastered=1)
    _progress(uid, 'Braton', 'primary', in_possession=1)
    _progress(uid, 'Serration', 'mod', in_possession=1, owned_rank=4)
    _progress(uid, 'Arcane Energize', 'arcane', in_possession=1, owned_rank=2, target_rank=5)
    _progress(uid, 'Ferrite', 'resource', in_possession=1, mastered=1, note='farm, "lots"')
    _progress(uid, 'Alloy Plate', 'resource', in_possession=1)


def _rows(data, filename):
    """{name: row dict} of one CSV file of an export ZIP."""
    return {r['name']: r for r in csv.DictReader(io.StringIO(read_zip(data)[filename]))}


@pytest.fixture
def synced(app, monkeypatch):
    """(user id, export written before the sync) after one resource sync over the old
    catalogue; runs inside an app context."""
    _fixed_wfcd(monkeypatch)
    with app.app_context():
        uid = User.create('resource-user', password='pw-resource-1').id
        _seed_before_the_fix(uid)
        assert (_flag('Ferrite'), _flag('Alloy Plate')) == (1, 1)
        before = export_user_data(uid)
        wiki_sync_mods._sync_resources()
        yield uid, before


# ── Write path: the resource sync's upsert ─────────────────────────────────────

@pytest.mark.parametrize('name', ['Ferrite', 'Alloy Plate', 'Morphics'])
def test_the_resource_sync_writes_every_row_with_grants_mastery_0(synced, name):
    assert _flag(name) == 0


def test_a_stored_resource_row_keeps_its_id_and_its_progress_through_the_sync(app, monkeypatch):
    _fixed_wfcd(monkeypatch)
    with app.app_context():
        uid = User.create('id-user', password='pw-id-user-1').id
        _seed_before_the_fix(uid)
        before = _id('Ferrite', 'resource')
        wiki_sync_mods._sync_resources()
        assert _id('Ferrite', 'resource') == before
        row = Progress.get_for_user(uid, before)
    assert (row['in_possession'], row['mastered'], row['note']) == (1, 1, 'farm, "lots"')


@pytest.mark.parametrize('name,category', [('Serration', 'mod'),
                                           ('Arcane Energize', 'arcane'),
                                           ('Excalibur', 'warframe')])
def test_the_resource_sync_leaves_other_categories_flags_alone(synced, name, category):
    assert _flag(name, category) == (1 if category == 'warframe' else 0)


# ── Interface: the four readers over the resource rows ───────────────────────

def test_the_mastery_summary_does_not_count_a_mastered_resource(synced):
    uid, _ = synced
    summary = Progress.get_mastery_summary(uid)
    assert summary['by_category'] == {'warframe': 1}
    assert summary['total_mastered'] == 1


def test_the_immediate_gains_do_not_list_a_possessed_unmastered_resource(synced):
    uid, _ = synced
    assert [r['name'] for r in Progress.get_immediate_gains(uid)] == ['Braton']


def test_the_export_keeps_every_resource_row_with_its_progress(synced):
    uid, _ = synced
    rows = _rows(export_user_data(uid), 'resource.csv')
    assert sorted(rows) == ['Alloy Plate', 'Ferrite', 'Morphics']
    ferrite, alloy = rows['Ferrite'], rows['Alloy Plate']
    assert (ferrite['in_possession'], ferrite['mastered'], ferrite['note']) == \
        ('1', '1', 'farm, "lots"')
    assert (alloy['in_possession'], alloy['mastered']) == ('1', '0')


def test_an_export_from_before_the_fix_imports_and_round_trips(synced):
    _, before = synced
    old = _rows(before, 'resource.csv')
    assert sorted(old) == ['Alloy Plate', 'Ferrite']
    bob = User.create('resource-bob', password='pw-resource-2').id
    import_user_data(bob, io.BytesIO(before))
    again = _rows(export_user_data(bob), 'resource.csv')
    for name in old:
        assert again[name] == old[name], name


def test_a_resource_csv_alone_imports_into_the_fixed_catalogue(synced):
    _, before = synced
    bob = User.create('resource-carl', password='pw-resource-3').id
    import_user_data(bob, make_zip({'resource.csv': read_zip(before)['resource.csv']}))
    row = Progress.get_for_user(bob, _id('Ferrite', 'resource'))
    assert (row['in_possession'], row['mastered'], row['note']) == (1, 1, 'farm, "lots"')


@pytest.mark.parametrize('filename,name,fields', [
    ('mod.csv', 'Serration', {'in_possession': '1', 'owned_rank': '4'}),
    ('arcane.csv', 'Arcane Energize', {'in_possession': '1', 'owned_rank': '2',
                                       'target_rank': '5'}),
])
def test_the_export_of_mods_and_arcanes_is_unchanged(synced, filename, name, fields):
    uid, before = synced
    after = _rows(export_user_data(uid), filename)
    assert after == _rows(before, filename)
    assert {k: after[name][k] for k in fields} == fields


def test_a_k_drive_part_moved_to_resource_with_progress_is_in_the_export(app, monkeypatch):
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd', lambda name: [])
    monkeypatch.setattr(wiki_sync_requests, '_wiki_category_members', lambda name: [])
    with app.app_context():
        uid = User.create('kdrive-user', password='pw-kdrive-1').id
        Item.upsert('Arc Twelve', 'k-drive', source='standard')
        _progress(uid, 'Arc Twelve', 'k-drive', in_possession=1, note='reactor')
        wiki_sync_gear._sync_k_drives()
        assert _flag('Arc Twelve') == 0
        rows = _rows(export_user_data(uid), 'resource.csv')
    assert (rows['Arc Twelve']['in_possession'], rows['Arc Twelve']['note']) == ('1', 'reactor')


# ── The summary's total counts mastery items only ─────────────────────────────

def test_the_summary_total_counts_only_mastery_items(synced):
    uid, _ = synced
    # Excalibur and Braton grant mastery; Serration, Arcane Energize and the three
    # resource rows do not.
    assert Progress.get_mastery_summary(uid)['total_items'] == 2


def test_the_dashboard_shows_mastered_over_mastery_items(admin_client, app, monkeypatch):
    _fixed_wfcd(monkeypatch, [{'name': 'Ferrite'}])
    with app.app_context():
        uid = User.get_by_username('admin').id
        Item.upsert('Excalibur', 'warframe', source='standard')
        Item.upsert('Serration', 'mod', source='standard', grants_mastery=False)
        Item.upsert('Arcane Energize', 'arcane', source='standard', grants_mastery=False)
        Item.upsert('Ferrite', 'resource', source='standard')
        _progress(uid, 'Excalibur', 'warframe', in_possession=1, mastered=1)
        _progress(uid, 'Ferrite', 'resource', in_possession=1, mastered=1)
        wiki_sync_mods._sync_resources()
        summary = Progress.get_mastery_summary(uid)
    assert (summary['total_mastered'], summary['total_items']) == (1, 1)
    page = admin_client.get('/dashboard').get_data(as_text=True)
    assert 'Overall completion: 1 / 1 items mastered' in page
    assert 'var totalItemsAll = 1;' in page
