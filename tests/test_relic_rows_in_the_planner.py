"""Relic rows of drop_sources where the planner reads drop sources: the Live Session Advisor
offers no relic as a node, the route planner and get_drop_sources leave relic rows out, and
the farming plan and the drop modal still answer with relic rows present. Also the
relic_drops migration, on both backends.

Every test reads back rows it seeded itself; nothing is fetched.
"""
from pathlib import Path

import pytest

from models import db as db_module
from models.db import get_connection, get_db
from models.item import Item
from services import farming_service as fs

# (item, part, relic, Intact chance)
RELIC_ROWS = [
    ('Ash Prime', 'Neuroptics', 'Lith A1 Relic', 25.33),
    ('Ash Prime', 'Neuroptics', 'Axi V1 Relic', 2.0),       # vaulted: no location
    ('Ash Prime', 'Blueprint', 'Lith A1 Relic', 2.0),       # a part only relics hold
]

COMPONENT_RELIC = ' neo s1 RELIC'

# (relic, location, mission type, rotation, chance, avg runs)
LOCATIONS = [
    ('Lith A1 Relic', 'Mantle — Earth', 'Survival', 'A', 14.29, 7.0),
    ('Lith A1 Relic', 'Kiliken — Venus', 'Excavation', None, 20.0, 5.0),
]


def _seed(db):
    for name, category in [('Ash Prime', 'warframe'), ('Ferrite', 'resource')]:
        Item.upsert(name, category, source='standard')
    ids = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM items')}
    for item, part, relic, chance in RELIC_ROWS:
        db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, "
                   "avg_runs, source_type, notes) VALUES (?, 'Relic', ?, ?, NULL, 'relic', ?)",
                   (ids[item], relic, chance, part))
    db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs, "
               "source_type, notes) VALUES (?, 'Survival', 'Mantle — Earth', 5, 20, 'component', "
               "'Systems')", (ids['Ash Prime'],))
    # A WFCD component row naming a relic (spelt in another case, outer blanks): a relic
    # all the same, whichever source wrote it.
    db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs, "
               "source_type, notes) VALUES (?, 'Relic', ?, 50, 2, 'component', 'Neuroptics')",
               (ids['Ash Prime'], COMPONENT_RELIC))
    for row in LOCATIONS:
        db.execute('INSERT INTO relic_drops (relic_name, location, mission_type, rotation, '
                   'drop_chance, avg_runs) VALUES (?, ?, ?, ?, ?, ?)', row)
    db.execute("INSERT INTO component_ingredients (item_id, component_name, ingredient_name, "
               "ingredient_qty) VALUES (?, 'Neuroptics', 'Ferrite', 100)", (ids['Ash Prime'],))
    db.commit()
    return ids


@pytest.fixture
def seeded(app, admin_client):
    with app.app_context():
        db = get_db()
        ids = _seed(db)
        admin = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
        yield db, ids, admin


# ── The advisor and the route planner leave relics out ───────────────────────

def test_the_advisor_offers_no_relic_as_a_node(seeded, admin_client):
    nodes = admin_client.get('/farming/nodes').get_json()['nodes']
    assert nodes == ['Mantle — Earth']


@pytest.mark.parametrize('relic', ['Lith A1 Relic', COMPONENT_RELIC],
                         ids=['a relic row', 'a component row naming a relic'])
def test_a_relic_set_as_the_node_gives_no_opportunity(seeded, admin_client, relic):
    db, ids, admin = seeded
    db.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)', (admin, ids['Ash Prime']))
    db.commit()
    found = fs.get_advisor_opportunities(admin, relic)
    assert (found['opportunities'], found['harvest_summary']) == ([], [])
    harvest = fs.get_advisor_opportunities(admin, 'Mantle — Earth')['harvest_summary']
    assert [h['name'] for h in harvest] == ['Ash Prime — Systems']


def test_get_drop_sources_leaves_relic_rows_out(seeded):
    db, ids, admin = seeded
    assert [(r['source_type'], r['notes']) for r in fs.get_drop_sources(ids['Ash Prime'])] == [
        ('component', 'Systems')]


def test_the_route_planner_never_routes_to_a_relic(seeded):
    db, ids, admin = seeded
    db.execute("DELETE FROM drop_sources WHERE notes = 'Systems'")
    db.commit()
    assert fs.compute_farming_route(admin, [ids['Ash Prime']]) == []
    assert fs.get_drop_sources(ids['Ash Prime']) == []


# ── The plan and the drop modal still answer with relic rows present ─────────

def test_the_goal_plan_answers_with_relic_rows_present(seeded, admin_client):
    db, ids, admin = seeded
    db.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)', (admin, ids['Ash Prime']))
    db.commit()
    goal = db.execute('SELECT id FROM farming_goals').fetchone()[0]
    response = admin_client.get(f'/farming/goals/{goal}/plan')
    assert response.status_code == 200
    names = {c['component_name'] for c in response.get_json()['plan']['components']}
    assert 'Systems' in names
    assert admin_client.get('/farming/').status_code == 200


def test_the_plan_lists_a_parts_relics_after_its_sources_with_runs(seeded):
    db, ids, admin = seeded
    comps = {c['component_name']: c for c in fs.get_item_plan(ids['Ash Prime'], admin)['components']}
    assert [d['location'] for d in comps['Neuroptics']['drops']] == [
        COMPONENT_RELIC, 'Lith A1 Relic', 'Axi V1 Relic']
    assert [d['location'] for d in comps['Blueprint']['drops']] == ['Lith A1 Relic']


def test_the_drop_modal_answers_with_relic_rows_present(seeded, admin_client):
    db, ids, admin = seeded
    response = admin_client.get(f'/collection/drops/{ids["Ash Prime"]}')
    assert response.status_code == 200 and response.get_json()['ok'] is True


# ── The migration (axis 5, toolchain) ────────────────────────────────────────

def _user_data(db):
    return {table: sorted(tuple(r) for r in db.execute(f'SELECT * FROM {table}'))
            for table in ('users', 'user_progress', 'farming_goals', 'settings', 'items')}


def test_a_database_of_the_previous_version_migrates_and_keeps_its_data(seeded):
    db, ids, admin = seeded
    db.execute("INSERT INTO user_progress (user_id, item_id, in_possession, note) "
               "VALUES (?, ?, 1, 'kept')", (admin, ids['Ash Prime']))
    db.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)', (admin, ids['Ash Prime']))
    db.execute("INSERT INTO settings (user_id, key, value) VALUES (?, 'market_cache_hours', '6')",
               (admin,))
    db.execute('DROP TABLE relic_drops')
    db.execute('UPDATE schema_version SET version = 8')
    db.commit()
    before = _user_data(db)

    conn = get_connection()
    db_module._apply_migrations(conn)
    conn.close()

    assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 9
    assert db.execute('SELECT COUNT(*) FROM relic_drops').fetchone()[0] == 0
    assert _user_data(db) == before


def test_the_migration_runs_again_without_harm(seeded):
    db, ids, admin = seeded
    db_module._migrate_v9(db)
    assert db.execute('SELECT COUNT(*) FROM relic_drops').fetchone()[0] == len(LOCATIONS)


class _Recorder:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(' '.join(sql.split()))

    def commit(self):
        pass


def test_the_mssql_migration_creates_the_table_and_its_index_once():
    db = _Recorder()
    db_module._migrate_v9(db, 'mssql')
    create, index = db.statements
    assert create.startswith("IF OBJECT_ID('relic_drops', 'U') IS NULL CREATE TABLE relic_drops")
    assert 'IDENTITY(1,1)' in create and 'relic_name NVARCHAR(255) NOT NULL' in create
    assert index.startswith('IF NOT EXISTS') and 'CREATE INDEX idx_relic_drops_relic' in index


def test_the_relic_sync_uses_no_sqlite_only_sql():
    source = (Path(__file__).resolve().parent.parent / 'app' / 'services' / 'relic_sync.py').read_text(
        encoding='utf-8')
    for sqlite_only in ('LIMIT ', "datetime('now')", 'ON CONFLICT', 'INSERT OR ', 'executescript'):
        assert sqlite_only not in source
