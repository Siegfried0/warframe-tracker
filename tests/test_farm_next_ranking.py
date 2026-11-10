"""The dashboard's "Farm next" list is ranked by expected farming time.

Without pinned goals the list holds the caller's unowned, unmastered items with a known
farming time first, fewest expected hours first (every part of the item counted, the
buy-or-farm rule's estimate), then the items whose time is unknown (no usable source for a
needed part, or a part only relics dropping nowhere hold); equal times keep the category
and name order. The first `FARM_NEXT_ROWS` are shown, each with its estimate in a "Farming
time" column (`~2.0h`, `—` when unknown).

The drop rows of all unowned items are read a fixed number of items a query, and the relics
dropping nowhere looked up once, so loading the list issues as many queries for 60 items as
for 6; the batched read and the one-item read give the same sources.

Network refused (tests/conftest.py).
"""
import math
import re

import pytest

from models.db import get_db
from routes.dashboard import FARM_NEXT_ROWS
from services import buy_vs_farm
from services.buy_vs_farm import (expected_hours, hours_label, item_sources,
                                  item_sources_many, source_minutes)
from services.farming_service import farm_next_items
from test_dashboard_farm_next import (  # noqa: F401  (fixtures: admin_id, requests_made)
    admin_id, answer_of, farm_card, requests_made)

# A drop row without a mission type counts 5 minutes a run: 12 runs are one hour.
RUNS_PER_HOUR = 12


def add(name, category='primary', slug=None):
    db = get_db()
    db.execute('INSERT INTO items (name, category, market_url_name) VALUES (?, ?, ?)',
               (name, category, slug))
    db.commit()
    return db.execute('SELECT id FROM items WHERE name = ? AND category = ?',
                      (name, category)).fetchone()[0]


def drop(item, avg_runs=None, location='Earth/Mantle', source_type=None, notes=None,
         drop_chance=None, mission_type=None):
    get_db().execute(
        'INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs, '
        'source_type, notes) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (item, mission_type, location, drop_chance, avg_runs, source_type, notes))
    get_db().commit()


def hours(item, h):
    """One own mission source worth `h` hours."""
    drop(item, avg_runs=h * RUNS_PER_HOUR)


def relic_drop(relic, location='Lua/Apollo'):
    get_db().execute('INSERT INTO relic_drops (relic_name, location, avg_runs) VALUES (?, ?, 3)',
                     (relic, location))
    get_db().commit()


def ingredient(item, component):
    get_db().execute('INSERT INTO component_ingredients (item_id, component_name, '
                     "ingredient_name) VALUES (?, ?, 'Ferrite')", (item, component))
    get_db().commit()


def progress(user_id, item, in_possession=0, mastered=0):
    get_db().execute('INSERT INTO user_progress (user_id, item_id, in_possession, mastered) '
                     'VALUES (?, ?, ?, ?)', (user_id, item, in_possession, mastered))
    get_db().commit()


def listed(user_id, limit=FARM_NEXT_ROWS):
    return farm_next_items(user_id, None, None, limit)


def names(rows):
    return [row['name'] for row in rows]


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------

def test_known_times_come_first_quickest_first_then_unknown_in_category_and_name_order(
        app, admin_id):
    with app.app_context():
        hours(add('Aklex', 'secondary'), 3)
        hours(add('Zylok', 'secondary'), 0.5)
        add('Braton', 'primary')                              # no drop row: unknown
        kronen = add('Kronen', 'melee')
        drop(kronen, source_type='relic', location='Axi K1 Relic', notes='Blade',
             drop_chance=10)                                  # its only relic drops nowhere
        relic_drop('Lith Z1 Relic')                           # drop data is synced
        rows = listed(admin_id)
    assert names(rows) == ['Zylok', 'Aklex', 'Kronen', 'Braton']
    assert [row['farm_hours'] for row in rows] == [0.5, 3.0, None, None]
    assert [row['farm_time'] for row in rows] == ['~0.5h', '~3.0h', '—', '—']


def test_equal_times_fall_back_to_category_then_name(app, admin_id):
    with app.app_context():
        for name, category in [('Burston', 'primary'), ('Ack', 'secondary'),
                               ('Atlas', 'warframe'), ('Boltor', 'primary'),
                               ('Afuris', 'secondary')]:
            hours(add(name, category), 2)
        hours(add('Zylok', 'secondary'), 1)
        rows = listed(admin_id)
    assert names(rows) == ['Zylok', 'Boltor', 'Burston', 'Ack', 'Afuris', 'Atlas']


def test_an_item_without_any_drop_row_is_unknown_not_zero_hours(app, admin_id):
    with app.app_context():
        add('Aaa Empty', 'primary')
        hours(add('Zzz Slow', 'primary'), 400)
        rows = listed(admin_id)
    assert names(rows) == ['Zzz Slow', 'Aaa Empty']
    assert rows[1]['farm_hours'] is None


def test_a_partless_item_ranks_by_its_own_best_source(app, admin_id):
    with app.app_context():
        own = add('Own Source', 'primary')
        drop(own, avg_runs=48)                       # 4 h
        drop(own, avg_runs=18)                       # 1.5 h: the best
        parted = add('Parted', 'primary')
        drop(parted, avg_runs=12, source_type='component', notes='Barrel')   # 1 h
        drop(parted, avg_runs=12, source_type='component', notes='Stock')    # 1 h
        rows = listed(admin_id)
    assert names(rows) == ['Own Source', 'Parted']
    assert [row['farm_hours'] for row in rows] == [1.5, 2.0]


@pytest.mark.parametrize('flags', [(1, 0), (0, 1), (1, 1)],
                         ids=['owned', 'mastered', 'owned and mastered'])
def test_owned_or_mastered_items_never_appear_however_quick(app, admin_id, flags):
    with app.app_context():
        quick = add('Quick', 'primary')
        hours(quick, 0.1)
        hours(add('Slow', 'primary'), 9)
        progress(admin_id, quick, *flags)
        rows = listed(admin_id)
    assert names(rows) == ['Slow']


def test_another_users_progress_does_not_hide_an_item(app, admin_id):
    from models.user import User
    with app.app_context():
        quick = add('Quick', 'primary')
        hours(quick, 0.1)
        progress(User.create('alice', password='pw-alice-1').id, quick, 1, 1)
        rows = listed(admin_id)
    assert names(rows) == ['Quick']


# ---------------------------------------------------------------------------
# Axis 3: sizes, and the batched read chunked under the parameter limits
# ---------------------------------------------------------------------------

def catalogue(count, prefix='Item'):
    """`count` items whose hours fall as their names rise: alphabetical and time order are
    reversed. Item n (from 0) takes count - n hours."""
    db = get_db()
    db.executemany('INSERT INTO items (name, category) VALUES (?, ?)',
                   [(f'{prefix} {n:05d}', 'primary') for n in range(count)])
    ids = [r[0] for r in db.execute('SELECT id FROM items WHERE name LIKE ? ORDER BY name',
                                    (f'{prefix} %',))]
    db.executemany('INSERT INTO drop_sources (item_id, location, avg_runs) VALUES (?, ?, ?)',
                   [(item, 'Earth/Mantle', (count - n) * RUNS_PER_HOUR)
                    for n, item in enumerate(ids)])
    db.commit()
    return ids


@pytest.mark.parametrize('count', [0, 1, 9, 10, 11, 25])
def test_the_list_holds_the_ten_quickest_of_any_number_of_candidates(app, admin_id, count):
    with app.app_context():
        catalogue(count)
        rows = listed(admin_id)
    want = [f'Item {n:05d}' for n in reversed(range(count))][:FARM_NEXT_ROWS]
    assert names(rows) == want
    assert [row['farm_hours'] for row in rows] == [float(count - n) for n in
                                                   reversed(range(count))][:FARM_NEXT_ROWS]


def test_an_empty_catalogue_lists_nothing_and_shows_the_empty_message(app, admin_client):
    card = farm_card(admin_client)
    assert '<table' not in card
    assert 'All items mastered or no items in database.' in card


def test_many_hundreds_of_items_are_read_in_chunks_under_the_parameter_limit(app, admin_id):
    count = 2 * buy_vs_farm.SOURCES_CHUNK + 1
    with app.app_context():
        ids = catalogue(count)
        statements = []
        get_db().set_trace_callback(statements.append)
        try:
            rows = listed(admin_id)
            many = item_sources_many(ids)
        finally:
            get_db().set_trace_callback(None)
        one_by_one = {item: item_sources(item) for item in ids}
    assert names(rows) == [f'Item {n:05d}' for n in range(count - 1, count - 11, -1)]
    assert many == one_by_one
    reads = [s for s in statements if 'FROM drop_sources' in s]
    assert len(reads) == 2 * 3                      # 1001 ids: 3 chunks, list and direct read
    assert max(s.count(',') for s in reads) < 999   # under SQLite's default parameter limit
    assert buy_vs_farm.SOURCES_CHUNK < 999 and buy_vs_farm.SOURCES_CHUNK < 2100


# ---------------------------------------------------------------------------
# The batched read agrees with the one-item read, and stays bounded
# ---------------------------------------------------------------------------

def mixed_catalogue(prefix='', relic_drops=True):
    """Parts, relics (one dropping somewhere, one nowhere), a part-less item, a part known
    only from its recipe, a component row whose location is a relic. Returns the ids."""
    ids = []
    parted = add(f'{prefix}Parted', 'primary')
    drop(parted, avg_runs=24, source_type='component', notes='Barrel')
    drop(parted, avg_runs=36, source_type='component', notes='barrel ')   # same part, worse
    drop(parted, source_type='relic', location='Lith B1 Relic', notes='Stock', drop_chance=25)
    ids.append(parted)
    vaulted = add(f'{prefix}Vaulted', 'melee')
    drop(vaulted, source_type='relic', location='Axi V1 Relic', notes='Blade', drop_chance=10)
    drop(vaulted, avg_runs=6, source_type='component', notes='Handle')
    ids.append(vaulted)
    both = add(f'{prefix}Both', 'melee')
    drop(both, source_type='relic', location='Axi V1 Relic', notes='Blade', drop_chance=10)
    drop(both, source_type='component', location='lith b1 relic', notes='Blade',
         drop_chance=50)
    ids.append(both)
    own = add(f'{prefix}Own', 'secondary')
    drop(own, avg_runs=30, mission_type='Capture')
    ids.append(own)
    recipe = add(f'{prefix}Recipe', 'secondary')
    drop(recipe, avg_runs=6, source_type='component', notes='Receiver')
    ingredient(recipe, 'Link')
    ids.append(recipe)
    ids.append(add(f'{prefix}Bare', 'warframe'))
    if relic_drops:
        relic_drop('Lith B1 Relic')
    return ids


@pytest.mark.parametrize('relic_drops', [True, False], ids=['drops synced', 'relic_drops empty'])
def test_each_listed_items_hours_equal_the_one_item_estimate(app, admin_id, relic_drops):
    with app.app_context():
        ids = mixed_catalogue(relic_drops=relic_drops)
        rows = listed(admin_id)
        assert item_sources_many(ids) == {item: item_sources(item) for item in ids}
        want = {item: expected_hours(item_sources(item)) for item in ids}
    assert sorted(row['id'] for row in rows) == sorted(ids)
    for row in rows:
        assert row['farm_hours'] == want[row['id']]
        assert row['farm_time'] == hours_label(want[row['id']])
    known = [row['farm_hours'] for row in rows if row['farm_hours'] is not None]
    assert known == sorted(known)
    assert all(row['farm_hours'] is None for row in rows[len(known):])
    if relic_drops:
        assert {row['name'] for row in rows[len(known):]} == {'Vaulted', 'Recipe', 'Bare'}
    else:
        assert {row['name'] for row in rows[len(known):]} == {'Recipe', 'Bare'}


def test_the_listed_items_buy_answers_are_the_ones_one_item_gives(app, admin_client, admin_id,
                                                                    requests_made):
    from test_dashboard_farm_next import cache, ago, STALE_AGE, set_setting
    with app.app_context():
        ids = mixed_catalogue()
        for n, item in enumerate(ids):
            get_db().execute('UPDATE items SET market_url_name = ? WHERE id = ?',
                             (f'slug_{n}', item))
            cache(item, 20 + n, ago(STALE_AGE))
        set_setting(admin_id, 'plat_per_hour', '10')
        rows = farm_next_items(admin_id, None, 10, FARM_NEXT_ROWS, 6, None)
    for row in rows:
        status, body = answer_of(admin_client, row['id'])
        assert (status, body['recommend_buy'], body['recommend_reason']) == \
            (200, row['recommend_buy'], row['recommend_reason'])
    assert requests_made == []


def statements_of_a_load(user_id, count):
    statements = []
    get_db().set_trace_callback(statements.append)
    try:
        rows = listed(user_id)
    finally:
        get_db().set_trace_callback(None)
    assert len(rows) == min(count, FARM_NEXT_ROWS)
    return statements


def test_loading_the_list_issues_as_many_queries_for_sixty_items_as_for_six(app, admin_id):
    counts = {}
    with app.app_context():
        for size in (6, 60):
            for n in range(size // 6):
                mixed_catalogue(prefix=f'{size}-{n:03d} ', relic_drops=not counts and n == 0)
            statements = statements_of_a_load(admin_id, size)
            counts[size] = len(statements)
            assert not any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                           for s in statements)
    assert counts[6] == counts[60]


# ---------------------------------------------------------------------------
# Axis 5: corrupt drop rows make a time unknown and never raise
# ---------------------------------------------------------------------------

# (case id, drop row fields of an item's only (own) source)
CORRUPT_ROWS = [
    ('runs NULL', {'avg_runs': None}),
    ('runs 0', {'avg_runs': 0}),
    ('runs negative', {'avg_runs': -5}),
    ('runs NaN', {'avg_runs': float('nan')}),
    ('runs infinite', {'avg_runs': float('inf')}),
    ('runs overflow to infinity', {'avg_runs': 1e308}),
    ('runs text', {'avg_runs': 'many'}),
    ('relic chance NULL', {'source_type': 'relic', 'location': 'Lith C1 Relic',
                           'notes': 'Blade', 'drop_chance': None}),
    ('relic chance 0', {'source_type': 'relic', 'location': 'Lith C1 Relic',
                        'notes': 'Blade', 'drop_chance': 0}),
    ('relic chance negative', {'source_type': 'relic', 'location': 'Lith C1 Relic',
                               'notes': 'Blade', 'drop_chance': -1}),
    ('relic chance tiny: overflow', {'source_type': 'relic', 'location': 'Lith C1 Relic',
                                     'notes': 'Blade', 'drop_chance': 1e-320}),
    ('part notes NULL', {'avg_runs': None, 'source_type': 'component', 'notes': None}),
    ('location NULL', {'avg_runs': None, 'location': None}),
]


@pytest.mark.parametrize('case', CORRUPT_ROWS, ids=lambda c: c[0])
def test_a_corrupt_drop_row_makes_the_time_unknown_without_raising(app, admin_id, case):
    _, fields = case
    with app.app_context():
        hours(add('Zzz Known', 'primary'), 2)
        corrupt = add('Aaa Corrupt', 'primary')
        drop(corrupt, **fields)
        relic_drop('Lith C1 Relic')
        rows = listed(admin_id)
        want = expected_hours(item_sources(corrupt))
    assert want is None
    assert names(rows) == ['Zzz Known', 'Aaa Corrupt']
    assert (rows[1]['farm_hours'], rows[1]['farm_time']) == (None, '—')


def test_a_corrupt_row_beside_a_good_one_leaves_the_good_one_as_the_part_source(app, admin_id):
    with app.app_context():
        item = add('Mixed', 'primary')
        drop(item, avg_runs=1e308, source_type='component', notes='Barrel')
        drop(item, avg_runs=float('nan'), source_type='component', notes='Barrel')
        drop(item, avg_runs=24, source_type='component', notes='Barrel')
        rows = listed(admin_id)
    assert rows[0]['farm_hours'] == 2.0


def test_parts_whose_sum_overflows_make_the_time_unknown():
    big = {'parts': {'a': 1.7e308, 'b': 1.7e308}, 'own': None, 'vaulted': {}}
    assert expected_hours(big) is None
    assert source_minutes({'source_type': None, 'location': 'Earth/Mantle',
                           'mission_type': None, 'avg_runs': 1e308,
                           'drop_chance': None}) is None


@pytest.mark.parametrize('value, label', [(None, '—'), (2, '~2.0h'), (0.04, '~0.0h'),
                                          (1234.56, '~1234.6h')])
def test_the_hours_label_writes_hours_as_the_rules_reasons_do(value, label):
    assert hours_label(value) == label
    if value is not None:
        assert buy_vs_farm.recommend(None, value)['reason'] == f'{label} farming'


# ---------------------------------------------------------------------------
# Axis 9: the served list
# ---------------------------------------------------------------------------

def card_rows(card):
    """(name, farming time cell) of each Farm next row, in page order."""
    found = []
    for row in re.findall(r'<tr class="farm-next-row"[^>]*>(.*?)</tr>', card, re.S):
        cells = [' '.join(re.sub(r'<[^>]+>', ' ', c).split())
                 for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        found.append((cells[0], cells[2]))
    return found


def test_the_served_list_shows_the_farming_time_column_in_ranked_order(app, admin_client):
    with app.app_context():
        hours(add('Aklex', 'secondary'), 3)
        hours(add('Zylok', 'secondary'), 0.5)
        add('Braton', 'primary', slug='braton_set')
    card = farm_card(admin_client)
    header = re.search(r'<thead>(.*?)</thead>', card, re.S).group(1)
    assert re.findall(r'<th[^>]*>(.*?)</th>', header) == \
        ['Name', 'Category', 'Farming time', 'Market']
    assert card_rows(card) == [('Zylok', '~0.5h'), ('Aklex', '~3.0h'), ('Braton', '—')]
    assert 'class="farm-next-hours" style="white-space:nowrap"' in card


def test_the_estimate_is_finite_or_a_dash_for_every_listed_row(app, admin_id):
    with app.app_context():
        mixed_catalogue()
        rows = listed(admin_id)
    for row in rows:
        assert row['farm_hours'] is None or math.isfinite(row['farm_hours'])
        assert re.fullmatch(r'~\d+\.\dh|—', row['farm_time'])
