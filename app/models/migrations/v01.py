"""Schema version 1: the base tables, one function per backend.

The runner in models/db.py imports `_migrate_v1` and calls it with the connection and the
backend's name; nothing here imports from models/db.py."""


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
