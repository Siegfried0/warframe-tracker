"""Where relic drop sources are shown: a part's relics and each relic's best locations in the
farming plan and in the collection's drop modal, a relic without a location shown as dropping
from no mission now.

Every test reads back rows it seeded itself, as the drop sync writes them; nothing is fetched.
"""
from pathlib import Path

import pytest

from models.db import get_db
from models.item import Item
from services import farming_service as fs
from services import relic_sources

LONG_RELIC = 'Lith ' + 'Q' * 180 + ' Relic'
LONG_NODE = 'N' * 200 + ' — Earth'

# (item, part, relic, Intact chance)
RELIC_ROWS = [
    ('Ash Prime', 'Neuroptics', 'Lith A1 Relic', 25.33),
    ('Ash Prime', 'Neuroptics', 'Axi V1 Relic', 2.0),       # vaulted: no location
    ('Ash Prime', 'Blueprint', 'Lith A1 Relic', 2.0),       # a part only relics hold
    ('Ash Prime', 'Blueprint', LONG_RELIC, 11.0),
    ('Ash Prime', 'Systems', 'Meso S2 Relic', 11.0),        # a part with WFCD drops too
]

# (relic, location, mission type, rotation, chance, avg runs)
LOCATIONS = [
    ('Lith A1 Relic', 'Mantle — Earth', 'Survival', 'A', 14.29, 7.0),
    ('Lith A1 Relic', 'Mantle — Earth', 'Survival', 'B', 7.14, 14.0),
    ('Lith A1 Relic', 'Kiliken — Venus', 'Excavation', None, 20.0, 5.0),
    ('Lith A1 Relic', 'Tessera — Venus', 'Defense', 'C', 4.0, 25.0),   # fourth best: not shown
    (LONG_RELIC, LONG_NODE, 'Survival', 'A', 10.0, 10.0),
    ('Meso S2 Relic', 'Io — Jupiter', 'Defense', 'B', 12.5, 8.0),
]


def _seed(db):
    for name, category in [('Ash Prime', 'warframe'), ('Braton Prime', 'primary'),
                           ('Ferrite', 'resource')]:
        Item.upsert(name, category, source='standard')
    ids = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM items')}
    for item, part, relic, chance in RELIC_ROWS:
        _relic_row(db, ids[item], part, relic, chance)
    db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs, "
               "source_type, notes) VALUES (?, 'Survival', 'Mantle — Earth', 5, 20, 'component', "
               "'Systems')", (ids['Ash Prime'],))
    for row in LOCATIONS:
        _location(db, *row)
    db.execute("INSERT INTO component_ingredients (item_id, component_name, ingredient_name, "
               "ingredient_qty) VALUES (?, 'Neuroptics', 'Ferrite', 100)", (ids['Ash Prime'],))
    db.commit()
    return ids


def _relic_row(db, item_id, part, relic, chance):
    db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, "
               "avg_runs, source_type, notes) VALUES (?, 'Relic', ?, ?, NULL, 'relic', ?)",
               (item_id, relic, chance, part))


def _location(db, relic, location, mission_type, rotation, chance, runs):
    db.execute('INSERT INTO relic_drops (relic_name, location, mission_type, rotation, '
               'drop_chance, avg_runs) VALUES (?, ?, ?, ?, ?, ?)',
               (relic, location, mission_type, rotation, chance, runs))


@pytest.fixture
def seeded(app, admin_client):
    with app.app_context():
        db = get_db()
        ids = _seed(db)
        admin = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
        yield db, ids, admin


def _where(relic):
    return [(loc['location'], loc['rotation']) for loc in relic['locations']]


def _parts(ids, admin, item='Ash Prime'):
    return {c['component_name']: c for c in fs.get_item_plan(ids[item], admin)['components']}


# ── The farming plan ─────────────────────────────────────────────────────────

def test_the_plan_shows_each_parts_relics_and_their_best_three_locations(seeded):
    db, ids, admin = seeded
    parts = _parts(ids, admin)
    assert sorted(parts) == ['Blueprint', 'Neuroptics', 'Systems']

    lith, axi = parts['Neuroptics']['relics']
    assert (lith['relic'], lith['drop_chance'], lith['vaulted']) == ('Lith A1 Relic', 25.33, False)
    # four locations, the three needing the fewest runs shown
    assert _where(lith) == [('Kiliken — Venus', None), ('Mantle — Earth', 'A'),
                            ('Mantle — Earth', 'B')]
    assert [loc['avg_runs'] for loc in lith['locations']] == [5.0, 7.0, 14.0]
    assert (axi['relic'], axi['vaulted'], axi['locations']) == ('Axi V1 Relic', True, [])
    # a relic is no drop row of the part
    assert parts['Neuroptics']['drops'] == []


def test_a_part_with_wfcd_drops_and_relics_shows_both_apart(seeded):
    db, ids, admin = seeded
    systems = _parts(ids, admin)['Systems']
    assert [(d['location'], d['avg_runs']) for d in systems['drops']] == [('Mantle — Earth', 20)]
    assert [(r['relic'], _where(r)) for r in systems['relics']] == [
        ('Meso S2 Relic', [('Io — Jupiter', 'B')])]


def test_a_part_only_relics_hold_is_in_the_plan_with_the_long_names_whole(seeded):
    db, ids, admin = seeded
    first, second = _parts(ids, admin)['Blueprint']['relics']
    # the relic that drops more easily comes first
    assert (first['relic'], second['relic']) == ('Lith A1 Relic', LONG_RELIC)
    assert _where(second) == [(LONG_NODE, 'A')]


def test_a_part_in_many_relics_lists_every_relic_those_dropping_somewhere_first(seeded):
    db, ids, admin = seeded
    for n in range(12):
        _relic_row(db, ids['Ash Prime'], 'Chassis', f'Neo C{n} Relic', 2)
    _location(db, 'Neo C7 Relic', 'Mantle — Earth', 'Survival', 'C', 5, 20)
    db.commit()
    relics = relic_sources.relics_by_part(ids['Ash Prime'])['Chassis']
    assert len(relics) == 12
    assert relics[0]['relic'] == 'Neo C7 Relic' and not relics[0]['vaulted']
    assert all(r['vaulted'] for r in relics[1:])
    # the vaulted ones by name, so the order is the same on every read
    assert [r['relic'] for r in relics[1:]] == sorted(
        (f'Neo C{n} Relic' for n in range(12) if n != 7), key=str.casefold)


def test_relics_with_the_same_easiest_location_come_by_the_higher_chance(seeded):
    db, ids, admin = seeded
    for relic, chance in [('Neo Z1 Relic', 2.0), ('Neo Z2 Relic', 25.33), ('Neo Z3 Relic', 11.0)]:
        _relic_row(db, ids['Ash Prime'], 'Chassis', relic, chance)
        _location(db, relic, 'Io — Jupiter', 'Defense', 'A', 10, 10)
    db.commit()
    assert [r['relic'] for r in _parts(ids, admin)['Chassis']['relics']] == [
        'Neo Z2 Relic', 'Neo Z3 Relic', 'Neo Z1 Relic']


@pytest.mark.parametrize('spelling', [' neo s1 RELIC', 'Neo  S1   Relic', 'NEO S1 RELIC'])
def test_a_component_row_naming_a_relic_lists_it_as_a_relic_with_its_locations(seeded, spelling):
    # WFCD names a relic as a part's location; spelt in another case or with other blanks
    # it is the same relic, so its locations are found.
    db, ids, admin = seeded
    db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs, "
               "source_type, notes) VALUES (?, 'Relic', ?, 11, 9.1, 'component', 'Chassis')",
               (ids['Ash Prime'], spelling))
    _location(db, 'Neo S1 Relic', 'Io — Jupiter', 'Defense', 'A', 10, 10)
    db.commit()
    chassis = _parts(ids, admin)['Chassis']
    assert chassis['drops'] == []
    [relic] = chassis['relics']
    assert (relic['relic'], relic['drop_chance'], relic['vaulted']) == (' '.join(spelling.split()),
                                                                        11, False)
    assert _where(relic) == [('Io — Jupiter', 'A')]


def test_a_relic_named_by_a_relic_row_and_a_component_row_is_listed_once(seeded):
    db, ids, admin = seeded
    db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs, "
               "source_type, notes) VALUES (?, 'Relic', 'lith a1 relic', 50, 2, 'component', "
               "'neuroptics')", (ids['Ash Prime'],))
    db.commit()
    relics = _parts(ids, admin)['Neuroptics']['relics']
    assert [(r['relic'], r['drop_chance']) for r in relics] == [
        ('Lith A1 Relic', 25.33), ('Axi V1 Relic', 2.0)]


def test_the_goal_plan_carries_the_relics(seeded, admin_client):
    db, ids, admin = seeded
    db.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)', (admin, ids['Ash Prime']))
    db.commit()
    goal = db.execute('SELECT id FROM farming_goals').fetchone()[0]
    plan = admin_client.get(f'/farming/goals/{goal}/plan').get_json()['plan']
    parts = {c['component_name']: c for c in plan['components']}
    assert [r['relic'] for r in parts['Neuroptics']['relics']] == ['Lith A1 Relic', 'Axi V1 Relic']
    assert parts['Neuroptics']['relics'][1]['vaulted'] is True


def test_an_item_without_relics_has_none_in_its_plan(seeded):
    db, ids, admin = seeded
    assert fs.get_item_plan(ids['Braton Prime'], admin)['components'] == []


# ── Names as stored: special characters, non-ASCII, long (axis 1) ────────────

NAMES = [
    ('html', 'Lith <b>&"\' Relic', '<img src=x onerror=1> — Earth'),
    ('non-ascii', 'Lith Ä1 Rélic', 'Ōrokin Ŧower — Lua'),
    ('long', 'Axi ' + 'W' * 300 + ' Relic', 'M' * 400 + ' — Mars'),
]


@pytest.mark.parametrize('relic, node', [n[1:] for n in NAMES], ids=[n[0] for n in NAMES])
def test_relic_and_node_names_come_back_unchanged(seeded, admin_client, relic, node):
    db, ids, admin = seeded
    _relic_row(db, ids['Braton Prime'], 'Barrel', relic, 7.5)
    _location(db, relic, node, 'Survival', 'A', 10, 10)
    db.commit()
    [got] = _parts(ids, admin, 'Braton Prime')['Barrel']['relics']
    assert (got['relic'], _where(got)) == (relic, [(node, 'A')])
    body = admin_client.get(f'/collection/drops/{ids["Braton Prime"]}').get_json()
    assert [(r['relic'], _where(r)) for r in body['relics']] == [(relic, [(node, 'A')])]


def test_the_pages_escape_every_name_they_draw_from_the_plan_and_the_modal(admin_client):
    farming = admin_client.get('/farming/').get_data(as_text=True)
    for drawn in ('escText(relic.relic)', "escText((loc.mission_type || '') + ' — ' + "
                  "(loc.location || ''))", 'escText(loc.rotation)',
                  'escText(comp.component_name)',
                  "escText((drop.mission_type || '') + ' — ' + (drop.location || ''))"):
        assert drawn in farming
    collection = admin_client.get('/collection/warframe').get_data(as_text=True)
    for drawn in ('escHtml(rl.part', 'escHtml(rl.relic', 'escHtml(loc.rotation)',
                  "escHtml((loc.mission_type || '') + ' — ' + (loc.location || ''))",
                  'escHtml(d.location', 'escHtml(d.mission_type'):
        assert drawn in collection


# ── The collection's drop modal ──────────────────────────────────────────────

def test_the_drop_modal_lists_the_relics_beside_the_other_drops(seeded, admin_client):
    db, ids, admin = seeded
    body = admin_client.get(f'/collection/drops/{ids["Ash Prime"]}').get_json()
    assert body['ok'] is True and body['item_name'] == 'Ash Prime'
    assert [d['location'] for d in body['drops']] == ['Mantle — Earth']
    listed = [(r['part'], r['relic'], r['vaulted'], len(r['locations'])) for r in body['relics']]
    assert listed == [('Blueprint', 'Lith A1 Relic', False, 3), ('Blueprint', LONG_RELIC, False, 1),
                      ('Neuroptics', 'Lith A1 Relic', False, 3),
                      ('Neuroptics', 'Axi V1 Relic', True, 0),
                      ('Systems', 'Meso S2 Relic', False, 1)]


def test_the_drop_modal_leaves_a_component_row_naming_a_relic_out_of_its_drops(seeded,
                                                                               admin_client):
    db, ids, admin = seeded
    db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs, "
               "source_type, notes) VALUES (?, 'Relic', 'Neo S1 Relic', 90, 1.1, 'component', "
               "'Chassis')", (ids['Ash Prime'],))
    db.commit()
    body = admin_client.get(f'/collection/drops/{ids["Ash Prime"]}').get_json()
    assert [d['location'] for d in body['drops']] == ['Mantle — Earth']
    assert ('Chassis', 'Neo S1 Relic', True) in [(r['part'], r['relic'], r['vaulted'])
                                                 for r in body['relics']]


def test_the_drop_modal_of_an_item_without_drops_is_empty(seeded, admin_client):
    db, ids, admin = seeded
    body = admin_client.get(f'/collection/drops/{ids["Braton Prime"]}').get_json()
    assert (body['ok'], body['drops'], body['relics']) == (True, [], [])


def test_the_drop_modal_shows_at_most_twenty_other_drops(seeded, admin_client):
    db, ids, admin = seeded
    for n in range(25):
        db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, "
                   "avg_runs, source_type) VALUES (?, 'Survival', ?, ?, 1, 'mission')",
                   (ids['Braton Prime'], f'Node {n} — Earth', n + 1))
    db.commit()
    body = admin_client.get(f'/collection/drops/{ids["Braton Prime"]}').get_json()
    assert [d['drop_chance'] for d in body['drops']] == list(range(25, 5, -1))


def test_the_drop_modal_of_an_unknown_item_is_not_found(seeded, admin_client):
    assert admin_client.get('/collection/drops/999999').status_code == 404


def test_the_collection_offers_the_drop_modal_for_items_built_from_parts(seeded, admin_client):
    page = admin_client.get('/collection/warframe').get_data(as_text=True)
    assert 'class="wf-farm-it' in page and 'class="wf-plan-it' in page
    assert 'r.relics' in page and 'Vaulted: drops from no mission now' in page


def test_the_farming_page_draws_a_relic_and_a_vaulted_relic(admin_client):
    page = admin_client.get('/farming/').get_data(as_text=True)
    assert 'function relicBadge(relic)' in page and 'relicBadge(comp.relics[0])' in page
    assert 'vaulted: drops from no mission now' in page


# ── Many relics: the reads are chunked (axis 9) ──────────────────────────────

def test_more_relics_than_one_query_takes_are_all_read(seeded, monkeypatch):
    db, ids, admin = seeded
    monkeypatch.setattr(relic_sources, '_CHUNK', 2)
    parts = _parts(ids, admin)
    assert [len(r['locations']) for r in parts['Blueprint']['relics']] == [3, 1]
    assert [r['vaulted'] for r in parts['Neuroptics']['relics']] == [False, True]


# ── Portable SQL; the route holds no SQL ─────────────────────────────────────

def test_the_relic_reads_use_no_sqlite_only_sql():
    source = (Path(__file__).resolve().parent.parent / 'app' / 'services' /
              'relic_sources.py').read_text(encoding='utf-8')
    for sqlite_only in ('LIMIT ', "datetime('now')", 'ON CONFLICT', 'INSERT OR ', 'executescript'):
        assert sqlite_only not in source


def test_the_drop_route_holds_no_sql():
    source = (Path(__file__).resolve().parent.parent / 'app' / 'routes' /
              'collection.py').read_text(encoding='utf-8')
    assert 'get_db' not in source and 'SELECT' not in source
