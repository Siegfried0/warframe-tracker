import sqlite3
from flask import g, current_app

try:
    import pyodbc as _pyodbc
except ImportError:
    _pyodbc = None

from models.migrations.v01 import _migrate_v1
from models.migrations.v02_v08 import (
    _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6, _migrate_v7, _migrate_v8)
from models.migrations.v09_v11 import _migrate_v9, _migrate_v10, _migrate_v11

CURRENT_SCHEMA_VERSION = 11

# What each backend raises for a violated constraint (UNIQUE, FOREIGN KEY, NOT NULL).
INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + (
    (_pyodbc.IntegrityError,) if _pyodbc is not None else ())


# ── MSSQL compatibility layer ────────────────────────────────────────────────

class _MSRow(dict):
    """Dict-like row for pyodbc results; also supports integer index access."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _MSCursor:
    def __init__(self, cursor, raw_conn):
        self._cur = cursor
        self._raw_conn = raw_conn

    def _wrap(self, raw):
        if raw is None or self._cur.description is None:
            return raw
        cols = [d[0] for d in self._cur.description]
        return _MSRow(zip(cols, raw))

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        if self._cur.description is None:
            return []
        cols = [d[0] for d in self._cur.description]
        return [_MSRow(zip(cols, r)) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        c = self._raw_conn.cursor()
        c.execute('SELECT SCOPE_IDENTITY()')
        row = c.fetchone()
        return int(row[0]) if row and row[0] is not None else None


class _MSConnection:
    def __init__(self, raw):
        self._conn = raw

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return _MSCursor(cur, self._conn)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


# ── Connection factory ───────────────────────────────────────────────────────

def get_connection():
    if current_app.config.get('DB_BACKEND') == 'mssql':
        if _pyodbc is None:
            raise RuntimeError(
                'pyodbc is required for MSSQL mode. Install it with: pip install pyodbc'
            )
        raw = _pyodbc.connect(current_app.config['MSSQL_CONNECTION_STRING'])
        return _MSConnection(raw)

    conn = sqlite3.connect(
        current_app.config['DATABASE_PATH'],
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    return conn


def get_db():
    if 'db' not in g:
        g.db = get_connection()
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    current_app.teardown_appcontext(close_db)
    db = get_connection()
    _apply_migrations(db)
    db.close()


# ── Migrations ───────────────────────────────────────────────────────────────

def _apply_migrations(db):
    backend = current_app.config.get('DB_BACKEND', 'sqlite')

    if backend == 'mssql':
        db.execute('''
            IF OBJECT_ID('schema_version', 'U') IS NULL
            CREATE TABLE schema_version (version INT PRIMARY KEY)
        ''')
    else:
        db.execute('''
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        ''')
    db.commit()

    row = db.execute('SELECT version FROM schema_version').fetchone()
    current = row['version'] if row else 0

    migrations = [_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6, _migrate_v7, _migrate_v8,
                  _migrate_v9, _migrate_v10, _migrate_v11]
    for i, migrate in enumerate(migrations, start=1):
        if current < i:
            migrate(db, backend)
            if current == 0:
                db.execute('INSERT INTO schema_version (version) VALUES (?)', (i,))
            else:
                db.execute('UPDATE schema_version SET version = ?', (i,))
            db.commit()
            current = i
