"""The relic pass of the drop sync: relic contents (the ``relics`` section of the drop tables)
become ``relic`` rows of ``drop_sources``, relic locations (the relic entries of
``missionRewards``) become ``relic_drops`` rows — rebuilt on every sync, one row per (item,
part, relic) whichever source lists it, and nothing at all when the tables cannot be read.

The drop tables and the WFCD files are hand-written, minimal copies of their shapes; the
drop-table request is mocked at ``requests.get`` and the WFCD files at ``_fetch_wfcd``, so no
test reaches the network.
"""
import copy
import json

import pytest
import requests

from models.db import get_db
from models.item import Item
from services import relic_sync, wiki_sync

CATALOGUE = [
    ('Ash', 'warframe'),
    ('Gyre', 'warframe'),           # no Gyre Prime in the catalogue
    ('Ash Prime', 'warframe'),
    ('Braton Prime', 'primary'),
    ('Ōmen Prime', 'warframe'),     # non-ASCII, listed upper-cased in a relic
    ('Forma', 'resource'),          # a relic's Forma Blueprint is no part of an item
]

WFCD = {
    'Warframes.json': [{
        'name': 'Ash Prime', 'masterable': True,
        'components': [
            # the same part, relic and item as the relics section: stored once
            {'name': 'Neuroptics', 'drops': [
                {'location': 'Lith A1 Relic', 'type': 'Ash Prime Neuroptics Blueprint',
                 'chance': 0.2533}]},
            # a relic the relics section does not name for this part: kept
            {'name': 'Systems', 'drops': [
                {'location': 'Neo S1 Relic', 'type': 'Ash Prime Systems Blueprint',
                 'chance': 0.11}]},
        ],
    }],
}

TABLES = {
    'relics': [
        {'tier': 'Lith', 'relicName': 'A1', 'state': 'Intact', 'rewards': [
            {'itemName': 'Ash Prime Neuroptics Blueprint', 'rarity': 'Common', 'chance': 25.33},
            {'itemName': 'Forma Blueprint', 'rarity': 'Common', 'chance': 25.33},
            {'itemName': 'Braton Prime Receiver', 'rarity': 'Uncommon', 'chance': 11},
            {'itemName': 'Ash Prime Blueprint', 'rarity': 'Rare', 'chance': 2},
        ]},
        # the same relic in another state: its chances are not read
        {'tier': 'Lith', 'relicName': 'A1', 'state': 'Radiant', 'rewards': [
            {'itemName': 'Ash Prime Neuroptics Blueprint', 'rarity': 'Common', 'chance': 16.67},
            {'itemName': 'Ash Prime Chassis Blueprint', 'rarity': 'Common', 'chance': 16.67},
        ]},
        # vaulted: no mission location
        {'tier': 'Axi', 'relicName': 'V1', 'state': 'Intact', 'rewards': [
            {'itemName': 'Ash Prime Neuroptics Blueprint', 'rarity': 'Rare', 'chance': 2},
        ]},
        # a relic name already ending in "Relic"; odd spellings; the item and its set
        {'tier': 'Meso', 'relicName': 'B2 Relic', 'state': ' intact ', 'rewards': [
            {'itemName': '  ash prime   SYSTEMS blueprint ', 'rarity': 'Uncommon', 'chance': 11},
            {'itemName': 'ŌMEN PRIME Chassis Blueprint', 'rarity': 'Uncommon', 'chance': 11},
            {'itemName': 'Braton Prime Set', 'rarity': 'Rare', 'chance': 5},
            {'itemName': 'Ash Prime', 'rarity': 'Rare', 'chance': 3},
        ]},
    ],
    'missionRewards': {
        'Earth': {
            'Mantle': {'gameMode': 'Survival', 'rewards': {
                'A': [{'itemName': 'Lith A1 Relic', 'rarity': 'Common', 'chance': 14.29},
                      {'itemName': ' lith  a1 RELIC ', 'rarity': 'Common', 'chance': 10}],
                'B': [{'itemName': 'Lith A1 Relic', 'rarity': 'Common', 'chance': 7.14}],
                'C': [{'itemName': 'Meso B2 Relic', 'rarity': 'Rare', 'chance': 5}],
            }},
        },
        'Venus': {
            'Kiliken': {'gameMode': 'Excavation', 'rewards': [
                {'itemName': 'Lith A1 Relic', 'rarity': 'Common', 'chance': 20},
                {'itemName': 'Forma Blueprint', 'rarity': 'Common', 'chance': 20},
            ]},
        },
    },
}

# (item, part, relic, Intact chance) of every relic row a good sync writes
RELIC_ROWS = {
    ('Ash Prime', 'Neuroptics', 'Lith A1 Relic', 25.33),
    ('Ash Prime', 'Blueprint', 'Lith A1 Relic', 2.0),
    ('Ash Prime', 'Neuroptics', 'Axi V1 Relic', 2.0),
    ('Ash Prime', 'Systems', 'Meso B2 Relic', 11.0),
    ('Braton Prime', 'Receiver', 'Lith A1 Relic', 11.0),
    ('Ōmen Prime', 'Chassis', 'Meso B2 Relic', 11.0),
}

# (relic, location, mission type, rotation, chance, avg runs) of every relic_drops row
RELIC_LOCATIONS = {
    ('Lith A1 Relic', 'Mantle — Earth', 'Survival', 'A', 14.29, 7.0),
    ('Lith A1 Relic', 'Mantle — Earth', 'Survival', 'B', 7.14, 14.0),
    ('Lith A1 Relic', 'Kiliken — Venus', 'Excavation', None, 20.0, 5.0),
    ('Meso B2 Relic', 'Mantle — Earth', 'Survival', 'C', 5.0, 20.0),
}


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code, self._payload, self._text = status, payload, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error', response=self)

    def json(self):
        if self._text is not None:
            return json.loads(self._text)
        return self._payload


@pytest.fixture
def source(monkeypatch):
    """Serve `source.response` (a FakeResponse, or an exception to raise) for the drop
    tables and the WFCD files above; count the drop-table requests."""
    class Source:
        response = FakeResponse(payload=copy.deepcopy(TABLES))
        calls = 0

    def fake_get(url, timeout=None, **kwargs):
        assert url == wiki_sync._WARFRAMESTAT_DROPS
        Source.calls += 1
        if isinstance(Source.response, BaseException):
            raise Source.response
        return Source.response

    monkeypatch.setattr(requests, 'get', fake_get)
    monkeypatch.setattr(wiki_sync, '_fetch_wfcd', lambda name: copy.deepcopy(WFCD.get(name, [])))
    return Source


@pytest.fixture
def ctx(app):
    with app.app_context():
        for name, category in CATALOGUE:
            Item.upsert(name, category, source='standard')
        yield get_db()


def relic_rows(db):
    return {(r['name'], r['notes'], r['location'], r['drop_chance']) for r in db.execute(
        "SELECT i.name, d.notes, d.location, d.drop_chance FROM drop_sources d "
        "JOIN items i ON i.id = d.item_id WHERE d.source_type = 'relic'")}


def relic_locations(db):
    return {tuple(r) for r in db.execute(
        'SELECT relic_name, location, mission_type, rotation, drop_chance, avg_runs '
        'FROM relic_drops')}


def component_rows(db):
    return {(r['name'], r['notes'], r['location']) for r in db.execute(
        "SELECT i.name, d.notes, d.location FROM drop_sources d "
        "JOIN items i ON i.id = d.item_id WHERE d.source_type = 'component'")}


# ── A good read ──────────────────────────────────────────────────────────────

def test_each_part_gets_one_relic_row_per_relic_holding_it(ctx, source):
    wiki_sync._sync_drop_sources()
    assert relic_rows(ctx) == RELIC_ROWS
    assert source.calls == 1


def test_relic_drops_holds_each_relics_mission_locations(ctx, source):
    wiki_sync._sync_drop_sources()
    assert relic_locations(ctx) == RELIC_LOCATIONS


def test_a_relic_row_names_no_mission_and_no_average_runs(ctx, source):
    wiki_sync._sync_drop_sources()
    rows = ctx.execute("SELECT DISTINCT mission_type, avg_runs FROM drop_sources "
                       "WHERE source_type = 'relic'").fetchall()
    assert [tuple(r) for r in rows] == [('Relic', None)]


def test_a_wfcd_component_row_naming_the_same_relic_is_stored_once(ctx, source):
    wiki_sync._sync_drop_sources()
    assert component_rows(ctx) == {('Ash Prime', 'Systems', 'Neo S1 Relic')}
    neuroptics_at_lith = ctx.execute(
        "SELECT COUNT(*) FROM drop_sources WHERE notes = 'Neuroptics' "
        "AND location = 'Lith A1 Relic'").fetchone()[0]
    assert neuroptics_at_lith == 1


def test_mission_rewards_still_give_their_mission_rows(ctx, source):
    wiki_sync._sync_drop_sources()
    rows = ctx.execute("SELECT COUNT(*) FROM drop_sources WHERE source_type = 'mission'"
                       ).fetchone()[0]
    # Forma Blueprint at Kiliken resolves to the Forma resource, as before this pass
    assert rows == 1


# ── Repetition (axis 3) ──────────────────────────────────────────────────────

def test_a_second_sync_rebuilds_the_same_rows(ctx, source):
    wiki_sync._sync_drop_sources()
    first = (relic_rows(ctx), relic_locations(ctx), component_rows(ctx))
    wiki_sync._sync_drop_sources()
    assert (relic_rows(ctx), relic_locations(ctx), component_rows(ctx)) == first
    assert ctx.execute('SELECT COUNT(*) FROM relic_drops').fetchone()[0] == len(RELIC_LOCATIONS)


def test_the_same_relic_listed_twice_keeps_one_row_with_the_higher_chance(ctx, source):
    tables = copy.deepcopy(TABLES)
    tables['relics'].append({'tier': 'LITH', 'relicName': ' a1 ', 'state': 'Intact', 'rewards': [
        {'itemName': 'Braton Prime Receiver', 'chance': 12.5}]})
    source.response = FakeResponse(payload=tables)
    wiki_sync._sync_drop_sources()
    braton = {row for row in relic_rows(ctx) if row[0] == 'Braton Prime'}
    assert braton == {('Braton Prime', 'Receiver', 'Lith A1 Relic', 12.5)}


def test_one_part_in_many_relics_gets_a_row_for_each(ctx, source):
    tables = copy.deepcopy(TABLES)
    tables['relics'] = [
        {'tier': 'Neo', 'relicName': f'N{n}', 'state': 'Intact', 'rewards': [
            {'itemName': 'Braton Prime Barrel', 'chance': 2}]}
        for n in range(1, 41)]
    source.response = FakeResponse(payload=tables)
    wiki_sync._sync_drop_sources()
    assert len(relic_rows(ctx)) == 40


# ── Names (axis 1) ───────────────────────────────────────────────────────────

PARTS = [
    ('prime before plain', 'Ash Prime Neuroptics Blueprint', 'Ash Prime', 'Neuroptics'),
    ('plain item', 'Ash Neuroptics Blueprint', 'Ash', 'Neuroptics'),
    ('case and blanks', '  ash  prime  neuroptics BLUEPRINT ', 'Ash Prime', 'neuroptics'),
    ('main blueprint', 'Ash Prime Blueprint', 'Ash Prime', 'Blueprint'),
    ('weapon part', 'Braton Prime Receiver', 'Braton Prime', 'Receiver'),
    ('non-ASCII case', 'ŌMEN PRIME Chassis Blueprint', 'Ōmen Prime', 'Chassis'),
    ('the item itself', 'Ash Prime', None, None),
    ('the whole set', 'Ash Prime Set', None, None),
    ('no item', 'Forma Blueprint', None, None),
    ('a name prefix but not a word', 'Ashen Prime Barrel', None, None),
    ('not text', None, None, None),
    # a prime the catalogue does not hold yet is no part of the plain item
    ('a prime missing from the catalogue', 'Gyre Prime Systems Blueprint', None, None),
]


@pytest.mark.parametrize('case, reward, item, part', PARTS, ids=[p[0] for p in PARTS])
def test_a_reward_resolves_to_the_longest_item_name_and_its_part(ctx, case, reward, item, part):
    ids = {r['name']: r['id'] for r in ctx.execute('SELECT id, name FROM items')}
    match = relic_sync.match_part(reward, relic_sync.item_lookup(ctx))
    assert match == ((ids[item], part) if item else None)


RELIC_NAMES = [
    ('plain', 'Lith', 'A1', 'Lith A1 Relic'),
    ('blanks', ' Lith ', '  A1 ', 'Lith A1 Relic'),
    ('already named relic', 'Meso', 'B2 Relic', 'Meso B2 Relic'),
    ('lower-case relic', 'Meso', 'B2 relic', 'Meso B2 relic'),
    ('no tier', '', 'A1', ''),
    ('tier not text', None, 'A1', ''),
    ('name not text', 'Lith', 7, ''),
]


@pytest.mark.parametrize('case, tier, name, want', RELIC_NAMES, ids=[r[0] for r in RELIC_NAMES])
def test_a_relic_is_named_as_the_mission_tables_name_it(case, tier, name, want):
    assert relic_sync.relic_name(tier, name) == want


# ── Bad rewards (axis 4) ─────────────────────────────────────────────────────

BAD_REWARDS = [
    ('chance 0', {'itemName': 'Braton Prime Stock', 'chance': 0}),
    ('negative chance', {'itemName': 'Braton Prime Stock', 'chance': -11}),
    ('chance as text', {'itemName': 'Braton Prime Stock', 'chance': '11'}),
    ('chance true', {'itemName': 'Braton Prime Stock', 'chance': True}),
    ('chance over 100', {'itemName': 'Braton Prime Stock', 'chance': 101}),
    ('chance not finite', {'itemName': 'Braton Prime Stock', 'chance': float('inf')}),
    ('chance missing', {'itemName': 'Braton Prime Stock'}),
    ('name missing', {'chance': 11}),
    ('name not text', {'itemName': 42, 'chance': 11}),
    ('reward not an object', 'Braton Prime Stock'),
    ('chance a huge integer', {'itemName': 'Braton Prime Stock', 'chance': 10 ** 400}),
]


@pytest.mark.parametrize('case, reward', BAD_REWARDS, ids=[b[0] for b in BAD_REWARDS])
def test_a_reward_without_a_name_or_a_valid_chance_is_skipped(ctx, source, case, reward):
    tables = copy.deepcopy(TABLES)
    tables['relics'][0]['rewards'].append(reward)
    named = isinstance(reward, dict) and isinstance(reward.get('itemName'), str)
    tables['missionRewards']['Earth']['Mantle']['rewards']['A'].append(
        dict(reward, itemName='Neo S1 Relic') if named else reward)
    source.response = FakeResponse(payload=tables)
    wiki_sync._sync_drop_sources()
    assert relic_rows(ctx) == RELIC_ROWS
    assert relic_locations(ctx) == RELIC_LOCATIONS


def test_a_mission_reward_with_a_text_chance_fails_no_sync(ctx, source):
    tables = copy.deepcopy(TABLES)
    tables['missionRewards']['Venus']['Kiliken']['rewards'].append(
        {'itemName': 'Ash Prime Chassis Blueprint', 'chance': '5'})
    source.response = FakeResponse(payload=tables)
    wiki_sync._sync_drop_sources()
    assert relic_rows(ctx) == RELIC_ROWS


# ── A failed or malformed read (axis 4) ──────────────────────────────────────

def _with(**sections):
    tables = copy.deepcopy(TABLES)
    for name, value in sections.items():
        if value is _MISSING:
            del tables[name]
        else:
            tables[name] = value
    return FakeResponse(payload=tables)


_MISSING = object()

FAILURES = [
    ('timeout', requests.Timeout('slow')),
    ('connection error', requests.ConnectionError('refused')),
    ('HTTP error', FakeResponse(status=503)),
    ('not JSON', FakeResponse(text='<html>maintenance</html>')),
    ('JSON array', FakeResponse(payload=[TABLES])),
    ('JSON null', FakeResponse(payload=None)),
    ('relics missing', _with(relics=_MISSING)),
    ('missionRewards missing', _with(missionRewards=_MISSING)),
    ('relics an object', _with(relics={'Lith A1': TABLES['relics'][0]})),
    ('relics text', _with(relics='Lith A1 Relic')),
    ('missionRewards a list', _with(missionRewards=[TABLES['missionRewards']])),
    ('missionRewards null', _with(missionRewards=None)),
]


@pytest.mark.parametrize('case, response', FAILURES, ids=[f[0] for f in FAILURES])
def test_a_failed_or_malformed_read_leaves_no_relic_data(ctx, source, case, response):
    wiki_sync._sync_drop_sources()
    assert relic_rows(ctx) and relic_locations(ctx)
    source.response = response
    total = wiki_sync._sync_drop_sources()
    assert relic_rows(ctx) == set()
    assert relic_locations(ctx) == set()
    # the WFCD rows stay, the WFCD relic row too, as without the drop tables
    assert component_rows(ctx) == {('Ash Prime', 'Neuroptics', 'Lith A1 Relic'),
                                   ('Ash Prime', 'Systems', 'Neo S1 Relic')}
    assert total >= 2


def test_the_full_sync_stays_ok_when_the_drop_tables_fail(app, ctx, source, monkeypatch):
    for name in ('_sync_warframes', '_sync_weapons', '_sync_kitgun_chambers', '_sync_companions',
                 '_sync_archwings', '_sync_k_drives', '_sync_amps', '_sync_mods', '_sync_arcanes',
                 '_sync_resources', '_sync_component_ingredients'):
        monkeypatch.setattr(wiki_sync, name, lambda *args: 0)
    monkeypatch.setattr(wiki_sync, '_backfill_null_item_images', lambda db, cats: 0)
    monkeypatch.setattr(wiki_sync, '_sync_wiki_images_for_missing', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_item_images', lambda db: (0, 0))
    monkeypatch.setattr(wiki_sync, '_sync_component_wiki_images', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_component_images', lambda db: (0, 0))
    monkeypatch.setattr(wiki_sync, 'sync_resurgence', lambda: {'ok': False, 'error': 'mocked'})
    monkeypatch.setattr(wiki_sync, 'sync_market_slugs', lambda: {'ok': False, 'error': 'mocked'})
    source.response = _with(relics='broken')
    result = wiki_sync.run_sync(app)
    # two WFCD component rows and the mission row; no relic row
    assert result['ok'] is True and result['counts']['drop_sources'] == 3
    assert relic_rows(ctx) == set()


# ── The write path (axis 7) ──────────────────────────────────────────────────

def _tables(db):
    names = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: sorted(tuple(r) for r in db.execute(f'SELECT * FROM "{name}"'))
            for name in names}


def test_the_sync_writes_only_drop_sources_and_relic_drops(app, admin_client, ctx, source):
    admin = ctx.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    ash = ctx.execute("SELECT id FROM items WHERE name = 'Ash Prime'").fetchone()[0]
    ctx.execute('INSERT INTO user_progress (user_id, item_id, in_possession, note) '
                'VALUES (?, ?, 1, ?)', (admin, ash, 'kept'))
    ctx.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)', (admin, ash))
    ctx.execute('INSERT INTO farming_checklist (user_id, item_id, component_name, acquired) '
                "VALUES (?, ?, 'Neuroptics', 1)", (admin, ash))
    ctx.commit()
    before = _tables(ctx)
    wiki_sync._sync_drop_sources()
    after = _tables(ctx)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {'drop_sources', 'relic_drops'}


def test_a_failure_mid_rebuild_keeps_the_previous_rows(ctx, source, monkeypatch):
    wiki_sync._sync_drop_sources()
    before = (relic_rows(ctx), relic_locations(ctx), component_rows(ctx))

    def broken(db, rows):
        raise RuntimeError('disk full')

    monkeypatch.setattr(relic_sync, '_drop_listed_twice', broken)
    with pytest.raises(RuntimeError):
        wiki_sync._sync_drop_sources()
    assert (relic_rows(ctx), relic_locations(ctx), component_rows(ctx)) == before


# ── Where the planner reads the rows the sync wrote ──────────────────────────

def _fail_tables(source):
    source.response = requests.Timeout('slow')


@pytest.mark.parametrize('tables', ['read', 'failed'])
def test_the_advisor_offers_no_relic_name_as_a_node_after_a_sync(ctx, source, admin_client, tables):
    # A relic is no node whichever source names it: a WFCD component row naming a relic
    # (one the relics section does not list for that part, or every one when the drop
    # tables could not be read) is no node either.
    if tables == 'failed':
        _fail_tables(source)
    wiki_sync._sync_drop_sources()
    nodes = admin_client.get('/farming/nodes').get_json()['nodes']
    assert [n for n in nodes if relic_sync.is_relic_name(n)] == []


def test_the_farming_plan_keeps_a_parts_relic_source_after_a_sync(ctx, source):
    # Before the relic pass the plan listed the WFCD row "Neuroptics: Lith A1 Relic"; a read
    # of the drop tables that also lists it must not take the source out of the plan
    # (it stays there when the drop tables fail).
    from services import farming_service as fs
    ash = ctx.execute("SELECT id FROM items WHERE name = 'Ash Prime'").fetchone()[0]
    admin = ctx.execute("SELECT id FROM users ORDER BY id").fetchone()
    user = admin[0] if admin else 1

    def neuroptics_sources():
        plan = fs.get_item_plan(ash, user)
        comps = {c['component_name']: c for c in plan['components']}
        comp = comps.get('Neuroptics', {})
        return ({d['location'] for d in comp.get('drops', [])}
                | {r.get('relic') for r in comp.get('relics', [])})

    _fail_tables(source)
    wiki_sync._sync_drop_sources()
    assert 'Lith A1 Relic' in neuroptics_sources()

    source.response = FakeResponse(payload=copy.deepcopy(TABLES))
    wiki_sync._sync_drop_sources()
    assert 'Lith A1 Relic' in neuroptics_sources()


@pytest.mark.parametrize('tables', ['read', 'failed'])
def test_the_route_planner_routes_to_no_relic_name_after_a_sync(ctx, source, tables):
    from services import farming_service as fs
    if tables == 'failed':
        _fail_tables(source)
    wiki_sync._sync_drop_sources()
    ash = ctx.execute("SELECT id FROM items WHERE name = 'Ash Prime'").fetchone()[0]
    user = ctx.execute("SELECT id FROM users ORDER BY id").fetchone()
    stops = fs.compute_farming_route(user[0] if user else 1, [ash])
    assert [s['location'] for s in stops if relic_sync.is_relic_name(s['location'])] == []
