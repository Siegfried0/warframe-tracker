"""Schema versions 2 to 8: each migration takes the connection and the backend's name, as the
runner in models/db.py calls it; nothing here imports from models/db.py."""


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
