"""The item sync's shared catalogue functions are called through their module.

Every function of ``services.wiki_sync_catalogue`` is called as
``wiki_sync_catalogue.<name>(...)``, never through a name imported from it: Python binds a
``from`` import once, when the importing module loads, so a replacement on the catalogue
module would reach no step that had imported the old function by name; a module attribute is
looked up at every call. The catalogue calls its own functions by bare name, which looks up
the module's globals at call time, so a replacement reaches those calls too. A catalogue
function is never read as a value outside a call, whatever it is read through (the module's
name, a dotted import path, an alias made by assignment, a call's result), and no module but
the catalogue names one as a string (``getattr``, ``vars()[...]``, ``__dict__[...]``): such a
read binds the function once, like a ``from`` import; so does a catalogue function's bare
name read as a value outside the catalogue. The catalogue module itself, by its name or its
dotted import path, is read only as the value of an attribute read, and never through a dunder
attribute (``__dict__``, ``__getattribute__``): passed to ``getattr``, ``vars`` or any call,
assigned, used as a default argument or iterated, it lets a function be named at run time
where no check reads it; so does the module's name as a string (``getattr(services, ...)``,
``importlib.import_module(...)``, ``sys.modules[...]``). Constants may be imported by name. Requests are
mocked at the requests module; no test reaches the network.
"""
import ast
import inspect

import pytest

from models.db import get_db
from services import (resurgence_sync, wiki_sync, wiki_sync_amps, wiki_sync_catalogue,
                      wiki_sync_gear, wiki_sync_ingredients, wiki_sync_kitguns, wiki_sync_mods,
                      wiki_sync_requests)
from test_wiki_sync_layout import (GEAR_FILES, INGREDIENT_FILES, MOD_FILES, SYNC_MODULES,
                                   _seed_ingredient_owners, _tree)

# The catalogue's functions, read from the module so a function added later is covered.
HELPERS = sorted(name for name, obj in vars(wiki_sync_catalogue).items()
                 if inspect.isfunction(obj) and obj.__module__ == wiki_sync_catalogue.__name__)
# The steps that call the catalogue's functions.
STEP_MODULES = [wiki_sync_gear, wiki_sync_kitguns, wiki_sync_amps, wiki_sync_mods,
                wiki_sync_ingredients]
MODULES = [*SYNC_MODULES, resurgence_sync]
CATALOGUE_NAMES = ('wiki_sync_catalogue', 'services.wiki_sync_catalogue')


# ── Toolchain: the import graph and the call sites ───────────────────────────

def test_the_catalogue_module_holds_every_shared_function():
    assert HELPERS == ['_cleanup_source_components', '_detect_source', '_upsert_wfcd',
                       '_upsert_wiki', '_wfcd_image_url']


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__)
def test_no_sync_module_imports_a_catalogue_function_by_name(module):
    # Absolute or relative (`from .wiki_sync_catalogue import ...`), aliased or not, nested
    # or not: the catalogue is matched by the last part of the module named.
    assert [alias.name for node in ast.walk(_tree(module)) if isinstance(node, ast.ImportFrom)
            and (node.module or '').rsplit('.', 1)[-1] == 'wiki_sync_catalogue'
            for alias in node.names if alias.name in HELPERS] == []


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__)
def test_the_catalogue_module_is_bound_under_its_own_name(module):
    tree = _tree(module)
    aliases = [alias.asname for node in ast.walk(tree)
               if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
               if alias.name in CATALOGUE_NAMES]
    assert [asname for asname in aliases if asname not in (None, 'wiki_sync_catalogue')] == []
    # The module read as a whole object (passed to `getattr`, `vars` or any call, assigned,
    # used as a default argument, iterated), by its bare name or by its dotted path
    # (`services.wiki_sync_catalogue`), lets a function be named at run time where no check
    # reads it: only an attribute read keeps the module's own name.
    read = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    catalogue = [node for node in ast.walk(tree)
                 if (isinstance(node, ast.Name) and node.id == 'wiki_sync_catalogue')
                 or (isinstance(node, ast.Attribute) and node.attr == 'wiki_sync_catalogue')]
    assert [ast.unparse(node) for node in catalogue if id(node) not in read] == []
    # A dunder read on the module (`__dict__`, `__getattribute__`) hands its namespace out,
    # where a function is named by a string that no guard reads.
    assert [node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr.startswith('__')
            and any(node.value is c for c in catalogue)] == []
    # The module's name as a string constant (`getattr(services, 'wiki_sync_catalogue')`,
    # `importlib.import_module('services.wiki_sync_catalogue')`, `sys.modules[...]`) reaches
    # the module as a whole object all the same.
    assert [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value in CATALOGUE_NAMES] == []


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__)
def test_every_catalogue_function_is_called_through_the_catalogue_module(module):
    bare, qualified, elsewhere = [], [], []
    tree = _tree(module)
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    # A catalogue function read as a value (an assignment, a default argument) is bound once,
    # like a `from` import: only a call reads the module at call time. Whatever the attribute
    # is read from (the module's name, a dotted import path, an alias made by assignment, a
    # call's result), a read that is not a call is held.
    held = [node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in HELPERS
            and id(node) not in called]
    # A catalogue function's bare name read as a value (after the catalogue's namespace was
    # copied into the module's globals) is held the same way; the catalogue itself may read
    # its own functions by bare name, which looks up its globals at call time.
    bare_held = [node.id for node in ast.walk(tree)
                 if isinstance(node, ast.Name) and node.id in HELPERS
                 and isinstance(node.ctx, ast.Load) and id(node) not in called]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in HELPERS:
            bare.append(node.func.id)
        if isinstance(node.func, ast.Attribute) and node.func.attr in HELPERS:
            through = (isinstance(node.func.value, ast.Name)
                       and node.func.value.id == 'wiki_sync_catalogue')
            (qualified if through else elsewhere).append(node.func.attr)
    if module is not wiki_sync_catalogue:
        assert bare == []
        assert bare_held == []
    assert elsewhere == []
    assert held == []
    assert bool(qualified) == (module in STEP_MODULES)


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__)
def test_no_sync_module_names_a_catalogue_function_as_a_string(module):
    # A reflective read (`getattr(m, '_upsert_wfcd')`, `vars(m)['_upsert_wfcd']`,
    # `m.__dict__['_upsert_wfcd']`) names the function as a string, never as an attribute, and
    # binds it once all the same: a catalogue function's name as a string constant is refused
    # outright outside the catalogue itself, whatever it is passed to.
    if module is wiki_sync_catalogue:
        return
    assert [node.value for node in ast.walk(_tree(module))
            if isinstance(node, ast.Constant) and node.value in HELPERS] == []


# ── The seam: one replacement on the catalogue module reaches every step ─────

KITGUN_FILES = {'Secondary.json': [{'name': 'Kitgun', 'type': 'Secondary', 'components': [
    {'name': 'Catchmoon Chamber', 'imageName': 'catchmoon.png'}]}]}
MARK = 'https://cdn.example/marked.png'


def _answer_requests(monkeypatch, files, members=None):
    members = members or {}
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wfcd', lambda f: files.get(f, []))
    monkeypatch.setattr(wiki_sync_requests, '_wiki_category_members',
                        lambda category: members.get(category, []))
    monkeypatch.setattr(wiki_sync_requests, '_wiki_batch_images', lambda titles: {})
    monkeypatch.setattr(wiki_sync_requests, '_fetch_wiki_module', lambda title: '')


@pytest.mark.parametrize('step, files, calls', [
    ('_sync_kitgun_chambers', KITGUN_FILES, [('secondary', 'kitgun')]),
    ('_sync_weapons', GEAR_FILES, [('melee', 'zaw')]),
    # Called twice: the replacement is reached both times.
    ('_sync_amps', GEAR_FILES, [('amp', 'standard'), ('amp', 'prime')]),
])
def test_a_cleanup_replaced_on_the_catalogue_module_is_called_by_each_step(
        app, monkeypatch, step, files, calls):
    _answer_requests(monkeypatch, files)
    called = []
    monkeypatch.setattr(wiki_sync_catalogue, '_cleanup_source_components',
                        lambda category, source, names: called.append((category, source)) or 0)
    with app.app_context():
        getattr(wiki_sync, step)()
    assert called == calls


@pytest.mark.parametrize('step, calls', [
    ('_sync_warframes', [('Excalibur', 'warframe')]),
    # Called once per file: the replacement is reached both times.
    ('_sync_companions', [('Kavasa Kubrow', 'companion'), ('Carrier', 'companion')]),
    ('_sync_archwings', [('Odonata', 'archwing')]),
    ('_sync_k_drives', [('Oloro Moa', 'k-drive')]),
])
def test_a_wfcd_upsert_replaced_on_the_catalogue_module_is_called_by_each_gear_step(
        app, monkeypatch, step, calls):
    _answer_requests(monkeypatch, GEAR_FILES)
    called = []
    monkeypatch.setattr(wiki_sync_catalogue, '_upsert_wfcd',
                        lambda item, category, **kwargs:
                        called.append((item['name'], category)) or True)
    with app.app_context():
        count = getattr(wiki_sync, step)()
    assert called == calls
    assert count == len(calls)


def test_a_wiki_upsert_replaced_on_the_catalogue_module_is_called_by_the_k_drive_step(
        app, monkeypatch):
    _answer_requests(monkeypatch, {}, {'K-Drive': ['Flatbelly'], 'Necramechs': ['Voidrig']})
    called = []
    monkeypatch.setattr(wiki_sync_catalogue, '_upsert_wiki',
                        lambda title, category: called.append((title, category)) or True)
    with app.app_context():
        count = wiki_sync._sync_k_drives()
    assert called == [('Flatbelly', 'k-drive'), ('Voidrig', 'k-drive')]
    assert count == 2


# The row each step writes a picture URL to; the warframe's comes through the catalogue's
# own bare call inside its WFCD upsert.
IMAGE_ROWS = [
    ('_sync_warframes', GEAR_FILES, "SELECT image_url FROM items WHERE name = 'Excalibur'"),
    ('_sync_kitgun_chambers', KITGUN_FILES,
     "SELECT image_url FROM items WHERE name = 'Catchmoon'"),
    ('_sync_amps', GEAR_FILES, "SELECT image_url FROM items WHERE name = 'Raplak'"),
    ('_sync_mods', MOD_FILES, "SELECT image_url FROM items WHERE name = 'Serration'"),
    ('_sync_mods', MOD_FILES, "SELECT image_url FROM items WHERE name = 'Rifle Riven Mod'"),
    ('_sync_arcanes', MOD_FILES, "SELECT image_url FROM items WHERE name = 'Arcane Energize'"),
    ('_sync_resources', MOD_FILES, "SELECT image_url FROM items WHERE name = 'Ferrite'"),
    ('_sync_component_ingredients', INGREDIENT_FILES,
     "SELECT image_url FROM item_component_meta WHERE component_name = 'Barrel'"),
    ('_sync_component_ingredients', INGREDIENT_FILES,
     "SELECT image_url FROM items WHERE name = 'Alloy Plate'"),
]


@pytest.mark.parametrize('step, files, query', IMAGE_ROWS,
                         ids=[f'{row[0]}-{row[2].rsplit(" = ", 1)[1]}' for row in IMAGE_ROWS])
def test_an_image_url_replaced_on_the_catalogue_module_reaches_each_step(
        app, monkeypatch, step, files, query):
    _answer_requests(monkeypatch, files)
    monkeypatch.setattr(wiki_sync_catalogue, '_wfcd_image_url', lambda obj: MARK)
    with app.app_context():
        _seed_ingredient_owners()
        getattr(wiki_sync, step)()
        row = get_db().execute(query).fetchone()
    assert row['image_url'] == MARK


@pytest.mark.parametrize('step, files, members, name', [
    ('_sync_warframes', GEAR_FILES, {}, 'Excalibur'),
    ('_sync_amps', GEAR_FILES, {}, 'Raplak'),
    ('_sync_k_drives', {}, {'K-Drive': ['Flatbelly']}, 'Flatbelly'),
])
def test_a_source_rule_replaced_on_the_catalogue_module_reaches_each_step(
        app, monkeypatch, step, files, members, name):
    _answer_requests(monkeypatch, files, members)
    monkeypatch.setattr(wiki_sync_catalogue, '_detect_source', lambda title: 'prime')
    with app.app_context():
        getattr(wiki_sync, step)()
        row = get_db().execute('SELECT source FROM items WHERE name = ?', (name,)).fetchone()
    assert row['source'] == 'prime'
