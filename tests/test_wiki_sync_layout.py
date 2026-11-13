"""The item sync's modules: one entry point, one requests seam, one logger name.

``services.wiki_sync`` runs the sync and binds every step as a name of its own, so a test or
an operator that replaces ``wiki_sync._sync_<step>`` replaces the step ``run_sync`` runs. Every
HTTP request lives in ``services.wiki_sync_requests`` and every other module calls it through
that module's attribute, so one replacement there covers every caller. Every module logs as
``services.wiki_sync``. Requests are mocked at ``requests.get`` or at the requests module;
no test reaches the network.
"""
import ast
import inspect
import logging

import pytest
import requests

from models.db import get_db
from models.item import Item
from services import (image_cache, resurgence_sync, wiki_sync, wiki_sync_amps,
                      wiki_sync_catalogue, wiki_sync_gear, wiki_sync_images,
                      wiki_sync_ingredients, wiki_sync_kitguns, wiki_sync_mods,
                      wiki_sync_requests)

SYNC_MODULES = [wiki_sync, wiki_sync_requests, wiki_sync_catalogue, wiki_sync_gear,
                wiki_sync_kitguns, wiki_sync_amps, wiki_sync_mods, wiki_sync_ingredients,
                wiki_sync_images]
# The modules that call a request without being the requests module.
CALLERS = [wiki_sync, wiki_sync_catalogue, wiki_sync_gear, wiki_sync_kitguns, wiki_sync_amps,
           wiki_sync_mods, wiki_sync_ingredients, wiki_sync_images, resurgence_sync]
FETCHES = sorted(name for name, obj in vars(wiki_sync_requests).items()
                 if inspect.isfunction(obj) and obj.__module__ == wiki_sync_requests.__name__)

STEPS = ['_sync_warframes', '_sync_weapons', '_sync_kitgun_chambers', '_sync_companions',
         '_sync_archwings', '_sync_k_drives', '_sync_amps', '_sync_mods', '_sync_arcanes',
         '_sync_resources', '_sync_component_ingredients', '_sync_drop_sources']
# Every name other code or the tests reach on services.wiki_sync.
ENTRY_NAMES = ['run_sync', 'log', 'image_cache', 'sync_resurgence', 'sync_market_slugs',
               '_backfill_null_item_images', '_sync_wiki_images_for_missing', '_sync_item_images',
               '_sync_component_wiki_images', '_sync_component_images',
               '_sync_wfcd_component_drops', '_sync_resource_drops', *STEPS]


def _tree(module) -> ast.Module:
    return ast.parse(inspect.getsource(module))


def _imports(module) -> list[tuple[str, str]]:
    """(module imported from, name) for every import in the module, nested ones included."""
    found = []
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Import):
            found += [(alias.name, '') for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found += [(node.module or '', alias.name) for alias in node.names]
    return found


# ── Toolchain: the import graph and the logger ───────────────────────────────

def test_the_requests_module_holds_every_fetch():
    assert FETCHES == ['_fetch_drop_tables', '_fetch_wfcd', '_fetch_wiki_module',
                       '_wiki_batch_images', '_wiki_category_members']


@pytest.mark.parametrize('module', SYNC_MODULES, ids=lambda m: m.__name__)
def test_every_sync_module_logs_as_the_sync(module):
    assert module.log.name == 'services.wiki_sync'


@pytest.mark.parametrize('module', [wiki_sync_requests, wiki_sync_catalogue, wiki_sync_gear,
                                    wiki_sync_kitguns, wiki_sync_amps, wiki_sync_mods,
                                    wiki_sync_ingredients, wiki_sync_images, resurgence_sync],
                         ids=lambda m: m.__name__)
def test_no_module_the_sync_imports_imports_the_entry_point(module):
    imported = _imports(module)
    assert [pair for pair in imported if pair[0] == 'services.wiki_sync'] == []
    assert ('services', 'wiki_sync') not in imported


@pytest.mark.parametrize('module', CALLERS, ids=lambda m: m.__name__)
def test_no_caller_imports_a_fetch_by_name(module):
    assert [name for source, name in _imports(module)
            if source == 'services.wiki_sync_requests' and name in FETCHES] == []


@pytest.mark.parametrize('module', CALLERS, ids=lambda m: m.__name__)
def test_every_fetch_is_called_through_the_requests_module(module):
    bare, qualified = [], []
    for node in ast.walk(_tree(module)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in FETCHES:
            bare.append(node.func.id)
        if (isinstance(node.func, ast.Attribute) and node.func.attr in FETCHES
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'wiki_sync_requests'):
            qualified.append(node.func.attr)
    assert bare == []
    assert bool(qualified) == (module is not wiki_sync_catalogue)   # the helpers request nothing


@pytest.mark.parametrize('module', [wiki_sync, wiki_sync_catalogue, wiki_sync_gear,
                                    wiki_sync_kitguns, wiki_sync_amps, wiki_sync_mods,
                                    wiki_sync_ingredients, wiki_sync_images],
                         ids=lambda m: m.__name__)
def test_only_the_requests_module_imports_requests(module):
    assert ('requests', '') not in _imports(module)
    assert not hasattr(module, 'requests')


# ── Interface: the names reached on the entry point ──────────────────────────

@pytest.mark.parametrize('name', ENTRY_NAMES)
def test_every_name_reached_on_the_entry_point_stays_there(name):
    assert hasattr(wiki_sync, name)


def _replace_every_step(monkeypatch):
    """Each step answers its own index; the image steps and the readers answer nothing."""
    for index, name in enumerate(STEPS):
        monkeypatch.setattr(wiki_sync, name, lambda *args, index=index: 100 + index)
    monkeypatch.setattr(wiki_sync, '_backfill_null_item_images', lambda db, cats: 0)
    monkeypatch.setattr(wiki_sync, '_sync_wiki_images_for_missing', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_item_images', lambda db: (0, 0))
    monkeypatch.setattr(wiki_sync, '_sync_component_wiki_images', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_component_images', lambda db: (0, 0))
    monkeypatch.setattr(wiki_sync, 'sync_resurgence', lambda: {'ok': True})
    monkeypatch.setattr(wiki_sync, 'sync_market_slugs', lambda: {'ok': True, 'matched': 0})


@pytest.mark.parametrize('count_key, step', [
    ('warframes', '_sync_warframes'), ('weapons', '_sync_weapons'),
    ('kitgun_chambers', '_sync_kitgun_chambers'), ('companions', '_sync_companions'),
    ('archwings', '_sync_archwings'), ('k_drives', '_sync_k_drives'), ('amps', '_sync_amps'),
    ('mods', '_sync_mods'), ('arcanes', '_sync_arcanes'), ('resources', '_sync_resources'),
    ('ingredients', '_sync_component_ingredients'), ('drop_sources', '_sync_drop_sources'),
])
def test_a_step_replaced_on_the_entry_point_is_the_step_the_sync_runs(
        app, monkeypatch, count_key, step):
    _replace_every_step(monkeypatch)
    result = wiki_sync.run_sync(app)
    assert result['ok'] is True
    assert result['counts'][count_key] == 100 + STEPS.index(step)


# ── Dependency errors: a request that fails in the requests module ───────────

def _refuse(*args, **kwargs):
    raise requests.ConnectionError('refused')


def test_a_failing_wfcd_request_ends_the_sync_as_a_failed_result(app, monkeypatch, caplog):
    monkeypatch.setattr(requests, 'get', _refuse)
    with caplog.at_level(logging.ERROR, logger='services.wiki_sync'):
        result = wiki_sync.run_sync(app)
    assert result == {'ok': False, 'error': 'refused'}
    assert [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.ERROR] == [
        ('services.wiki_sync', 'Sync failed: refused')]


def test_kitgun_chambers_fall_back_to_the_curated_list_when_every_request_fails(
        app, monkeypatch, caplog):
    monkeypatch.setattr(requests, 'get', _refuse)
    with app.app_context(), caplog.at_level(logging.INFO, logger='services.wiki_sync'):
        count = wiki_sync._sync_kitgun_chambers()
        names = {r['name'] for r in get_db().execute(
            "SELECT name FROM items WHERE category = 'secondary' AND source = 'kitgun'")}
    assert count == len(wiki_sync_kitguns._KNOWN_KITGUN_CHAMBERS) == len(names)
    assert names == set(wiki_sync_kitguns._KNOWN_KITGUN_CHAMBERS)
    messages = [r.getMessage() for r in caplog.records if r.name == 'services.wiki_sync']
    assert 'WFCD Secondary.json kitgun scan failed (refused)' in messages
    assert 'WFCD Misc.json kitgun scan failed (refused)' in messages
    assert 'Wiki Category:Kitgun Chambers lookup failed (refused)' in messages


# ── The seam: one replacement in the requests module reaches the moved step ──

def test_a_fetch_replaced_on_the_requests_module_reaches_kitgun_chambers(app, monkeypatch):
    files = {'Secondary.json': [{'name': 'Kitgun', 'type': 'Secondary', 'components': [
        {'name': 'Catchmoon Chamber', 'imageName': 'catchmoon.png'}]}]}
    asked = []
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd',
                        lambda name: asked.append(name) or files.get(name, []))
    monkeypatch.setattr(wiki_sync_requests, '_wiki_category_members', lambda category: [])
    with app.app_context():
        wiki_sync._sync_kitgun_chambers()
        row = get_db().execute(
            "SELECT image_url FROM items WHERE name = 'Catchmoon' AND source = 'kitgun'"
        ).fetchone()
    assert asked == ['Secondary.json', 'Misc.json']
    assert row['image_url'] == 'https://cdn.warframestat.us/img/catchmoon.png'


# The steps that moved out of the entry point, each with the module that holds it.
MOVED_STEPS = [
    (wiki_sync_gear, '_sync_warframes'), (wiki_sync_gear, '_sync_weapons'),
    (wiki_sync_gear, '_sync_companions'), (wiki_sync_gear, '_sync_archwings'),
    (wiki_sync_gear, '_sync_k_drives'), (wiki_sync_kitguns, '_sync_kitgun_chambers'),
    (wiki_sync_amps, '_sync_amps'), (wiki_sync_mods, '_sync_mods'),
    (wiki_sync_mods, '_sync_arcanes'), (wiki_sync_mods, '_sync_resources'),
    (wiki_sync_ingredients, '_sync_component_ingredients'),
    (wiki_sync_images, '_backfill_null_item_images'),
    (wiki_sync_images, '_sync_wiki_images_for_missing'),
    (wiki_sync_images, '_sync_item_images'),
    (wiki_sync_images, '_sync_component_wiki_images'),
    (wiki_sync_images, '_sync_component_images'),
]


@pytest.mark.parametrize('module, step', MOVED_STEPS, ids=lambda v: getattr(v, '__name__', v))
def test_the_entry_point_binds_each_moved_step_from_the_module_that_holds_it(module, step):
    assert getattr(wiki_sync, step) is getattr(module, step)
    assert getattr(module, step).__module__ == module.__name__


# One masterable entry per WFCD file the gear steps read; a Pets.json MOA goes to k-drive.
GEAR_FILES = {
    'Warframes.json': [{'name': 'Excalibur', 'masterable': True}],
    'Primary.json': [{'name': 'Braton', 'masterable': True, 'type': 'Rifle'}],
    'Secondary.json': [], 'Melee.json': [], 'Arch-Gun.json': [], 'Arch-Melee.json': [],
    'Pets.json': [{'name': 'Kavasa Kubrow', 'type': 'Pets'}, {'name': 'Oloro Moa', 'type': 'Pets'}],
    'Sentinels.json': [{'name': 'Carrier', 'masterable': True}],
    'Archwing.json': [{'name': 'Odonata', 'masterable': True}],
    'Amps.json': [{'name': 'Raplak Prism', 'masterable': True}],
}


@pytest.mark.parametrize('step, category, name', [
    ('_sync_warframes', 'warframe', 'Excalibur'), ('_sync_weapons', 'primary', 'Braton'),
    ('_sync_companions', 'companion', 'Carrier'), ('_sync_archwings', 'archwing', 'Odonata'),
    ('_sync_k_drives', 'k-drive', 'Oloro Moa'), ('_sync_amps', 'amp', 'Raplak'),
])
def test_a_fetch_replaced_on_the_requests_module_reaches_each_gear_step(
        app, monkeypatch, step, category, name):
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd', lambda f: GEAR_FILES.get(f, []))
    monkeypatch.setattr(wiki_sync_requests, '_wiki_category_members', lambda category: [])
    monkeypatch.setattr(wiki_sync_requests, '_wiki_batch_images', lambda titles: {})
    with app.app_context():
        count = getattr(wiki_sync, step)()
        row = get_db().execute(
            'SELECT 1 FROM items WHERE name = ? AND category = ?', (name, category)).fetchone()
    assert count >= 1
    assert row is not None


@pytest.mark.parametrize('step', ['_sync_warframes', '_sync_weapons', '_sync_companions',
                                  '_sync_archwings', '_sync_k_drives'])
def test_a_refused_request_in_a_gear_step_reaches_the_caller(app, monkeypatch, step):
    monkeypatch.setattr(requests, 'get', _refuse)
    with app.app_context(), pytest.raises(requests.ConnectionError, match='refused'):
        getattr(wiki_sync, step)()


def test_a_row_write_that_raises_in_a_gear_step_ends_the_sync_as_a_failed_result(
        app, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError('write refused')
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd', lambda f: GEAR_FILES.get(f, []))
    monkeypatch.setattr(wiki_sync_gear, '_upsert_wfcd', broken)
    with caplog.at_level(logging.ERROR, logger='services.wiki_sync'):
        result = wiki_sync.run_sync(app)
    assert result == {'ok': False, 'error': 'write refused'}
    assert [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.ERROR] == [
        ('services.wiki_sync', 'Sync failed: write refused')]


def test_amps_fall_back_to_the_curated_list_when_every_request_fails(app, monkeypatch, caplog):
    monkeypatch.setattr(requests, 'get', _refuse)
    with app.app_context(), caplog.at_level(logging.INFO, logger='services.wiki_sync'):
        count = wiki_sync._sync_amps()
        names = {r['name'] for r in get_db().execute(
            "SELECT name FROM items WHERE category = 'amp'")}
    assert count == len(wiki_sync_amps._KNOWN_AMPS) == len(names)
    assert names == set(wiki_sync_amps._KNOWN_AMPS)
    messages = [r.getMessage() for r in caplog.records if r.name == 'services.wiki_sync']
    assert 'WFCD Amps.json not available (refused) — trying Misc.json' in messages
    assert 'Could not fetch WFCD Misc.json (refused) — falling back to wiki' in messages
    assert 'Wiki Category:Amps lookup failed (refused) — using hardcoded fallback' in messages


# ── Mods, arcanes and resources ──────────────────────────────────────────────

# One entry per WFCD file the mod steps read; two rivens of one class collapse to one row.
MOD_FILES = {
    'Mods.json': [{'name': 'Serration', 'type': 'Rifle Mod', 'polarity': 'Madurai',
                   'fusionLimit': 10, 'tradable': True},
                  {'name': 'Rifle Riven Mod', 'type': 'Rifle Riven Mod'},
                  {'name': 'Veiled Rifle Riven', 'type': 'Rifle Riven Mod'}],
    'Arcanes.json': [{'name': 'Arcane Energize', 'fusionLimit': 5}],
    'Resources.json': [{'name': 'Ferrite', 'tradable': False}],
}


@pytest.mark.parametrize('step, category, names', [
    ('_sync_mods', 'mod', {'Serration', 'Rifle Riven Mod'}),
    ('_sync_arcanes', 'arcane', {'Arcane Energize'}),
    ('_sync_resources', 'resource', {'Ferrite'}),
])
def test_a_fetch_replaced_on_the_requests_module_reaches_each_mod_step(
        app, monkeypatch, step, category, names):
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd', lambda f: MOD_FILES.get(f, []))
    with app.app_context():
        count = getattr(wiki_sync, step)()
        found = {r['name'] for r in get_db().execute(
            'SELECT name FROM items WHERE category = ?', (category,))}
    assert count == len(names)
    assert found == names


@pytest.mark.parametrize('step, message', [
    ('_sync_mods', 'Could not fetch Mods.json: refused'),
    ('_sync_resources', 'Could not fetch Resources.json: refused'),
])
def test_a_refused_request_in_mods_or_resources_syncs_nothing_and_warns(
        app, monkeypatch, caplog, step, message):
    monkeypatch.setattr(requests, 'get', _refuse)
    with app.app_context(), caplog.at_level(logging.INFO, logger='services.wiki_sync'):
        count = getattr(wiki_sync, step)()
    assert count == 0
    assert [(r.name, r.levelno) for r in caplog.records if r.getMessage() == message] == [
        ('services.wiki_sync', logging.WARNING)]


def test_arcanes_fall_back_to_the_curated_list_when_every_request_fails(app, monkeypatch, caplog):
    monkeypatch.setattr(requests, 'get', _refuse)
    with app.app_context(), caplog.at_level(logging.INFO, logger='services.wiki_sync'):
        count = wiki_sync._sync_arcanes()
        names = {r['name'] for r in get_db().execute(
            "SELECT name FROM items WHERE category = 'arcane'")}
    assert count == len(wiki_sync_mods._KNOWN_ARCANES) == len(names)
    assert names == set(wiki_sync_mods._KNOWN_ARCANES)
    messages = [r.getMessage() for r in caplog.records if r.name == 'services.wiki_sync']
    assert 'WFCD Arcanes.json not available (refused) — trying Misc.json' in messages
    assert 'Could not fetch WFCD Misc.json (refused) — falling back to wiki' in messages
    assert 'Wiki Category:Arcanes lookup failed (refused) — using hardcoded fallback' in messages


class _BrokenItem:
    @staticmethod
    def upsert(**kwargs):
        raise RuntimeError('write refused')


@pytest.mark.parametrize('step', ['_sync_mods', '_sync_arcanes', '_sync_resources'])
def test_a_row_write_that_raises_in_a_mod_step_ends_the_sync_as_a_failed_result(
        app, monkeypatch, caplog, step):
    _replace_every_step(monkeypatch)
    monkeypatch.setattr(wiki_sync, step, getattr(wiki_sync_mods, step))
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd', lambda f: MOD_FILES.get(f, []))
    monkeypatch.setattr(wiki_sync_mods, 'Item', _BrokenItem)
    with caplog.at_level(logging.ERROR, logger='services.wiki_sync'):
        result = wiki_sync.run_sync(app)
    assert result == {'ok': False, 'error': 'write refused'}
    assert [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.ERROR] == [
        ('services.wiki_sync', 'Sync failed: write refused')]


# ── Component ingredients ────────────────────────────────────────────────────

# A weapon whose component has a recipe (WFCD; its Forma is skipped) and a warframe part
# whose recipe is in the wiki's blueprints module.
INGREDIENT_FILES = {
    'Primary.json': [{'name': 'Braton', 'masterable': True, 'components': [
        {'name': 'Barrel', 'imageName': 'barrel.png',
         'components': [{'name': 'Alloy Plate', 'itemCount': 50},
                        {'name': 'Forma', 'itemCount': 1}]}]}],
}
BLUEPRINTS_MODULE = (
    '["WispChassisBlueprint"] = { Result = "Wisp Chassis", '
    'Parts = { { Count = 2, Name = "Morphics" } } },')


def _seed_ingredient_owners():
    for name, category in (('Braton', 'primary'), ('Wisp', 'warframe')):
        Item.upsert(name=name, category=category, source='standard')
    get_db().commit()


def _ingredient_rows():
    return sorted(tuple(r) for r in get_db().execute(
        'SELECT i.name, c.component_name, c.ingredient_name, c.ingredient_qty '
        'FROM component_ingredients c JOIN items i ON i.id = c.item_id'))


def test_a_fetch_replaced_on_the_requests_module_reaches_the_ingredient_step(app, monkeypatch):
    modules = []
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd',
                        lambda f: INGREDIENT_FILES.get(f, []))
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wiki_module',
                        lambda title: modules.append(title) or BLUEPRINTS_MODULE)
    status = {'complete': True}
    with app.app_context():
        _seed_ingredient_owners()
        count = wiki_sync._sync_component_ingredients(status)
        rows = _ingredient_rows()
        resources = {r['name'] for r in get_db().execute(
            "SELECT name FROM items WHERE category = 'resource'")}
        meta = [tuple(r) for r in get_db().execute(
            'SELECT component_name, image_url FROM item_component_meta')]
    assert count == 2
    assert rows == [('Braton', 'Barrel', 'Alloy Plate', 50), ('Wisp', 'Chassis', 'Morphics', 2)]
    assert resources == {'Alloy Plate', 'Morphics'}
    assert meta == [('Barrel', 'https://cdn.warframestat.us/img/barrel.png')]
    assert modules == ['Module:Blueprints/data']
    assert status == {'complete': True}


def test_a_refused_request_in_the_ingredient_step_skips_each_source_and_warns(
        app, monkeypatch, caplog):
    monkeypatch.setattr(requests, 'get', _refuse)
    status = {'complete': True}
    with app.app_context(), caplog.at_level(logging.INFO, logger='services.wiki_sync'):
        _seed_ingredient_owners()
        count = wiki_sync._sync_component_ingredients(status)
        rows = _ingredient_rows()
    assert count == 0
    assert rows == []
    assert status == {'complete': False}
    warnings = [(r.name, r.getMessage()) for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        ('services.wiki_sync', f'Skipping {filename} for ingredient sync: refused')
        for filename, _ in wiki_sync_ingredients._INGREDIENT_SOURCES
    ] + [('services.wiki_sync', 'Could not fetch wiki blueprints module (non-fatal): refused')]


@pytest.mark.parametrize('files, module', [(INGREDIENT_FILES, ''), ({}, BLUEPRINTS_MODULE)],
                         ids=['wfcd', 'wiki'])
def test_a_row_write_that_raises_in_the_ingredient_step_ends_the_sync_as_a_failed_result(
        app, monkeypatch, caplog, files, module):
    with app.app_context():
        _seed_ingredient_owners()
    _replace_every_step(monkeypatch)
    monkeypatch.setattr(wiki_sync, '_sync_component_ingredients',
                        wiki_sync_ingredients._sync_component_ingredients)
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd', lambda f: files.get(f, []))
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wiki_module', lambda title: module)
    monkeypatch.setattr(wiki_sync_ingredients, 'Item', _BrokenItem)
    with caplog.at_level(logging.ERROR, logger='services.wiki_sync'):
        result = wiki_sync.run_sync(app)
    assert result == {'ok': False, 'error': 'write refused'}
    assert [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.ERROR] == [
        ('services.wiki_sync', 'Sync failed: write refused')]


# ── Images ───────────────────────────────────────────────────────────────────

THUMB = 'https://wiki.example/thumb/{}.png'
# The three wiki passes: how each is called, the table it writes, a title it asks for.
WIKI_PASSES = {
    '_backfill_null_item_images': (lambda db: wiki_sync._backfill_null_item_images(
        db, ['primary']), 'items', 'Braton'),
    '_sync_wiki_images_for_missing': (lambda db: wiki_sync._sync_wiki_images_for_missing(db),
                                      'items', 'Braton'),
    '_sync_component_wiki_images': (lambda db: wiki_sync._sync_component_wiki_images(db),
                                    'item_component_meta', 'Excalibur Chassis Blueprint'),
}
# The two cache passes and the table each writes.
CACHE_PASSES = {'_sync_item_images': 'items', '_sync_component_images': 'item_component_meta'}


def _seed_image_rows(remote: bool):
    """A weapon and a warframe with one component; each with a remote picture URL or none."""
    db = get_db()
    for name, category in (('Braton', 'primary'), ('Excalibur', 'warframe')):
        Item.upsert(name=name, category=category, source='standard',
                    image_url=THUMB.format(name) if remote else None)
    frame = db.execute("SELECT id FROM items WHERE name = 'Excalibur'").fetchone()['id']
    db.execute('INSERT INTO item_component_meta (item_id, component_name, image_url) '
               'VALUES (?, ?, ?)', (frame, 'Chassis', THUMB.format('Chassis') if remote else None))
    db.commit()
    return db


def _image_urls(db, table):
    return sorted(r['image_url'] for r in db.execute(
        f'SELECT image_url FROM {table} WHERE image_url IS NOT NULL'))


def _save_picture(url, dest):
    dest.write_bytes(b'picture')
    return True


@pytest.mark.parametrize('name, replacement, counts', [
    ('_backfill_null_item_images', lambda db, cats: 201, {'weapon_images_backfilled': 201}),
    ('_sync_wiki_images_for_missing', lambda db: 202, {'images_wiki_fetched': 202}),
    ('_sync_item_images', lambda db: (203, 204),
     {'images_downloaded': 203, 'images_already_cached': 204}),
    ('_sync_component_wiki_images', lambda db: 205, {'comp_images_wiki_fetched': 205}),
    ('_sync_component_images', lambda db: (206, 207),
     {'comp_images_downloaded': 206, 'comp_images_already_cached': 207}),
], ids=lambda v: v if isinstance(v, str) else '')
def test_an_image_pass_replaced_on_the_entry_point_is_the_pass_the_sync_runs(
        app, monkeypatch, name, replacement, counts):
    _replace_every_step(monkeypatch)
    monkeypatch.setattr(wiki_sync, name, replacement)
    result = wiki_sync.run_sync(app)
    assert result['ok'] is True
    assert {key: result['counts'][key] for key in counts} == counts


@pytest.mark.parametrize('name', WIKI_PASSES)
def test_a_fetch_replaced_on_the_requests_module_reaches_each_wiki_image_pass(
        app, monkeypatch, name):
    run, table, title = WIKI_PASSES[name]
    asked = []
    monkeypatch.setattr(wiki_sync_requests, '_wiki_batch_images', lambda titles: asked.extend(
        titles) or {t: THUMB.format(t) for t in titles})
    with app.app_context():
        db = _seed_image_rows(remote=False)
        count = run(db)
        urls = _image_urls(db, table)
    assert title in asked
    assert count == len(asked) == len(urls)
    assert THUMB.format(title) in urls


@pytest.mark.parametrize('name', WIKI_PASSES)
def test_a_refused_request_in_a_wiki_image_pass_writes_nothing_and_warns(
        app, monkeypatch, caplog, name):
    run, table, _ = WIKI_PASSES[name]
    monkeypatch.setattr(requests, 'get', _refuse)
    with app.app_context(), caplog.at_level(logging.INFO, logger='services.wiki_sync'):
        db = _seed_image_rows(remote=False)
        count = run(db)
        urls = _image_urls(db, table)
    assert (count, urls) == (0, [])
    assert [(r.name, r.getMessage()) for r in caplog.records if r.levelno == logging.WARNING] == [
        ('services.wiki_sync', 'Wiki batch image fetch failed: refused')]


@pytest.mark.parametrize('name', CACHE_PASSES)
def test_a_download_replaced_on_the_cache_module_reaches_each_cache_pass(app, monkeypatch, name):
    monkeypatch.setattr(image_cache, 'download', _save_picture)
    with app.app_context():
        db = _seed_image_rows(remote=True)
        counts = getattr(wiki_sync, name)(db)
        urls = _image_urls(db, CACHE_PASSES[name])
    assert counts == (len(urls), 0)
    assert urls and all(url.startswith('/static/images/') for url in urls)


@pytest.mark.parametrize('name', CACHE_PASSES)
def test_a_refused_download_in_a_cache_pass_keeps_the_remote_url(app, monkeypatch, name):
    monkeypatch.setattr(requests, 'get', _refuse)
    with app.app_context():
        db = _seed_image_rows(remote=True)
        before = _image_urls(db, CACHE_PASSES[name])
        counts = getattr(wiki_sync, name)(db)
        after = _image_urls(db, CACHE_PASSES[name])
    assert counts == (0, 0)
    assert after == before != []


@pytest.mark.parametrize('name, table', [*((n, v[1]) for n, v in WIKI_PASSES.items()),
                                         *CACHE_PASSES.items()])
def test_a_row_write_that_raises_in_an_image_pass_ends_the_sync_as_a_failed_result(
        app, monkeypatch, caplog, name, table):
    with app.app_context():
        db = _seed_image_rows(remote=name in CACHE_PASSES)
        db.execute(f'CREATE TRIGGER refuse_picture BEFORE UPDATE OF image_url ON {table} '
                   "BEGIN SELECT RAISE(ABORT, 'write refused'); END")
        db.commit()
    _replace_every_step(monkeypatch)
    monkeypatch.setattr(wiki_sync, name, getattr(wiki_sync_images, name))
    monkeypatch.setattr(wiki_sync_requests, '_wiki_batch_images',
                        lambda titles: {t: THUMB.format(t) for t in titles})
    monkeypatch.setattr(image_cache, 'download', _save_picture)
    with caplog.at_level(logging.ERROR, logger='services.wiki_sync'):
        result = wiki_sync.run_sync(app)
    assert result == {'ok': False, 'error': 'write refused'}
    assert [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.ERROR] == [
        ('services.wiki_sync', 'Sync failed: write refused')]
