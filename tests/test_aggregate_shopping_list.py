"""The aggregate shopping list (``GET /farming/aggregate``): every goal's still-needed
ingredients summed across the user's goals, a crafted component's ingredients left out, the
owned quantity capped at the need, an ingredient two goals share marked shared, and the
readiness the share of the summed need already held.

Every test reads back rows it seeded itself; nothing is fetched.
"""
from models.db import get_db
from models.item import Item
from services import farming_service as fs
from services.farming_progress import set_goal_progress


def _seed(db, admin):
    for name, category in [('Ash Prime', 'warframe'), ('Rhino Prime', 'warframe'),
                           ('Ferrite', 'resource'), ('Orokin Cell', 'resource')]:
        Item.upsert(name, category, source='standard')
    ids = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM items')}
    rows = [('Ash Prime', 'Chassis', 'Ferrite', 100),
            ('Ash Prime', 'Systems', 'Orokin Cell', 2),
            ('Rhino Prime', 'Chassis', 'Ferrite', 50),
            ('Rhino Prime', 'Neuroptics', 'Orokin Cell', 3)]
    for item, comp, ing, qty in rows:
        db.execute('INSERT INTO component_ingredients (item_id, component_name, '
                   'ingredient_name, ingredient_qty) VALUES (?, ?, ?, ?)',
                   (ids[item], comp, ing, qty))
    db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, "
               "avg_runs, source_type, notes) VALUES (?, 'Survival', 'Mantle — Earth', 30.0, "
               "4, 'resource', NULL)", (ids['Ferrite'],))
    goals = {}
    for item in ('Ash Prime', 'Rhino Prime'):
        cur = db.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)',
                         (admin, ids[item]))
        goals[item] = cur.lastrowid
    db.commit()
    return goals


def test_the_aggregate_list_sums_every_goal_and_leaves_out_crafted_parts(app, admin_client):
    with app.app_context():
        db = get_db()
        admin = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
        goals = _seed(db, admin)
        # Rhino's Neuroptics are crafted: its 3 Orokin Cells are needed no more.
        assert set_goal_progress(admin, goals['Rhino Prime'], 'component', 'Neuroptics',
                                 comp_state='crafted')
        # 500 Ferrite held on Ash's goal: capped at the summed need of 150.
        assert set_goal_progress(admin, goals['Ash Prime'], 'ingredient', 'Ferrite',
                                 quantity_owned=500)
        direct = fs.get_aggregate_shopping_list(admin)
    body = admin_client.get('/farming/aggregate').get_json()
    assert body['ok'] is True
    assert body['resources'] == direct['resources']
    by_name = {r['ingredient_name']: r for r in body['resources']}
    assert sorted(by_name) == ['Ferrite', 'Orokin Cell']
    ferrite, cell = by_name['Ferrite'], by_name['Orokin Cell']
    assert (ferrite['qty_needed'], ferrite['qty_owned'], ferrite['still_needed']) == (150, 150, 0)
    assert ferrite['shared'] is True
    assert sorted(ferrite['goal_labels']) == ['Ash Prime', 'Rhino Prime']
    assert [d['location'] for d in ferrite['drops']] == ['Mantle — Earth']
    assert (cell['qty_needed'], cell['qty_owned'], cell['still_needed']) == (2, 0, 2)
    assert cell['shared'] is False and cell['goal_labels'] == ['Ash Prime']
    assert body['readiness_pct'] == int(150 / 152 * 100)


def test_a_user_without_goals_has_an_empty_aggregate_list(admin_client):
    body = admin_client.get('/farming/aggregate').get_json()
    assert body == {'ok': True, 'resources': [], 'readiness_pct': 0}
