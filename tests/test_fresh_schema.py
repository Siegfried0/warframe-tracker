"""A fresh SQLite database ends at one exact schema: moving the migration functions between
modules changes no table, column, default, key, index or constraint.

The expected values were recorded from the code before the migrations moved out of the
connection module; they change only with a new migration (and then on purpose).
"""
import sqlite3

from models import db as db_module


def _fresh(app):
    with app.app_context():
        conn = db_module.get_connection()
        db_module._apply_migrations(conn)
        conn.close()
    return sqlite3.connect(app.config['DATABASE_PATH'])


def _columns_and_indexes(con):
    """{table: (columns, indexes)}: each column `name type [NN] [=default] [pkN]`, each index
    `name[!](col,...)` (! = unique), sorted by index name."""
    out = {}
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    for t in tables:
        cols = []
        for _, name, typ, notnull, default, pk in con.execute(f'PRAGMA table_info("{t}")'):
            cols.append(' '.join(x for x in (name, typ, 'NN' if notnull else '',
                                             f'={default}' if default is not None else '',
                                             f'pk{pk}' if pk else '') if x))
        idx = []
        for _, name, unique, *_rest in sorted(con.execute(f'PRAGMA index_list("{t}")'), key=lambda r: r[1]):
            on = ','.join(r[2] for r in con.execute(f'PRAGMA index_info("{name}")'))
            idx.append(f"{name}{'!' if unique else ''}({on})")
        out[t] = (cols, idx)
    return out


def _master(con):
    """Every sqlite_master row as `type name tbl_name: sql`, whitespace collapsed: foreign keys,
    ON DELETE, CHECKs and partial index predicates included."""
    return [f"{typ} {name} {tbl}: {' '.join((sql or '').split())}"
            for typ, name, tbl, sql in con.execute(
                'SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name')]


COLUMNS_AND_INDEXES = {
    'component_ingredients': (['id INTEGER pk1', 'item_id INTEGER NN', 'component_name TEXT NN', 'ingredient_name TEXT NN', 'ingredient_qty INTEGER NN =1', "updated_at TEXT NN =datetime('now')"],
        ['idx_ci_item(item_id)']),
    'component_market_slugs': (['item_id INTEGER NN pk1', 'component_name TEXT NN pk2', 'market_url_name TEXT NN'],
        ['sqlite_autoindex_component_market_slugs_1!(item_id,component_name)']),
    'drop_sources': (['id INTEGER pk1', 'item_id INTEGER NN', 'mission_type TEXT', 'location TEXT', 'drop_chance REAL', 'avg_runs REAL', 'source_type TEXT', 'notes TEXT', "updated_at TEXT NN =datetime('now')"],
        ['idx_drop_item(item_id)']),
    'farming_checklist': (['id INTEGER pk1', 'user_id INTEGER NN', 'item_id INTEGER NN', 'component_name TEXT NN', 'acquired INTEGER NN =0', "updated_at TEXT NN =datetime('now')"],
        ['idx_cl_user(user_id)', 'sqlite_autoindex_farming_checklist_1!(user_id,item_id,component_name)']),
    'farming_goals': (['id INTEGER pk1', 'user_id INTEGER NN', 'item_id INTEGER NN', 'pinned INTEGER NN =0', "display_mode TEXT NN ='tree'", 'sort_order INTEGER NN =0', "created_at TEXT NN =datetime('now')"],
        ['idx_fg_user(user_id)', 'sqlite_autoindex_farming_goals_1!(user_id,item_id)']),
    'farming_progress': (['id INTEGER pk1', 'user_id INTEGER NN', 'goal_id INTEGER NN', 'row_type TEXT NN', 'row_key TEXT NN', 'acquired INTEGER NN =0', 'quantity_owned INTEGER NN =0', "updated_at TEXT NN =datetime('now')", 'comp_state TEXT'],
        ['idx_fp_goal(goal_id)', 'sqlite_autoindex_farming_progress_1!(goal_id,row_type,row_key)']),
    'item_component_meta': (['item_id INTEGER NN pk1', 'component_name TEXT NN pk2', 'image_url TEXT'],
        ['sqlite_autoindex_item_component_meta_1!(item_id,component_name)']),
    'items': (['id INTEGER pk1', 'name TEXT NN', 'category TEXT NN', 'source TEXT', 'wiki_url TEXT', 'vaulted INTEGER NN =0', 'resurgence_available INTEGER NN =0', 'market_url_name TEXT', 'tradable INTEGER NN =1', "date_added TEXT NN =datetime('now')", "updated_at TEXT NN =datetime('now')", 'image_url TEXT', 'grants_mastery INTEGER NN =1', 'mod_type TEXT', 'mod_polarity TEXT', 'mod_max_rank INTEGER'],
        ['idx_items_category(category)', 'sqlite_autoindex_items_1!(name,category)']),
    'market_cache': (['item_id INTEGER pk1', 'platinum_price INTEGER', "fetched_at TEXT NN =datetime('now')"],
        []),
    'market_slug_cache': (['market_url_name TEXT pk1', 'platinum_price INTEGER', 'fetched_at TEXT NN'],
        ['sqlite_autoindex_market_slug_cache_1!(market_url_name)']),
    'pinned_routes': (['id INTEGER pk1', 'user_id INTEGER NN', 'route_data TEXT NN', "pinned_at TEXT NN =datetime('now')"],
        []),
    'relic_drops': (['id INTEGER pk1', 'relic_name TEXT NN', 'location TEXT NN', 'mission_type TEXT', 'rotation TEXT', 'drop_chance REAL', 'avg_runs REAL'],
        ['idx_relic_drops_relic(relic_name)']),
    'schema_version': (['version INTEGER pk1'],
        []),
    'settings': (['id INTEGER pk1', 'user_id INTEGER', 'key TEXT NN', 'value TEXT'],
        ['sqlite_autoindex_settings_1!(user_id,key)']),
    'user_market_watchlist': (['id INTEGER pk1', 'user_id INTEGER NN', 'item_id INTEGER NN', "added_at TEXT NN =datetime('now')"],
        ['sqlite_autoindex_user_market_watchlist_1!(user_id,item_id)']),
    'user_progress': (['id INTEGER pk1', 'user_id INTEGER NN', 'item_id INTEGER NN', 'in_possession INTEGER NN =0', 'mastered INTEGER NN =0', 'subsumed INTEGER NN =0', 'resurgence_watch INTEGER NN =0', 'note TEXT', "updated_at TEXT NN =datetime('now')", 'owned_rank INTEGER', 'target_rank INTEGER'],
        ['idx_progress_item(item_id)', 'idx_progress_user(user_id)', 'sqlite_autoindex_user_progress_1!(user_id,item_id)']),
    'users': (['id INTEGER pk1', 'username TEXT NN', 'email TEXT', 'password_hash TEXT', "role TEXT NN ='user'", "auth_type TEXT NN ='local'", 'oidc_sub TEXT', 'is_active INTEGER NN =1', "created_at TEXT NN =datetime('now')", 'last_login TEXT'],
        ['sqlite_autoindex_users_1!(username)', 'sqlite_autoindex_users_2!(email)', 'sqlite_autoindex_users_3!(oidc_sub)']),
}

MASTER = [
    'index idx_ci_item component_ingredients: CREATE INDEX idx_ci_item ON component_ingredients(item_id)',
    'index idx_cl_user farming_checklist: CREATE INDEX idx_cl_user ON farming_checklist(user_id)',
    'index idx_drop_item drop_sources: CREATE INDEX idx_drop_item ON drop_sources(item_id)',
    'index idx_fg_user farming_goals: CREATE INDEX idx_fg_user ON farming_goals(user_id)',
    'index idx_fp_goal farming_progress: CREATE INDEX idx_fp_goal ON farming_progress(goal_id)',
    'index idx_items_category items: CREATE INDEX idx_items_category ON items(category)',
    'index idx_progress_item user_progress: CREATE INDEX idx_progress_item ON user_progress(item_id)',
    'index idx_progress_user user_progress: CREATE INDEX idx_progress_user ON user_progress(user_id)',
    'index idx_relic_drops_relic relic_drops: CREATE INDEX idx_relic_drops_relic ON relic_drops(relic_name)',
    'index sqlite_autoindex_component_market_slugs_1 component_market_slugs: ',
    'index sqlite_autoindex_farming_checklist_1 farming_checklist: ',
    'index sqlite_autoindex_farming_goals_1 farming_goals: ',
    'index sqlite_autoindex_farming_progress_1 farming_progress: ',
    'index sqlite_autoindex_item_component_meta_1 item_component_meta: ',
    'index sqlite_autoindex_items_1 items: ',
    'index sqlite_autoindex_market_slug_cache_1 market_slug_cache: ',
    'index sqlite_autoindex_settings_1 settings: ',
    'index sqlite_autoindex_user_market_watchlist_1 user_market_watchlist: ',
    'index sqlite_autoindex_user_progress_1 user_progress: ',
    'index sqlite_autoindex_users_1 users: ',
    'index sqlite_autoindex_users_2 users: ',
    'index sqlite_autoindex_users_3 users: ',
    "table component_ingredients component_ingredients: CREATE TABLE component_ingredients ( id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, component_name TEXT NOT NULL, ingredient_name TEXT NOT NULL, ingredient_qty INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT (datetime('now')) )",
    'table component_market_slugs component_market_slugs: CREATE TABLE component_market_slugs ( item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, component_name TEXT NOT NULL, market_url_name TEXT NOT NULL, PRIMARY KEY (item_id, component_name) )',
    "table drop_sources drop_sources: CREATE TABLE drop_sources ( id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, mission_type TEXT, location TEXT, drop_chance REAL, avg_runs REAL, source_type TEXT, notes TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')) )",
    "table farming_checklist farming_checklist: CREATE TABLE farming_checklist ( id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, component_name TEXT NOT NULL, acquired INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(user_id, item_id, component_name) )",
    "table farming_goals farming_goals: CREATE TABLE farming_goals ( id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, pinned INTEGER NOT NULL DEFAULT 0, display_mode TEXT NOT NULL DEFAULT 'tree', sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(user_id, item_id) )",
    "table farming_progress farming_progress: CREATE TABLE farming_progress ( id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, goal_id INTEGER NOT NULL REFERENCES farming_goals(id) ON DELETE CASCADE, row_type TEXT NOT NULL, row_key TEXT NOT NULL, acquired INTEGER NOT NULL DEFAULT 0, quantity_owned INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT (datetime('now')), comp_state TEXT, UNIQUE(goal_id, row_type, row_key) )",
    'table item_component_meta item_component_meta: CREATE TABLE item_component_meta ( item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, component_name TEXT NOT NULL, image_url TEXT, PRIMARY KEY (item_id, component_name) )',
    "table items items: CREATE TABLE items ( id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, source TEXT, wiki_url TEXT, vaulted INTEGER NOT NULL DEFAULT 0, resurgence_available INTEGER NOT NULL DEFAULT 0, market_url_name TEXT, tradable INTEGER NOT NULL DEFAULT 1, date_added TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')), image_url TEXT, grants_mastery INTEGER NOT NULL DEFAULT 1, mod_type TEXT, mod_polarity TEXT, mod_max_rank INTEGER, UNIQUE(name, category) )",
    "table market_cache market_cache: CREATE TABLE market_cache ( item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE, platinum_price INTEGER, fetched_at TEXT NOT NULL DEFAULT (datetime('now')) )",
    'table market_slug_cache market_slug_cache: CREATE TABLE market_slug_cache ( market_url_name TEXT PRIMARY KEY, platinum_price INTEGER, fetched_at TEXT NOT NULL )',
    "table pinned_routes pinned_routes: CREATE TABLE pinned_routes ( id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, route_data TEXT NOT NULL, pinned_at TEXT NOT NULL DEFAULT (datetime('now')) )",
    'table relic_drops relic_drops: CREATE TABLE relic_drops ( id INTEGER PRIMARY KEY, relic_name TEXT NOT NULL, location TEXT NOT NULL, mission_type TEXT, rotation TEXT, drop_chance REAL, avg_runs REAL )',
    'table schema_version schema_version: CREATE TABLE schema_version ( version INTEGER PRIMARY KEY )',
    'table settings settings: CREATE TABLE settings ( id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, key TEXT NOT NULL, value TEXT, UNIQUE(user_id, key) )',
    "table user_market_watchlist user_market_watchlist: CREATE TABLE user_market_watchlist ( id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, added_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(user_id, item_id) )",
    "table user_progress user_progress: CREATE TABLE user_progress ( id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, in_possession INTEGER NOT NULL DEFAULT 0, mastered INTEGER NOT NULL DEFAULT 0, subsumed INTEGER NOT NULL DEFAULT 0, resurgence_watch INTEGER NOT NULL DEFAULT 0, note TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')), owned_rank INTEGER, target_rank INTEGER, UNIQUE(user_id, item_id) )",
    "table users users: CREATE TABLE users ( id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE, password_hash TEXT, role TEXT NOT NULL DEFAULT 'user', auth_type TEXT NOT NULL DEFAULT 'local', oidc_sub TEXT UNIQUE, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')), last_login TEXT )",
]


def test_a_fresh_database_has_every_column_and_index_as_recorded(app):
    con = _fresh(app)
    try:
        assert _columns_and_indexes(con) == COLUMNS_AND_INDEXES
    finally:
        con.close()


def test_a_fresh_database_has_every_definition_as_recorded(app):
    con = _fresh(app)
    try:
        assert _master(con) == MASTER
    finally:
        con.close()
