"""Adding a Farming Planner goal when the user's largest `sort_order` is at the column's
maximum: the user's goals are renumbered 0..n-1 in the planner's order, then the new goal
goes last; below the maximum the add writes `MAX + 1` and touches no other row.

The matrices: the largest stored value {no goals, 0, a negative value, 2^31 - 1, 2^31,
2^63 - 2, 2^63 - 1, two goals tied at 2^63 - 1, a pinned goal at the maximum over an
unpinned one below it} x the backend's bound {SQLite 64-bit, MSSQL 32-bit}; the way the
value got there (the update route, a CSV import); repeated adds after the overflow; a
duplicate add; another user's rows; concurrent adds and moves of one user.
"""
import io
import threading

import pytest

from models.db import get_db
from models.item import Item
from models.user import User
from services.farming_service import (
    add_farming_goal,
    get_farming_goals,
    move_farming_goal,
)
from services.import_service import import_user_data

INT64_MAX = 2**63 - 1
INT32_MAX = 2**31 - 1

# Each set: (item name, pinned, sort_order) in insertion order.
STORED = {
    'none': [],
    'zero': [('A', 0, 0)],
    'negative': [('A', 0, -5), ('B', 0, -3)],
    'int32-max': [('A', 0, 4), ('B', 0, INT32_MAX)],
    'int32-max+1': [('A', 0, 4), ('B', 0, INT32_MAX + 1)],
    'int64-max-1': [('A', 0, 4), ('B', 0, INT64_MAX - 1)],
    'int64-max': [('A', 0, 4), ('B', 0, INT64_MAX), ('C', 0, 9)],
    'tied-at-max': [('A', 0, INT64_MAX), ('B', 0, INT64_MAX), ('C', 0, 2)],
    'pinned-at-max': [('U', 0, 7), ('P', 1, INT64_MAX), ('V', 0, 3)],
}

# (set, backend, renumbered?, the new goal's sort_order)
CASES = [
    ('none', 'sqlite', False, 0),
    ('zero', 'sqlite', False, 1),
    ('negative', 'sqlite', False, -2),
    ('int32-max', 'sqlite', False, INT32_MAX + 1),
    ('int32-max+1', 'sqlite', False, INT32_MAX + 2),
    ('int64-max-1', 'sqlite', False, INT64_MAX),
    ('int64-max', 'sqlite', True, 3),
    ('tied-at-max', 'sqlite', True, 3),
    ('pinned-at-max', 'sqlite', True, 3),
    ('zero', 'mssql', False, 1),
    ('int32-max', 'mssql', True, 2),
    ('int32-max+1', 'mssql', True, 2),
]


def uid(app, name):
    with app.app_context():
        return User.get_by_username(name).id


def item_ids(app, *names):
    with app.app_context():
        db = get_db()
        for name in names:
            Item.upsert(name, 'warframe', source='standard')
        return {n: db.execute('SELECT id FROM items WHERE name = ?', (n,)).fetchone()[0]
                for n in names}


def seed(app, user, rows):
    """Insert the goals for `user` straight into the table; returns {name: goal id}."""
    items = item_ids(app, *[name for name, _p, _o in rows])
    ids = {}
    with app.app_context():
        db = get_db()
        for name, pinned, order in rows:
            db.execute('INSERT INTO farming_goals (user_id, item_id, pinned, sort_order) '
                       'VALUES (?, ?, ?, ?)', (user, items[name], pinned, order))
            db.commit()
            ids[name] = db.execute('SELECT id FROM farming_goals WHERE user_id = ? AND item_id = ?',
                                   (user, items[name])).fetchone()[0]
    return ids


def page(app, user):
    """The planner's order as [(name, pinned, sort_order)]."""
    with app.app_context():
        return [(g['item_name'], g['pinned'], g['sort_order']) for g in get_farming_goals(user)]


def all_rows(app):
    with app.app_context():
        return [tuple(r) for r in get_db().execute(
            'SELECT id, user_id, item_id, pinned, display_mode, sort_order '
            'FROM farming_goals ORDER BY id').fetchall()]


def add(app, user, name):
    item = item_ids(app, name)[name]
    with app.app_context():
        return add_farming_goal(user, item)


@pytest.fixture
def admin(app, admin_client):
    return uid(app, 'admin')


@pytest.fixture
def other(app, admin):
    with app.app_context():
        User.create('other', 'other-password-1')
    return uid(app, 'other')


def add_as(app, user, name, backend_name):
    """Add with the app's backend setting at `backend_name` (the bound is read from it); the
    connection is opened first, so it stays this test's SQLite database."""
    item = item_ids(app, name)[name]
    old = app.config.get('DB_BACKEND')
    with app.app_context():
        get_db()
        app.config['DB_BACKEND'] = backend_name
        try:
            return add_farming_goal(user, item)
        finally:
            app.config['DB_BACKEND'] = old


@pytest.mark.parametrize('goal_set, backend_name, renumbered, new_order', CASES)
def test_an_add_puts_the_new_goal_last_and_keeps_the_others_order(
        app, admin, other, goal_set, backend_name, renumbered, new_order):
    seed(app, admin, STORED[goal_set])
    seed(app, other, [('O1', 0, INT64_MAX), ('O2', 1, 5)])
    others_before = [r for r in all_rows(app) if r[1] == other]
    before = page(app, admin)

    goal = add_as(app, admin, 'New', backend_name)

    assert goal['already_exists'] is False
    assert goal['sort_order'] == new_order
    after = page(app, admin)
    # The new goal is last among the unpinned goals; the old goals keep their order.
    assert after[-1] == ('New', 0, new_order)
    assert [n for n, _p, _o in after[:-1]] == [n for n, _p, _o in before]
    if renumbered:
        assert [o for _n, _p, o in after] == list(range(len(after)))
    else:
        assert after[:-1] == before
    with app.app_context():
        stored = get_db().execute('SELECT sort_order FROM farming_goals WHERE id = ?',
                                  (goal['goal_id'],)).fetchone()[0]
    assert stored == goal['sort_order']
    assert [r for r in all_rows(app) if r[1] == other] == others_before


def test_the_planner_route_adds_a_goal_after_one_was_set_to_the_maximum_by_update(
        app, admin, admin_client):
    ids = seed(app, admin, [('A', 0, 0), ('B', 0, 1), ('C', 0, 2)])
    assert admin_client.post(f"/farming/goals/{ids['A']}/update",
                             json={'sort_order': INT64_MAX}).status_code == 200
    assert page(app, admin)[-1] == ('A', 0, INT64_MAX)

    new_item = item_ids(app, 'New')['New']
    response = admin_client.post('/farming/goals/add', json={'item_id': new_item})

    assert response.status_code == 200
    assert [(n, o) for n, _p, o in page(app, admin)] == [
        ('B', 0), ('C', 1), ('A', 2), ('New', 3)]


def test_an_add_works_after_an_import_set_a_goal_to_the_maximum(app, admin):
    seed(app, admin, [('A', 0, 0), ('B', 0, 1)])
    item_ids(app, 'C')
    csv = f'name,category,pinned,sort_order\nC,warframe,1,{INT64_MAX}\nA,warframe,0,7\n'
    with app.app_context():
        import_user_data(admin, io.BytesIO(csv.encode('utf-8')), 'farming_goals.csv')
    assert page(app, admin) == [('C', 1, INT64_MAX), ('B', 0, 1), ('A', 0, 7)]

    goal = add(app, admin, 'New')

    assert goal['sort_order'] == 3
    assert page(app, admin) == [('C', 1, 0), ('B', 0, 1), ('A', 0, 2), ('New', 0, 3)]


def test_three_adds_after_the_overflow_renumber_once(app, admin):
    seed(app, admin, [('A', 0, INT64_MAX), ('B', 0, 3)])
    first = add(app, admin, 'N1')
    after_first = all_rows(app)
    second = add(app, admin, 'N2')
    third = add(app, admin, 'N3')

    assert [first['sort_order'], second['sort_order'], third['sort_order']] == [2, 3, 4]
    # The later adds wrote only their own row.
    assert all_rows(app)[:len(after_first)] == after_first
    assert [n for n, _p, _o in page(app, admin)] == ['B', 'A', 'N1', 'N2', 'N3']


def test_adding_a_goal_that_exists_returns_its_stored_values_and_renumbers_nothing(app, admin):
    seed(app, admin, [('A', 0, INT64_MAX), ('B', 0, 3)])
    before = all_rows(app)

    again = add(app, admin, 'A')

    assert again['already_exists'] is True
    assert again['sort_order'] == INT64_MAX
    assert all_rows(app) == before


def test_concurrent_adds_and_moves_of_one_user_keep_every_goal_once_in_a_valid_order(
        app, admin):
    ids = seed(app, admin, [('A', 0, INT64_MAX), ('B', 0, INT64_MAX - 1), ('P', 1, 4)])
    names = [f'N{i}' for i in range(12)]
    items = item_ids(app, *names)
    errors = []

    def adder():
        try:
            with app.app_context():
                for name in names:
                    add_farming_goal(admin, items[name])
        except Exception as err:  # surfaced in the main thread
            errors.append(err)

    def mover():
        try:
            with app.app_context():
                for _ in range(12):
                    move_farming_goal(admin, ids['A'], 'up')
                    move_farming_goal(admin, ids['A'], 'down')
        except Exception as err:
            errors.append(err)

    threads = [threading.Thread(target=adder), threading.Thread(target=mover)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    order = page(app, admin)
    assert sorted(n for n, _p, _o in order) == sorted(['A', 'B', 'P'] + names)
    assert order[0][0] == 'P'
    # The new goals stay in the order they were added, after the old ones.
    assert [n for n, _p, _o in order if n.startswith('N')] == names
    orders = [o for _n, _p, o in order]
    assert len(set(orders)) == len(orders)
    assert max(orders) <= INT64_MAX
