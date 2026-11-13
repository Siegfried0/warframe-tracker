"""The drop-source sync's module: where it lives, how it is reached, how it fails.

``services.wiki_sync_drops`` holds the drop-source sync; the entry point binds
``_sync_drop_sources`` (and the two WFCD passes other code reaches there) as names of its own.
Inside the module the drop step calls its passes by the module's own names. Every request goes
through ``services.wiki_sync_requests``; the WFCD files and the drop tables are hand-written,
minimal copies of their shape, mocked there or at ``requests.get``. No test reaches the network.
"""
import ast
import copy
import inspect
import logging

import pytest
import requests

from models.db import get_db
from models.item import Item
from services import relic_sync, wiki_sync, wiki_sync_drops, wiki_sync_requests

# Every definition the drop-source sync is made of.
DROP_DEFINITIONS = ['_build_item_prefix_lookup', '_match_drop_to_item', '_wfcd_chance_percent',
                    '_insert_wfcd_drop', '_log_skipped_wfcd_drops', '_sync_resource_drops',
                    '_sync_wfcd_component_drops', '_sync_mission_reward_drops',
                    '_sync_drop_sources']
# The passes the drop step runs, in the order it runs them.
DROP_PASSES = ['_sync_wfcd_component_drops', '_sync_resource_drops',
               '_sync_mission_reward_drops']
# Every step the entry point runs; the drop step is the one left real below.
STEPS = ['_sync_warframes', '_sync_weapons', '_sync_kitgun_chambers', '_sync_companions',
         '_sync_archwings', '_sync_k_drives', '_sync_amps', '_sync_mods', '_sync_arcanes',
         '_sync_resources', '_sync_component_ingredients']

WFCD = {
    'Warframes.json': [{'name': 'Rhino', 'masterable': True, 'components': [
        {'name': 'Chassis', 'drops': [
            {'location': 'Venus/Fossa (Assassination)', 'type': 'Rhino Chassis Blueprint',
             'chance': 0.2256}]}]}],
    'Resources.json': [{'name': 'Ferrite', 'drops': [
        {'location': 'Mercury/Apollodorus', 'type': 'Ferrite', 'chance': 0.5}]}],
}
TABLES = {'missionRewards': {'Earth': {'Mantle': {'gameMode': 'Survival', 'rewards': {
    'A': [{'itemName': 'Braton Blueprint', 'chance': 14.29}]}}}}}
CATALOGUE = [('Rhino', 'warframe'), ('Ferrite', 'resource'), ('Braton', 'primary')]


def _seed_catalogue():
    for name, category in CATALOGUE:
        Item.upsert(name=name, category=category, source='standard')
    get_db().commit()


def _drop_rows():
    return sorted((r['name'], r['source_type'], r['location'], r['drop_chance'])
                  for r in get_db().execute(
                      'SELECT i.name, d.source_type, d.location, d.drop_chance '
                      'FROM drop_sources d JOIN items i ON i.id = d.item_id'))


def _replace_wfcd_and_tables(monkeypatch):
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd',
                        lambda name: copy.deepcopy(WFCD.get(name, [])))
    monkeypatch.setattr(wiki_sync_requests, '_fetch_drop_tables', lambda: copy.deepcopy(TABLES))


def _refuse(*args, **kwargs):
    raise requests.ConnectionError('refused')


# ── Interface: where each definition lives ───────────────────────────────────

@pytest.mark.parametrize('name', DROP_DEFINITIONS)
def test_each_drop_definition_lives_in_the_drop_module(name):
    assert getattr(wiki_sync_drops, name).__module__ == wiki_sync_drops.__name__


def test_the_entry_point_defines_only_the_entry_point():
    tree = ast.parse(inspect.getsource(wiki_sync))
    assert [node.name for node in tree.body if isinstance(node, ast.FunctionDef)] == [
        'run_sync', '_run_sync_steps']


def test_the_drop_module_logs_as_the_sync():
    assert wiki_sync_drops.log.name == 'services.wiki_sync'


# ── The seam: the drop step runs its passes by the drop module's names ───────

@pytest.mark.parametrize('name', DROP_PASSES)
def test_a_pass_replaced_on_the_drop_module_is_the_pass_the_drop_step_runs(
        app, monkeypatch, name):
    monkeypatch.setattr(wiki_sync_requests, '_fetch_drop_tables', lambda: None)
    for index, other in enumerate(DROP_PASSES):
        monkeypatch.setattr(wiki_sync_drops, other, lambda *args, index=index: 10 ** index)
    monkeypatch.setattr(wiki_sync_drops, name, lambda *args: 1000)
    with app.app_context():
        total = wiki_sync._sync_drop_sources()
    replaced = 10 ** DROP_PASSES.index(name)
    assert total == sum(10 ** i for i in range(len(DROP_PASSES))) - replaced + 1000


@pytest.mark.parametrize('name', ['_sync_wfcd_component_drops', '_sync_resource_drops'])
def test_a_pass_replaced_on_the_entry_point_leaves_the_drop_step_unchanged(
        app, monkeypatch, name):
    _replace_wfcd_and_tables(monkeypatch)
    monkeypatch.setattr(wiki_sync, name, _refuse)
    with app.app_context():
        _seed_catalogue()
        total = wiki_sync._sync_drop_sources()
        rows = _drop_rows()
    assert total == len(rows) == 3


# ── A fetch replaced on the requests module reaches each pass ────────────────

def test_a_fetch_replaced_on_the_requests_module_reaches_each_drop_pass(app, monkeypatch):
    _replace_wfcd_and_tables(monkeypatch)
    with app.app_context():
        _seed_catalogue()
        total = wiki_sync._sync_drop_sources()
        rows = _drop_rows()
    assert total == 3
    assert rows == [
        ('Braton', 'mission', 'Mantle — Earth', 14.29),
        ('Ferrite', 'resource', 'Mercury/Apollodorus', 50.0),
        ('Rhino', 'component', 'Venus/Fossa (Assassination)', 22.56),
    ]


# ── Dependency errors: a refused request, a row write that raises ────────────

def test_a_refused_request_in_the_drop_step_writes_nothing_and_warns_per_source(
        app, monkeypatch, caplog):
    monkeypatch.setattr(requests, 'get', _refuse)
    with app.app_context(), caplog.at_level(logging.INFO, logger='services.wiki_sync'):
        _seed_catalogue()
        total = wiki_sync._sync_drop_sources()
        rows = _drop_rows()
    assert (total, rows) == (0, [])
    component_files = ['Warframes.json', 'Primary.json', 'Secondary.json', 'Melee.json',
                       'Arch-Gun.json', 'Arch-Melee.json', 'Archwing.json', 'Sentinels.json',
                       'Pets.json']
    assert [(r.name, r.getMessage()) for r in caplog.records if r.levelno == logging.WARNING] == [
        *(('services.wiki_sync', f'Skipping {f} for drop sync: refused') for f in component_files),
        ('services.wiki_sync', 'Could not fetch Resources.json for drop sync: refused'),
        ('services.wiki_sync', 'Could not fetch warframestat drop tables (non-fatal): refused'),
    ]


def _refuse_rows_of(source_type):
    """A trigger that refuses one source type's drop rows (a type from the fixed list below)."""
    return ('CREATE TRIGGER refuse_drop BEFORE INSERT ON drop_sources '
            f"WHEN NEW.source_type = '{source_type}' "
            "BEGIN SELECT RAISE(ABORT, 'write refused'); END")


def _broken_relic_drops(db, tables):
    raise RuntimeError('write refused')


@pytest.mark.parametrize('source_type', ['component', 'resource', 'mission', 'relic'])
def test_a_row_write_that_raises_in_the_drop_step_ends_the_sync_and_keeps_the_old_rows(
        app, monkeypatch, caplog, source_type):
    with app.app_context():
        _seed_catalogue()
        db = get_db()
        db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, "
                   "avg_runs, source_type, notes) SELECT id, 'Survival', 'Old Node', 10, 10, "
                   "'mission', 'kept' FROM items WHERE name = 'Braton'")
        if source_type != 'relic':
            db.execute(_refuse_rows_of(source_type))
        db.commit()
        before = _drop_rows()
    for index, name in enumerate(STEPS):
        monkeypatch.setattr(wiki_sync, name, lambda *args, index=index: index)
    for name in ('_backfill_null_item_images', '_sync_wiki_images_for_missing',
                 '_sync_component_wiki_images'):
        monkeypatch.setattr(wiki_sync, name, lambda *args: 0)
    for name in ('_sync_item_images', '_sync_component_images'):
        monkeypatch.setattr(wiki_sync, name, lambda *args: (0, 0))
    monkeypatch.setattr(wiki_sync, 'sync_resurgence', lambda: {'ok': True})
    monkeypatch.setattr(wiki_sync, 'sync_market_slugs', lambda: {'ok': True, 'matched': 0})
    _replace_wfcd_and_tables(monkeypatch)
    if source_type == 'relic':
        monkeypatch.setattr(relic_sync, 'sync_relic_drops', _broken_relic_drops)
    with caplog.at_level(logging.ERROR, logger='services.wiki_sync'):
        result = wiki_sync.run_sync(app)
    with app.app_context():
        after = _drop_rows()
    assert result == {'ok': False, 'error': 'write refused'}
    assert [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.ERROR] == [
        ('services.wiki_sync', 'Sync failed: write refused')]
    assert after == before == [('Braton', 'mission', 'Old Node', 10.0)]
