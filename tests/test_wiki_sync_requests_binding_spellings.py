"""Every spelling that binds a fetch at import time is refused by the layout tests.

A caller that holds a fetch of ``wiki_sync_requests`` under a name of its own (a ``from``
import, absolute or relative, with or without an alias; a module-level assignment; a default
argument), or holds the requests module under another name, keeps the old function when a test
replaces it on ``wiki_sync_requests``. The same tests keep the sync's modules off the entry
point and off the ``requests`` library, in every spelling of the import. Each row hands the
layout tests a small synthetic step module and expects at least one of them to refuse it.
Nothing is imported or run from the rows' sources.
"""
import ast

import pytest

import test_wiki_sync_layout as layout
import test_wiki_sync_requests_calls as calls
from services import wiki_sync_gear

# Every row also calls a fetch through the module, as a real step module does.
QUALIFIED = "def other():\n    return wiki_sync_requests._fetch_wfcd('Warframes.json')\n"
HEADER = 'from services import wiki_sync_requests\n'

SPELLINGS = {
    'from_import': (
        'from services.wiki_sync_requests import _fetch_wfcd\n'
        "def step():\n    return _fetch_wfcd('Primary.json')\n"),
    'from_import_alias': (
        'from services.wiki_sync_requests import _fetch_wfcd as fetch\n'
        "def step():\n    return fetch('Primary.json')\n"),
    'from_import_nested': (
        'def step():\n'
        '    from services.wiki_sync_requests import _fetch_wfcd as fetch\n'
        "    return fetch('Primary.json')\n"),
    # Passed on, not called: only the import itself is left to refuse.
    'relative_from_import': (
        'from .wiki_sync_requests import _fetch_wfcd\n'
        "def step():\n    return list(map(_fetch_wfcd, ['Primary.json']))\n"),
    'relative_from_import_alias': (
        'from .wiki_sync_requests import _fetch_wfcd as fetch\n'
        "def step():\n    return fetch('Primary.json')\n"),
    'relative_from_import_alias_nested': (
        'def step():\n'
        '    from .wiki_sync_requests import _fetch_wfcd as fetch\n'
        "    return fetch('Primary.json')\n"),
    'module_alias': (
        'from services import wiki_sync_requests as req\n'
        "def step():\n    return req._fetch_wfcd('Primary.json')\n"),
    'relative_module_alias': (
        'from . import wiki_sync_requests as req\n'
        "def step():\n    return req._fetch_wfcd('Primary.json')\n"),
    'import_module_alias': (
        'import services.wiki_sync_requests as req\n'
        "def step():\n    return req._fetch_wfcd('Primary.json')\n"),
    'module_level_assignment': (
        'fetch = wiki_sync_requests._fetch_wfcd\n'
        "def step():\n    return fetch('Primary.json')\n"),
    'default_argument': (
        'def step(fetch=wiki_sync_requests._fetch_wfcd):\n'
        "    return fetch('Primary.json')\n"),
    'keyword_only_default_argument': (
        'def step(*, fetch=wiki_sync_requests._fetch_wfcd):\n'
        "    return fetch('Primary.json')\n"),
    'class_attribute': (
        'class Step:\n    fetch = staticmethod(wiki_sync_requests._fetch_wfcd)\n'
        "def step():\n    return Step.fetch('Primary.json')\n"),
    'star_import': (
        'from services.wiki_sync_requests import *\n'
        "def step():\n    return list(map(_fetch_wfcd, ['Primary.json']))\n"),
    # A fetch read as a value through the module's dotted path, or through the module held
    # under another name by an assignment: bound once at import, like the rows above.
    'dotted_path_module_level_assignment': (
        'import services.wiki_sync_requests\n'
        'fetch = services.wiki_sync_requests._fetch_wfcd\n'
        "def step():\n    return fetch('Primary.json')\n"),
    'dotted_path_default_argument': (
        'import services.wiki_sync_requests\n'
        'def step(fetch=services.wiki_sync_requests._fetch_wfcd):\n'
        "    return fetch('Primary.json')\n"),
    'module_assigned_alias_then_fetch_assignment': (
        'req = wiki_sync_requests\n'
        'fetch = req._fetch_wfcd\n'
        "def step():\n    return fetch('Primary.json')\n"),
    'module_assigned_alias_then_default_argument': (
        'req = wiki_sync_requests\n'
        'def step(fetch=req._fetch_wfcd):\n'
        "    return fetch('Primary.json')\n"),
    # The module passed on as a value: `getattr` names the fetch by a string, which no guard
    # reads, so the module itself (by its bare name or its dotted path) leaving an attribute
    # read is what is refused.
    'getattr_with_a_string': (
        "fetch = getattr(wiki_sync_requests, '_fetch_wfcd')\n"
        "def step():\n    return fetch('Primary.json')\n"),
    'module_as_default_argument': (
        'def step(req=wiki_sync_requests):\n'
        "    return req._fetch_wfcd('Primary.json')\n"),
    # The module reached through its dotted path, or held under another name by an assignment
    # from that path, with the fetch named by a string: bound once at import, like the rows
    # above.
    'dotted_path_getattr_with_a_string': (
        'import services.wiki_sync_requests\n'
        "fetch = getattr(services.wiki_sync_requests, '_fetch_wfcd')\n"
        "def step():\n    return fetch('Primary.json')\n"),
    'dotted_path_module_assigned_alias_then_getattr': (
        'import services.wiki_sync_requests\n'
        'req = services.wiki_sync_requests\n'
        "fetch = getattr(req, '_fetch_wfcd')\n"
        "def step():\n    return fetch('Primary.json')\n"),
    'module_dict_subscript': (
        "fetch = wiki_sync_requests.__dict__['_fetch_wfcd']\n"
        "def step():\n    return fetch('Primary.json')\n"),
    'vars_of_the_module': (
        "fetch = vars(wiki_sync_requests)['_fetch_wfcd']\n"
        "def step():\n    return fetch('Primary.json')\n"),
    'lambda_default_argument': (
        "step = lambda fetch=wiki_sync_requests._fetch_wfcd: fetch('Primary.json')\n"),
    'walrus_at_module_level': (
        '(fetch := wiki_sync_requests._fetch_wfcd)\n'
        "def step():\n    return fetch('Primary.json')\n"),
    'partial_at_module_level': (
        'import functools\n'
        "fetch = functools.partial(wiki_sync_requests._fetch_wfcd, 'Primary.json')\n"
        'def step():\n    return fetch()\n'),
    'sys_modules_attribute': (
        'import sys\n'
        "fetch = sys.modules['services.wiki_sync_requests']._fetch_wfcd\n"
        "def step():\n    return fetch('Primary.json')\n"),
    'dotted_path_module_as_default_argument': (
        'import services.wiki_sync_requests\n'
        'def step(req=services.wiki_sync_requests):\n'
        "    return req._fetch_wfcd('Primary.json')\n"),
    # The entry point and the requests library, in the spellings the old guards missed.
    'entry_point_relative_from_import': (
        'from .wiki_sync import run_sync\n'
        'def step():\n    return run_sync\n'),
    'entry_point_relative_module': (
        'from . import wiki_sync\n'
        'def step():\n    return wiki_sync.run_sync\n'),
    'requests_from_import': (
        'from requests import get\n'
        "def step():\n    return get('http://localhost/')\n"),
}


def _refusals(monkeypatch, source):
    tree = ast.parse(HEADER + source + QUALIFIED)
    monkeypatch.setattr(layout, '_tree', lambda module: tree)
    monkeypatch.setattr(calls, '_tree', lambda module: tree)
    refused = []
    for guard in (layout.test_no_module_the_sync_imports_imports_the_entry_point,
                  calls.test_no_caller_imports_a_fetch_by_name,
                  calls.test_the_requests_module_is_bound_under_its_own_name,
                  calls.test_every_fetch_is_called_through_the_requests_module,
                  layout.test_only_the_requests_module_imports_requests):
        try:
            guard(wiki_sync_gear)
        except AssertionError:
            refused.append(guard.__name__)
    return refused


def test_a_step_module_that_calls_through_the_requests_module_is_accepted(monkeypatch):
    assert _refusals(monkeypatch, "def step():\n"
                                  "    return wiki_sync_requests._fetch_wfcd('P.json')\n") == []


@pytest.mark.parametrize('spelling', list(SPELLINGS))
def test_a_fetch_or_module_bound_at_import_time_is_refused(monkeypatch, spelling):
    assert _refusals(monkeypatch, SPELLINGS[spelling]) != []

