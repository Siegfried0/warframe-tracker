# file-length: exempt -- over the length limit; a split into smaller modules is planned
import sqlite3
from flask import g, current_app

try:
    import pyodbc as _pyodbc
except ImportError:
    _pyodbc = None

from models.migrations.v01 import _migrate_v1
from models.migrations.v02_v08 import _migrate_v2, _migrate_v3, _migrate_v4

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


def _migrate_v11(db, backend='sqlite'):
    """Add component_market_slugs and market_slug_cache: market slugs and prices of components.

    Expand only (two new tables, no row of another table changes). component_market_slugs maps a
    component of an item (`item_id`, `component_name`, as item_component_meta names it) to the
    warframe.market slug its listing has; market_slug_cache holds the last price fetched for a
    slug. Both hold catalogue and cache data only, so neither is exported or imported.

    Filled by: the sync's market step (Run Sync Now on the Wiki Sync page; the scheduled sync)
    writes the component slugs; a refresh click beside a component's price in the Farming
    Planner writes that slug's price.
    """
    if backend == 'mssql':
        _migrate_v11_mssql(db)
    else:
        _migrate_v11_sqlite(db)


def _migrate_v11_sqlite(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS component_market_slugs (
            item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            component_name  TEXT    NOT NULL,
            market_url_name TEXT    NOT NULL,
            PRIMARY KEY (item_id, component_name)
        );
        CREATE TABLE IF NOT EXISTS market_slug_cache (
            market_url_name TEXT PRIMARY KEY,
            platinum_price  INTEGER,
            fetched_at      TEXT NOT NULL
        );
    ''')
    db.commit()


def _migrate_v11_mssql(db):
    stmts = [
        """
        IF OBJECT_ID('component_market_slugs', 'U') IS NULL
        CREATE TABLE component_market_slugs (
            item_id         INT           NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            component_name  NVARCHAR(500) NOT NULL,
            market_url_name NVARCHAR(255) NOT NULL,
            PRIMARY KEY (item_id, component_name)
        )
        """,
        """
        IF OBJECT_ID('market_slug_cache', 'U') IS NULL
        CREATE TABLE market_slug_cache (
            market_url_name NVARCHAR(255) PRIMARY KEY,
            platinum_price  INT,
            fetched_at      NVARCHAR(50)  NOT NULL
        )
        """,
    ]
    for stmt in stmts:
        db.execute(stmt.strip())
    db.commit()


# The users columns that are unique when not NULL, and the filtered unique index that says so on MSSQL.
# A fixed list in code: the only names migration v10 places into its SQL.
_USERS_UNIQUE_WHEN_NOT_NULL = (('email', 'uq_users_email'), ('oidc_sub', 'uq_users_oidc_sub'))


def _migrate_v10(db, backend='sqlite'):
    """Let users.email and users.oidc_sub hold NULL in any number of rows on MSSQL.

    v1 declared both columns UNIQUE inline. SQL Server treats NULLs as equal under a UNIQUE
    constraint, so a second user without an email (or without an OIDC subject) was refused there.
    On MSSQL each of the two constraints becomes a unique index filtered on `<col> IS NOT NULL`:
    non-NULL values stay unique, NULL is allowed in any number of rows. SQLite's UNIQUE already
    allows many NULLs, so on SQLite no table changes. No row is changed, no column is dropped.

    Filled by: nothing; no field is added. The migration changes which rows MSSQL accepts.
    """
    if backend == 'mssql':
        _migrate_v10_mssql(db)
    else:
        _migrate_v10_sqlite(db)


def _migrate_v10_sqlite(db):
    db.commit()


def _migrate_v10_mssql(db):
    # Per column: create the filtered index first, then drop the constraint, so the non-NULL
    # values are never without a uniqueness rule. Each statement is guarded: a re-run, or a run
    # after an interruption between the two, ends with the filtered index and no constraint.
    # The constraint's name is the one SQL Server generated: it is read from the catalogue
    # (a UQ constraint of users whose index has exactly one key column, that column) and reaches
    # the dynamic SQL only through QUOTENAME.
    for column, index in _USERS_UNIQUE_WHEN_NOT_NULL:
        db.execute(f"""
            IF NOT EXISTS (
                SELECT 1 FROM sys.indexes WHERE name = '{index}' AND object_id = OBJECT_ID('users')
            ) CREATE UNIQUE INDEX {index} ON users({column}) WHERE {column} IS NOT NULL
        """.strip())
        db.execute(f"""
            DECLARE @name sysname = (
                SELECT TOP 1 kc.name FROM sys.key_constraints kc
                WHERE kc.parent_object_id = OBJECT_ID('users') AND kc.type = 'UQ'
                  AND (SELECT COUNT(*) FROM sys.index_columns ic
                       WHERE ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id
                         AND ic.key_ordinal > 0) = 1
                  AND EXISTS (
                      SELECT 1 FROM sys.index_columns ic
                      JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                      WHERE ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id
                        AND ic.key_ordinal > 0 AND c.name = '{column}'));
            IF @name IS NOT NULL
            BEGIN
                DECLARE @sql NVARCHAR(MAX) = N'ALTER TABLE users DROP CONSTRAINT ' + QUOTENAME(@name);
                EXEC sp_executesql @sql;
            END
        """.strip())
    db.commit()


def _migrate_v9(db, backend='sqlite'):
    """Add the relic_drops catalogue table: where each void relic drops.

    Expand only (a new table, no user data); the drop sync rebuilds its rows.

    Filled by: a sync (Run Sync Now on the Wiki Sync page; the scheduled sync runs SYNC_INTERVAL_HOURS
    after the app starts). The rows show as the relics holding a Prime part and where each relic drops,
    on the Farming page and in an item's drop sources.
    """
    if backend == 'mssql':
        _migrate_v9_mssql(db)
    else:
        _migrate_v9_sqlite(db)


def _migrate_v9_sqlite(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS relic_drops (
            id           INTEGER PRIMARY KEY,
            relic_name   TEXT NOT NULL,
            location     TEXT NOT NULL,
            mission_type TEXT,
            rotation     TEXT,
            drop_chance  REAL,
            avg_runs     REAL
        );
        CREATE INDEX IF NOT EXISTS idx_relic_drops_relic ON relic_drops(relic_name);
    ''')
    db.commit()


def _migrate_v9_mssql(db):
    stmts = [
        """
        IF OBJECT_ID('relic_drops', 'U') IS NULL
        CREATE TABLE relic_drops (
            id           INT           IDENTITY(1,1) PRIMARY KEY,
            relic_name   NVARCHAR(255) NOT NULL,
            location     NVARCHAR(500) NOT NULL,
            mission_type NVARCHAR(255),
            rotation     NVARCHAR(50),
            drop_chance  FLOAT,
            avg_runs     FLOAT
        )
        """,
        """
        IF NOT EXISTS (
            SELECT 1 FROM sys.indexes
            WHERE name = 'idx_relic_drops_relic' AND object_id = OBJECT_ID('relic_drops')
        ) CREATE INDEX idx_relic_drops_relic ON relic_drops(relic_name)
        """,
    ]
    for stmt in stmts:
        db.execute(stmt.strip())
    db.commit()


def _migrate_v8(db, backend='sqlite'):
    """Add mod/arcane columns to items and user_progress.

    items: mod_type, mod_polarity, mod_max_rank (used for category='mod'/'arcane')
    user_progress: owned_rank (arcane current rank), target_rank (arcane user goal)
    """
    if backend == 'mssql':
        stmts = [
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('items') AND name = 'mod_type'
            )
            ALTER TABLE items ADD mod_type NVARCHAR(100)
            """,
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('items') AND name = 'mod_polarity'
            )
            ALTER TABLE items ADD mod_polarity NVARCHAR(100)
            """,
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('items') AND name = 'mod_max_rank'
            )
            ALTER TABLE items ADD mod_max_rank INT
            """,
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('user_progress') AND name = 'owned_rank'
            )
            ALTER TABLE user_progress ADD owned_rank INT
            """,
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('user_progress') AND name = 'target_rank'
            )
            ALTER TABLE user_progress ADD target_rank INT
            """,
        ]
        for stmt in stmts:
            db.execute(stmt.strip())
        db.commit()
    else:
        for col_def in [
            'ALTER TABLE items ADD COLUMN mod_type TEXT',
            'ALTER TABLE items ADD COLUMN mod_polarity TEXT',
            'ALTER TABLE items ADD COLUMN mod_max_rank INTEGER',
            'ALTER TABLE user_progress ADD COLUMN owned_rank INTEGER',
            'ALTER TABLE user_progress ADD COLUMN target_rank INTEGER',
        ]:
            try:
                db.execute(col_def)
            except Exception:
                pass  # column already exists
        db.commit()


def _migrate_v7(db, backend='sqlite'):
    """Add grants_mastery column to items.

    K-Drive component parts (Board, Deck, Grip, Jet pieces) do not award MR.
    They are written with grants_mastery=0 by _sync_k_drives(); everything else
    defaults to 1 so existing rows are unaffected.
    """
    if backend == 'mssql':
        db.execute('''
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('items') AND name = 'grants_mastery'
            )
            ALTER TABLE items ADD grants_mastery INT NOT NULL DEFAULT 1
        ''')
        db.commit()
    else:
        try:
            db.execute('ALTER TABLE items ADD COLUMN grants_mastery INTEGER NOT NULL DEFAULT 1')
            db.commit()
        except Exception:
            pass  # column already exists


def _migrate_v6(db, backend='sqlite'):
    """Add comp_state column to farming_progress for 3-state component tracking.

    comp_state values:
      NULL       → pending (not yet started)
      'blueprint' → blueprint obtained but not yet crafted; resources still needed
      'crafted'   → fully crafted; its resources are excluded from required totals
    """
    if backend == 'mssql':
        db.execute('''
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('farming_progress') AND name = 'comp_state'
            )
            ALTER TABLE farming_progress ADD comp_state NVARCHAR(20)
        ''')
        db.commit()
    else:
        try:
            db.execute('ALTER TABLE farming_progress ADD COLUMN comp_state TEXT')
            db.commit()
        except Exception:
            pass  # column already exists


def _migrate_v5(db, backend='sqlite'):
    """Add item_component_meta table to store per-component image URLs."""
    if backend == 'mssql':
        stmts = [
            """
            IF OBJECT_ID('item_component_meta', 'U') IS NULL
            CREATE TABLE item_component_meta (
                item_id        INT           NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                component_name NVARCHAR(500) NOT NULL,
                image_url      NVARCHAR(MAX),
                PRIMARY KEY (item_id, component_name)
            )
            """,
        ]
        for stmt in stmts:
            db.execute(stmt.strip())
        db.commit()
    else:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS item_component_meta (
                item_id        INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                component_name TEXT    NOT NULL,
                image_url      TEXT,
                PRIMARY KEY (item_id, component_name)
            );
        ''')
        db.commit()
