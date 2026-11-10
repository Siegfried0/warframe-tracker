"""Buy or farm for a part only vaulted relics hold: no mission drops it, so buying is the
only way.

Oracles: the rule as a table (the part vaulted / farmable / held by a vaulted relic and a
mission / without a source) x (relic drop data empty / synced) x (price under the threshold /
over / none) x (settings all unset / set), through Farm next and the rule itself, with the
check that fired and the reason text; relic names compared apart from case and blanks; the
order of several vaulted parts; the goal plan with a vaulted part crafted, held as a
blueprint or still needed; the reason escaped on the dashboard. Network refused
(tests/conftest.py); a market request fails the test.
"""
import itertools
from datetime import datetime

import pytest

from models.db import get_db
from models.item import Item
from models.user import User
from services import market_api
from services.buy_vs_farm import (expected_hours, item_sources, recommend, vaulted_parts)
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


def add_item(name='Braton Prime', slug='braton_prime_set'):
    Item.upsert(name, 'primary', source='standard', tradable=True)
    db = get_db()
    db.execute("UPDATE items SET market_url_name = ? WHERE name = ? AND category = 'primary'",
               (slug, name))
    db.commit()
    return db.execute("SELECT id FROM items WHERE name = ? AND category = 'primary'",
                      (name,)).fetchone()[0]


def drop(item_id, mission_type, location, chance, runs, source_type, part):
    get_db().execute(
        '''INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs,
                                     source_type, notes) VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (item_id, mission_type, location, chance, runs, source_type, part))
    get_db().commit()


def relic_drop(relic):
    """A place where ``relic`` drops: it is not vaulted."""
    get_db().execute(
        '''INSERT INTO relic_drops (relic_name, location, mission_type, rotation, drop_chance,
                                    avg_runs) VALUES (?, 'Hepit (Capture)', 'Capture', NULL,
                                    10, 10)''', (relic,))
    get_db().commit()


def recipe(item_id, part):
    get_db().execute('INSERT INTO component_ingredients (item_id, component_name, '
                     "ingredient_name, ingredient_qty) VALUES (?, ?, 'Morphics', 1)",
                     (item_id, part))
    get_db().commit()


def cache(item_id, price):
    get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                     'VALUES (?, ?, ?)', (item_id, price, datetime.utcnow().isoformat()))
    get_db().commit()


def store_setting(user_id, key, value):
    get_db().execute('INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)',
                     (user_id, key, value))
    get_db().commit()


# Relic rows as the drop sync writes them: Lith A1 drops nowhere (no relic_drops row), Meso B2
# drops somewhere (its relic_drops row spelt otherwise). Each is 100 / 10 % = 10 openings
# x 5 min = 50 min when counted; the mission row is 10 Capture runs x 3 min = 30 min.
VAULTED_RELIC = ('Relic', 'Lith A1 Relic', 10, None, 'relic', 'Barrel')
DROPPING_RELIC = ('Relic', 'Meso B2 Relic', 10, None, 'relic', 'Barrel')
MISSION = ('Braton Prime Barrel', 'Ceres/Draco (Capture)', 10, 10, 'component', 'Barrel')

# part case: (drop rows, recipe parts, minutes with relic drops synced, minutes without)
PARTS = {
    'part vaulted':        ([VAULTED_RELIC], [], 'vaulted', 50),
    'part farmable':       ([DROPPING_RELIC], [], 50, 50),
    'vaulted and mission': ([VAULTED_RELIC, MISSION], [], 30, 30),
    'part without source': ([], ['Barrel'], None, None),
}
SYNCED = {'relic drops empty': False, 'relic drops synced': True}
PRICES = {'price under the threshold': 15, 'price over': 300, 'no price': None}
SETTINGS = {
    'settings unset': {'threshold': None, 'cap': None, 'rate': None},
    'settings set':   {'threshold': 20, 'cap': 1, 'rate': 1000},
}
VAULTED_REASON = 'Barrel vaulted: no mission drops it'


def expected(part, synced, price, settings):
    """(check, reason, expected hours): threshold, then vaulted, then the time."""
    _, _, minutes_synced, minutes_empty = PARTS[part]
    minutes = minutes_synced if SYNCED[synced] else minutes_empty
    price, s = PRICES[price], SETTINGS[settings]
    if price is not None and s['threshold'] is not None and price <= s['threshold']:
        hours = None if minutes in (None, 'vaulted') else minutes / 60
        return 'threshold', '15p ≤ threshold 20p', hours
    if minutes == 'vaulted':
        return 'vaulted', VAULTED_REASON, None
    if minutes is None:
        return None, 'farming time unknown', None
    hours = minutes / 60                   # under the 1 h cap
    if price is not None and s['rate']:    # under 1 h x 1000p/h is over 300p
        return 'time_value', f'~{hours:.1f}h farming × 1000p/h > market {price}p', hours
    return None, f'~{hours:.1f}h farming', hours


RULE_ROWS = list(itertools.product(PARTS, SYNCED, PRICES, SETTINGS))


@pytest.mark.parametrize('part, synced, price, settings', RULE_ROWS,
                         ids=[' / '.join(row) for row in RULE_ROWS])
def test_a_vaulted_part_recommends_buying_after_the_threshold(app, part, synced, price,
                                                              settings):
    drops, recipes, _, _ = PARTS[part]
    s = SETTINGS[settings]
    check, reason, hours = expected(part, synced, price, settings)
    with app.app_context():
        item = add_item()
        for d in drops:
            drop(item, *d)
        for r in recipes:
            recipe(item, r)
        if SYNCED[synced]:
            relic_drop('meso  b2 RELIC')
        if PRICES[price] is not None:
            cache(item, PRICES[price])
        sources = item_sources(item)
        answer = recommend(PRICES[price], expected_hours(sources), s['threshold'], s['cap'],
                           s['rate'], vaulted=vaulted_parts(sources))
        row = dict(get_db().execute('SELECT * FROM items WHERE id = ?', (item,)).fetchone())
        farm_next = enrich_with_cached_market([row], s['threshold'], s['rate'], 6,
                                              s['cap'])[0]
    assert (answer['check'], answer['reason']) == (check, reason)
    assert answer['recommend'] is (check is not None)
    assert answer['expected_hours'] == pytest.approx(hours)
    assert (farm_next['recommend_buy'], farm_next['recommend_reason']) == \
        (check is not None, reason)
    assert farm_next['expected_hours'] == pytest.approx(hours)


# ── Names and order ──────────────────────────────────────────────────────────

# (case id, drop rows, relic_drops names, vaulted part names or [] when none)
NAME_ROWS = [
    ('the relic named in other case and blanks still drops',
     [('Relic', '  LITH  a1 relic ', 10, None, 'relic', 'Barrel')], ['Lith A1 Relic'], []),
    ('a part row naming a vaulted relic as its location is vaulted',
     [('Braton Prime Barrel', 'Lith A1 Relic', 10, 99, 'component', 'Barrel')],
     ['Meso B2 Relic'], ['Barrel']),
    ('two vaulted relics of one part: vaulted once',
     [VAULTED_RELIC, ('Relic', 'Neo C3 Relic', 20, None, 'relic', ' barrel ')],
     ['Meso B2 Relic'], ['Barrel']),
    ('a vaulted and a dropping relic: farmable',
     [VAULTED_RELIC, DROPPING_RELIC], ['Meso B2 Relic'], []),
    ('several vaulted parts, by their names apart from case',
     [('Relic', 'Lith A1 Relic', 10, None, 'relic', 'stock'),
      ('Relic', 'Lith A1 Relic', 10, None, 'relic', 'Receiver'),
      ('Relic', 'Lith A1 Relic', 10, None, 'relic', '  Barrel  ')],
     ['Meso B2 Relic'], ['Barrel', 'Receiver', 'stock']),
    ('a relic row without a relic name counts its chance as before',
     [('Relic', '', 10, None, 'relic', 'Barrel')], ['Meso B2 Relic'], []),
]


@pytest.mark.parametrize('row', NAME_ROWS, ids=lambda r: r[0])
def test_vaulted_parts_compare_relic_names_apart_from_case_and_blanks(app, row):
    _, drops, synced, parts = row
    with app.app_context():
        item = add_item()
        for d in drops:
            drop(item, *d)
        for relic in synced:
            relic_drop(relic)
        sources = item_sources(item)
    assert vaulted_parts(sources) == parts
    answer = recommend(None, expected_hours(sources), vaulted=vaulted_parts(sources))
    if parts:
        assert (answer['check'], answer['reason']) == \
            ('vaulted', f'{", ".join(parts)} vaulted: no mission drops it')
    else:
        assert answer['check'] is None


# ── The goal plan ────────────────────────────────────────────────────────────

def vaulted_goal(user_id):
    """Braton Prime: Barrel held only by a vaulted relic, Stock 40 min (Exterminate)."""
    item = add_item()
    drop(item, *VAULTED_RELIC)
    drop(item, 'Braton Prime Stock', 'Earth/E Prime (Exterminate)', 10, 10, 'component',
         'Stock')
    relic_drop('Meso B2 Relic')
    cache(item, 300)
    return add_farming_goal(user_id, item)['goal_id']


# (case id, Barrel's state, recommend, reason, hours)
GOAL_ROWS = [
    ('the vaulted part still needed', None, True, VAULTED_REASON, None),
    ('set back to pending', 'pending', True, VAULTED_REASON, None),
    ('the vaulted part crafted', 'crafted', False, '~0.7h farming', 40 / 60),
    ('the vaulted part held as a blueprint', 'blueprint', False, '~0.7h farming', 40 / 60),
]


@pytest.mark.parametrize('row', GOAL_ROWS, ids=lambda r: r[0])
def test_the_goal_plan_recommends_buying_only_while_a_vaulted_part_is_needed(
        app, admin_client, admin_id, row):
    _, state, buy, reason, hours = row
    with app.app_context():
        goal = vaulted_goal(admin_id)
        if state:
            set_goal_progress(admin_id, goal, 'component', 'Barrel', comp_state=state)
    for _ in range(2):                                   # opened twice: the same answer
        response = admin_client.get(f'/farming/goals/{goal}/plan')
        assert response.status_code == 200
        plan = response.get_json()['plan']
        assert (plan['recommend_buy'], plan['recommend_reason']) == (buy, reason)
        assert plan['expected_hours'] == pytest.approx(hours)


def test_the_threshold_wins_over_a_vaulted_part_in_the_goal_plan(app, admin_id):
    with app.app_context():
        goal = vaulted_goal(admin_id)
        store_setting(admin_id, 'buy_threshold_plat', '300')
        plan = get_goal_plan_with_progress(goal, admin_id)
    assert (plan['recommend_buy'], plan['recommend_reason']) == \
        (True, '300p ≤ threshold 300p')


# ── The interface ────────────────────────────────────────────────────────────

def test_the_dashboard_shows_a_vaulted_part_name_escaped(app, admin_client):
    with app.app_context():
        item = add_item()
        drop(item, 'Relic', 'Lith A1 Relic', 10, None, 'relic', '<b>"Barrel"</b>')
        relic_drop('Meso B2 Relic')
    html = admin_client.get('/dashboard').get_data(as_text=True)
    assert '<b>"Barrel"' not in html
    assert ('title="&lt;b&gt;&#34;Barrel&#34;&lt;/b&gt; vaulted: no mission drops it">Buy'
            in html)
