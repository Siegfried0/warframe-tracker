"""The farming services' modules: one import path, no import cycle.

``services.farming_service`` stays the import path of every farming name a route or a test
reaches, also of the names defined in the farming modules beside it: it imports them from
there. ``services.farming_progress`` and ``services.farming_item_plan`` import no other
farming module, the aggregate list and the advisor import only the progress module, and no
farming module imports ``services.farming_service``, so the modules load in any order; code
inside them calls a function from the module that defines it. No farming module is exempt
from the file-length limit.
"""
import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services import (farming_advisor, farming_aggregate, farming_goal_plan, farming_goals,
                      farming_item_plan, farming_progress, farming_service)

APP_DIR = Path(__file__).resolve().parent.parent / 'app'

# Every name a route or a test reaches on services.farming_service, by the module that
# defines it.
FACADE_NAMES = {
    farming_goals: ['GOAL_MOVE_DIRECTIONS', 'add_farming_goal', 'get_farming_goals',
                    'get_pinned_goals', 'move_farming_goal', 'remove_farming_goal',
                    'search_goal_items', 'update_farming_goal'],
    farming_progress: ['set_goal_progress'],
    farming_goal_plan: ['get_goal_plan_with_progress'],
    farming_item_plan: ['get_item_plan'],
    farming_aggregate: ['get_aggregate_shopping_list'],
    farming_advisor: ['GUARANTEED_DROP_PERCENT', 'get_advisor_opportunities', 'get_all_nodes'],
}
FACADE_CASES = [(module, name) for module, names in FACADE_NAMES.items() for name in names]
# The goal-progress helpers the other farming modules read.
PROGRESS_HELPERS = ['_comp_state_for', '_compute_progress_pct', '_get_progress_map',
                    '_ingredient_drops_for']
FARMING_MODULES = ['services.farming_service', 'services.farming_goals',
                   'services.farming_progress', 'services.farming_goal_plan',
                   'services.farming_item_plan', 'services.farming_aggregate',
                   'services.farming_advisor']
# The progress helpers each of the aggregate list and the advisor reads.
SESSION_HELPERS = [(farming_aggregate, '_comp_state_for'), (farming_aggregate, '_get_progress_map'),
                   (farming_aggregate, '_ingredient_drops_for'),
                   (farming_advisor, '_comp_state_for'), (farming_advisor, '_get_progress_map')]


def _resolve_from(node: ast.ImportFrom, package: str) -> str:
    """The absolute name of the module a ``from`` import reads, a relative one resolved
    against ``package``; one that climbs above the top package keeps only what it names."""
    if not node.level:
        return node.module or ''
    parts = package.split('.') if package else []
    up = node.level - 1
    base = parts[:len(parts) - up] if up <= len(parts) else []
    return '.'.join(base + ([node.module] if node.module else []))


def _imported_modules(source: str, package: str) -> set[str]:
    """Every module the source imports from, nested imports included, by its absolute name:
    a relative import is resolved against ``package``, and ``from services import m`` or a
    relative ``from . import m`` (any level) records the module ``m`` it names as well."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_from(node, package)
            if resolved:
                found.add(resolved)
            if resolved == 'services' or (node.level and node.module is None):
                prefix = f'{resolved}.' if resolved else ''
                found |= {prefix + alias.name for alias in node.names}
    return found


def _module_imports(module) -> set[str]:
    """``_imported_modules`` over a loaded module's own source and package."""
    return _imported_modules(inspect.getsource(module), module.__package__)


# ── Interface: the names reached on services.farming_service ─────────────────

@pytest.mark.parametrize('module, name', FACADE_CASES,
                         ids=[f'{m.__name__}.{n}' for m, n in FACADE_CASES])
def test_a_name_reached_on_farming_service_is_the_defining_modules_object(module, name):
    assert getattr(farming_service, name) is getattr(module, name)


@pytest.mark.parametrize('module, name', FACADE_CASES,
                         ids=[f'{m.__name__}.{n}' for m, n in FACADE_CASES])
def test_a_moved_function_is_defined_in_its_own_module(module, name):
    obj = getattr(module, name)
    if inspect.isfunction(obj):
        assert obj.__module__ == module.__name__


@pytest.mark.parametrize('name', PROGRESS_HELPERS)
def test_the_progress_helpers_are_defined_in_the_progress_module(name):
    assert getattr(farming_progress, name).__module__ == 'services.farming_progress'
    assert getattr(farming_service, name) is getattr(farming_progress, name)


def test_farming_goals_reads_progress_from_the_progress_module():
    assert farming_goals._compute_progress_pct is farming_progress._compute_progress_pct


@pytest.mark.parametrize('name', PROGRESS_HELPERS)
def test_the_goal_plan_reads_progress_from_the_progress_module(name):
    assert getattr(farming_goal_plan, name) is getattr(farming_progress, name)


def test_the_goal_plan_reads_the_item_plan_from_the_item_plan_module():
    assert farming_goal_plan.get_item_plan is farming_item_plan.get_item_plan


@pytest.mark.parametrize('module, name', SESSION_HELPERS,
                         ids=[f'{m.__name__}.{n}' for m, n in SESSION_HELPERS])
def test_the_aggregate_list_and_the_advisor_read_progress_from_the_progress_module(module, name):
    assert getattr(module, name) is getattr(farming_progress, name)


# ── Toolchain: the import graph ──────────────────────────────────────────────

def test_the_progress_module_imports_no_other_farming_module():
    imported = _module_imports(farming_progress)
    assert sorted(m for m in imported if m.startswith('services.farming')) == []


def test_the_item_plan_module_imports_no_other_farming_module():
    imported = _module_imports(farming_item_plan)
    assert sorted(m for m in imported if m.startswith('services.farming')) == []


@pytest.mark.parametrize('module', [farming_aggregate, farming_advisor],
                         ids=lambda m: m.__name__)
def test_the_aggregate_list_and_the_advisor_import_only_the_progress_module(module):
    imported = _module_imports(module)
    assert sorted(m for m in imported if m.startswith('services.farming')) == [
        'services.farming_progress']


@pytest.mark.parametrize('module', [farming_goals, farming_progress, farming_goal_plan,
                                    farming_item_plan, farming_aggregate, farming_advisor],
                         ids=lambda m: m.__name__)
def test_no_goal_module_imports_farming_service(module):
    assert 'services.farming_service' not in _module_imports(module)


# Each spelling of an import the guards above must see, with the absolute names the reader
# records for it when the source sits in the package given.
IMPORT_SPELLINGS = [
    ('absolute_from', 'from services.farming_progress import x\n', 'services',
     {'services.farming_progress'}),
    ('absolute_from_package', 'from services import farming_service\n', 'services',
     {'services', 'services.farming_service'}),
    ('import_as', 'import services.farming_service as fs\n', 'services',
     {'services.farming_service'}),
    ('relative_from_module', 'from .farming_progress import x\n', 'services',
     {'services.farming_progress'}),
    ('relative_from_module_aliased', 'from .farming_progress import x as y\n', 'services',
     {'services.farming_progress'}),
    ('relative_from_dot', 'from . import farming_service\n', 'services',
     {'services', 'services.farming_service'}),
    ('relative_from_dot_aliased', 'from . import farming_service as fs\n', 'services',
     {'services', 'services.farming_service'}),
    ('relative_from_dot_two_names', 'from . import farming_progress, farming_service\n',
     'services', {'services', 'services.farming_progress', 'services.farming_service'}),
    ('relative_nested_in_a_function',
     'def f():\n    from .farming_progress import x\n    return x\n', 'services',
     {'services.farming_progress'}),
    ('relative_nested_dot_import',
     'def f():\n    from . import farming_service\n    return farming_service\n', 'services',
     {'services', 'services.farming_service'}),
    ('relative_two_levels_from_a_subpackage', 'from ..farming_progress import x\n',
     'services.sub', {'services.farming_progress'}),
    ('relative_above_the_top_package', 'from .. import farming_service\n', 'services',
     {'farming_service'}),
]


@pytest.mark.parametrize('source, package, expected',
                         [row[1:] for row in IMPORT_SPELLINGS],
                         ids=[row[0] for row in IMPORT_SPELLINGS])
def test_the_import_reader_records_each_spelling_by_its_absolute_name(source, package,
                                                                      expected):
    assert _imported_modules(source, package) == expected


@pytest.mark.parametrize('path', sorted((APP_DIR / 'services').glob('farming*.py')),
                         ids=lambda p: p.name)
def test_no_farming_module_is_exempt_from_the_file_length_limit(path):
    assert 'file-length: exempt' not in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('module_name', FARMING_MODULES)
def test_each_farming_module_imports_alone_in_a_fresh_interpreter(tmp_path, module_name):
    env = dict(os.environ, DATABASE_PATH=str(tmp_path / 'unused.db'))
    result = subprocess.run([sys.executable, '-c', f'import {module_name}'],
                            cwd=str(APP_DIR), capture_output=True, text=True, timeout=60,
                            env=env)
    assert result.returncode == 0, result.stderr
