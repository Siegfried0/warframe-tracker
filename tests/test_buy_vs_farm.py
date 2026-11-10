"""Buy or farm: one rule for the dashboard's Farm next list and the Farming Planner.

Oracles: the rule as a table (price none / at or below / above the threshold) x (hour cap
unset / not reached / passed) x (rate unset / time x rate below / above the price) x (time
known / unknown), with which check fired and the reason text; the farming time as a table
(mission types in and out of the minutes table, relic sources, parts without a source, a
goal's crafted, blueprint and pending parts); the goal plan and Farm next answering from
both. Network refused (tests/conftest.py); a market request fails the test.
"""
import itertools
from datetime import datetime, timedelta

import pytest

from models.db import get_db
from models.item import Item
from models.user import User
from services import market_api
from services.auth_service import SETTING_VALUE_MAX
from services.buy_vs_farm import expected_hours, item_sources, minutes_per_run, recommend
from services.farming_service import (add_farming_goal, enrich_with_cached_market,
                                      get_goal_plan_with_progress, set_goal_progress)


@pytest.fixture(autouse=True)
def no_market_request(monkeypatch):
    def fake_get(url, *args, **kwargs):
        raise AssertionError(f'unexpected market request: {url}')
    monkeypatch.setattr(market_api.requests, 'get', fake_get)


@pytest.fixture
def admin_id(app, admin_client):
    with app.app_context():
        return User.get_by_username('admin').id


# ── The rule ─────────────────────────────────────────────────────────────────

THRESHOLD = 20
PRICES = {'no price': None, 'at or below the threshold': 15, 'above the threshold': 300}
CAPS = {'cap unset': None, 'cap 0': 0, 'cap not reached': 50, 'cap passed': 5}
RATES = {'rate unset': None, 'rate 0': 0, 'time x rate below the price': 1,
         'time x rate above the price': 100}
HOURS = {'time known': 10.0, 'time unknown': None}

REASONS = {
    'threshold': '15p ≤ threshold 20p',
    'hour_cap': '~10.0h farming > hour cap 5h',
    'time_value': '~10.0h farming × 100p/h > market 300p',
    None: '~10.0h farming',
    'unknown': 'farming time unknown',
}


def expected_answer(price, cap, rate, hours):
    """The rule spelled out case by case: threshold, then hour cap, then time value."""
    if price is not None and price <= THRESHOLD:
        return 'threshold', REASONS['threshold']
    if hours is None:
        return None, REASONS['unknown']
    if cap == 5:                                   # 10 h is over 5 h, under 50 h
        return 'hour_cap', REASONS['hour_cap']
    if price is not None and rate == 100:          # 10 h x 100p/h = 1000p > 300p
        return 'time_value', REASONS['time_value']
    return None, REASONS[None]


RULE_ROWS = list(itertools.product(PRICES, CAPS, RATES, HOURS))


@pytest.mark.parametrize('price, cap, rate, hours', RULE_ROWS,
                         ids=[' / '.join(row) for row in RULE_ROWS])
def test_the_rule_fires_the_first_check_that_applies(price, cap, rate, hours):
    args = PRICES[price], CAPS[cap], RATES[rate], HOURS[hours]
    check, reason = expected_answer(*args)
    answer = recommend(args[0], args[3], buy_threshold_plat=THRESHOLD, farm_hour_cap=args[1],
                       plat_per_hour=args[2])
    assert (answer['check'], answer['reason']) == (check, reason)
    assert answer['recommend'] is (check is not None)
    assert answer['expected_hours'] == args[3]


# (case id, price, hours, threshold, cap, rate, check)
EDGE_ROWS = [
    ('price equal to the threshold buys', 20, None, 20, None, None, 'threshold'),
    ('threshold unset: no threshold check', 15, None, None, None, None, None),
    ('threshold 0 is a threshold: a 0p price buys', 0, None, 0, None, None, 'threshold'),
    ('threshold 0, 1p: farm', 1, None, 0, None, None, None),
    ('hours equal to the cap: farm', None, 5.0, None, 5, None, None),
    ('just over the cap: buy', None, 5.01, None, 5, None, 'hour_cap'),
    ('the cap fires without a price', None, 6.0, None, 5, 1000, 'hour_cap'),
    ('time x rate equal to the price: farm', 100, 2.0, None, None, 50, None),
    ('time value needs a price', None, 2.0, None, None, 50, None),
    ('0 hours (every part done): farm', 100, 0.0, None, 1, 1000, None),
    ('the largest cap', None, 1e9, None, SETTING_VALUE_MAX, None, None),
    ('the largest rate', 10 ** 15, 0.5, None, None, SETTING_VALUE_MAX, 'time_value'),
    ('threshold wins over the cap', 10, 99.0, 20, 5, None, 'threshold'),
    ('the cap wins over the time value', 300, 10.0, None, 5, 100, 'hour_cap'),
]


@pytest.mark.parametrize('row', EDGE_ROWS, ids=lambda r: r[0])
def test_the_rule_at_its_edges(row):
    _, price, hours, threshold, cap, rate, check = row
    assert recommend(price, hours, threshold, cap, rate)['check'] == check


# ── Minutes per run ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('mission_type, minutes', [
    ('Capture', 3), ('capture', 3), ('  CAPTURE ', 3), ('Exterminate', 4),
    ('Mobile  Defense', 6), ('Assassination', 6), ('Spy', 6), ('Survival', 5),
    ('Ash Prime Neuroptics Blueprint', 5), ('Unknown', 5), ('', 5), (None, 5), (7, 5),
])
def test_minutes_per_run_come_from_the_mission_type(mission_type, minutes):
    assert minutes_per_run(mission_type) == minutes


# A part's drop row, as the drop sync writes it, names the reward as its type and the game
# mode only in brackets in its location.
@pytest.mark.parametrize('mission_type, location, minutes', [
    ('Rhino Chassis Blueprint', 'Ceres/Draco (Capture)', 3),
    ('Rhino Chassis Blueprint', 'Venus/Fossa (Assassination)', 6),
    ('Rhino Chassis Blueprint', 'Mars/War ( spy )', 6),
    ('Rhino Chassis Blueprint', 'Earth/Mantle (Mobile  Defense), Rotation C', 6),
    ('Rhino Chassis Blueprint', 'Lua/Circulus (Survival) (Rotation C)', 5),
    ('Rhino Chassis Blueprint', 'Earth/E Prime (Exterminate) (Rotation A)', 4),
    ('Rhino Chassis Blueprint', 'Ceres/Draco', 5),
    ('Rhino Chassis Blueprint', 'Ceres/Draco (Unknown Mode)', 5),
    ('Rhino Chassis Blueprint', 'Ceres/Draco (Capture', 5),
    ('Rhino Chassis Blueprint', None, 5),
    ('Rhino Chassis Blueprint', 7, 5),
    ('Spy', 'Ceres/Draco (Capture)', 6),
])
def test_minutes_per_run_fall_back_to_the_game_mode_in_the_location(mission_type, location,
                                                                    minutes):
    assert minutes_per_run(mission_type, location) == minutes


# ── The farming time of an item ──────────────────────────────────────────────

def add_item(name, slug='braton_prime_set'):
    Item.upsert(name, 'primary', source='standard', tradable=True)
    db = get_db()
    db.execute("UPDATE items SET market_url_name = ? WHERE name = ? AND category = 'primary'",
               (slug, name))
    db.commit()
    return db.execute("SELECT id FROM items WHERE name = ? AND category = 'primary'",
                      (name,)).fetchone()[0]


def drop(item_id, mission_type, location, chance, runs, source_type, part=None):
    get_db().execute(
        '''INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs,
                                     source_type, notes) VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (item_id, mission_type, location, chance, runs, source_type, part))
    get_db().commit()


def recipe(item_id, part, ingredient='Morphics'):
    get_db().execute('INSERT INTO component_ingredients (item_id, component_name, '
                     'ingredient_name, ingredient_qty) VALUES (?, ?, ?, 1)',
                     (item_id, part, ingredient))
    get_db().commit()


# Part rows as the drop sync writes them: the reward name as the type, the game mode in
# brackets in the location. Barrel: 10 runs x 3 min; Stock: 10 runs x 4 min.
BARREL = ('Braton Prime Barrel', 'Ceres/Draco (Capture)', 10, 10, 'component', 'Barrel')
STOCK = ('Braton Prime Stock', 'Earth/E Prime (Exterminate)', 10, 10, 'component', 'Stock')

# (case id, drop rows (mission type, location, chance, runs, source type, part), recipe
#  parts, needed parts or None for all, expected minutes or None for unknown)
TIME_ROWS = [
    ('own mission source, type in the table', [('Capture', 'Ceres/Draco', 5, 20, 'mission', None)],
     [], None, 60),
    ('own source, type not in the table: 5 min',
     [('Unknown', 'Earth/Mantle', 5, 12, None, None)], [], None, 60),
    ('own source, best of two by minutes, not by runs',
     [('Spy', 'Mars/War', 10, 10, 'mission', None),
      ('Capture', 'Mars/Ara', 7, 15, 'mission', None)], [], None, 45),
    ('no source at all', [], [], None, None),
    ('own source without a run count', [('Capture', 'Ceres/Draco', None, None, 'mission', None)],
     [], None, None),
    ('relic row: 100 / chance openings x 5 min',
     [('Relic', 'Lith A1 Relic', 25, None, 'relic', 'Barrel')], [], None, 20),
    ('component row naming a relic: openings x 5 min, not runs x mission minutes',
     [('Braton Prime Barrel', 'Lith A1 Relic', 10, 99, 'component', 'Barrel')], [], None, 50),
    ('parts add up, each its best source (the game mode in the location, as synced)',
     [BARREL,
      ('Relic', 'Meso B2 Relic', 10, None, 'relic', 'Barrel'),
      STOCK], [], None, 30 + 40),
    ('a part row without a game mode in its location: 5 min',
     [('Braton Prime Barrel', 'Ceres/Draco', 10, 10, 'component', 'Barrel')], [], None, 50),
    ('a part with only a recipe makes the time unknown',
     [BARREL], ['Receiver'], None, None),
    ('the part without a source is not needed',
     [BARREL], ['Receiver'], ['Barrel'], 30),
    ('needed parts in another spelling',
     [BARREL], [], ['  barrel '], 30),
    ('no part needed: 0 hours',
     [BARREL], [], [], 0),
    ('parts are counted, not the own sources beside them',
     [BARREL,
      ('Exterminate', 'Earth/E Prime', 50, 2, 'mission', None)], [], None, 30),
    ('a hand-edited zero or negative chance is no source',
     [('Relic', 'Lith A1 Relic', 0, None, 'relic', 'Barrel'),
      ('Braton Prime Stock', 'Ceres/Draco (Capture)', 10, -3, 'component', 'Stock')],
     [], None, None),
]


@pytest.mark.parametrize('row', TIME_ROWS, ids=lambda r: r[0])
def test_the_farming_time_sums_the_best_source_of_each_needed_part(app, row):
    _, drops, recipes, needed, minutes = row
    with app.app_context():
        item = add_item('Braton Prime')
        for d in drops:
            drop(item, *d)
        for part in recipes:
            recipe(item, part)
        hours = expected_hours(item_sources(item), needed)
    if minutes is None:
        assert hours is None
    else:
        assert hours == pytest.approx(minutes / 60)


# ── The goal plan ────────────────────────────────────────────────────────────

def cache(item_id, price):
    get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                     'VALUES (?, ?, ?)', (item_id, price, datetime.utcnow().isoformat()))
    get_db().commit()


def store_setting(user_id, key, value):
    get_db().execute('INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)',
                     (user_id, key, value))
    get_db().commit()


def goal_with_three_parts(user_id, price=100):
    """Braton Prime: Barrel 30 min (Capture), Stock 40 min (Exterminate), Receiver 50 min
    (a relic at 10 %); a fresh cached price."""
    item = add_item('Braton Prime')
    drop(item, *BARREL)
    drop(item, *STOCK)
    drop(item, 'Relic', 'Lith A1 Relic', 10, None, 'relic', 'Receiver')
    cache(item, price)
    return item, add_farming_goal(user_id, item)['goal_id']


# (case id, part states, expected minutes)
GOAL_ROWS = [
    ('every part pending', {}, 30 + 40 + 50),
    ('a crafted part is not needed', {'Barrel': 'crafted'}, 40 + 50),
    ('a part held as a blueprint is not needed', {'Stock': 'blueprint'}, 30 + 50),
    ('a part set back to pending is needed', {'Receiver': 'pending'}, 30 + 40 + 50),
    ('every part done', {'Barrel': 'crafted', 'Stock': 'blueprint', 'Receiver': 'crafted'}, 0),
]


@pytest.mark.parametrize('row', GOAL_ROWS, ids=lambda r: r[0])
def test_the_goal_plan_counts_only_the_parts_still_needed(app, admin_id, row):
    _, states, minutes = row
    with app.app_context():
        _, goal = goal_with_three_parts(admin_id)
        for part, state in states.items():
            set_goal_progress(admin_id, goal, 'component', part, comp_state=state)
        plan = get_goal_plan_with_progress(goal, admin_id)
    assert plan['expected_hours'] == pytest.approx(minutes / 60)


# (case id, settings, price, recommend, reason)
GOAL_ANSWER_ROWS = [
    ('no setting: farm, with the time', {}, 100, False, '~2.0h farming'),
    ('the threshold', {'buy_threshold_plat': '100'}, 100, True, '100p ≤ threshold 100p'),
    ('the hour cap', {'farm_hour_cap': '1'}, 100, True, '~2.0h farming > hour cap 1h'),
    ('the time value', {'plat_per_hour': '60'}, 100, True,
     '~2.0h farming × 60p/h > market 100p'),
    ('the time value not reached', {'plat_per_hour': '50'}, 100, False, '~2.0h farming'),
    ('no cached price: only the cap can fire', {'farm_hour_cap': '1', 'plat_per_hour': '999'},
     None, True, '~2.0h farming > hour cap 1h'),
]


@pytest.mark.parametrize('row', GOAL_ANSWER_ROWS, ids=lambda r: r[0])
def test_the_goal_plan_answers_buy_or_farm_from_the_users_settings(app, admin_client,
                                                                   admin_id, row):
    _, settings, price, buy, reason = row
    with app.app_context():
        _, goal = goal_with_three_parts(admin_id, price=price)
        for key, value in settings.items():
            store_setting(admin_id, key, value)
    for _ in range(2):                                   # opened twice: the same answer
        response = admin_client.get(f'/farming/goals/{goal}/plan')
        assert response.status_code == 200
        plan = response.get_json()['plan']
        assert (plan['recommend_buy'], plan['recommend_reason']) == (buy, reason)
        assert plan['expected_hours'] == pytest.approx(2.0)


def test_a_goal_not_on_the_market_has_no_answer(app, admin_id):
    with app.app_context():
        item = add_item('Braton Prime', slug=None)
        drop(item, *BARREL)
        store_setting(admin_id, 'farm_hour_cap', '0')
        plan = get_goal_plan_with_progress(add_farming_goal(admin_id, item)['goal_id'],
                                           admin_id)
    assert plan['market'] is None
    assert (plan['recommend_buy'], plan['recommend_reason'], plan['expected_hours']) == \
        (False, None, None)


def test_a_goal_with_a_part_without_source_says_the_time_is_unknown(app, admin_id):
    with app.app_context():
        _, goal = goal_with_three_parts(admin_id)
        recipe(get_db().execute("SELECT id FROM items WHERE name = 'Braton Prime'")
               .fetchone()[0], 'Blueprint')
        store_setting(admin_id, 'farm_hour_cap', '1')
        plan = get_goal_plan_with_progress(goal, admin_id)
    assert (plan['recommend_buy'], plan['recommend_reason'], plan['expected_hours']) == \
        (False, 'farming time unknown', None)


# ── Farm next ────────────────────────────────────────────────────────────────

def farm_next(item_id, threshold=None, rate=None, cap=None):
    items = [dict(get_db().execute('SELECT * FROM items WHERE id = ?', (item_id,)).fetchone())]
    return enrich_with_cached_market(items, threshold, rate, 6, cap)[0]


def test_farm_next_uses_the_minutes_of_the_mission_type(app):
    """120 Capture runs are 6 h at 3 min (10 h at the old flat 5 min); at 20p/h that is
    120p: a 150p price is not worth buying."""
    with app.app_context():
        item = add_item('Braton Prime')
        drop(item, 'Capture', 'Ceres/Draco', 1, 120, 'mission')
        cache(item, 150)
        answer = farm_next(item, rate=20)
    assert (answer['recommend_buy'], answer['expected_hours']) == (False, 6.0)
    assert answer['recommend_reason'] == '~6.0h farming'


def test_farm_next_counts_every_part_of_the_item(app):
    with app.app_context():
        item = add_item('Braton Prime')
        drop(item, *BARREL)
        drop(item, *STOCK)
        cache(item, 150)
        answer = farm_next(item, cap=1)
    assert answer['expected_hours'] == pytest.approx(70 / 60)
    assert (answer['recommend_buy'], answer['recommend_reason']) == \
        (True, '~1.2h farming > hour cap 1h')


def test_farm_next_recommends_by_the_hour_cap_without_a_cached_price(app, admin_client,
                                                                     admin_id):
    with app.app_context():
        item = add_item('Braton Prime')
        drop(item, 'Capture', 'Ceres/Draco', 1, 100, 'mission')
        store_setting(admin_id, 'farm_hour_cap', '2')
    html = admin_client.get('/dashboard').get_data(as_text=True)
    assert 'title="~5.0h farming &gt; hour cap 2h">Buy' in html


def test_farm_next_keeps_the_simple_threshold(app):
    with app.app_context():
        item = add_item('Braton Prime')
        cache(item, 40)
        answer = farm_next(item, threshold=50)
    assert (answer['recommend_buy'], answer['recommend_reason']) == (True, '40p ≤ threshold 50p')
