"""Schema versions 9 to 11: each migration takes the connection and the backend's name, as the
runner in models/db.py calls it; nothing here imports from models/db.py."""


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
