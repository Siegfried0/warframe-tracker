"""The migration functions live in the package models/migrations; models/db.py keeps the schema
version and the runner, and imports each `_migrate_vN`, so `db._migrate_vN` stays the name the
runner calls and a test can replace."""
import ast
import sqlite3
from pathlib import Path

import pytest

from models import db as db_module
from models.migrations import v01, v02_v08, v09_v11

PACKAGE = Path(__file__).resolve().parent.parent / 'app' / 'models' / 'migrations'
MODULES = sorted(PACKAGE.glob('*.py'), key=lambda p: p.name)


def test_every_migration_is_reachable_on_db():
    for v in range(1, db_module.CURRENT_SCHEMA_VERSION + 1):
        assert callable(getattr(db_module, f'_migrate_v{v}')), v
    assert db_module._migrate_v1 is v01._migrate_v1
    for v in range(2, 9):
        assert getattr(db_module, f'_migrate_v{v}') is getattr(v02_v08, f'_migrate_v{v}')
    for v in range(9, 12):
        assert getattr(db_module, f'_migrate_v{v}') is getattr(v09_v11, f'_migrate_v{v}')


def test_the_runner_calls_every_migration_on_db_once_in_order(app, tmp_path, monkeypatch):
    called = []
    for v in range(1, db_module.CURRENT_SCHEMA_VERSION + 1):
        monkeypatch.setattr(db_module, f'_migrate_v{v}', lambda db, backend, v=v: called.append((v, backend)))
    conn = sqlite3.connect(tmp_path / 'empty.db')
    conn.row_factory = sqlite3.Row
    try:
        with app.app_context():
            db_module._apply_migrations(conn)
        version = conn.execute('SELECT version FROM schema_version').fetchone()[0]
    finally:
        conn.close()
    assert called == [(v, 'sqlite') for v in range(1, db_module.CURRENT_SCHEMA_VERSION + 1)]
    assert version == db_module.CURRENT_SCHEMA_VERSION


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield '.' * node.level + (node.module or '')


@pytest.mark.parametrize('path', MODULES, ids=lambda p: p.name)
def test_no_module_of_the_package_imports_the_connection_module(path):
    # db.py imports the package: an import back would be a cycle
    names = list(_imports(ast.parse(path.read_text(encoding='utf-8'))))
    assert not [n for n in names if n.split('.')[-1] == 'db' or n.startswith('.')], names


@pytest.mark.parametrize('path', MODULES, ids=lambda p: p.name)
def test_every_module_of_the_package_is_under_the_length_limit(path):
    text = path.read_text(encoding='utf-8')
    assert len(text.splitlines()) < 400
    assert 'file-length: exempt' not in text


def test_the_package_re_exports_nothing():
    tree = ast.parse((PACKAGE / '__init__.py').read_text(encoding='utf-8'))
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)   # its docstring only


def test_the_package_s_modules_are_found():
    # the scans above would pass for nothing on an empty list
    assert {'__init__.py', 'v01.py', 'v02_v08.py', 'v09_v11.py'} <= {p.name for p in MODULES}


def test_db_py_holds_no_migration_and_no_length_exemption():
    # the connection layer, the schema version and the runner stay; every migration is in the package
    text = (PACKAGE.parent / 'db.py').read_text(encoding='utf-8')
    assert len(text.splitlines()) < 400
    assert 'file-length: exempt' not in text
    defs = [n.name for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)]
    assert not [d for d in defs if d.startswith('_migrate_v')], defs
