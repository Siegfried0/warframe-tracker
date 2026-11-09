"""Every id or integer a request passes to SQL is checked first: 400 or 404, never 500.

SQL's INTEGER / BIGINT holds a signed 64-bit value; a larger Python int makes the driver
raise (a 500), and an unknown item id on a write breaks a foreign key (a 500). The matrix
runs each route that takes an item id in its JSON body against every hostile spelling,
checks the status and reads every user row back to show that a refused request wrote
nothing. The URL ids, the farming plan's id lists and the goal integers follow.

A fresh database per matrix cell would make this file slow, so each route runs all its
spellings on one app: every spelling reads the rows before and after its own request, and
the test lists every spelling that went wrong.
"""
import json

import pytest
from flask import url_for

from models.db import get_db
from models.item import Item
from models.user import User
from services.farming_service import add_farming_goal, set_checklist
from services.request_ids import (
    Id64Converter,
    RequestIdError,
    int64,
    item_ids_in,
    parse_item_id,
)


class _Marker:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


KNOWN = _Marker('known-id')          # replaced by the arcane's id
KNOWN_STR = _Marker('known-id-str')  # replaced by the arcane's id as a string
MISSING = _Marker('missing-key')     # the key is left out of the body

MAX = 9223372036854775807  # 2**63 - 1, written out so a changed bound cannot size the test
MIN = -9223372036854775808

# (spelling, status on a route that also takes the string form)
SPELLINGS = [
    (KNOWN, 200),
    (KNOWN_STR, 200),
    (0, 404),
    (-1, 404),
    (999999, 404),
    (MAX, 404),
    (9223372036854775808, 404),
    (MIN, 404),
    (-9223372036854775809, 404),
    (10 ** 30, 404),
    ('999999', 404),
    ('9223372036854775807', 404),
    ('9223372036854775808', 404),
    ('9' * 30, 404),
    ('0' * 40 + '999999', 404),
    (True, 400),
    (False, 400),
    (1.0, 400),
    (1.5, 400),
    ('abc', 400),
    ('', 400),
    ('١', 400),        # ARABIC-INDIC DIGIT ONE: passes str.isdigit()
    ('１', 400),        # FULLWIDTH DIGIT ONE
    ('-1', 400),
    ('+1', 400),
    (' 1', 400),
    ('1 ', 400),
    ('1\n', 400),
    ('1.0', 400),
    (None, 400),
    ([1], 400),
    ({'id': 1}, 400),
    (MISSING, 400),
]

BODY_ROUTES = {
    '/collection/note': {'note': 'hand-typed'},
    '/collection/update-rank': {'owned_rank': 2},
    '/collection/update-target': {'target_rank': 3},
    '/farming/goals/add': {},
    '/farming/checklist': {'component_name': 'Blueprint', 'acquired': True},
    '/market/watch': {},
    '/market/unwatch': {},
}

USER_TABLES = ('user_progress', 'user_market_watchlist', 'farming_goals',
               'farming_progress', 'farming_checklist')


@pytest.fixture
def world(app, admin_client):
    """The admin's rows on a second item, an arcane to aim at, and a second user."""
    with app.app_context():
        db = get_db()
        Item.upsert('Filler A', 'primary', source='standard')
        Item.upsert('Arcane Energize', 'arcane', market_url_name='arcane_energize',
                    mod_max_rank=5)
        Item.upsert('Braton', 'primary', source='standard')
        ids = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM items')}
        admin = User.get_by_username('admin').id
        other = User.create('bob', password='pw-bob-12345').id
        db.execute('INSERT INTO user_progress (user_id, item_id, note) VALUES (?, ?, ?)',
                   (admin, ids['Braton'], 'kept'))
        db.execute('INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
                   (admin, ids['Braton']))
        db.commit()
        goal = add_farming_goal(admin, ids['Braton'])['goal_id']
        set_checklist(admin, ids['Braton'], 'Barrel', True)
    assert 999999 not in ids.values()
    return {'arcane': ids['Arcane Energize'], 'braton': ids['Braton'],
            'admin': admin, 'other': other, 'goal': goal}


def user_rows(app):
    with app.app_context():
        db = get_db()
        return {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1, 2')]
                for t in USER_TABLES}


def users(app):
    with app.app_context():
        return [tuple(r) for r in get_db().execute('SELECT * FROM users ORDER BY id')]


def body_for(route, spelling, world):
    body = dict(BODY_ROUTES[route])
    if spelling is KNOWN:
        body['item_id'] = world['arcane']
    elif spelling is KNOWN_STR:
        body['item_id'] = str(world['arcane'])
    elif spelling is not MISSING:
        body['item_id'] = spelling
    return body


def expected(route, spelling, status):
    """`/market/unwatch` is idempotent: a well-formed id in range answers 200."""
    if route == '/market/unwatch' and status == 404:
        number = int(spelling)
        if MIN <= number <= MAX:
            return 200
    return status


def post_raw(client, path, body):
    """Post `body` through Python's JSON, which writes Infinity and NaN as a browser cannot."""
    return client.post(path, data=json.dumps(body), content_type='application/json')


# ---------------------------------------------------------------------------
# Routes with an item id in the JSON body
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('route', list(BODY_ROUTES))
def test_a_body_item_id_gives_the_stated_status_and_a_refusal_writes_nothing(
        app, admin_client, world, route):
    wrong = []
    for spelling, status in SPELLINGS:
        want = expected(route, spelling, status)
        before = user_rows(app)
        response = admin_client.post(route, json=body_for(route, spelling, world))
        if response.status_code != want:
            wrong.append(f'{spelling!r}: {response.status_code}, want {want}')
        elif (want != 200 or route == '/market/unwatch') and user_rows(app) != before:
            wrong.append(f'{spelling!r}: answered {want} but wrote')
    assert not wrong, wrong


BAD_BODIES = ['not json', '[1, 2]', '"x"', '', 'null', '42', '{"item_id": 1']


@pytest.mark.parametrize('route', list(BODY_ROUTES))
def test_a_body_that_is_not_a_json_object_is_400_and_writes_nothing(app, admin_client, world,
                                                                    route):
    before = user_rows(app)
    statuses = {data: admin_client.post(route, data=data,
                                        content_type='application/json').status_code
                for data in BAD_BODIES}
    assert statuses == {data: 400 for data in BAD_BODIES}
    assert user_rows(app) == before


def test_the_toggle_still_takes_json_integers_only(app, admin_client, world):
    before = user_rows(app)
    spellings = ['١', '1', '-1', str(world['arcane'])]
    statuses = [admin_client.post('/collection/toggle',
                                  json={'item_id': s, 'field': 'mastered'}).status_code
                for s in spellings]
    assert statuses == [400] * len(spellings)
    assert user_rows(app) == before


def test_a_valid_id_writes_on_every_body_route(app, admin_client, world):
    for form in (int, str):
        item = form(world['arcane'])

        def send(route, **extra):
            return admin_client.post(route, json={'item_id': item, **extra})

        assert send('/collection/note', note=f'n-{form.__name__}').status_code == 200
        assert send('/collection/update-rank', owned_rank=9).get_json()['owned_rank'] == 5
        assert send('/collection/update-target', target_rank=3).status_code == 200
        assert send('/farming/goals/add').status_code == 200
        assert send('/farming/checklist', component_name='Blueprint',
                    acquired=True).status_code == 200
        assert send('/market/watch').status_code == 200
        with app.app_context():
            db = get_db()
            progress = db.execute('SELECT note, owned_rank, target_rank FROM user_progress '
                                  'WHERE user_id = ? AND item_id = ?',
                                  (world['admin'], world['arcane'])).fetchone()
            assert tuple(progress) == (f'n-{form.__name__}', 5, 3)
            for table in ('farming_goals', 'farming_checklist', 'user_market_watchlist'):
                assert db.execute(f'SELECT COUNT(*) FROM {table} '
                                  'WHERE user_id = ? AND item_id = ?',
                                  (world['admin'], world['arcane'])).fetchone()[0] == 1
        assert send('/market/unwatch').status_code == 200
        with app.app_context():
            assert get_db().execute('SELECT COUNT(*) FROM user_market_watchlist '
                                    'WHERE item_id = ?', (world['arcane'],)).fetchone()[0] == 0


RANKS = [float('inf'), float('-inf'), float('nan'), 'abc', [1]]


@pytest.mark.parametrize('route,key', [('/collection/update-rank', 'owned_rank'),
                                       ('/collection/update-target', 'target_rank')])
def test_a_rank_that_is_no_finite_integer_is_400_and_writes_nothing(app, admin_client, world,
                                                                    route, key):
    before = user_rows(app)
    statuses = [post_raw(admin_client, route, {'item_id': world['arcane'], key: rank}).status_code
                for rank in RANKS]
    assert statuses == [400] * len(RANKS)
    assert user_rows(app) == before


# ---------------------------------------------------------------------------
# Ids in the URL
# ---------------------------------------------------------------------------

OUT_OF_RANGE_PATHS = ['9223372036854775808', '18446744073709551616', '1' + '0' * 30,
                      '9' * 400, '%D9%A1', '%EF%BC%91', '-1', '+1', '1.0', '0x10', '%201']

URL_ROUTE_IDS = ['drops', 'price', 'goal-update', 'goal-progress', 'goal-plan', 'goal-move',
                 'goal-remove',
                 'admin-role', 'admin-toggle', 'admin-reset-password', 'admin-delete']


def url_cases(world):
    item, goal, other = world['arcane'], world['goal'], world['other']
    form = {'data': {'role': 'admin', 'confirm': 'DELETE'}}
    return [
        ('GET', '/collection/drops/{}', item, {}),
        ('GET', '/market/price/{}', item, {}),
        ('POST', '/farming/goals/{}/update', goal, {'json': {'pinned': True}}),
        ('POST', '/farming/goals/{}/progress', goal,
         {'json': {'row_type': 'component', 'row_key': 'Barrel', 'acquired': True}}),
        ('GET', '/farming/goals/{}/plan', goal, {}),
        ('POST', '/farming/goals/{}/move', goal, {'json': {'direction': 'up'}}),
        ('POST', '/farming/goals/{}/remove', goal, {'json': {}}),
        ('POST', '/admin/user/{}/role', other, form),
        ('POST', '/admin/user/{}/toggle', other, {}),
        ('POST', '/admin/user/{}/reset-password', other, {'json': {'password': 'long-enough-1'}}),
        ('POST', '/admin/user/{}/delete', other, form),
    ]


@pytest.fixture
def no_market_fetch(monkeypatch):
    import routes.market
    monkeypatch.setattr(routes.market, 'get_price',
                        lambda item_id, url_name, hours: {'platinum_price': 7})


@pytest.mark.parametrize('case', range(len(URL_ROUTE_IDS)), ids=URL_ROUTE_IDS)
def test_a_url_id_outside_the_range_is_404_and_writes_nothing(app, admin_client, world,
                                                              no_market_fetch, case):
    method, path, _, kwargs = url_cases(world)[case]
    before, users_before = user_rows(app), users(app)
    statuses = {segment: admin_client.open(path.format(segment), method=method,
                                           **kwargs).status_code
                for segment in OUT_OF_RANGE_PATHS}
    assert statuses == {segment: 404 for segment in OUT_OF_RANGE_PATHS}
    assert user_rows(app) == before
    assert users(app) == users_before


def test_a_valid_url_id_still_reaches_its_route(app, admin_client, world, no_market_fetch):
    statuses = {}
    for name, (method, path, row_id, kwargs) in zip(URL_ROUTE_IDS, url_cases(world)):
        statuses[name] = admin_client.open(path.format(row_id), method=method,
                                           **kwargs).status_code
    redirects = {'admin-role', 'admin-toggle', 'admin-delete'}
    assert statuses == {name: 302 if name in redirects else 200 for name in URL_ROUTE_IDS}


def test_the_largest_id_is_routed_to_each_route(app, world):
    adapter = app.url_map.bind('localhost')
    for method, path, _, _ in url_cases(world):
        endpoint, args = adapter.match(path.format(MAX), method=method)
        assert list(args.values()) == [MAX], (path, endpoint, args)


def test_the_url_builders_the_templates_use_still_build(app):
    with app.test_request_context():
        assert url_for('collection.drops', item_id=0) == '/collection/drops/0'
        assert url_for('market.price', item_id=0) == '/market/price/0'
        assert url_for('farming.goals_plan', goal_id=12) == '/farming/goals/12/plan'


@pytest.mark.parametrize('segment,value', [('0', 0), ('7', 7), ('007', 7),
                                           ('9223372036854775807', MAX),
                                           ('0' * 30 + '5', 5)])
def test_the_url_converter_reads_ascii_digits_in_range(app, segment, value):
    assert Id64Converter(app.url_map).to_python(segment) == value


# ---------------------------------------------------------------------------
# Integers in the body that are not item ids
# ---------------------------------------------------------------------------

def goal_row(app, goal):
    with app.app_context():
        return tuple(get_db().execute('SELECT sort_order, pinned FROM farming_goals WHERE id = ?',
                                      (goal,)).fetchone())


BAD_SORT_ORDERS = [9223372036854775808, -9223372036854775809, 10 ** 30,
                   float('inf'), float('nan'), 'abc', None, [1]]


def test_a_sort_order_outside_the_range_is_ignored_and_the_other_fields_written(
        app, admin_client, world):
    path = f"/farming/goals/{world['goal']}/update"
    sort_before = goal_row(app, world['goal'])[0]
    wrong = []
    for n, sort_order in enumerate(BAD_SORT_ORDERS):
        pinned = n % 2 == 0  # alternates, so each request's own write shows
        status = post_raw(admin_client, path, {'sort_order': sort_order,
                                               'pinned': pinned}).status_code
        row = goal_row(app, world['goal'])
        if status != 200 or row != (sort_before, int(pinned)):
            wrong.append((sort_order, status, row))
    assert not wrong, wrong


def test_a_sort_order_in_range_is_written_as_today(app, admin_client, world):
    path = f"/farming/goals/{world['goal']}/update"
    cases = [(MAX, MAX), (MIN, MIN), ('12', 12), (3.9, 3), (True, 1)]
    stored = []
    for sort_order, _ in cases:
        assert admin_client.post(path, json={'sort_order': sort_order}).status_code == 200
        stored.append(goal_row(app, world['goal'])[0])
    assert stored == [want for _, want in cases]


def test_a_goal_update_body_that_is_not_a_json_object_is_400(app, admin_client, world):
    before = goal_row(app, world['goal'])
    bodies = ['[1]', 'null', '"x"', 'not json']
    statuses = [admin_client.post(f"/farming/goals/{world['goal']}/update", data=data,
                                  content_type='application/json').status_code
                for data in bodies]
    assert statuses == [400] * len(bodies)
    assert goal_row(app, world['goal']) == before


BAD_QUANTITIES = [9223372036854775808, -9223372036854775809, 10 ** 30,
                  float('inf'), float('-inf'), float('nan')]


def test_a_quantity_outside_the_range_is_400_and_writes_nothing(app, admin_client, world):
    path = f"/farming/goals/{world['goal']}/progress"
    before = user_rows(app)
    statuses = [post_raw(admin_client, path, {'row_type': 'ingredient', 'row_key': 'Morphics',
                                              'quantity_owned': quantity}).status_code
                for quantity in BAD_QUANTITIES]
    assert statuses == [400] * len(BAD_QUANTITIES)
    assert user_rows(app) == before


def test_a_quantity_in_range_is_written_as_today(app, admin_client, world):
    path = f"/farming/goals/{world['goal']}/progress"
    cases = [(MAX, MAX), (MIN, 0), (4, 4)]
    stored = []
    for quantity, _ in cases:
        response = admin_client.post(path, json={'row_type': 'ingredient', 'row_key': 'Morphics',
                                                  'quantity_owned': quantity})
        assert response.status_code == 200
        with app.app_context():
            stored.append(get_db().execute(
                'SELECT quantity_owned FROM farming_progress WHERE goal_id = ? '
                "AND row_key = 'Morphics'", (world['goal'],)).fetchone()[0])
    assert stored == [want for _, want in cases]


# ---------------------------------------------------------------------------
# The farming plan's id lists: a bad entry is dropped, the request is not refused
# ---------------------------------------------------------------------------

PLAN_ROUTES = ['/farming/compute', '/farming/plan']

BAD_LIST_ENTRIES = [9223372036854775808, -9223372036854775809, 10 ** 30, '9' * 30,
                    '9223372036854775808', '١', '-1', -1, ' 1', '1.0', 1.0, 1.5,
                    True, False, None, '', 'abc', [1], {'id': 1}]


def test_a_list_of_only_bad_entries_is_400_never_500(app, admin_client, world):
    statuses = [admin_client.post(route, json={'item_ids': BAD_LIST_ENTRIES}).status_code
                for route in PLAN_ROUTES]
    assert statuses == [400, 400]


def test_the_good_entries_of_a_list_are_kept(app, admin_client, world):
    statuses = [admin_client.post(route, json={'item_ids': BAD_LIST_ENTRIES + [form(world['arcane'])]}
                                  ).status_code
                for route in PLAN_ROUTES for form in (int, str)]
    assert statuses == [200] * 4


def test_a_body_without_a_list_of_ids_is_400_never_500(app, admin_client, world):
    bodies = ['[1]', 'null', '"12"', 'not json', '{"item_ids": "12"}', '{"item_ids": 12}', '{}']
    statuses = {(route, data): admin_client.post(route, data=data,
                                                 content_type='application/json').status_code
                for route in PLAN_ROUTES for data in bodies}
    assert statuses == {key: 400 for key in statuses}


def test_item_ids_in_keeps_exactly_the_well_formed_in_range_ids():
    values = [0, 7, '8', '009', MAX, str(MAX)] + BAD_LIST_ENTRIES
    assert item_ids_in(values) == [0, 7, 8, 9, MAX, MAX]


# ---------------------------------------------------------------------------
# The shared check itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value,strings,result', [
    (0, False, 0), (MAX, False, MAX), (MIN, False, MIN),
    ('42', True, 42), ('0042', True, 42), (str(MAX), True, MAX),
    ('0' * 5000 + '1', True, 1),
])
def test_parse_item_id_accepts(value, strings, result):
    assert parse_item_id(value, strings=strings) == result


@pytest.mark.parametrize('value,strings,status', [
    ('42', False, 400), (True, True, 400), (1.0, True, 400), ('١', True, 400),
    ('-1', True, 400), (None, True, 400), ([], True, 400),
    (9223372036854775808, False, 404), (-9223372036854775809, False, 404),
    ('9223372036854775808', True, 404), ('9' * 5000, True, 404),
])
def test_parse_item_id_refuses(value, strings, status):
    with pytest.raises(RequestIdError) as err:
        parse_item_id(value, strings=strings)
    assert err.value.status == status


@pytest.mark.parametrize('value', [9223372036854775808, -9223372036854775809, float('inf'),
                                   float('nan'), '9' * 5000, 'abc'])
def test_int64_refuses_with_value_error(value):
    with pytest.raises(ValueError):
        int64(value)
