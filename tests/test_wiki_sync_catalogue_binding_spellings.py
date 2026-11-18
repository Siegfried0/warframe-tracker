"""Every spelling that binds a catalogue function at import time is refused by the layout tests.

A step module that holds a catalogue function under a name of its own (a ``from`` import,
absolute or relative, with or without an alias; a module-level assignment, a class attribute,
a positional, keyword-only or lambda default argument, whether the function is read through
the module's name, a dotted import path, an alias made by assignment or a call's result; a
reflective read by its name as a string; its bare name after the catalogue's namespace was
copied into the module; the module itself read as a whole object, by its name or its dotted
path or by its name as a string, where the function's name is built at run time) keeps the
old function when a test replaces it on ``wiki_sync_catalogue``. The layout tests read a module's source; each row hands them a small
synthetic step module and expects at least one of them to refuse it. Nothing is imported or
run from the rows' sources.
"""
import ast

import pytest

import test_wiki_sync_catalogue_calls as calls
import test_wiki_sync_layout as layout
from services import wiki_sync_gear

# Every row also calls a helper through the module, as a real step module does.
QUALIFIED = "def other(item):\n    return wiki_sync_catalogue._detect_source(item['name'])\n"
HEADER = 'from services import wiki_sync_catalogue\n'

SPELLINGS = {
    'from_import': (
        'from services.wiki_sync_catalogue import _upsert_wfcd\n'
        "def step(item):\n    return _upsert_wfcd(item, 'warframe')\n"),
    'from_import_alias': (
        'from services.wiki_sync_catalogue import _upsert_wfcd as upsert\n'
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'from_import_nested': (
        'def step(item):\n'
        '    from services.wiki_sync_catalogue import _upsert_wfcd as upsert\n'
        "    return upsert(item, 'warframe')\n"),
    'module_alias': (
        'from services import wiki_sync_catalogue as cat\n'
        "def step(item):\n    return cat._upsert_wfcd(item, 'warframe')\n"),
    'relative_from_import': (
        'from .wiki_sync_catalogue import _upsert_wfcd\n'
        "def step(item):\n    return _upsert_wfcd(item, 'warframe')\n"),
    'relative_from_import_alias': (
        'from .wiki_sync_catalogue import _upsert_wfcd as upsert\n'
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'relative_from_import_alias_nested': (
        'def step(item):\n'
        '    from .wiki_sync_catalogue import _upsert_wfcd as upsert\n'
        "    return upsert(item, 'warframe')\n"),
    'relative_module_alias': (
        'from . import wiki_sync_catalogue as cat\n'
        "def step(item):\n    return cat._upsert_wfcd(item, 'warframe')\n"),
    'module_level_assignment': (
        'upsert = wiki_sync_catalogue._upsert_wfcd\n'
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'default_argument': (
        'def step(item, upsert=wiki_sync_catalogue._upsert_wfcd):\n'
        "    return upsert(item, 'warframe')\n"),
    'keyword_only_default_argument': (
        'def step(item, *, upsert=wiki_sync_catalogue._upsert_wfcd):\n'
        "    return upsert(item, 'warframe')\n"),
    'lambda_default_argument': (
        "step = lambda item, upsert=wiki_sync_catalogue._upsert_wfcd: upsert(item, 'w')\n"),
    'class_attribute': (
        'class Step:\n    upsert = wiki_sync_catalogue._upsert_wfcd\n'),
    # A dotted import path: the function is read from an attribute, not from a plain name.
    'dotted_path_module_level_assignment': (
        'import services.wiki_sync_catalogue\n'
        'upsert = services.wiki_sync_catalogue._upsert_wfcd\n'
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'dotted_path_default_argument': (
        'import services.wiki_sync_catalogue\n'
        'def step(item, upsert=services.wiki_sync_catalogue._upsert_wfcd):\n'
        "    return upsert(item, 'warframe')\n"),
    # A module alias made by assignment, which the import checks never see.
    'alias_by_assignment_module_level_assignment': (
        'cat = wiki_sync_catalogue\n'
        'upsert = cat._upsert_wfcd\n'
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'alias_by_assignment_default_argument': (
        'cat = wiki_sync_catalogue\n'
        'def step(item, upsert=cat._upsert_wfcd):\n'
        "    return upsert(item, 'warframe')\n"),
    # The module read as a call's result.
    'call_result_module_level_assignment': (
        'import importlib\n'
        "upsert = importlib.import_module('services.wiki_sync_catalogue')._upsert_wfcd\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    # The function's name as a string: never an attribute in the tree.
    'getattr_constant_name': (
        "upsert = getattr(wiki_sync_catalogue, '_upsert_wfcd')\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'getattr_constant_name_default_argument': (
        "def step(item, upsert=getattr(wiki_sync_catalogue, '_upsert_wfcd')):\n"
        "    return upsert(item, 'warframe')\n"),
    'vars_subscript': (
        "upsert = vars(wiki_sync_catalogue)['_upsert_wfcd']\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'dunder_dict_subscript': (
        "upsert = wiki_sync_catalogue.__dict__['_upsert_wfcd']\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'attrgetter_constant_name': (
        'import operator\n'
        "upsert = operator.attrgetter('_upsert_wfcd')(wiki_sync_catalogue)\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    # The catalogue's namespace copied into the module's own globals: the function is then
    # read by its bare name, neither an attribute nor a string.
    'namespace_copy_bare_name_assignment': (
        'globals().update(vars(wiki_sync_catalogue))\n'
        'upsert = _upsert_wfcd\n'
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'namespace_copy_bare_name_default_argument': (
        'globals().update(vars(wiki_sync_catalogue))\n'
        'def step(item, upsert=_upsert_wfcd):\n'
        "    return upsert(item, 'warframe')\n"),
    # The module itself read as a whole object: the function's name is built at run time, so
    # it is neither an attribute nor a string constant in the tree.
    'getattr_name_built_by_concatenation': (
        "upsert = getattr(wiki_sync_catalogue, '_upsert_' + 'wfcd')\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'namespace_copy_by_comprehension': (
        "fns = {n.strip('_'): f for n, f in vars(wiki_sync_catalogue).items()}\n"
        "def step(item):\n    return fns['upsert_wfcd'](item, 'warframe')\n"),
    'dotted_path_getattr_name_built_by_concatenation': (
        'import services.wiki_sync_catalogue\n'
        "upsert = getattr(services.wiki_sync_catalogue, '_upsert_' + 'wfcd')\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'dotted_path_vars_comprehension': (
        'import services.wiki_sync_catalogue\n'
        'fns = {n: f for n, f in vars(services.wiki_sync_catalogue).items()}\n'
        "def step(item):\n    return fns['_upsert_' + 'wfcd'](item, 'warframe')\n"),
    'dunder_dict_get_name_built_by_concatenation': (
        "upsert = wiki_sync_catalogue.__dict__.get('_upsert_' + 'wfcd')\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'dunder_getattribute_name_built_by_concatenation': (
        "upsert = wiki_sync_catalogue.__getattribute__('_upsert_' + 'wfcd')\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'attrgetter_name_built_by_concatenation': (
        'import operator\n'
        "upsert = operator.attrgetter('_upsert_' + 'wfcd')(wiki_sync_catalogue)\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'module_alias_by_assignment': (
        'cat = wiki_sync_catalogue\n'
        "def step(item):\n    return cat._upsert_wfcd(item, 'warframe')\n"),
    'module_passed_as_default_argument': (
        'def step(item, cat=wiki_sync_catalogue):\n'
        "    return cat._upsert_wfcd(item, 'warframe')\n"),
    'module_passed_as_keyword_only_default_argument': (
        'def step(item, *, cat=wiki_sync_catalogue):\n'
        "    return getattr(cat, '_upsert_' + 'wfcd')(item, 'warframe')\n"),
    'module_passed_as_call_argument': (
        'def _pick(module):\n    return getattr(module, \'_upsert_\' + \'wfcd\')\n'
        'upsert = _pick(wiki_sync_catalogue)\n'
        "def step(item):\n    return upsert(item, 'warframe')\n"),
    'module_iterated_in_comprehension': (
        "fns = [getattr(m, '_upsert_' + 'wfcd') for m in (wiki_sync_catalogue,)]\n"
        "def step(item):\n    return fns[0](item, 'warframe')\n"),
}

# The module reached without its name (through ``sys.modules`` or ``importlib`` with a name
# built at run time from parts that are not the module's name) is not visible to any check that reads the source: kept here so a guard
# that starts refusing it turns the row green and is noticed.
UNGUARDED = {
    'sys_modules_name_built_by_concatenation': (
        'import sys\n'
        "upsert = getattr(sys.modules['services.wiki_sync_' + 'catalogue'], '_upsert_' + 'wfcd')\n"
        "def step(item):\n    return upsert(item, 'warframe')\n"),
}


def _refusals(monkeypatch, source):
    tree = ast.parse(HEADER + source + QUALIFIED)
    monkeypatch.setattr(layout, '_tree', lambda module: tree)
    monkeypatch.setattr(calls, '_tree', lambda module: tree)
    refused = []
    for guard in (calls.test_no_sync_module_imports_a_catalogue_function_by_name,
                  calls.test_the_catalogue_module_is_bound_under_its_own_name,
                  calls.test_every_catalogue_function_is_called_through_the_catalogue_module,
                  calls.test_no_sync_module_names_a_catalogue_function_as_a_string):
        try:
            guard(wiki_sync_gear)
        except AssertionError:
            refused.append(guard.__name__)
    return refused


def test_a_step_module_that_calls_through_the_catalogue_is_accepted(monkeypatch):
    assert _refusals(monkeypatch, "def step(item):\n"
                                  "    return wiki_sync_catalogue._upsert_wfcd(item, 'w')\n") == []


@pytest.mark.parametrize('spelling', list(SPELLINGS))
def test_a_catalogue_function_bound_at_import_time_is_refused(monkeypatch, spelling):
    assert _refusals(monkeypatch, SPELLINGS[spelling]) != []


@pytest.mark.xfail(strict=True, reason='a module reached without its name is not in the tree')
@pytest.mark.parametrize('spelling', list(UNGUARDED))
def test_a_catalogue_module_reached_without_its_name_is_refused(monkeypatch, spelling):
    assert _refusals(monkeypatch, UNGUARDED[spelling]) != []
