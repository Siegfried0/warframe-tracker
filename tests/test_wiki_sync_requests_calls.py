"""Every fetch of the item sync is called through the requests module.

Every HTTP request of the sync lives in ``services.wiki_sync_requests``, and every other module
calls it as ``wiki_sync_requests.<fetch>(...)``: Python binds a ``from`` import, an alias of
the module kept in another name, an assignment or a default argument once, when the module
loads, so a replacement on the requests module would reach no caller that held the old
function; a module attribute is looked up at every call. Nothing here reaches the network.
"""
import ast

import pytest

from services import wiki_sync, wiki_sync_catalogue
from test_wiki_sync_layout import CALLERS, FETCHES, _imports, _last, _tree

REQUESTS_NAMES = ('wiki_sync_requests', 'services.wiki_sync_requests')


# ── Toolchain: the import graph and the call sites ───────────────────────────

@pytest.mark.parametrize('module', CALLERS, ids=lambda m: m.__name__)
def test_no_caller_imports_a_fetch_by_name(module):
    # Absolute or relative (`from .wiki_sync_requests import ...`), aliased or not, nested or
    # not: the requests module is matched by the last part of the module named.
    # A `*` import would bind every fetch a later change makes public.
    assert [name for source, name in _imports(module)
            if _last(source) == 'wiki_sync_requests' and (name in FETCHES or name == '*')] == []


@pytest.mark.parametrize('module', CALLERS, ids=lambda m: m.__name__)
def test_the_requests_module_is_bound_under_its_own_name(module):
    tree = _tree(module)
    aliases = [alias.asname for node in ast.walk(tree)
               if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
               if alias.name in REQUESTS_NAMES]
    assert [asname for asname in aliases if asname not in (None, 'wiki_sync_requests')] == []
    # The module read as a value (`req = wiki_sync_requests`, a default argument, an argument
    # passed on), by its bare name or by its dotted path (`services.wiki_sync_requests`), is
    # held under another name too: only an attribute read keeps its own name.
    read = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    module = [node for node in ast.walk(tree)
              if (isinstance(node, ast.Name) and node.id == 'wiki_sync_requests')
              or (isinstance(node, ast.Attribute) and node.attr == 'wiki_sync_requests')]
    assert [ast.unparse(node) for node in module if id(node) not in read] == []
    # A dunder read on the module (`__dict__`) hands its namespace out, where a fetch is
    # named by a string that no guard reads.
    assert [node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr.startswith('__')
            and any(node.value is m for m in module)] == []


@pytest.mark.parametrize('module', CALLERS, ids=lambda m: m.__name__)
def test_every_fetch_is_called_through_the_requests_module(module):
    bare, qualified, elsewhere = [], [], []
    tree = _tree(module)
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    # A fetch read from the module as a value (an assignment, a default argument) is bound
    # once, like a `from` import: only a call reads the module at call time. Whatever the
    # attribute is read from (`wiki_sync_requests`, `services.wiki_sync_requests`, an alias).
    held = [node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in FETCHES
            and id(node) not in called]
    for node in ast.walk(tree):
        # A fetch under its bare name, called or passed on, was bound by name.
        if isinstance(node, ast.Name) and node.id in FETCHES:
            bare.append(node.id)
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in FETCHES:
            through = (isinstance(node.func.value, ast.Name)
                       and node.func.value.id == 'wiki_sync_requests')
            (qualified if through else elsewhere).append(node.func.attr)
    assert bare == []
    assert elsewhere == []
    assert held == []
    # The helpers and the entry point request nothing themselves.
    assert bool(qualified) == (module not in (wiki_sync_catalogue, wiki_sync))
