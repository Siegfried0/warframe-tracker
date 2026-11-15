"""Every spelling that binds a catalogue function at import time is refused by the layout tests.

A step module that holds a catalogue function under a name of its own (a ``from`` import,
absolute or relative, with or without an alias; a module-level assignment; a default
argument) keeps the old function when a test replaces it on ``wiki_sync_catalogue``. The
layout tests read a module's source; each row hands them a small synthetic step module and
expects at least one of them to refuse it. Nothing is imported or run from the rows' sources.
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
}


def _refusals(monkeypatch, source):
    tree = ast.parse(HEADER + source + QUALIFIED)
    monkeypatch.setattr(layout, '_tree', lambda module: tree)
    monkeypatch.setattr(calls, '_tree', lambda module: tree)
    refused = []
    for guard in (calls.test_no_sync_module_imports_a_catalogue_function_by_name,
                  calls.test_the_catalogue_module_is_bound_under_its_own_name,
                  calls.test_every_catalogue_function_is_called_through_the_catalogue_module):
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
