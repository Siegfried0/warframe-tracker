"""Every other value a request passes to SQL or a stored row is type-checked first.

The sqlite3 driver refuses a list or an object as a parameter, and an int beyond 64 bits
(each a 500); a route that runs `str()` over a value would store a list as its text. Each
route below takes one value of a fixed JSON type: the matrix sends it every hostile
spelling, checks the status (never 500) and reads the user's rows, the password hash and
the session's node back to show that a refused request changed nothing. The pager follows:
a page of any size answers 200.

A fresh database per matrix cell would make this file slow, so each route runs all its
spellings on one app: every spelling reads the state before and after its own request,
and the test lists every spelling that went wrong.
"""
import pytest

from models.db import get_db
from models.item import Item
from models.user import User
from services.farming_service import add_farming_goal, pin_route, set_checklist
from services.request_ids import RequestIdError, json_list, json_text, page_number


class _Marker:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


MISSING = _Marker('missing-key')  # the key is left out of the body

LONG_TEXT = 'x' * 5000

# Every hostile spelling of one body value.
SPELLINGS = ['Hydron (Sedna)', '', None, MISSING, 0, 1.5, 9223372036854775808, True, False,
             [1], ['a', 'b'], {'a': 1}, {}, LONG_TEXT]

USER_TABLES = ('user_progress', 'farming_progress', 'farming_checklist', 'pinned_routes')

NODE_KEY = 'advisor_node'
START_NODE = 'Start (Earth)'


@pytest.fixture
def world(app, admin_client):
    """An item with a note, a goal, a checklist row, a pinned route, a second local user."""
    with app.app_context():
        db = get_db()
        Item.upsert('Braton', 'primary', source='standard')
        braton = db.execute("SELECT id FROM items WHERE name = 'Braton'").fetchone()[0]
        admin = User.get_by_username('admin').id
        other = User.create('bob', password='pw-bob-12345').id
        db.execute('INSERT INTO user_progress (user_id, item_id, note) VALUES (?, ?, ?)',
                   (admin, braton, 'kept'))
        db.commit()
        goal = add_farming_goal(admin, braton)['goal_id']
        set_checklist(admin, braton, 'Barrel', True)
        pin_route(admin, [{'node': 'kept'}])
    with admin_client.session_transaction() as sess:
        sess[NODE_KEY] = START_NODE
    return {'braton': braton, 'admin': admin, 'other': other, 'goal': goal}


def state(app, client):
    """The user rows, every password hash and the session's advisor node."""
    with app.app_context():
        db = get_db()
        rows = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1, 2')]
                for t in USER_TABLES}
        rows['passwords'] = [tuple(r) for r in
                             db.execute('SELECT id, password_hash FROM users ORDER BY id')]
    with client.session_transaction() as sess:
        rows['node'] = sess.get(NODE_KEY)
    return rows


def reset(app, client, world):
    """Put back what a request the matrix expects to succeed has written."""
    with app.app_context():
        db = get_db()
        db.execute("UPDATE user_progress SET note = 'kept' WHERE user_id = ? AND item_id = ?",
                   (world['admin'], world['braton']))
        db.commit()
        pin_route(world['admin'], [{'node': 'kept'}])
    with client.session_transaction() as sess:
        sess[NODE_KEY] = START_NODE


# route, key, the rest of the body, the status for each spelling
def _note(s):
    return 200 if isinstance(s, str) or s is None or s is MISSING else 400


def _node(s):
    return 200 if isinstance(s, str) or s is MISSING else 400


def _route(s):
    return 200 if isinstance(s, list) or s is MISSING else 400


def _password(s):
    return 200 if isinstance(s, str) and s != '' else 400


def _component(s):
    return 200 if isinstance(s, str) else 400


def _row_key(s):
    return 200 if isinstance(s, str) else 400


def _row_type(s):
    return 400  # only 'component' and 'ingredient' are row types


def routes(world):
    goal = f"/farming/goals/{world['goal']}/progress"
    return {
        'note': ('/collection/note', 'note', {'item_id': world['braton']}, _note),
        'node': ('/farming/advisor/set', 'node_location', {}, _node),
        'pin': ('/farming/pin', 'route', {}, _route),
        'password': (f"/admin/user/{world['other']}/reset-password", 'password', {}, _password),
        'component': ('/farming/checklist', 'component_name',
                      {'item_id': world['braton'], 'acquired': True}, _component),
        'row_key': (goal, 'row_key', {'row_type': 'ingredient', 'quantity_owned': 3}, _row_key),
        'row_type': (goal, 'row_type', {'row_key': 'Barrel', 'quantity_owned': 3}, _row_type),
    }


ROUTE_NAMES = ['note', 'node', 'pin', 'password', 'component', 'row_key', 'row_type']

@pytest.mark.parametrize('name', ROUTE_NAMES)
def test_a_body_value_of_the_wrong_type_is_400_and_changes_nothing(app, admin_client, world,
                                                                   name):
    path, key, rest, status_of = routes(world)[name]
    wrong = []
    for spelling in SPELLINGS:
        want = status_of(spelling)
        body = dict(rest)
        if spelling is not MISSING:
            body[key] = spelling
        before = state(app, admin_client)
        response = admin_client.post(path, json=body)
        if response.status_code != want:
            wrong.append(f'{spelling!r:.40}: {response.status_code}, want {want}')
        elif want != 200 and state(app, admin_client) != before:
            wrong.append(f'{spelling!r:.40}: answered {want} but changed the state')
        reset(app, admin_client, world)
    assert not wrong, wrong


def test_a_valid_value_is_written_on_every_route(app, admin_client, world):
    goal = world['goal']
    assert admin_client.post('/collection/note', json={'item_id': world['braton'],
                                                        'note': LONG_TEXT}).status_code == 200
    assert admin_client.post('/farming/advisor/set',
                             json={'node_location': ' Hydron (Sedna) '}).status_code == 200
    assert admin_client.post('/farming/pin', json={'route': [{'node': 'new'}]}).status_code == 200
    assert admin_client.post(f"/admin/user/{world['other']}/reset-password",
                             json={'password': 'a-new-password'}).status_code == 200
    assert admin_client.post('/farming/checklist',
                             json={'item_id': world['braton'], 'component_name': 'Stock',
                                   'acquired': True}).status_code == 200
    assert admin_client.post(f'/farming/goals/{goal}/progress',
                             json={'row_type': 'ingredient', 'row_key': 'Ferrite',
                                   'quantity_owned': 7}).status_code == 200
    with admin_client.session_transaction() as sess:
        assert sess[NODE_KEY] == 'Hydron (Sedna)'
    with app.app_context():
        db = get_db()
        assert db.execute('SELECT note FROM user_progress WHERE item_id = ?',
                          (world['braton'],)).fetchone()[0] == LONG_TEXT
        assert db.execute('SELECT route_data FROM pinned_routes').fetchone()[0] \
            == '[{"node": "new"}]'
        assert User.get_by_id(world['other']).check_password('a-new-password')
        assert db.execute("SELECT acquired FROM farming_checklist "
                          "WHERE component_name = 'Stock'").fetchone()[0] == 1
        assert db.execute("SELECT quantity_owned FROM farming_progress "
                          "WHERE row_key = 'Ferrite'").fetchone()[0] == 7


def test_the_spellings_the_ui_sends_still_work(app, admin_client, world):
    """The note dialog sends the item id as a string; null clears a note; no key saves ""."""
    item = str(world['braton'])
    for note, stored in (('a note', 'a note'), (None, None), (MISSING, '')):
        body = {'item_id': item} if note is MISSING else {'item_id': item, 'note': note}
        assert admin_client.post('/collection/note', json=body).status_code == 200
        with app.app_context():
            assert get_db().execute('SELECT note FROM user_progress WHERE item_id = ?',
                                    (world['braton'],)).fetchone()[0] == stored
    # An empty or missing node clears the session's node.
    for body in ({'node_location': ''}, {}):
        with admin_client.session_transaction() as sess:
            sess[NODE_KEY] = START_NODE
        response = admin_client.post('/farming/advisor/set', json=body)
        assert response.get_json() == {'ok': True, 'node': None}
        with admin_client.session_transaction() as sess:
            assert NODE_KEY not in sess
    # A missing route pins an empty one.
    assert admin_client.post('/farming/pin', json={}).status_code == 200
    with app.app_context():
        assert get_db().execute('SELECT route_data FROM pinned_routes').fetchone()[0] == '[]'
    # The reset dialog's short and empty passwords keep their messages.
    path = f"/admin/user/{world['other']}/reset-password"
    assert admin_client.post(path, json={'password': ''}).get_json() == \
        {'error': 'Password is required.'}
    assert admin_client.post(path, json={'password': 'short'}).get_json() == \
        {'error': 'Password must be at least 8 characters.'}


BAD_BODIES = ['not json', '[1, 2]', '["route"]', '"x"', '', 'null', '42', 'true',
              '{"route": 1',
              # A JSON list nested deeper than the parser's recursion limit.
              '[' * 100000 + ']' * 100000,
              '{"route": ' + '[' * 100000 + ']' * 100000 + '}']

SHAPE_ROUTES = ['node', 'pin', 'password', 'row_key', 'note', 'component']


@pytest.mark.parametrize('name', SHAPE_ROUTES)
def test_a_body_that_is_not_a_json_object_is_400_and_changes_nothing(app, admin_client, world,
                                                                    name):
    path = routes(world)[name][0]
    before = state(app, admin_client)
    statuses = {data: admin_client.post(path, data=data,
                                        content_type='application/json').status_code
                for data in BAD_BODIES}
    assert statuses == {data: 400 for data in BAD_BODIES}
    assert state(app, admin_client) == before


# A JSON string the driver cannot bind: a lone surrogate (JSON "\ud800") has no UTF-8 form.
UNENCODABLE = 'Hydron ' + chr(0xD800)

TEXT_ROUTES = ['note', 'node', 'password', 'component', 'row_key']


@pytest.mark.parametrize('name', TEXT_ROUTES)
def test_a_string_the_database_cannot_encode_is_400_and_changes_nothing(app, admin_client,
                                                                        world, name):
    path, key, rest, _ = routes(world)[name]
    body = dict(rest)
    body[key] = UNENCODABLE if name != 'password' else 'long-enough-' + chr(0xD800)
    before = state(app, admin_client)
    status = admin_client.post(path, json=body).status_code
    assert (status, state(app, admin_client) == before) == (400, True)


def test_a_node_the_route_accepted_never_breaks_a_later_page(app, admin_client, world):
    admin_client.post('/farming/advisor/set', json={'node_location': UNENCODABLE})
    statuses = {p: admin_client.get(p).status_code
                for p in ('/farming/', '/farming/advisor', '/dashboard')}
    assert statuses == {'/farming/': 200, '/farming/advisor': 200, '/dashboard': 200}


# ---------------------------------------------------------------------------
# The collection pager
# ---------------------------------------------------------------------------

PAGES = ['1', '0', '-1', '2', '9223372036854775807', '9223372036854775808',
         '1' + '0' * 30, '9' * 5000, '-' + '9' * 5000, '+' + '9' * 5000, 'abc', '١', '1.5',
         '', ' 7 ']


@pytest.mark.parametrize('per_page', [24, 48, 96, 100, 200])
def test_a_page_of_any_size_answers_200(app, admin_client, world, per_page):
    wrong = {}
    for page in PAGES:
        response = admin_client.get('/collection/primary',
                                    query_string={'page': page, 'per_page': per_page})
        if response.status_code != 200:
            wrong[page[:30]] = response.status_code
    assert not wrong


def test_a_huge_page_shows_the_last_page_as_a_page_past_the_last_does(app, admin_client, world):
    """One item, so one page: a page past it shows no item but stays on page 1 of 1."""
    for page in ('5', '9223372036854775808', '1' + '0' * 30, '9' * 5000):
        html = admin_client.get('/collection/primary', query_string={'page': page}).get_data(
            as_text=True)
        assert 'Braton' not in html, page[:30]
    assert 'Braton' in admin_client.get('/collection/primary').get_data(as_text=True)


MAX = 9223372036854775807  # 2**63 - 1, written out so a changed bound cannot size the test


@pytest.mark.parametrize('per_page', [1, 24, 48, 96, 100, 200])
@pytest.mark.parametrize('value', ['9223372036854775808', '1' + '0' * 30, '9' * 5000,
                                   ' +' + '9' * 5000 + ' '])
def test_a_huge_page_keeps_its_offset_inside_64_bits(per_page, value):
    page = page_number(value, per_page)
    assert 0 <= (page - 1) * per_page <= MAX
    assert (page - 1) * per_page + per_page > MAX  # the last page whose offset fits


@pytest.mark.parametrize('value,page', [('1', 1), ('3', 3), (' 3 ', 3), ('+3', 3), ('0', 1),
                                        ('-1', 1), ('-' + '9' * 5000, 1), ('abc', 1),
                                        ('', 1), ('1.5', 1), (None, 1), (1, 1)])
def test_a_page_that_is_no_integer_or_below_one_is_page_one(value, page):
    assert page_number(value, 48) == page


# ---------------------------------------------------------------------------
# The checks themselves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value', [0, 1.5, True, [], ['a'], {}, {'a': 'b'}])
def test_json_text_refuses_every_other_type(value):
    for nullable in (False, True):
        with pytest.raises(RequestIdError) as err:
            json_text(value, 'note', nullable=nullable)
        assert err.value.status == 400


def test_json_text_takes_null_only_when_nullable():
    assert json_text('', 'note') == ''
    assert json_text(None, 'note', nullable=True) is None
    with pytest.raises(RequestIdError):
        json_text(None, 'note')


@pytest.mark.parametrize('value', [None, '', 'abc', 0, True, {}, {'a': [1]}])
def test_json_list_refuses_every_other_type(value):
    with pytest.raises(RequestIdError) as err:
        json_list(value, 'route')
    assert err.value.status == 400
