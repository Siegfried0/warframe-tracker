"""users.email and users.oidc_sub: unique when not NULL, any number of NULLs, on both backends.

SQL Server treats NULLs as equal under a UNIQUE constraint, so the inline UNIQUE of the first
schema refused a second user without an email (or without an OIDC subject) there. Migration v10
replaces each of the two constraints on MSSQL by a unique index filtered on `IS NOT NULL`. The
MSSQL statements are recorded through a stand-in connection (no server here); on SQLite the
migration changes no table, and the rule is checked on real rows.
"""
import re

import pytest

from models import db as db_module
from models.db import INTEGRITY_ERRORS, get_connection

COLUMNS = [('email', 'uq_users_email'), ('oidc_sub', 'uq_users_oidc_sub')]


class _Recorder:
    """Stands in for the MSSQL connection: records each statement, its parameters and commits."""

    def __init__(self):
        self.statements = []
        self.params = []
        self.commits = 0

    def execute(self, sql, params=()):
        self.statements.append(' '.join(sql.split()))
        self.params.append(params)

    def commit(self):
        self.commits += 1


def _recorded(runs=1):
    db = _Recorder()
    for _ in range(runs):
        db_module._migrate_v10(db, 'mssql')
    return db


def _create_and_drop(statements, column):
    """The create and the drop statement of one column, in the order they were issued."""
    mine = [s for s in statements if f"'{column}'" in s or f'({column})' in s]
    assert len(mine) == 2, mine
    return mine


# ── MSSQL: the statements (toolchain, repetition, interruption) ──────────────

@pytest.mark.parametrize('column, index', COLUMNS, ids=[c for c, _ in COLUMNS])
def test_mssql_creates_a_unique_index_filtered_on_not_null_guarded_by_its_name(column, index):
    create, _ = _create_and_drop(_recorded().statements, column)
    assert create == (
        f"IF NOT EXISTS ( SELECT 1 FROM sys.indexes WHERE name = '{index}' "
        f"AND object_id = OBJECT_ID('users') ) "
        f"CREATE UNIQUE INDEX {index} ON users({column}) WHERE {column} IS NOT NULL")


@pytest.mark.parametrize('column, index', COLUMNS, ids=[c for c, _ in COLUMNS])
def test_mssql_drops_only_the_single_column_unique_constraint_of_that_column(column, index):
    _, drop = _create_and_drop(_recorded().statements, column)
    # Found in the catalogue: a UQ constraint of users ...
    assert "FROM sys.key_constraints kc WHERE kc.parent_object_id = OBJECT_ID('users') " \
           "AND kc.type = 'UQ'" in drop
    # ... whose index has exactly one key column ...
    assert re.search(r"\(SELECT COUNT\(\*\) FROM sys\.index_columns ic WHERE "
                     r"ic\.object_id = kc\.parent_object_id AND ic\.index_id = kc\.unique_index_id "
                     r"AND ic\.key_ordinal > 0\) = 1", drop)
    # ... and that key column is this column, and no other column is named.
    assert f"AND ic.key_ordinal > 0 AND c.name = '{column}'" in drop
    others = {c for c, _ in COLUMNS} - {column}
    assert not any(f"'{o}'" in drop for o in others)
    # The name is never assumed: it reaches the dynamic SQL only through QUOTENAME, and only
    # when the catalogue found one.
    assert 'UQ__' not in drop
    assert drop.count('ALTER TABLE users DROP CONSTRAINT') == 1
    guard = drop.index('IF @name IS NOT NULL')
    assert guard < drop.index("N'ALTER TABLE users DROP CONSTRAINT ' + QUOTENAME(@name)")
    assert guard < drop.index('EXEC sp_executesql @sql')


def test_mssql_makes_the_index_before_it_drops_the_constraint_for_each_column():
    statements = _recorded().statements
    assert len(statements) == 2 * len(COLUMNS)
    for column, _ in COLUMNS:
        create, drop = _create_and_drop(statements, column)
        assert 'CREATE UNIQUE INDEX' in create and 'DROP CONSTRAINT' in drop
        assert statements.index(create) < statements.index(drop)


_DDL = re.compile(r'\b(CREATE|ALTER|DROP)\b')
_GUARDS = (re.compile(r"^IF NOT EXISTS \( SELECT 1 FROM sys\.indexes WHERE name = '\w+' "
                      r"AND object_id = OBJECT_ID\('users'\) \) CREATE UNIQUE INDEX "),
           re.compile(r'IF @name IS NOT NULL BEGIN DECLARE @sql NVARCHAR\(MAX\) = '
                      r"N'ALTER TABLE users DROP CONSTRAINT ' \+ QUOTENAME\(@name\); "
                      r'EXEC sp_executesql @sql; END$'))


def test_mssql_run_twice_issues_no_unguarded_ddl_and_binds_no_value():
    """A re-run (the version is written only after the whole migration, so an interrupted run
    starts again from the first statement) issues only guarded DDL: each statement is a no-op
    when its work is already done."""
    db = _recorded(runs=2)
    assert db.statements == _recorded().statements * 2
    for stmt in db.statements:
        assert _DDL.search(stmt)
        guards = [g for g in _GUARDS if g.search(stmt)]
        assert len(guards) == 1, stmt
        # No DDL outside the guarded part.
        assert not _DDL.search(guards[0].sub('', stmt)), stmt
    assert db.params == [()] * len(db.statements)
    assert db.commits == 2


def test_mssql_changes_no_row_and_drops_no_column():
    for stmt in _recorded().statements:
        for word in ('INSERT', 'UPDATE ', 'DELETE', 'DROP COLUMN', 'ALTER COLUMN', 'DROP TABLE',
                     'DROP INDEX'):
            assert word not in stmt


# ── SQLite: nothing changes, and the rule holds on real rows (interface) ─────

@pytest.fixture
def db(app):
    with app.app_context():
        conn = get_connection()
        try:
            yield conn
        finally:
            conn.close()


def _schema(db):
    return sorted(tuple(r) for r in db.execute(
        'SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name'))


def test_sqlite_v10_from_version_9_changes_no_table_and_no_row(db):
    db.execute("INSERT INTO users (username, email, oidc_sub) VALUES ('a', 'a@example.test', NULL)")
    db.execute("INSERT INTO users (username, email, oidc_sub, auth_type) VALUES ('b', NULL, 's', 'oidc')")
    db.execute('UPDATE schema_version SET version = 9')
    db.commit()
    schema, users = _schema(db), db.execute('SELECT * FROM users ORDER BY id').fetchall()

    db_module._apply_migrations(db)

    assert db.execute('SELECT version FROM schema_version').fetchone()[0] == db_module.CURRENT_SCHEMA_VERSION
    assert _schema(db) == schema
    assert [tuple(r) for r in db.execute('SELECT * FROM users ORDER BY id')] == [tuple(r) for r in users]


def test_sqlite_v10_run_again_changes_nothing(db):
    schema = _schema(db)
    db_module._migrate_v10(db)
    db_module._migrate_v10(db, 'sqlite')
    assert _schema(db) == schema


@pytest.mark.parametrize('column', [c for c, _ in COLUMNS])
def test_sqlite_stores_many_users_without_a_value_in_the_column(db, column):
    for name in ('one', 'two', 'three'):
        db.execute(f'INSERT INTO users (username, {column}) VALUES (?, NULL)', (name,))
    db.commit()
    assert db.execute(f'SELECT COUNT(*) FROM users WHERE {column} IS NULL').fetchone()[0] == 3


@pytest.mark.parametrize('column', [c for c, _ in COLUMNS])
def test_sqlite_refuses_a_second_equal_value_as_an_integrity_error(db, column):
    db.execute(f'INSERT INTO users (username, {column}) VALUES (?, ?)', ('one', 'same'))
    db.commit()
    with pytest.raises(INTEGRITY_ERRORS):
        db.execute(f'INSERT INTO users (username, {column}) VALUES (?, ?)', ('two', 'same'))
    db.rollback()
    assert db.execute(f'SELECT COUNT(*) FROM users WHERE {column} = ?', ('same',)).fetchone()[0] == 1
