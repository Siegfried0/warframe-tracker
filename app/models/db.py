# file-length: exempt -- over the length limit; a split into smaller modules is planned
import sqlite3
from flask import g, current_app

try:
    import pyodbc as _pyodbc
except ImportError:
    _pyodbc = None

CURRENT_SCHEMA_VERSION = 8

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

    migrations = [_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6, _migrate_v7, _migrate_v8]
    for i, migrate in enumerate(migrations, start=1):
        if current < i:
            migrate(db, backend)
            if current == 0:
                db.execute('INSERT INTO schema_version (version) VALUES (?)', (i,))
            else:
                db.execute('UPDATE schema_version SET version = ?', (i,))
            db.commit()
            current = i


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


def _migrate_v4(db, backend='sqlite'):
    """Add image_url to items; add farming_goals and farming_progress tables."""
    if backend == 'mssql':
        stmts = [
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('items') AND name = 'image_url'
            )
            ALTER TABLE items ADD image_url NVARCHAR(MAX)
            """,
            """
            IF OBJECT_ID('farming_goals', 'U') IS NULL
            CREATE TABLE farming_goals (
                id           INT          IDENTITY(1,1) PRIMARY KEY,
                user_id      INT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                item_id      INT          NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                pinned       INT          NOT NULL DEFAULT 0,
                display_mode NVARCHAR(10) NOT NULL DEFAULT 'tree',
                sort_order   INT          NOT NULL DEFAULT 0,
                created_at   NVARCHAR(50) NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120)),
                CONSTRAINT uq_farming_goals_user_item UNIQUE (user_id, item_id)
            )
            """,
            """
            IF OBJECT_ID('farming_progress', 'U') IS NULL
            CREATE TABLE farming_progress (
                id             INT          IDENTITY(1,1) PRIMARY KEY,
                user_id        INT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                goal_id        INT          NOT NULL REFERENCES farming_goals(id) ON DELETE CASCADE,
                row_type       NVARCHAR(20) NOT NULL,
                row_key        NVARCHAR(500) NOT NULL,
                acquired       INT          NOT NULL DEFAULT 0,
                quantity_owned INT          NOT NULL DEFAULT 0,
                updated_at     NVARCHAR(50) NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120)),
                CONSTRAINT uq_farming_progress_goal UNIQUE (goal_id, row_type, row_key)
            )
            """,
            """
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_fg_user' AND object_id=OBJECT_ID('farming_goals'))
            CREATE INDEX idx_fg_user ON farming_goals(user_id)
            """,
            """
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_fp_goal' AND object_id=OBJECT_ID('farming_progress'))
            CREATE INDEX idx_fp_goal ON farming_progress(goal_id)
            """,
        ]
        for stmt in stmts:
            db.execute(stmt.strip())
        db.commit()
    else:
        try:
            db.execute('ALTER TABLE items ADD COLUMN image_url TEXT')
            db.commit()
        except Exception:
            pass  # column already exists
        db.executescript('''
            CREATE TABLE IF NOT EXISTS farming_goals (
                id           INTEGER PRIMARY KEY,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                pinned       INTEGER NOT NULL DEFAULT 0,
                display_mode TEXT    NOT NULL DEFAULT 'tree',
                sort_order   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS farming_progress (
                id             INTEGER PRIMARY KEY,
                user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                goal_id        INTEGER NOT NULL REFERENCES farming_goals(id) ON DELETE CASCADE,
                row_type       TEXT    NOT NULL,
                row_key        TEXT    NOT NULL,
                acquired       INTEGER NOT NULL DEFAULT 0,
                quantity_owned INTEGER NOT NULL DEFAULT 0,
                updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(goal_id, row_type, row_key)
            );

            CREATE INDEX IF NOT EXISTS idx_fg_user ON farming_goals(user_id);
            CREATE INDEX IF NOT EXISTS idx_fp_goal ON farming_progress(goal_id);
        ''')
        db.commit()


def _migrate_v3(db, backend='sqlite'):
    """Add component_ingredients (crafting recipes) and farming_checklist (per-user tick-off)."""
    if backend == 'mssql':
        stmts = [
            """
            IF OBJECT_ID('component_ingredients', 'U') IS NULL
            CREATE TABLE component_ingredients (
                id               INT           IDENTITY(1,1) PRIMARY KEY,
                item_id          INT           NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                component_name   NVARCHAR(500) NOT NULL,
                ingredient_name  NVARCHAR(500) NOT NULL,
                ingredient_qty   INT           NOT NULL DEFAULT 1,
                updated_at       NVARCHAR(50)  NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120))
            )
            """,
            """
            IF OBJECT_ID('farming_checklist', 'U') IS NULL
            CREATE TABLE farming_checklist (
                id             INT          IDENTITY(1,1) PRIMARY KEY,
                user_id        INT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                item_id        INT          NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                component_name NVARCHAR(500) NOT NULL,
                acquired       INT          NOT NULL DEFAULT 0,
                updated_at     NVARCHAR(50) NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120)),
                CONSTRAINT uq_checklist UNIQUE (user_id, item_id, component_name)
            )
            """,
            """
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_ci_item' AND object_id=OBJECT_ID('component_ingredients'))
            CREATE INDEX idx_ci_item ON component_ingredients(item_id)
            """,
            """
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_cl_user' AND object_id=OBJECT_ID('farming_checklist'))
            CREATE INDEX idx_cl_user ON farming_checklist(user_id)
            """,
        ]
        for stmt in stmts:
            db.execute(stmt.strip())
        db.commit()
    else:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS component_ingredients (
                id               INTEGER PRIMARY KEY,
                item_id          INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                component_name   TEXT    NOT NULL,
                ingredient_name  TEXT    NOT NULL,
                ingredient_qty   INTEGER NOT NULL DEFAULT 1,
                updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS farming_checklist (
                id             INTEGER PRIMARY KEY,
                user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                item_id        INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                component_name TEXT    NOT NULL,
                acquired       INTEGER NOT NULL DEFAULT 0,
                updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, item_id, component_name)
            );

            CREATE INDEX IF NOT EXISTS idx_ci_item ON component_ingredients(item_id);
            CREATE INDEX IF NOT EXISTS idx_cl_user  ON farming_checklist(user_id);
        ''')
        db.commit()


def _migrate_v2(db, backend='sqlite'):
    """Data fix: Bonewidow and Voidrig were seeded under category='warframe' by the
    WFCD sync. They are Necramechs and belong under 'k-drive'. The correct entries
    already exist; this migration removes the stale warframe-category rows."""
    names = ['Bonewidow', 'Voidrig']
    placeholders = ','.join('?' for _ in names)
    db.execute(
        f"DELETE FROM items WHERE name IN ({placeholders}) AND category = 'warframe'",
        names,
    )
    db.commit()


def _migrate_v1(db, backend='sqlite'):
    if backend == 'mssql':
        _migrate_v1_mssql(db)
    else:
        _migrate_v1_sqlite(db)


def _migrate_v1_sqlite(db):
    # INTEGER PRIMARY KEY is a rowid alias in SQLite — auto-increments without AUTOINCREMENT keyword
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY,
            username        TEXT    UNIQUE NOT NULL,
            email           TEXT    UNIQUE,
            password_hash   TEXT,
            role            TEXT    NOT NULL DEFAULT 'user',
            auth_type       TEXT    NOT NULL DEFAULT 'local',
            oidc_sub        TEXT    UNIQUE,
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            last_login      TEXT
        );

        CREATE TABLE IF NOT EXISTS items (
            id                      INTEGER PRIMARY KEY,
            name                    TEXT    NOT NULL,
            category                TEXT    NOT NULL,
            source                  TEXT,
            wiki_url                TEXT,
            vaulted                 INTEGER NOT NULL DEFAULT 0,
            resurgence_available    INTEGER NOT NULL DEFAULT 0,
            market_url_name         TEXT,
            tradable                INTEGER NOT NULL DEFAULT 1,
            date_added              TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(name, category)
        );

        CREATE TABLE IF NOT EXISTS user_progress (
            id               INTEGER PRIMARY KEY,
            user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_id          INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            in_possession    INTEGER NOT NULL DEFAULT 0,
            mastered         INTEGER NOT NULL DEFAULT 0,
            subsumed         INTEGER NOT NULL DEFAULT 0,
            resurgence_watch INTEGER NOT NULL DEFAULT 0,
            note             TEXT,
            updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, item_id)
        );

        CREATE TABLE IF NOT EXISTS drop_sources (
            id           INTEGER PRIMARY KEY,
            item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            mission_type TEXT,
            location     TEXT,
            drop_chance  REAL,
            avg_runs     REAL,
            source_type  TEXT,
            notes        TEXT,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS market_cache (
            item_id         INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
            platinum_price  INTEGER,
            fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_market_watchlist (
            id       INTEGER PRIMARY KEY,
            user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, item_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            id      INTEGER PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            key     TEXT NOT NULL,
            value   TEXT,
            UNIQUE(user_id, key)
        );

        CREATE TABLE IF NOT EXISTS pinned_routes (
            id         INTEGER PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            route_data TEXT NOT NULL,
            pinned_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_progress_user   ON user_progress(user_id);
        CREATE INDEX IF NOT EXISTS idx_progress_item   ON user_progress(item_id);
        CREATE INDEX IF NOT EXISTS idx_items_category  ON items(category);
        CREATE INDEX IF NOT EXISTS idx_drop_item       ON drop_sources(item_id);
    ''')


def _migrate_v1_mssql(db):
    stmts = [
        """
        IF OBJECT_ID('users', 'U') IS NULL
        CREATE TABLE users (
            id              INT           IDENTITY(1,1) PRIMARY KEY,
            username        NVARCHAR(255) UNIQUE NOT NULL,
            email           NVARCHAR(255) UNIQUE,
            password_hash   NVARCHAR(255),
            role            NVARCHAR(50)  NOT NULL DEFAULT 'user',
            auth_type       NVARCHAR(50)  NOT NULL DEFAULT 'local',
            oidc_sub        NVARCHAR(500) UNIQUE,
            is_active       INT           NOT NULL DEFAULT 1,
            created_at      NVARCHAR(50)  NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120)),
            last_login      NVARCHAR(50)
        )
        """,
        """
        IF OBJECT_ID('items', 'U') IS NULL
        CREATE TABLE items (
            id                      INT           IDENTITY(1,1) PRIMARY KEY,
            name                    NVARCHAR(500) NOT NULL,
            category                NVARCHAR(255) NOT NULL,
            source                  NVARCHAR(500),
            wiki_url                NVARCHAR(MAX),
            vaulted                 INT           NOT NULL DEFAULT 0,
            resurgence_available    INT           NOT NULL DEFAULT 0,
            market_url_name         NVARCHAR(255),
            tradable                INT           NOT NULL DEFAULT 1,
            date_added              NVARCHAR(50)  NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120)),
            updated_at              NVARCHAR(50)  NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120)),
            CONSTRAINT uq_items_name_category UNIQUE (name, category)
        )
        """,
        """
        IF OBJECT_ID('user_progress', 'U') IS NULL
        CREATE TABLE user_progress (
            id               INT          IDENTITY(1,1) PRIMARY KEY,
            user_id          INT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_id          INT          NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            in_possession    INT          NOT NULL DEFAULT 0,
            mastered         INT          NOT NULL DEFAULT 0,
            subsumed         INT          NOT NULL DEFAULT 0,
            resurgence_watch INT          NOT NULL DEFAULT 0,
            note             NVARCHAR(MAX),
            updated_at       NVARCHAR(50) NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120)),
            CONSTRAINT uq_progress_user_item UNIQUE (user_id, item_id)
        )
        """,
        """
        IF OBJECT_ID('drop_sources', 'U') IS NULL
        CREATE TABLE drop_sources (
            id           INT          IDENTITY(1,1) PRIMARY KEY,
            item_id      INT          NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            mission_type NVARCHAR(255),
            location     NVARCHAR(255),
            drop_chance  FLOAT,
            avg_runs     FLOAT,
            source_type  NVARCHAR(255),
            notes        NVARCHAR(MAX),
            updated_at   NVARCHAR(50) NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120))
        )
        """,
        """
        IF OBJECT_ID('market_cache', 'U') IS NULL
        CREATE TABLE market_cache (
            item_id         INT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
            platinum_price  INT,
            fetched_at      NVARCHAR(50) NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120))
        )
        """,
        """
        IF OBJECT_ID('user_market_watchlist', 'U') IS NULL
        CREATE TABLE user_market_watchlist (
            id       INT          IDENTITY(1,1) PRIMARY KEY,
            user_id  INT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_id  INT          NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            added_at NVARCHAR(50) NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120)),
            CONSTRAINT uq_watchlist_user_item UNIQUE (user_id, item_id)
        )
        """,
        """
        IF OBJECT_ID('settings', 'U') IS NULL
        CREATE TABLE settings (
            id      INT          IDENTITY(1,1) PRIMARY KEY,
            user_id INT          REFERENCES users(id) ON DELETE CASCADE,
            key     NVARCHAR(255) NOT NULL,
            value   NVARCHAR(MAX),
            CONSTRAINT uq_settings_user_key UNIQUE (user_id, key)
        )
        """,
        """
        IF OBJECT_ID('pinned_routes', 'U') IS NULL
        CREATE TABLE pinned_routes (
            id         INT          IDENTITY(1,1) PRIMARY KEY,
            user_id    INT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            route_data NVARCHAR(MAX) NOT NULL,
            pinned_at  NVARCHAR(50) NOT NULL DEFAULT (CONVERT(NVARCHAR(50), GETDATE(), 120))
        )
        """,
        """
        IF NOT EXISTS (
            SELECT * FROM sys.indexes
            WHERE name = 'idx_progress_user'
            AND object_id = OBJECT_ID('user_progress')
        ) CREATE INDEX idx_progress_user ON user_progress(user_id)
        """,
        """
        IF NOT EXISTS (
            SELECT * FROM sys.indexes
            WHERE name = 'idx_progress_item'
            AND object_id = OBJECT_ID('user_progress')
        ) CREATE INDEX idx_progress_item ON user_progress(item_id)
        """,
        """
        IF NOT EXISTS (
            SELECT * FROM sys.indexes
            WHERE name = 'idx_items_category'
            AND object_id = OBJECT_ID('items')
        ) CREATE INDEX idx_items_category ON items(category)
        """,
        """
        IF NOT EXISTS (
            SELECT * FROM sys.indexes
            WHERE name = 'idx_drop_item'
            AND object_id = OBJECT_ID('drop_sources')
        ) CREATE INDEX idx_drop_item ON drop_sources(item_id)
        """,
    ]
    for stmt in stmts:
        db.execute(stmt.strip())
    db.commit()
