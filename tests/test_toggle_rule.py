"""The subsume / in-possession / mastered toggle rule (ADR-0002) and its route.

The oracle is ADR-0002's table (§2) and its refusal (§3). States are written 'PMS':
in_possession, mastered, subsumed, each 0 or 1. Every case starts with resurgence_watch=1
so a toggle that must not touch it shows that it did not.
"""
import re

import pytest

from models.db import get_db
from models.item import Item
from models.progress import Progress
from models.user import User
from services.collection_service import ToggleError, toggle_flags, toggle_progress

STATES = ['000', '001', '010', '011', '100', '101', '110', '111']

# ADR-0002 §2, one toggle from each of the 8 states, the state it leaves.
ONE_TOGGLE = {
    'subsumed': {
        '000': '011', '010': '011', '100': '011', '110': '011',  # S ON: S=1, M=1, P=0
        '001': '000', '011': '010', '101': '100', '111': '110',  # S OFF: S=0
    },
    'mastered': {
        '000': '110', '100': '110',                               # M ON, S=0: M=1, P=1
        '001': '011', '101': '111',                               # M ON, S=1: M=1
        '010': '000', '011': '000', '110': '100', '111': '100',  # M OFF: M=0, S=0
    },
    'in_possession': {
        '000': '100', '001': '101', '010': '110', '011': '111',  # P ON: P=1
        '100': '000', '101': '001', '110': '010', '111': '011',  # P OFF: P=0
    },
    'resurgence_watch': {s: s for s in STATES},                   # flips, nothing else
}

# The same toggle twice. P and resurgence watch come back; S and M do not always:
# S ON leaves P=0 (the copy is destroyed) and M=1; M OFF clears S; M ON sets P.
TWO_TOGGLES = {
    'subsumed': {
        '000': '010', '001': '011', '010': '010', '011': '011',
        '100': '010', '101': '011', '110': '010', '111': '011',
    },
    'mastered': {
        '000': '100', '001': '000', '010': '110', '011': '110',
        '100': '100', '101': '100', '110': '110', '111': '110',
    },
    'in_possession': {s: s for s in STATES},
    'resurgence_watch': {s: s for s in STATES},
}

FLAGS = ('in_possession', 'mastered', 'subsumed')


def as_flags(state, watch=1):
    flags = {name: int(bit) for name, bit in zip(FLAGS, state)}
    flags['resurgence_watch'] = watch
    return flags


def as_state(flags):
    return ''.join(str(int(flags[name])) for name in FLAGS)


def matrix(table):
    return [(field, start, end) for field, rows in table.items()
            for start, end in sorted(rows.items())]


# ---------------------------------------------------------------------------
# The rule, pure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('field,start,end', matrix(ONE_TOGGLE))
def test_one_toggle_follows_the_adr_table(field, start, end):
    current = as_flags(start)
    result = {**current, **toggle_flags(field, current, 'warframe', None)}
    assert as_state(result) == end
    assert result['resurgence_watch'] == (0 if field == 'resurgence_watch' else 1)


def test_resurgence_watch_off_turns_on():
    assert toggle_flags('resurgence_watch', as_flags('000', watch=0), 'warframe', 'prime') == {
        'resurgence_watch': 1}


def test_a_missing_row_counts_as_all_flags_off():
    assert toggle_flags('mastered', {}, 'primary', None) == {'mastered': 1, 'in_possession': 1}


@pytest.mark.parametrize('category,source', [('primary', None), ('warframe', 'prime'),
                                             ('companion', 'prime'), ('mod', None)])
@pytest.mark.parametrize('start', ['000', '010', '100', '110'])
def test_subsume_on_is_refused_for_a_non_warframe_or_a_prime(category, source, start):
    with pytest.raises(ToggleError) as err:
        toggle_flags('subsumed', as_flags(start), category, source)
    assert err.value.status == 400


@pytest.mark.parametrize('category,source', [('primary', None), ('warframe', 'prime')])
def test_subsume_off_is_always_allowed(category, source):
    assert toggle_flags('subsumed', as_flags('011'), category, source) == {'subsumed': 0}


@pytest.mark.parametrize('source', [None, 'standard', 'kuva', 'umbra'])
def test_subsume_on_is_allowed_for_a_non_prime_warframe(source):
    assert toggle_flags('subsumed', as_flags('100'), 'warframe', source) == {
        'subsumed': 1, 'mastered': 1, 'in_possession': 0}


@pytest.mark.parametrize('field,start', [(f, s) for f in ('in_possession', 'mastered',
                                                          'resurgence_watch')
                                         for s in ('000', '110')])
def test_the_other_toggles_work_on_any_category(field, start):
    current = as_flags(start)
    expected = ONE_TOGGLE[field][start]
    assert as_state({**current, **toggle_flags(field, current, 'primary', 'prime')}) == expected


# ---------------------------------------------------------------------------
# On the test database
# ---------------------------------------------------------------------------

@pytest.fixture
def db_ctx(app):
    """An app context with two users and a non-Prime Warframe, a Prime Warframe and a rifle."""
    with app.app_context():
        alice = User.create('alice', password='pw-alice-1')
        bob = User.create('bob', password='pw-bob-1')
        Item.upsert('Excalibur', 'warframe', source='standard')
        Item.upsert('Excalibur Prime', 'warframe', source='prime')
        Item.upsert('Braton', 'primary', source='standard')
        ids = {r['name']: r['id'] for r in get_db().execute('SELECT id, name FROM items')}
        yield {'alice': alice.id, 'bob': bob.id, **ids}


def stored(user_id, item_id):
    return Progress.get_for_user(user_id, item_id)


def progress_rows():
    return get_db().execute('SELECT COUNT(*) FROM user_progress').fetchone()[0]


@pytest.mark.parametrize('field,start,end', matrix(TWO_TOGGLES))
def test_the_same_toggle_twice(db_ctx, field, start, end):
    user, item = db_ctx['alice'], db_ctx['Excalibur']
    Progress.upsert(user, item, **as_flags(start))
    toggle_progress(user, item, field)
    toggle_progress(user, item, field)
    row = stored(user, item)
    assert as_state(row) == end
    assert row['resurgence_watch'] == 1


def test_a_toggle_writes_only_the_current_users_row_for_that_item(db_ctx):
    alice, bob = db_ctx['alice'], db_ctx['bob']
    frame, rifle = db_ctx['Excalibur'], db_ctx['Braton']
    Progress.upsert(bob, frame, **as_flags('100'))
    Progress.upsert(alice, rifle, **as_flags('100'))
    toggle_progress(alice, frame, 'subsumed')
    assert as_state(stored(alice, frame)) == '011'
    assert as_state(stored(bob, frame)) == '100'
    assert as_state(stored(alice, rifle)) == '100'


@pytest.mark.parametrize('item', ['Excalibur Prime', 'Braton'])
def test_a_refused_subsume_writes_nothing(db_ctx, item):
    with pytest.raises(ToggleError):
        toggle_progress(db_ctx['alice'], db_ctx[item], 'subsumed')
    assert progress_rows() == 0


# ---------------------------------------------------------------------------
# The route, end to end
# ---------------------------------------------------------------------------

@pytest.fixture
def items(app, admin_client):
    with app.app_context():
        Item.upsert('Excalibur', 'warframe', source='standard')
        Item.upsert('Excalibur Prime', 'warframe', source='prime')
        Item.upsert('Braton', 'primary', source='standard')
        ids = {r['name']: r['id'] for r in get_db().execute('SELECT id, name FROM items')}
        ids['admin'] = User.get_by_username('admin').id
    return ids


def post(client, body):
    return client.post('/collection/toggle', json=body)


@pytest.mark.parametrize('field,end', [('in_possession', '100'), ('mastered', '110'),
                                       ('subsumed', '011'), ('resurgence_watch', '000')])
def test_the_route_writes_each_toggle(app, admin_client, items, field, end):
    response = post(admin_client, {'item_id': items['Excalibur'], 'field': field})
    assert response.status_code == 200
    body = response.get_json()
    assert body['ok'] is True
    assert body['new_value'] == 1
    with app.app_context():
        row = stored(items['admin'], items['Excalibur'])
    assert as_state(row) == end
    assert row['resurgence_watch'] == (1 if field == 'resurgence_watch' else 0)


def test_the_route_reports_the_off_value(app, admin_client, items):
    with app.app_context():
        Progress.upsert(items['admin'], items['Excalibur'], **as_flags('011'))
    body = post(admin_client, {'item_id': items['Excalibur'], 'field': 'mastered'}).get_json()
    assert body['new_value'] == 0
    assert body['written'] == {'mastered': 0, 'subsumed': 0}


@pytest.mark.parametrize('item', ['Excalibur Prime', 'Braton'])
def test_the_route_refuses_subsume_with_400(app, admin_client, items, item):
    response = post(admin_client, {'item_id': items[item], 'field': 'subsumed'})
    assert response.status_code == 400
    assert response.get_json()['ok'] is False
    with app.app_context():
        assert progress_rows() == 0


BAD_FIELDS = [None, '', 'Mastered', 'MASTERED', ' mastered', 'note', 'owned_rank', 1, ['mastered']]


@pytest.mark.parametrize('field', BAD_FIELDS)
def test_a_bad_field_is_400_and_writes_nothing(app, admin_client, items, field):
    response = post(admin_client, {'item_id': items['Excalibur'], 'field': field})
    assert response.status_code == 400
    with app.app_context():
        assert progress_rows() == 0


def test_a_missing_field_is_400(app, admin_client, items):
    assert post(admin_client, {'item_id': items['Excalibur']}).status_code == 400


BAD_ITEM_IDS = [None, '', 'abc', '1', 1.5, 1.0, True, False, [1], {'id': 1}]


@pytest.mark.parametrize('item_id', BAD_ITEM_IDS)
def test_a_non_integer_item_id_is_400_and_writes_nothing(app, admin_client, items, item_id):
    response = post(admin_client, {'item_id': item_id, 'field': 'mastered'})
    assert response.status_code == 400
    with app.app_context():
        assert progress_rows() == 0


def test_a_missing_item_id_is_400(app, admin_client, items):
    assert post(admin_client, {'field': 'mastered'}).status_code == 400


@pytest.mark.parametrize('item_id', [0, -1, 999999,
                                     # outside SQLite's 64-bit INTEGER: still an unknown item
                                     9223372036854775807, 9223372036854775808,
                                     -9223372036854775809, 100000000000000000000])
def test_an_unknown_item_is_404_and_writes_nothing(app, admin_client, items, item_id):
    response = post(admin_client, {'item_id': item_id, 'field': 'mastered'})
    assert response.status_code == 404
    with app.app_context():
        assert progress_rows() == 0


@pytest.mark.parametrize('data', ['not json', '[1, 2]', '"mastered"', '', 'null'])
def test_a_body_that_is_not_a_json_object_is_400(app, admin_client, items, data):
    response = admin_client.post('/collection/toggle', data=data,
                                 content_type='application/json')
    assert response.status_code == 400
    with app.app_context():
        assert progress_rows() == 0


def test_after_subsume_the_collection_page_shows_possession_grey(app, admin_client, items):
    frame = items['Excalibur']
    with app.app_context():
        Progress.upsert(items['admin'], frame, **as_flags('110', watch=0))
    assert post(admin_client, {'item_id': frame, 'field': 'subsumed'}).status_code == 200
    html = admin_client.get('/collection/warframe').get_data(as_text=True)
    card = re.search(r'class="wf-toggle ([\w-]+)"\s+data-item="%d"\s+data-field="in_possession"'
                     % frame, html)
    assert card is not None
    assert card.group(1) == 'grey-text'
