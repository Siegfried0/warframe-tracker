"""Relic locations from the bounty reward sections of the drop tables: every relic reward of a
bounty becomes a ``relic_drops`` row (the bounty and its hub as the location, ``Bounty`` as the
mission type, its rotation and stage as the rotation), beside the mission locations, from the
same one request. A bounty section that is missing or malformed is skipped and the rest is
still written; a relic only bounties drop is no longer shown as vaulted.

The drop tables are a hand-written, minimal copy of their published shape; the request is
mocked at ``requests.get`` and the WFCD files at ``_fetch_wfcd``, so no test reaches the
network.
"""
import copy
import logging

import pytest
import requests

from models.db import get_db
from models.item import Item
from services import relic_sources, relic_sync, wiki_sync

CATALOGUE = [('Ash Prime', 'warframe'), ('Braton Prime', 'primary')]

TABLES = {
    'relics': [
        {'tier': 'Lith', 'relicName': 'A1', 'state': 'Intact', 'rewards': [
            {'itemName': 'Ash Prime Neuroptics Blueprint', 'chance': 25.33}]},
        # dropped by bounties only
        {'tier': 'Axi', 'relicName': 'B7', 'state': 'Intact', 'rewards': [
            {'itemName': 'Braton Prime Receiver', 'chance': 11}]},
        # dropped by no mission and no bounty
        {'tier': 'Neo', 'relicName': 'V9', 'state': 'Intact', 'rewards': [
            {'itemName': 'Braton Prime Stock', 'chance': 2}]},
    ],
    'missionRewards': {
        'Earth': {'Mantle': {'gameMode': 'Survival', 'rewards': {
            'A': [{'itemName': 'Lith A1 Relic', 'chance': 14.29}]}}},
    },
    'cetusBountyRewards': [
        {'bountyLevel': 'Level 5 - 15 Cetus Bounty', 'rewards': {
            'A': [{'itemName': 'Axi B7 Relic', 'rarity': 'Uncommon', 'chance': 12.5,
                   'stage': 'Stage 2'},
                  # the same relic in a second stage of the same rotation
                  {'itemName': 'Axi B7 Relic', 'chance': 7.5, 'stage': 'Final Stage'},
                  # a non-relic reward
                  {'itemName': '2,000 Credits Cache', 'chance': 20, 'stage': 'Stage 1'}],
            'B': [{'itemName': 'Lith A1 Relic', 'chance': 10, 'stage': 'Stage 1'}],
            # the same relic and stage in another rotation
            'C': [{'itemName': 'Axi B7 Relic', 'chance': 5, 'stage': 'Stage 2'}],
        }},
        # the same relic in a second bounty level
        {'bountyLevel': 'Level 10 - 30 Cetus Bounty', 'rewards': {
            'A': [{'itemName': 'Axi B7 Relic', 'chance': 6.25, 'stage': 'Stage 3'}]}},
    ],
    'solarisBountyRewards': [
        {'bountyLevel': 'Level 10 - 30 Orb Vallis Bounty', 'rewards': {
            'A': [{'itemName': 'Axi B7 Relic', 'chance': 4, 'stage': 'Stage 1'}]}}],
    'deimosRewards': [
        {'bountyLevel': 'Level 15 - 25 Cambion Drift Bounty', 'rewards': {
            'B': [{'itemName': 'Axi B7 Relic', 'chance': 3, 'stage': 'Stage 2, Stage 3 of 4'}]}}],
    'zarimanRewards': [
        # rewards as one list, without a stage
        {'bountyLevel': 'Level 50 - 55 Zariman Bounty', 'rewards': [
            {'itemName': 'Axi B7 Relic', 'chance': 2.5}]}],
    'entratiLabRewards': [
        {'bountyLevel': 'Level 55 - 60 Sanctum Bounty', 'rewards': {
            'A': [{'itemName': 'Axi B7 Relic', 'chance': 2}]}}],
    'hexRewards': [
        {'bountyLevel': 'Level 65 - 70 Hex Bounty', 'rewards': {
            'C': [{'itemName': 'Axi B7 Relic', 'chance': 1.25, 'stage': 'Stage 4'}]}}],
}

MISSION = ('Lith A1 Relic', 'Mantle — Earth', 'Survival', 'A', 14.29, 7.0)

# (relic, location, mission type, rotation, chance, avg runs) each section gives
BY_SECTION = {
    'cetusBountyRewards': {
        ('Axi B7 Relic', 'Level 5 - 15 Cetus Bounty — Cetus', 'Bounty', 'A · Stage 2', 12.5, 8.0),
        ('Axi B7 Relic', 'Level 5 - 15 Cetus Bounty — Cetus', 'Bounty', 'A · Final Stage',
         7.5, 13.3),
        ('Lith A1 Relic', 'Level 5 - 15 Cetus Bounty — Cetus', 'Bounty', 'B · Stage 1', 10.0, 10.0),
        ('Axi B7 Relic', 'Level 5 - 15 Cetus Bounty — Cetus', 'Bounty', 'C · Stage 2', 5.0, 20.0),
        ('Axi B7 Relic', 'Level 10 - 30 Cetus Bounty — Cetus', 'Bounty', 'A · Stage 3',
         6.25, 16.0),
    },
    'solarisBountyRewards': {
        ('Axi B7 Relic', 'Level 10 - 30 Orb Vallis Bounty — Orb Vallis', 'Bounty', 'A · Stage 1',
         4.0, 25.0)},
    'deimosRewards': {
        ('Axi B7 Relic', 'Level 15 - 25 Cambion Drift Bounty — Cambion Drift', 'Bounty',
         'B · Stage 2, Stage 3 of 4', 3.0, 33.3)},
    'zarimanRewards': {
        ('Axi B7 Relic', 'Level 50 - 55 Zariman Bounty — Zariman', 'Bounty', None, 2.5, 40.0)},
    'entratiLabRewards': {
        ('Axi B7 Relic', 'Level 55 - 60 Sanctum Bounty — Sanctum Anatomica', 'Bounty', 'A',
         2.0, 50.0)},
    'hexRewards': {
        ('Axi B7 Relic', 'Level 65 - 70 Hex Bounty — Höllvania', 'Bounty', 'C · Stage 4',
         1.25, 80.0)},
}
ALL_BOUNTIES = set().union(*BY_SECTION.values())
SECTIONS = [section for section, _ in relic_sync.BOUNTY_SECTIONS]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def source(monkeypatch):
    """Serve `source.tables` as the drop tables; count the requests."""
    class Source:
        tables = copy.deepcopy(TABLES)
        calls = 0

    def fake_get(url, timeout=None, **kwargs):
        assert url == wiki_sync._WARFRAMESTAT_DROPS
        Source.calls += 1
        return FakeResponse(copy.deepcopy(Source.tables))

    monkeypatch.setattr(requests, 'get', fake_get)
    monkeypatch.setattr(wiki_sync, '_fetch_wfcd', lambda name: [])
    return Source


@pytest.fixture
def ctx(app):
    with app.app_context():
        for name, category in CATALOGUE:
            Item.upsert(name, category, source='standard')
        yield get_db()


def relic_locations(db):
    return {tuple(r) for r in db.execute(
        'SELECT relic_name, location, mission_type, rotation, drop_chance, avg_runs '
        'FROM relic_drops')}


def relic_rows(db):
    return {(r['notes'], r['location']) for r in db.execute(
        "SELECT notes, location FROM drop_sources WHERE source_type = 'relic'")}


def _item(db, name):
    return db.execute('SELECT id FROM items WHERE name = ?', (name,)).fetchone()[0]


# ── A good read ──────────────────────────────────────────────────────────────

def test_every_bounty_section_gives_its_relic_locations_beside_the_missions(ctx, source):
    wiki_sync._sync_drop_sources()
    assert relic_locations(ctx) == ALL_BOUNTIES | {MISSION}
    assert source.calls == 1


def test_a_relic_found_only_in_bounties_gets_only_bounty_rows(ctx, source):
    wiki_sync._sync_drop_sources()
    axi = {row for row in relic_locations(ctx) if row[0] == 'Axi B7 Relic'}
    assert axi and {row[2] for row in axi} == {'Bounty'}


def test_a_relic_in_a_mission_and_a_bounty_gets_both_rows(ctx, source):
    wiki_sync._sync_drop_sources()
    lith = {row for row in relic_locations(ctx) if row[0] == 'Lith A1 Relic'}
    assert lith == {MISSION, ('Lith A1 Relic', 'Level 5 - 15 Cetus Bounty — Cetus', 'Bounty',
                              'B · Stage 1', 10.0, 10.0)}


def test_relic_contents_are_unchanged_by_the_bounty_sections(ctx, source):
    wiki_sync._sync_drop_sources()
    with_bounties = relic_rows(ctx)
    source.tables = {k: v for k, v in TABLES.items() if k not in SECTIONS}
    wiki_sync._sync_drop_sources()
    assert relic_rows(ctx) == with_bounties
    assert relic_locations(ctx) == {MISSION}


@pytest.mark.parametrize('section', SECTIONS)
def test_each_bounty_section_alone_gives_its_rows(ctx, source, section):
    source.tables = {k: v for k, v in TABLES.items() if k not in SECTIONS or k == section}
    wiki_sync._sync_drop_sources()
    assert relic_locations(ctx) == BY_SECTION[section] | {MISSION}


def test_no_other_section_of_the_response_is_read(ctx, source):
    source.tables = copy.deepcopy(TABLES)
    for other in ('transientRewards', 'keyRewards', 'sortieRewards', 'cetusBounty'):
        source.tables[other] = copy.deepcopy(TABLES['cetusBountyRewards'])
    wiki_sync._sync_drop_sources()
    assert relic_locations(ctx) == ALL_BOUNTIES | {MISSION}


# ── Repetition (axis 3) ──────────────────────────────────────────────────────

def test_a_second_sync_rebuilds_the_same_bounty_rows(ctx, source):
    wiki_sync._sync_drop_sources()
    first = relic_locations(ctx)
    wiki_sync._sync_drop_sources()
    assert relic_locations(ctx) == first
    assert ctx.execute('SELECT COUNT(*) FROM relic_drops').fetchone()[0] == len(first)


def test_one_relic_rotation_and_stage_listed_twice_keeps_the_higher_chance(ctx, source):
    tables = copy.deepcopy(TABLES)
    tables['solarisBountyRewards'][0]['rewards']['A'] += [
        {'itemName': ' axi  b7 RELIC ', 'chance': 9, 'stage': ' stage  1 '},
        {'itemName': 'Axi B7 Relic', 'chance': 1, 'stage': 'Stage 1'}]
    source.tables = tables
    wiki_sync._sync_drop_sources()
    vallis = {row for row in relic_locations(ctx) if 'Orb Vallis' in row[1]}
    assert vallis == {('Axi B7 Relic', 'Level 10 - 30 Orb Vallis Bounty — Orb Vallis', 'Bounty',
                       'A · stage 1', 9.0, 11.1)}


# ── Spellings (axis 1) ───────────────────────────────────────────────────────

SPELLINGS = [
    ('case and blanks of the relic', {'itemName': '  aXI   b7 relic ', 'chance': 4},
     'Axi B7 Relic', 'A'),
    ('non-ASCII relic name', {'itemName': 'Lìth Ä1 Relic', 'chance': 4}, 'Lìth Ä1 Relic', 'A'),
    ('a stage with blanks', {'itemName': 'Axi B7 Relic', 'chance': 4, 'stage': ' Stage  2 '},
     'Axi B7 Relic', 'A · Stage 2'),
    ('a stage that is no text', {'itemName': 'Axi B7 Relic', 'chance': 4, 'stage': 2},
     'Axi B7 Relic', 'A'),
    ('an empty stage', {'itemName': 'Axi B7 Relic', 'chance': 4, 'stage': '  '},
     'Axi B7 Relic', 'A'),
]


@pytest.mark.parametrize('case, reward, relic, rotation', SPELLINGS, ids=[s[0] for s in SPELLINGS])
def test_a_bounty_reward_is_read_in_any_spelling(ctx, source, case, reward, relic, rotation):
    source.tables = {'relics': TABLES['relics'], 'missionRewards': TABLES['missionRewards'],
                     'hexRewards': [{'bountyLevel': 'Hex', 'rewards': {'A': [reward]}}]}
    wiki_sync._sync_drop_sources()
    bounty = {row[:4] for row in relic_locations(ctx) if row[2] == 'Bounty'}
    assert bounty == {(relic, 'Hex — Höllvania', 'Bounty', rotation)}


BOUNTY_NAMES = [
    ('html-special characters', {'bountyLevel': '<b>Level 5 & "15"</b>'},
     '<b>Level 5 & "15"</b> — Cetus'),
    ('outer and double blanks', {'bountyLevel': '  Level  5 - 15 '}, 'Level 5 - 15 — Cetus'),
    ('named by name', {'name': 'Ghoul Purge'}, 'Ghoul Purge — Cetus'),
    ('bountyLevel before name', {'bountyLevel': 'Level 5', 'name': 'Other'}, 'Level 5 — Cetus'),
    ('a blank bountyLevel falls back to name', {'bountyLevel': ' ', 'name': 'Ghoul Purge'},
     'Ghoul Purge — Cetus'),
    ('a very long name is cut to the column',
     {'bountyLevel': 'L' * 600}, 'L' * (relic_sync.LOCATION_WIDTH - 1) + '…'),
]


@pytest.mark.parametrize('case, names, location', BOUNTY_NAMES, ids=[b[0] for b in BOUNTY_NAMES])
def test_a_bounty_is_named_by_its_level_and_hub(ctx, source, case, names, location):
    entry = dict(names, rewards={'A': [{'itemName': 'Axi B7 Relic', 'chance': 4}]})
    source.tables = {'relics': TABLES['relics'], 'missionRewards': TABLES['missionRewards'],
                     'cetusBountyRewards': [entry]}
    wiki_sync._sync_drop_sources()
    assert {row[1] for row in relic_locations(ctx) if row[2] == 'Bounty'} == {location}


def test_a_rotation_and_a_long_stage_fit_the_rotation_column():
    rotation = relic_sync.bounty_rotation('C', 'Stage 2, Stage 3 of 4, Stage 3 of 5, and Final '
                                               'Stage of every other bounty')
    assert len(rotation) == relic_sync.ROTATION_WIDTH
    assert rotation.startswith('C · Stage 2, Stage 3 of 4') and rotation.endswith('…')
    assert relic_sync.bounty_rotation(None, None) is None
    assert relic_sync.bounty_rotation(None, 'Stage 1') == 'Stage 1'


# ── A malformed bounty section (axis 4) ──────────────────────────────────────

_MISSING = object()

BAD_SECTIONS = [
    ('missing', _MISSING),
    ('null', None),
    ('an object instead of a list', {'Level 5': TABLES['cetusBountyRewards'][0]}),
    ('text', 'Level 5 - 15 Cetus Bounty'),
    ('a number', 7),
    ('a list of non-objects', ['Level 5', 7, None, ['Axi B7 Relic']]),
    ('entries without a name', [{'rewards': {'A': [{'itemName': 'Axi B7 Relic', 'chance': 5}]}},
                                {'bountyLevel': 7, 'rewards': [
                                    {'itemName': 'Axi B7 Relic', 'chance': 5}]}]),
    ('entries without rewards', [{'bountyLevel': 'Level 5'},
                                 {'bountyLevel': 'Level 5', 'rewards': None},
                                 {'bountyLevel': 'Level 5', 'rewards': 'Axi B7 Relic'},
                                 {'bountyLevel': 'Level 5', 'rewards': {'A': 'Axi B7 Relic'}}]),
]


@pytest.mark.parametrize('broken', SECTIONS)
@pytest.mark.parametrize('case, value', BAD_SECTIONS, ids=[b[0] for b in BAD_SECTIONS])
def test_a_malformed_bounty_section_is_skipped_and_the_rest_is_written(
        ctx, source, broken, case, value):
    tables = copy.deepcopy(TABLES)
    if value is _MISSING:
        del tables[broken]
    else:
        tables[broken] = copy.deepcopy(value)
    source.tables = tables
    total = wiki_sync._sync_drop_sources()
    kept = ALL_BOUNTIES - BY_SECTION[broken]
    assert relic_locations(ctx) == kept | {MISSION}
    assert relic_rows(ctx) == {('Neuroptics', 'Lith A1 Relic'), ('Receiver', 'Axi B7 Relic'),
                               ('Stock', 'Neo V9 Relic')}
    assert total == 3


BAD_REWARDS = [
    ('chance 0', {'itemName': 'Neo V9 Relic', 'chance': 0}),
    ('negative chance', {'itemName': 'Neo V9 Relic', 'chance': -5}),
    ('chance over 100', {'itemName': 'Neo V9 Relic', 'chance': 100.5}),
    ('chance as text', {'itemName': 'Neo V9 Relic', 'chance': '5'}),
    ('chance true', {'itemName': 'Neo V9 Relic', 'chance': True}),
    ('chance missing', {'itemName': 'Neo V9 Relic'}),
    ('chance not finite', {'itemName': 'Neo V9 Relic', 'chance': float('nan')}),
    ('itemName missing', {'chance': 5, 'stage': 'Stage 1'}),
    ('itemName not text', {'itemName': ['Neo V9 Relic'], 'chance': 5}),
    ('reward not an object', 'Neo V9 Relic'),
]


@pytest.mark.parametrize('section', SECTIONS)
@pytest.mark.parametrize('case, reward', BAD_REWARDS, ids=[b[0] for b in BAD_REWARDS])
def test_a_bounty_reward_without_a_name_or_a_valid_chance_is_skipped(
        ctx, source, section, case, reward):
    tables = copy.deepcopy(TABLES)
    entry = tables[section][0]
    if isinstance(entry['rewards'], list):
        entry['rewards'].append(reward)
    else:
        next(iter(entry['rewards'].values())).append(reward)
    source.tables = tables
    wiki_sync._sync_drop_sources()
    assert relic_locations(ctx) == ALL_BOUNTIES | {MISSION}


def test_a_chance_of_exactly_100_is_read(ctx, source):
    source.tables = {'relics': TABLES['relics'], 'missionRewards': TABLES['missionRewards'],
                     'hexRewards': [{'bountyLevel': 'Hex', 'rewards': [
                         {'itemName': 'Neo V9 Relic', 'chance': 100}]}]}
    wiki_sync._sync_drop_sources()
    assert ('Neo V9 Relic', 'Hex — Höllvania', 'Bounty', None, 100.0, 1.0) in relic_locations(ctx)


@pytest.mark.parametrize('core', ['relics', 'missionRewards'])
def test_a_broken_relics_or_mission_section_still_empties_both_tables(ctx, source, core):
    wiki_sync._sync_drop_sources()
    assert relic_locations(ctx)
    source.tables = dict(copy.deepcopy(TABLES), **{core: 'broken'})
    wiki_sync._sync_drop_sources()
    assert relic_locations(ctx) == set()
    assert relic_rows(ctx) == set()


# ── The log line ─────────────────────────────────────────────────────────────

def test_the_sync_logs_how_many_relic_locations_each_bounty_section_gave(ctx, source, caplog):
    tables = copy.deepcopy(TABLES)
    del tables['zarimanRewards']
    tables['hexRewards'] = {'not': 'a list'}
    tables['entratiLabRewards'] = []
    source.tables = tables
    with caplog.at_level(logging.INFO, logger=relic_sync.log.name):
        wiki_sync._sync_drop_sources()
    lines = [r.getMessage() for r in caplog.records if r.name == relic_sync.log.name]
    assert ('Relic locations from bounties: cetusBountyRewards 5, solarisBountyRewards 1, '
            'deimosRewards 1, zarimanRewards skipped, entratiLabRewards 0, '
            'hexRewards skipped') in lines
    warnings = [r.getMessage() for r in caplog.records
                if r.name == relic_sync.log.name and r.levelno == logging.WARNING]
    assert warnings == [
        'Relic locations from zarimanRewards not read (non-fatal): zarimanRewards is missing',
        'Relic locations from hexRewards not read (non-fatal): hexRewards is not a list']
    assert 'Relic drops: 3 relic rows, 1 mission locations, 7 bounty locations' in lines


# ── The write path (axis 7) ──────────────────────────────────────────────────

def _tables(db):
    names = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: sorted(tuple(r) for r in db.execute(f'SELECT * FROM "{name}"'))
            for name in names}


def test_the_bounty_sections_write_only_relic_drops(ctx, source, admin_client):
    admin = ctx.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    braton = _item(ctx, 'Braton Prime')
    ctx.execute('INSERT INTO user_progress (user_id, item_id, in_possession) VALUES (?, ?, 1)',
                (admin, braton))
    ctx.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)', (admin, braton))
    ctx.commit()
    source.tables = {k: v for k, v in TABLES.items() if k not in SECTIONS}
    wiki_sync._sync_drop_sources()
    before = _tables(ctx)
    source.tables = copy.deepcopy(TABLES)
    wiki_sync._sync_drop_sources()
    after = _tables(ctx)
    assert {name for name in before if before[name] != after[name]} == {'relic_drops'}


def test_a_failure_mid_rebuild_keeps_the_previous_bounty_rows(ctx, source, monkeypatch):
    wiki_sync._sync_drop_sources()
    before = relic_locations(ctx)

    def broken(db, rows):
        raise RuntimeError('disk full')

    monkeypatch.setattr(relic_sync, '_drop_listed_twice', broken)
    with pytest.raises(RuntimeError):
        wiki_sync._sync_drop_sources()
    assert relic_locations(ctx) == before


# ── Reading the rows back (axis 9) ───────────────────────────────────────────

def test_a_relic_only_bounties_drop_is_not_vaulted_and_shows_its_bounties(ctx, source):
    wiki_sync._sync_drop_sources()
    relics = {r['relic']: r for r in relic_sources.item_relics(_item(ctx, 'Braton Prime'))}
    axi, neo = relics['Axi B7 Relic'], relics['Neo V9 Relic']
    assert axi['vaulted'] is False
    assert [(loc['location'], loc['mission_type'], loc['rotation'])
            for loc in axi['locations']] == [
        ('Level 5 - 15 Cetus Bounty — Cetus', 'Bounty', 'A · Stage 2'),
        ('Level 5 - 15 Cetus Bounty — Cetus', 'Bounty', 'A · Final Stage'),
        ('Level 10 - 30 Cetus Bounty — Cetus', 'Bounty', 'A · Stage 3')]
    assert neo['vaulted'] is True and neo['locations'] == []


def test_without_the_bounty_sections_the_same_relic_is_vaulted(ctx, source):
    source.tables = {k: v for k, v in TABLES.items() if k not in SECTIONS}
    wiki_sync._sync_drop_sources()
    relics = {r['relic']: r for r in relic_sources.item_relics(_item(ctx, 'Braton Prime'))}
    assert relics['Axi B7 Relic']['vaulted'] is True


def test_a_mission_and_a_bounty_location_are_ranked_by_average_runs(ctx, source):
    wiki_sync._sync_drop_sources()
    lith = relic_sources.item_relics(_item(ctx, 'Ash Prime'))[0]
    assert [(loc['mission_type'], loc['avg_runs']) for loc in lith['locations']] == [
        ('Survival', 7.0), ('Bounty', 10.0)]


def test_the_plan_and_the_modal_carry_the_bounty_locations(ctx, source, admin_client):
    from services import farming_service as fs
    wiki_sync._sync_drop_sources()
    braton = _item(ctx, 'Braton Prime')
    user = ctx.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    plan = fs.get_item_plan(braton, user)
    receiver = {c['component_name']: c for c in plan['components']}['Receiver']
    assert receiver['relics'][0]['vaulted'] is False
    assert receiver['relics'][0]['locations'][0]['mission_type'] == 'Bounty'
    modal = admin_client.get(f'/collection/drops/{braton}').get_json()
    axi = [r for r in modal['relics'] if r['relic'] == 'Axi B7 Relic'][0]
    assert axi['vaulted'] is False
    assert axi['locations'][0] == {
        'location': 'Level 5 - 15 Cetus Bounty — Cetus', 'mission_type': 'Bounty',
        'rotation': 'A · Stage 2', 'drop_chance': 12.5, 'avg_runs': 8.0}


def test_the_advisor_offers_no_bounty_as_a_node(ctx, source, admin_client):
    wiki_sync._sync_drop_sources()
    nodes = admin_client.get('/farming/nodes').get_json()['nodes']
    assert [n for n in nodes if 'Bounty' in n] == []
