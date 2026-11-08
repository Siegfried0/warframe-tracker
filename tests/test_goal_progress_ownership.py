"""A goal-progress write lands only in the caller's own goal (spec "User Accounts &
Authentication": all progress data is scoped to the authenticated user).

Axis 1 over the goal id: the caller's own goal, another user's goal, no goal at all; for
each, both row types. The write path (`farming_progress`) is checked by reading every row
back, so a write into the other user's goal shows even when the answer looks right.
"""
import pytest

from models.db import get_db
from models.item import Item
from models.user import User
from services.farming_service import add_farming_goal, set_goal_progress

MISSING_GOAL = 987654

# One row of each type, written into Alice's goal before every case.
SEEDED = [
    ('component', 'Blueprint', 1, 0, 'crafted'),
    ('ingredient', 'Morphics', 0, 3, None),
]

# A write that would change each seeded row, and one that would add a new row.
WRITES = [
    {'row_type': 'component', 'row_key': 'Blueprint', 'comp_state': 'pending'},
    {'row_type': 'ingredient', 'row_key': 'Morphics', 'quantity_owned': 50},
    {'row_type': 'component', 'row_key': 'Neuroptics', 'comp_state': 'blueprint'},
    {'row_type': 'ingredient', 'row_key': 'Orokin Cell', 'quantity_owned': 7},
]


def all_rows():
    return [tuple(r) for r in get_db().execute(
        'SELECT user_id, goal_id, row_type, row_key, acquired, quantity_owned, comp_state '
        'FROM farming_progress ORDER BY id').fetchall()]


@pytest.fixture
def goals(app, admin_client):
    """The admin (the caller, logged in) and Alice, each with a goal; Alice's has two rows."""
    with app.app_context():
        alice = User.create('alice', password='pw-alice-1').id
        admin = User.get_by_username('admin').id
        Item.upsert('Excalibur', 'warframe', source='standard')
        item = get_db().execute("SELECT id FROM items WHERE name = 'Excalibur'").fetchone()[0]
        alice_goal = add_farming_goal(alice, item)['goal_id']
        admin_goal = add_farming_goal(admin, item)['goal_id']
        for row_type, row_key, acquired, qty, state in SEEDED:
            assert set_goal_progress(alice, alice_goal, row_type, row_key, comp_state=state,
                                     acquired=bool(acquired), quantity_owned=qty)
        seeded = all_rows()
    assert len(seeded) == 2
    return {'alice': alice, 'admin': admin, 'alice_goal': alice_goal,
            'admin_goal': admin_goal, 'seeded': seeded}


def foreign_goal_ids(goals):
    return {'other user': goals['alice_goal'], 'no goal': MISSING_GOAL}


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('whose', ['other user', 'no goal'])
@pytest.mark.parametrize('write', WRITES, ids=lambda w: f"{w['row_type']}-{w['row_key']}")
def test_the_service_writes_nothing_into_a_goal_the_caller_does_not_own(app, goals, whose, write):
    goal_id = foreign_goal_ids(goals)[whose]
    with app.app_context():
        assert set_goal_progress(goals['admin'], goal_id, **write) is False
        assert all_rows() == goals['seeded']


@pytest.mark.parametrize('write', WRITES, ids=lambda w: f"{w['row_type']}-{w['row_key']}")
def test_the_service_writes_into_the_callers_own_goal(app, goals, write):
    with app.app_context():
        assert set_goal_progress(goals['admin'], goals['admin_goal'], **write) is True
        rows = all_rows()
    assert rows[:2] == goals['seeded']
    assert len(rows) == 3
    user_id, goal_id, row_type, row_key, _, qty, _ = rows[2]
    assert (user_id, goal_id, row_type, row_key) == (
        goals['admin'], goals['admin_goal'], write['row_type'], write['row_key'])
    assert qty == write.get('quantity_owned', 0)


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def post(client, goal_id, body):
    return client.post(f'/farming/goals/{goal_id}/progress', json=body)


@pytest.mark.parametrize('whose', ['other user', 'no goal'])
@pytest.mark.parametrize('write', WRITES, ids=lambda w: f"{w['row_type']}-{w['row_key']}")
def test_the_route_answers_404_and_writes_nothing_for_a_goal_the_caller_does_not_own(
        app, admin_client, goals, whose, write):
    response = post(admin_client, foreign_goal_ids(goals)[whose], write)
    assert response.status_code == 404
    assert response.get_json() == {'ok': False, 'error': 'Goal not found'}
    with app.app_context():
        assert all_rows() == goals['seeded']


def test_the_route_writes_the_callers_own_goal(app, admin_client, goals):
    response = post(admin_client, goals['admin_goal'],
                    {'row_type': 'component', 'row_key': 'Neuroptics', 'comp_state': 'crafted'})
    assert response.status_code == 200
    assert response.get_json() == {'ok': True}
    with app.app_context():
        rows = all_rows()
    assert rows[:2] == goals['seeded']
    assert rows[2] == (goals['admin'], goals['admin_goal'], 'component', 'Neuroptics', 1, 0,
                       'crafted')


def test_the_route_updates_a_row_already_in_the_callers_own_goal(app, admin_client, goals):
    body = {'row_type': 'ingredient', 'row_key': 'Morphics', 'quantity_owned': 4}
    assert post(admin_client, goals['admin_goal'], body).status_code == 200
    body['quantity_owned'] = 9
    assert post(admin_client, goals['admin_goal'], body).status_code == 200
    with app.app_context():
        rows = all_rows()
    assert rows[:2] == goals['seeded']
    assert rows[2:] == [(goals['admin'], goals['admin_goal'], 'ingredient', 'Morphics', 0, 9, None)]
