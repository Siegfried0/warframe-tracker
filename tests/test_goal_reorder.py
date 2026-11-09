"""Moving a Farming Planner goal up or down: one place within its pin group, then the
user's whole order renumbered 0..n-1, in one transaction, on the caller's rows only.

The matrices: goal sets {all unpinned, mixed pinned and unpinned, one goal, ties in
`sort_order`} x the goal's place in its group {first, middle, last} x direction {up, down};
the request's direction spelling and body shape; the goal's owner (the caller, another
user, nobody, an id outside the 64-bit range); repeated and concurrent moves; the
controls on the planner page and the dashboard's pinned goals in the new order.
"""
import re
import threading

import pytest

from models.db import get_db
from models.item import Item
from models.user import User
from services.farming_service import (
    add_farming_goal,
    get_farming_goals,
    get_pinned_goals,
    move_farming_goal,
    remove_farming_goal,
)

# Each set: (item name, pinned, sort_order) in insertion order. The mixed set's raw
# sort_order interleaves the groups, so the page order (pinned first) differs from it.
GOAL_SETS = {
    'unpinned': [('A', 0, 0), ('B', 0, 1), ('C', 0, 2), ('D', 0, 3)],
    'mixed': [('U1', 0, 0), ('P1', 1, 1), ('U2', 0, 2), ('P2', 1, 3), ('U3', 0, 4),
              ('P3', 1, 5)],
    'one': [('A', 0, 0)],
    'ties': [('A', 0, 0), ('B', 0, 0), ('C', 0, 0), ('D', 0, 0)],
    'ties-pinned': [('A', 1, 7), ('B', 1, 7), ('C', 0, 7), ('D', 0, 7), ('E', 0, 3)],
}

# (set, goal, direction, the page order afterwards); None: the goal is at the edge of its
# group and nothing changes.
MOVES = [
    ('unpinned', 'A', 'up', None),
    ('unpinned', 'A', 'down', 'B A C D'),
    ('unpinned', 'B', 'up', 'B A C D'),
    ('unpinned', 'B', 'down', 'A C B D'),
    ('unpinned', 'D', 'up', 'A B D C'),
    ('unpinned', 'D', 'down', None),
    ('mixed', 'P1', 'up', None),
    ('mixed', 'P1', 'down', 'P2 P1 P3 U1 U2 U3'),
    ('mixed', 'P2', 'up', 'P2 P1 P3 U1 U2 U3'),
    ('mixed', 'P2', 'down', 'P1 P3 P2 U1 U2 U3'),
    ('mixed', 'P3', 'up', 'P1 P3 P2 U1 U2 U3'),
    ('mixed', 'P3', 'down', None),
    ('mixed', 'U1', 'up', None),
    ('mixed', 'U1', 'down', 'P1 P2 P3 U2 U1 U3'),
    ('mixed', 'U2', 'up', 'P1 P2 P3 U2 U1 U3'),
    ('mixed', 'U2', 'down', 'P1 P2 P3 U1 U3 U2'),
    ('mixed', 'U3', 'up', 'P1 P2 P3 U1 U3 U2'),
    ('mixed', 'U3', 'down', None),
    ('one', 'A', 'up', None),
    ('one', 'A', 'down', None),
    ('ties', 'A', 'up', None),
    ('ties', 'A', 'down', 'B A C D'),
    ('ties', 'B', 'up', 'B A C D'),
    ('ties', 'B', 'down', 'A C B D'),
    ('ties', 'D', 'up', 'A B D C'),
    ('ties', 'D', 'down', None),
    ('ties-pinned', 'A', 'down', 'B A E C D'),
    ('ties-pinned', 'B', 'down', None),
    ('ties-pinned', 'E', 'up', None),
    ('ties-pinned', 'C', 'up', 'A B C E D'),
    ('ties-pinned', 'D', 'up', 'A B E D C'),
]

# The page order of each set before any move.
START = {
    'unpinned': 'A B C D',
    'mixed': 'P1 P2 P3 U1 U2 U3',
    'one': 'A',
    'ties': 'A B C D',
    'ties-pinned': 'A B E C D',
}


def user_id(app, name):
    with app.app_context():
        return User.get_by_username(name).id


def seed(app, user, goal_set, prefix=''):
    """Insert the set's goals for `user`; returns {name: goal id}."""
    ids = {}
    with app.app_context():
        db = get_db()
        for name, pinned, order in GOAL_SETS[goal_set]:
            Item.upsert(prefix + name, 'warframe', source='standard')
            item = db.execute('SELECT id FROM items WHERE name = ?', (prefix + name,)).fetchone()[0]
            db.execute('INSERT INTO farming_goals (user_id, item_id, pinned, sort_order) '
                       'VALUES (?, ?, ?, ?)', (user, item, pinned, order))
            db.commit()
            ids[name] = db.execute('SELECT id FROM farming_goals WHERE user_id = ? AND item_id = ?',
                                   (user, item)).fetchone()[0]
    return ids


def page_order(app, user, prefix=''):
    with app.app_context():
        return ' '.join(g['item_name'][len(prefix):] for g in get_farming_goals(user))


def goal_rows(app):
    with app.app_context():
        return [tuple(r) for r in get_db().execute(
            'SELECT id, user_id, item_id, pinned, display_mode, sort_order '
            'FROM farming_goals ORDER BY id').fetchall()]


def sort_orders(app, user):
    with app.app_context():
        return [g['sort_order'] for g in get_farming_goals(user)]


@pytest.fixture
def admin(app, admin_client):
    return user_id(app, 'admin')


# ---------------------------------------------------------------------------
# The move rule (the service)
# ---------------------------------------------------------------------------

def test_each_goal_set_starts_in_the_page_order_the_matrix_assumes(app, admin):
    for goal_set, expected in START.items():
        with app.app_context():
            get_db().execute('DELETE FROM farming_goals')
            get_db().commit()
        seed(app, admin, goal_set)
        assert page_order(app, admin) == expected, goal_set


@pytest.mark.parametrize('goal_set, goal, direction, expected', MOVES,
                         ids=[f'{s}-{g}-{d}' for s, g, d, _ in MOVES])
def test_a_move_changes_the_order_within_the_goals_pin_group(app, admin, goal_set, goal,
                                                             direction, expected):
    ids = seed(app, admin, goal_set)
    before = goal_rows(app)
    with app.app_context():
        assert move_farming_goal(admin, ids[goal], direction) is True
    if expected is None:
        assert goal_rows(app) == before
        assert page_order(app, admin) == START[goal_set]
    else:
        assert page_order(app, admin) == expected
        assert sort_orders(app, admin) == list(range(len(GOAL_SETS[goal_set])))


@pytest.mark.parametrize('goal_set', ['ties', 'ties-pinned'])
def test_the_first_move_tells_tied_goals_apart(app, admin, goal_set):
    ids = seed(app, admin, goal_set)
    with app.app_context():
        move_farming_goal(admin, ids['C'], 'down' if goal_set == 'ties' else 'up')
    orders = sort_orders(app, admin)
    assert len(set(orders)) == len(orders)


def test_pinning_or_unpinning_leaves_sort_order_alone(app, admin, admin_client):
    ids = seed(app, admin, 'mixed')
    with app.app_context():
        move_farming_goal(admin, ids['U2'], 'up')
    before = sort_orders(app, admin)
    assert admin_client.post(f"/farming/goals/{ids['P2']}/update",
                             json={'pinned': False}).status_code == 200
    with app.app_context():
        after = {g['item_name']: g['sort_order'] for g in get_farming_goals(admin)}
    assert after['P2'] == 1
    assert sorted(after.values()) == sorted(before)


@pytest.mark.parametrize('direction', ['UP', 'Down', ' up', '', None, 1, ['up']])
def test_the_service_refuses_a_direction_other_than_up_or_down(app, admin, direction):
    ids = seed(app, admin, 'unpinned')
    before = goal_rows(app)
    with app.app_context():
        assert move_farming_goal(admin, ids['B'], direction) is False
    assert goal_rows(app) == before


# ---------------------------------------------------------------------------
# Repetition
# ---------------------------------------------------------------------------

def test_up_then_down_returns_to_the_start(app, admin):
    ids = seed(app, admin, 'mixed')
    with app.app_context():
        move_farming_goal(admin, ids['U2'], 'up')
        move_farming_goal(admin, ids['U2'], 'down')
    assert page_order(app, admin) == START['mixed']


def test_ten_ups_bring_the_last_goal_of_a_group_to_its_top(app, admin):
    ids = seed(app, admin, 'mixed')
    with app.app_context():
        for _ in range(10):
            assert move_farming_goal(admin, ids['U3'], 'up') is True
    assert page_order(app, admin) == 'P1 P2 P3 U3 U1 U2'
    assert sort_orders(app, admin) == [0, 1, 2, 3, 4, 5]


def test_a_double_click_moves_twice_and_keeps_a_valid_order(app, admin, admin_client):
    ids = seed(app, admin, 'unpinned')
    for _ in range(2):
        assert admin_client.post(f"/farming/goals/{ids['D']}/move",
                                 json={'direction': 'up'}).status_code == 200
    assert page_order(app, admin) == 'A D B C'
    assert sort_orders(app, admin) == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Concurrency: two tabs of the same user
# ---------------------------------------------------------------------------

def test_concurrent_moves_of_one_user_leave_every_goal_once_in_a_valid_order(app, admin):
    ids = seed(app, admin, 'mixed')
    errors = []

    def mover(goals, direction):
        try:
            with app.app_context():
                for _ in range(15):
                    for name in goals:
                        move_farming_goal(admin, ids[name], direction)
        except Exception as err:  # surfaced in the main thread
            errors.append(err)

    threads = [threading.Thread(target=mover, args=(['U1', 'P3'], 'down')),
               threading.Thread(target=mover, args=(['U3', 'P1'], 'up'))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    order = page_order(app, admin).split()
    assert sorted(order) == sorted(START['mixed'].split())
    assert [n[0] for n in order] == ['P', 'P', 'P', 'U', 'U', 'U']
    assert sort_orders(app, admin) == [0, 1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# The route: spellings, bodies, owners
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('direction, expected', [('up', 'B A C D'), ('down', 'A C B D')])
def test_the_route_moves_the_goal_and_answers_ok(app, admin, admin_client, direction, expected):
    ids = seed(app, admin, 'unpinned')
    response = admin_client.post(f"/farming/goals/{ids['B']}/move", json={'direction': direction})
    assert response.status_code == 200
    assert response.get_json() == {'ok': True}
    assert page_order(app, admin) == expected


def test_a_move_at_the_edge_answers_ok_and_writes_nothing(app, admin, admin_client):
    ids = seed(app, admin, 'unpinned')
    before = goal_rows(app)
    response = admin_client.post(f"/farming/goals/{ids['A']}/move", json={'direction': 'up'})
    assert (response.status_code, response.get_json()) == (200, {'ok': True})
    assert goal_rows(app) == before


BAD_DIRECTIONS = {
    'upper case': {'direction': 'UP'},
    'leading blank': {'direction': ' up'},
    'trailing blank': {'direction': 'down '},
    'empty': {'direction': ''},
    'number': {'direction': 1},
    'true': {'direction': True},
    'null': {'direction': None},
    'missing': {},
    'list': {'direction': ['up']},
    'object': {'direction': {'up': 1}},
}


@pytest.mark.parametrize('body', list(BAD_DIRECTIONS.values()), ids=list(BAD_DIRECTIONS))
def test_a_direction_other_than_up_or_down_is_400_and_writes_nothing(app, admin, admin_client,
                                                                     body):
    ids = seed(app, admin, 'unpinned')
    before = goal_rows(app)
    response = admin_client.post(f"/farming/goals/{ids['B']}/move", json=body)
    assert response.status_code == 400
    assert response.get_json()['ok'] is False
    assert goal_rows(app) == before


BAD_BODIES = {
    'array': {'data': '["up"]'},
    'string': {'data': '"up"'},
    'number': {'data': '1'},
    'null': {'data': 'null'},
    'empty': {'data': ''},
    'not json': {'data': 'direction=up'},
}


@pytest.mark.parametrize('kwargs', list(BAD_BODIES.values()), ids=list(BAD_BODIES))
def test_a_body_that_is_not_a_json_object_is_400_and_writes_nothing(app, admin, admin_client,
                                                                    kwargs):
    ids = seed(app, admin, 'unpinned')
    before = goal_rows(app)
    response = admin_client.post(f"/farming/goals/{ids['B']}/move",
                                 content_type='application/json', **kwargs)
    assert response.status_code == 400
    assert goal_rows(app) == before


@pytest.mark.parametrize('whose', ['other user', 'no goal', 'largest id', 'outside the range'])
@pytest.mark.parametrize('direction', ['up', 'down'])
def test_a_goal_the_caller_does_not_own_is_404_and_both_orders_stay(app, admin, admin_client,
                                                                     whose, direction):
    with app.app_context():
        alice = User.create('alice', password='pw-alice-1').id
    seed(app, admin, 'unpinned')
    hers = seed(app, alice, 'unpinned', prefix='alice-')
    before = goal_rows(app)
    goal = {'other user': hers['B'], 'no goal': 987654, 'largest id': 2 ** 63 - 1,
            'outside the range': 2 ** 63}[whose]
    response = admin_client.post(f'/farming/goals/{goal}/move', json={'direction': direction})
    assert response.status_code == 404
    assert goal_rows(app) == before
    assert page_order(app, admin) == 'A B C D'
    assert page_order(app, alice, prefix='alice-') == 'A B C D'


def test_a_move_reads_and_writes_only_the_callers_goals(app, admin):
    with app.app_context():
        alice = User.create('alice', password='pw-alice-1').id
    mine = seed(app, admin, 'ties')
    seed(app, alice, 'ties', prefix='alice-')
    hers_before = [r for r in goal_rows(app) if r[1] == alice]
    with app.app_context():
        assert move_farming_goal(admin, mine['A'], 'down') is True
    assert [r for r in goal_rows(app) if r[1] == alice] == hers_before
    assert page_order(app, admin) == 'B A C D'


# ---------------------------------------------------------------------------
# The controls on the planner page
# ---------------------------------------------------------------------------

MOVE_BUTTON = re.compile(r'<button type="button" class="[^"]*goal-move-btn[^"]*"(.*?)>', re.S)


def move_buttons(html):
    """{(goal id, direction): [disabled, ...]} for each move button on the page."""
    buttons = {}
    for attrs in MOVE_BUTTON.findall(html):
        goal = int(re.search(r'data-goal="(\d+)"', attrs).group(1))
        direction = re.search(r'data-direction="(\w+)"', attrs).group(1)
        assert re.search(r'title="Move goal (up|down)"', attrs)
        assert re.search(r'aria-label="Move [^"]+ (up|down)"', attrs)
        buttons.setdefault((goal, direction), []).append(bool(re.search(r'\sdisabled\b', attrs)))
    return buttons


# (set, {goal: (up disabled, down disabled)})
CONTROL_STATES = [
    ('unpinned', {'A': (True, False), 'B': (False, False), 'C': (False, False),
                  'D': (False, True)}),
    ('mixed', {'P1': (True, False), 'P2': (False, False), 'P3': (False, True),
               'U1': (True, False), 'U2': (False, False), 'U3': (False, True)}),
    ('one', {'A': (True, True)}),
    ('ties-pinned', {'A': (True, False), 'B': (False, True), 'E': (True, False),
                     'C': (False, False), 'D': (False, True)}),
]


@pytest.mark.parametrize('goal_set, states', CONTROL_STATES, ids=[c[0] for c in CONTROL_STATES])
def test_every_card_shows_both_controls_disabled_only_at_the_edge_of_its_group(
        app, admin, admin_client, goal_set, states):
    ids = seed(app, admin, goal_set)
    html = admin_client.get('/farming/').get_data(as_text=True)
    buttons = move_buttons(html)
    expected = {}
    for name, (up, down) in states.items():
        # each card has the controls twice: in its compact and its expanded state
        expected[(ids[name], 'up')] = [up, up]
        expected[(ids[name], 'down')] = [down, down]
    assert buttons == expected


def test_a_page_without_goals_has_no_move_controls(app, admin, admin_client):
    html = admin_client.get('/farming/').get_data(as_text=True)
    assert move_buttons(html) == {}


def test_the_move_controls_are_keyboard_reachable_buttons(app, admin, admin_client):
    seed(app, admin, 'unpinned')
    html = admin_client.get('/farming/').get_data(as_text=True)
    tags = MOVE_BUTTON.findall(html)
    assert len(tags) == 16
    assert not any('tabindex="-1"' in attrs for attrs in tags)


def test_a_reload_after_a_move_shows_the_new_order(app, admin, admin_client):
    ids = seed(app, admin, 'unpinned')
    admin_client.post(f"/farming/goals/{ids['C']}/move", json={'direction': 'up'})
    html = admin_client.get('/farming/').get_data(as_text=True)
    cards = re.findall(r'id="goal-col-(\d+)"', html)
    assert cards == [str(ids[n]) for n in 'A C B D'.split()]


# ---------------------------------------------------------------------------
# The dashboard and the add function
# ---------------------------------------------------------------------------

def test_the_dashboards_pinned_goals_follow_the_new_order(app, admin, admin_client):
    ids = seed(app, admin, 'mixed', prefix='Goal ')
    with app.app_context():
        move_farming_goal(admin, ids['P3'], 'up')
        assert [g['item_name'] for g in get_pinned_goals(admin)] == ['Goal P1', 'Goal P3',
                                                                    'Goal P2']
    response = admin_client.get('/dashboard')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    positions = [html.find(f'>Goal {name}<') for name in ('P1', 'P3', 'P2')]
    assert -1 not in positions
    assert positions == sorted(positions)


def test_add_returns_the_sort_order_it_wrote(app, admin):
    with app.app_context():
        for name in ('Rhino', 'Mesa', 'Nova', 'Loki'):
            Item.upsert(name, 'warframe', source='standard')
        db = get_db()
        item = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM items').fetchall()}
        added = [add_farming_goal(admin, item[n]) for n in ('Rhino', 'Mesa', 'Nova')]
        assert [g['sort_order'] for g in added] == [0, 1, 2]
        remove_farming_goal(admin, added[1]['goal_id'])
        loki = add_farming_goal(admin, item['Loki'])
        assert loki['sort_order'] == 3
        again = add_farming_goal(admin, item['Nova'])
        assert again['already_exists'] is True
        assert again['sort_order'] == 2
        stored = {r['id']: r['sort_order'] for r in db.execute(
            'SELECT id, sort_order FROM farming_goals').fetchall()}
        assert stored[loki['goal_id']] == 3
