"""The catalogue module reached through a string constant naming it is refused.

A step module that reaches the catalogue module through a string equal to its name
(``getattr(services, 'wiki_sync_catalogue')``, ``vars(services)[...]``,
``importlib.import_module('services.wiki_sync_catalogue')``, ``sys.modules[...]``) holds the
module as a whole object, where a function's name built at run time binds it once. The
module's name is a constant in the tree, so a check that reads the source can see it.
"""
import pytest

from test_wiki_sync_catalogue_binding_spellings import _refusals

TAIL = "def step(item):\n    return upsert(item, 'warframe')\n"

SPELLINGS = {
    'getattr_on_the_package_by_constant_name': (
        'import services\n'
        "upsert = getattr(getattr(services, 'wiki_sync_catalogue'), '_upsert_' + 'wfcd')\n"
        + TAIL),
    'vars_of_the_package_by_constant_name': (
        'import services\n'
        "upsert = getattr(vars(services)['wiki_sync_catalogue'], '_upsert_' + 'wfcd')\n"
        + TAIL),
    'import_module_by_constant_name': (
        'import importlib\n'
        "upsert = getattr(importlib.import_module('services.wiki_sync_catalogue'),"
        " '_upsert_' + 'wfcd')\n" + TAIL),
    'sys_modules_by_constant_name': (
        'import sys\n'
        "upsert = getattr(sys.modules['services.wiki_sync_catalogue'], '_upsert_' + 'wfcd')\n"
        + TAIL),
}


@pytest.mark.parametrize('spelling', list(SPELLINGS))
def test_the_catalogue_module_named_as_a_string_is_refused(monkeypatch, spelling):
    assert _refusals(monkeypatch, SPELLINGS[spelling]) != []
