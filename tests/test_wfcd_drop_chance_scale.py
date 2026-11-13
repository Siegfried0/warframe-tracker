"""WFCD drop chances are fractions; the drop sync stores them in percent.

``drop_sources.drop_chance`` is a percentage and ``avg_runs`` is ``100 / drop_chance``. The
warframestat drop tables give percent (25.33); WFCD's warframe-items gives each
``drops[].chance`` as a fraction from 0 to 1 (0.2533 for the same drop). Both WFCD passes
(component drops and resource drops) convert through one helper and skip a chance that is not
a fraction rather than guess its scale.

The WFCD files are hand-written, minimal copies of their shape, mocked at ``_fetch_wfcd``; the
drop tables are mocked at ``requests.get``. No test reaches the network.
"""
import copy
import logging

import pytest
import requests

from models.db import get_db
from models.item import Item
from services import wiki_sync, wiki_sync_requests
from services.buy_vs_farm import RELIC_OPENING_MINUTES, expected_hours, item_sources

# (id, WFCD chance, stored drop_chance, stored avg_runs); None: no row is stored
GOOD_CHANCES = [
    ('fraction', 0.2533, 25.33, 3.9),
    ('a tenth', 0.1, 10.0, 10.0),
    ('one', 1, 100.0, 1.0),
    ('one as float', 1.0, 100.0, 1.0),
    ('tiny', 0.0001, 0.01, 10000.0),
    ('smallest kept', 1e-6, 0.0001, 1000000.0),
]

BAD_CHANCES = [
    ('zero', 0),
    ('zero float', 0.0),
    ('negative', -0.25),
    ('above one (percent)', 25.33),
    ('just above one', 1.0001),
    ('rounds to zero percent', 4.9e-7),
    ('far below the rounding', 1e-9),
    ('smallest float', 5e-324),
    ('true', True),
    ('false', False),
    ('string', '0.25'),
    ('empty string', ''),
    ('none', None),
    ('nan', float('nan')),
    ('infinity', float('inf')),
    ('minus infinity', float('-inf')),
    ('huge int', 10 ** 400),
    ('list', [0.25]),
    ('dict', {'chance': 0.25}),
]


def _part_file(*chances):
    """Rhino's Chassis dropping at one mission per chance, in the order given."""
    return {'Warframes.json': [{
        'name': 'Rhino', 'masterable': True,
        'components': [{'name': 'Chassis', 'drops': [
            {'location': f'Venus/Node{i} (Capture)', 'type': 'Rhino Chassis Blueprint',
             'chance': chance} for i, chance in enumerate(chances)]}],
    }]}


def _resource_file(*chances):
    return {'Resources.json': [{
        'name': 'Ferrite', 'drops': [
            {'location': f'Mercury/Node{i}', 'type': 'Ferrite', 'chance': chance}
            for i, chance in enumerate(chances)],
    }]}


SITES = {
    # site id: (sync function, WFCD files from chances, item, category, source_type)
    'component': (wiki_sync._sync_wfcd_component_drops, _part_file,
                  'Rhino', 'warframe', 'component'),
    'resource': (wiki_sync._sync_resource_drops, _resource_file,
                 'Ferrite', 'resource', 'resource'),
}


def _sync(app, monkeypatch, site, *chances):
    """Run one WFCD pass on the given chances; return its count and the stored rows
    as {location: (drop_chance, avg_runs)}."""
    sync, files, name, category, source_type = SITES[site]
    wfcd = files(*chances)
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd',
                        lambda filename: copy.deepcopy(wfcd.get(filename, [])))
    with app.app_context():
        Item.upsert(name, category, source='standard')
        db = get_db()
        count = sync(db)
        db.commit()
        rows = db.execute(
            'SELECT location, drop_chance, avg_runs FROM drop_sources WHERE source_type = ?',
            (source_type,)).fetchall()
        return count, {r['location']: (r['drop_chance'], r['avg_runs']) for r in rows}


@pytest.mark.parametrize('site', SITES)
@pytest.mark.parametrize('chance, stored, runs', [r[1:] for r in GOOD_CHANCES],
                         ids=[r[0] for r in GOOD_CHANCES])
def test_a_wfcd_fraction_is_stored_in_percent(app, monkeypatch, site, chance, stored, runs):
    count, rows = _sync(app, monkeypatch, site, chance)
    assert count == 1
    [(drop_chance, avg_runs)] = rows.values()
    assert drop_chance == stored
    assert avg_runs == runs


@pytest.mark.parametrize('site', SITES)
@pytest.mark.parametrize('chance', [r[1] for r in BAD_CHANCES], ids=[r[0] for r in BAD_CHANCES])
def test_a_wfcd_chance_that_is_no_fraction_stores_no_row_and_keeps_the_others(
        app, monkeypatch, site, chance):
    count, rows = _sync(app, monkeypatch, site, 0.5, chance, 0.2)
    assert count == 2
    assert sorted(rows.values()) == [(20.0, 5.0), (50.0, 2.0)]


@pytest.mark.parametrize('site', SITES)
def test_skipped_wfcd_drops_are_counted_in_one_warning(app, monkeypatch, caplog, site):
    with caplog.at_level(logging.WARNING, logger=wiki_sync.log.name):
        _sync(app, monkeypatch, site, 25.33, None, 0.5)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert 'skipped 2 drop(s)' in warnings[0]


@pytest.mark.parametrize('site', SITES)
def test_a_pass_skipping_nothing_logs_no_warning(app, monkeypatch, caplog, site):
    with caplog.at_level(logging.WARNING, logger=wiki_sync.log.name):
        _sync(app, monkeypatch, site, 0.5)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ── The whole drop sync: WFCD rows and the relic tables in one scale ──────────

WFCD = {
    'Warframes.json': [{
        'name': 'Ash Prime', 'masterable': True,
        'components': [
            # the relic tables name this part at the same relic: stored once
            {'name': 'Neuroptics', 'drops': [
                {'location': 'Lith A1 Relic', 'type': 'Ash Prime Neuroptics Blueprint',
                 'chance': 0.2533}]},
            # a relic the relic tables do not name for this part: the WFCD row is kept
            {'name': 'Systems', 'drops': [
                {'location': 'Neo S1 Relic', 'type': 'Ash Prime Systems Blueprint',
                 'chance': 0.11}]},
        ],
    }],
}

TABLES = {
    'relics': [
        {'tier': 'Lith', 'relicName': 'A1', 'state': 'Intact', 'rewards': [
            {'itemName': 'Ash Prime Neuroptics Blueprint', 'rarity': 'Common',
             'chance': 25.33}]},
    ],
    'missionRewards': {
        'Earth': {'Mantle': {'gameMode': 'Survival', 'rewards': {
            'A': [{'itemName': 'Lith A1 Relic', 'rarity': 'Common', 'chance': 14.29},
                  {'itemName': 'Neo S1 Relic', 'rarity': 'Common', 'chance': 14.29}]}}},
    },
}


class _Response:
    def raise_for_status(self):
        pass

    def json(self):
        return copy.deepcopy(TABLES)


@pytest.fixture
def synced(app, monkeypatch):
    """The whole drop sync over the files above; yields the db and Ash Prime's id."""
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd',
                        lambda name: copy.deepcopy(WFCD.get(name, [])))

    def fake_get(url, timeout=None, **kwargs):
        if url != wiki_sync_requests._WARFRAMESTAT_DROPS:
            raise requests.ConnectionError(f'unexpected request: {url}')
        return _Response()

    monkeypatch.setattr(requests, 'get', fake_get)
    with app.app_context():
        Item.upsert('Ash Prime', 'warframe', source='standard')
        wiki_sync._sync_drop_sources()
        db = get_db()
        yield db, db.execute("SELECT id FROM items WHERE name = 'Ash Prime'").fetchone()[0]


def test_a_kept_wfcd_relic_row_and_the_relic_tables_agree_in_scale(synced):
    db, item_id = synced
    rows = {(r['source_type'], r['notes'], r['location']): r['drop_chance']
            for r in db.execute('SELECT source_type, notes, location, drop_chance '
                                'FROM drop_sources WHERE item_id = ?', (item_id,))}
    assert rows == {
        ('relic', 'Neuroptics', 'Lith A1 Relic'): 25.33,
        ('component', 'Systems', 'Neo S1 Relic'): 11.0,
    }


def test_a_synced_part_drop_from_a_relic_counts_its_chance_in_percent(synced):
    db, item_id = synced
    sources = item_sources(item_id, db)
    # 100 / 11 openings of Neo S1 at one fissure run each, not 100 / 0.11
    assert sources['parts']['systems'] == pytest.approx(100 / 11 * RELIC_OPENING_MINUTES)
    assert expected_hours(sources, ['Systems']) == pytest.approx(100 / 11 * 5 / 60)


def test_a_synced_mission_part_drop_counts_its_chance_in_percent(app, monkeypatch):
    _sync(app, monkeypatch, 'component', 0.25)
    with app.app_context():
        item_id = get_db().execute("SELECT id FROM items WHERE name = 'Rhino'").fetchone()[0]
        # 4 runs of a 3-minute Capture: 12 minutes, not 1,200
        assert expected_hours(item_sources(item_id)) == pytest.approx(4 * 3 / 60)
