"""The Live Session Advisor's guaranteed-drop flag: drop_sources.drop_chance is a
percentage, so a node is guaranteed when its highest chance is at least 95 (percent),
and then every opportunity there is essentially free.

Every test reads back rows it seeded itself; nothing is fetched.
"""
import pytest

from models.db import get_db
from models.item import Item
from services import farming_service as fs

NODE = 'Mantle — Earth'

# (chances at the node, guaranteed?)
CHANCES = [
    ([0.95], False),
    ([25.33], False),
    ([94.99], False),
    ([95.0], True),
    ([100.0], True),
    ([None], False),
    ([None, 95.0], True),
    ([94.99, 5.0], False),
]


def _seed(db, admin, chances):
    Item.upsert('Ash Prime', 'warframe', source='standard')
    Item.upsert('Ferrite', 'resource', source='standard')
    ids = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM items')}
    parts = [f'Part {n}' for n in range(len(chances))]
    for part, chance in zip(parts, chances):
        db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, "
                   "avg_runs, source_type, notes) VALUES (?, 'Survival', ?, ?, NULL, "
                   "'component', ?)", (ids['Ash Prime'], NODE, chance, part))
        db.execute("INSERT INTO component_ingredients (item_id, component_name, "
                   "ingredient_name, ingredient_qty) VALUES (?, ?, 'Ferrite', 100)",
                   (ids['Ash Prime'], part))
    db.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)',
               (admin, ids['Ash Prime']))
    db.commit()
    return len(parts)


@pytest.mark.parametrize('chances, guaranteed', CHANCES,
                         ids=[' + '.join(str(c) for c in row[0]) for row in CHANCES])
def test_a_node_is_guaranteed_only_at_95_percent_or_more(app, admin_client, chances, guaranteed):
    with app.app_context():
        db = get_db()
        admin = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
        count = _seed(db, admin, chances)
        found = fs.get_advisor_opportunities(admin, NODE)
    assert found['guaranteed_free'] is guaranteed
    assert len(found['opportunities']) == count
    assert [o['essentially_free'] for o in found['opportunities']] == [guaranteed] * count


def test_the_threshold_is_in_percent():
    assert fs.GUARANTEED_DROP_PERCENT == 95.0
