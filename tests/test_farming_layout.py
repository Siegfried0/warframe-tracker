"""The farming services' modules: one import path, no import cycle.

``services.farming_service`` stays the import path of every farming name a route or a test
reaches, also of the names defined in the goal modules beside it: it imports them from
there. ``services.farming_progress`` imports no other farming module, and no farming module
imports ``services.farming_service``, so the modules load in any order; code inside them
calls a function from the module that defines it.
"""
import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services import farming_goals, farming_progress, farming_service

APP_DIR = Path(__file__).resolve().parent.parent / 'app'

# Every name a route or a test reaches on services.farming_service, by the module that
# defines it.
FACADE_NAMES = {
    farming_goals: ['GOAL_MOVE_DIRECTIONS', 'add_farming_goal', 'get_farming_goals',
                    'get_pinned_goals', 'move_farming_goal', 'remove_farming_goal',
                    'search_goal_items', 'update_farming_goal'],
    farming_progress: ['set_goal_progress'],
}
FACADE_CASES = [(module, name) for module, names in FACADE_NAMES.items() for name in names]
# The goal-progress helpers the other farming modules read.
PROGRESS_HELPERS = ['_comp_state_for', '_compute_progress_pct', '_get_progress_map',
                    '_ingredient_drops_for']
FARMING_MODULES = ['services.farming_service', 'services.farming_goals',
                   'services.farming_progress']


def _imported_modules(module) -> set[str]:
    """Every module the source imports from, nested imports included."""
    found = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or '')
            if node.module == 'services':
                found |= {f'services.{alias.name}' for alias in node.names}
    return found


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


# ── Toolchain: the import graph ──────────────────────────────────────────────

def test_the_progress_module_imports_no_other_farming_module():
    imported = _imported_modules(farming_progress)
    assert sorted(m for m in imported if m.startswith('services.farming')) == []


@pytest.mark.parametrize('module', [farming_goals, farming_progress],
                         ids=lambda m: m.__name__)
def test_no_goal_module_imports_farming_service(module):
    assert 'services.farming_service' not in _imported_modules(module)


@pytest.mark.parametrize('module_name', FARMING_MODULES)
def test_each_farming_module_imports_alone_in_a_fresh_interpreter(tmp_path, module_name):
    env = dict(os.environ, DATABASE_PATH=str(tmp_path / 'unused.db'))
    result = subprocess.run([sys.executable, '-c', f'import {module_name}'],
                            cwd=str(APP_DIR), capture_output=True, text=True, timeout=60,
                            env=env)
    assert result.returncode == 0, result.stderr
