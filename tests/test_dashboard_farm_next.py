"""The dashboard's "Farm next" list prices its items from the cache and requests no price.

Loading the dashboard never requests a market price, with or without pinned goals, whatever
the cache holds. Without pinned goals the list shows the unowned items with their cached
price of any age (a dash without one), an "old" mark with its fetch time when it is older
than the user's cache setting, and the buy recommendation computed from it; with
pinned goals the list is not built. The prices are read in one query and nothing is
written. The Farming Planner's goal plan keeps fetching the goal item's price on open, but
honours the user's market cache setting.

After a price refresh the page asks `GET /dashboard/farm-next/<id>` for one item's answer:
from the cache only, with the caller's settings, the same answer the load would draw; the page
redraws only that row's Buy mark and the price's colour from it.

Network refused (tests/conftest.py); the market request function is replaced by a recorder.
"""
import re
from datetime import datetime, timedelta

import pytest

import routes.dashboard
from models.db import get_db
from models.item import Item
from models.user import User
from services import market_api
from services.farming_service import (
    add_farming_goal,
    enrich_with_cached_market,
    farm_next_items,
    update_farming_goal,
)

FRESH_AGE = timedelta(hours=1)     # younger than the default 6 h
STALE_AGE = timedelta(hours=30)    # older than the default 6 h and than 24 h


def ago(age):
    return (datetime.utcnow() - age).isoformat()


def old_mark(fetched_at):
    return ' old (' + datetime.fromisoformat(fetched_at).strftime('%Y-%m-%d %H:%M') + ' UTC)'


@pytest.fixture
def requests_made(monkeypatch):
    """Every warframe.market request the code tries, recorded and answered with nothing."""
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        raise AssertionError(f'unexpected market request: {url}')

    monkeypatch.setattr(market_api.requests, 'get', fake_get)
    return calls


@pytest.fixture
def admin_id(app, admin_client):
    with app.app_context():
        return User.get_by_username('admin').id


def add_item(name, market_url_name='braton_prime_set', tradable=True):
    Item.upsert(name, 'primary', source='standard', tradable=tradable)
    db = get_db()
    db.execute("UPDATE items SET market_url_name = ? WHERE name = ? AND category = 'primary'",
               (market_url_name, name))
    db.commit()
    return db.execute("SELECT id FROM items WHERE name = ? AND category = 'primary'",
                      (name,)).fetchone()[0]


def cache(item_id, price, fetched_at):
    get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                     'VALUES (?, ?, ?)', (item_id, price, fetched_at))
    get_db().commit()


def set_setting(user_id, key, value):
    get_db().execute('INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)',
                     (user_id, key, value))
    get_db().commit()


def pin(user_id, item_id):
    goal = add_farming_goal(user_id, item_id)
    update_farming_goal(user_id, goal['goal_id'], pinned=1)
    return goal['goal_id']


def farm_card(client):
    response = client.get('/dashboard')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    start = html.index('Farm Next</span>')
    return html[start:html.index('Farming Planner</a>', start)]


def market_cell(card_html, name):
    """The text of the Market cell in the Farm next row of the item `name`."""
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', card_html, re.S):
        if name in row:
            last = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)[-1]
            return ' '.join(re.sub(r'<[^>]+>', ' ', last).split())
    raise AssertionError(f'no Farm next row for {name}')


def cache_rows():
    return [tuple(r) for r in get_db().execute(
        'SELECT item_id, platinum_price, fetched_at FROM market_cache ORDER BY item_id')]


# (case id, cached price, fetch age or None for no cache row, market cell without a threshold)
CACHE_STATES = [
    ('no row', None, None, '—'),
    ('fresh', 120, FRESH_AGE, '120p'),
    ('stale', 95, STALE_AGE, '95p'),
    ('price NULL', None, FRESH_AGE, '—'),
    ('price NULL stale', None, STALE_AGE, '—'),
]


@pytest.mark.parametrize('pinned', [False, True], ids=['no pinned goal', 'one pinned goal'])
@pytest.mark.parametrize('state', CACHE_STATES, ids=lambda s: s[0])
def test_loading_the_dashboard_requests_no_market_price_and_writes_no_cache_row(
        app, admin_client, admin_id, requests_made, state, pinned):
    _, price, age, want_cell = state
    fetched_at = ago(age) if age is not None else None
    if age == STALE_AGE:
        want_cell += old_mark(fetched_at)
    with app.app_context():
        item = add_item('Braton Prime')
        if age is not None:
            cache(item, price, fetched_at)
        if pinned:
            pin(admin_id, add_item('Soma Prime', 'soma_prime_set'))
        before = cache_rows()

    card = farm_card(admin_client)

    assert requests_made == []
    with app.app_context():
        assert cache_rows() == before
    if pinned:
        assert 'Pinned farming goals.' in card
        assert '<table' not in card
    else:
        assert market_cell(card, 'Braton Prime') == want_cell


# (case id, cached price, buy threshold or None, market cell)
THRESHOLD_CASES = [
    ('below threshold', 40, '50', 'Buy 40p'),
    ('at threshold', 50, '50', 'Buy 50p'),
    ('above threshold', 60, '50', '60p'),
    ('no threshold', 40, None, '40p'),
    ('no price', None, '50', '—'),
]


@pytest.mark.parametrize('case', THRESHOLD_CASES, ids=lambda c: c[0])
def test_the_buy_recommendation_comes_from_the_cached_price_and_the_threshold(
        app, admin_client, admin_id, requests_made, case):
    _, price, threshold, want_cell = case
    fetched_at = ago(STALE_AGE)
    with app.app_context():
        item = add_item('Braton Prime')
        cache(item, price, fetched_at)
        if threshold is not None:
            set_setting(admin_id, 'buy_threshold_plat', threshold)
    assert market_cell(farm_card(admin_client), 'Braton Prime') == want_cell + old_mark(fetched_at)
    assert requests_made == []


def test_the_farming_time_check_uses_the_cached_price(app, admin_client, admin_id,
                                                      requests_made):
    """120 runs at 5 min are 10 h; at 20p/h that is 200p, above a cached 150p: buy."""
    with app.app_context():
        item = add_item('Braton Prime')
        fetched_at = ago(STALE_AGE)
        cache(item, 150, fetched_at)
        get_db().execute('INSERT INTO drop_sources (item_id, location, avg_runs) '
                         "VALUES (?, 'Earth/Mantle', 120)", (item,))
        get_db().commit()
        set_setting(admin_id, 'plat_per_hour', '20')
    card = farm_card(admin_client)
    assert market_cell(card, 'Braton Prime') == 'Buy 150p' + old_mark(fetched_at)
    assert '~10.0h farming × 20p/h &gt; market 150p' in card
    assert requests_made == []


def test_an_item_without_a_market_slug_shows_no_price(app, admin_client, requests_made):
    with app.app_context():
        item = add_item('Braton Prime', market_url_name=None)
        cache(item, 120, ago(FRESH_AGE))
    assert market_cell(farm_card(admin_client), 'Braton Prime') == '—'
    assert requests_made == []


def test_with_pinned_goals_the_farm_next_list_is_not_built(app, admin_client, admin_id,
                                                           requests_made, monkeypatch):
    built = []
    monkeypatch.setattr(routes.dashboard, 'farm_next_items',
                        lambda *args, **kwargs: built.append(args) or [])
    with app.app_context():
        pin(admin_id, add_item('Soma Prime', 'soma_prime_set'))
    farm_card(admin_client)
    assert built == []


def test_the_prices_are_read_in_one_query(app, requests_made):
    with app.app_context():
        ids = [add_item(f'Item {n}', f'item_{n}') for n in range(1, 6)]
        for item_id, price in zip(ids, [10, None, 30, 40, 50]):
            cache(item_id, price, ago(STALE_AGE))
        items = [dict(r) for r in get_db().execute(
            'SELECT * FROM items WHERE id IN (?, ?, ?, ?, ?) ORDER BY id', ids)]
        statements = []
        get_db().set_trace_callback(statements.append)
        try:
            enrich_with_cached_market(items, None, None)
        finally:
            get_db().set_trace_callback(None)
    reads = [s for s in statements if 'market_cache' in s]
    assert len(reads) == 1
    assert reads[0].startswith('SELECT item_id, platinum_price, fetched_at FROM market_cache '
                               'WHERE item_id IN')
    assert not any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                   for s in statements)
    assert [i['market_price'] for i in items] == [10, None, 30, 40, 50]
    assert requests_made == []


def test_an_empty_list_reads_nothing(app):
    with app.app_context():
        statements = []
        get_db().set_trace_callback(statements.append)
        try:
            assert enrich_with_cached_market([], 10, 10) == []
        finally:
            get_db().set_trace_callback(None)
    assert statements == []


# ---------------------------------------------------------------------------
# The goal plan: one fetch on open, within the user's cache setting
# ---------------------------------------------------------------------------

@pytest.fixture
def market_answers(monkeypatch):
    """A recorded warframe.market request answered with one in-game sell order of 77p."""
    calls = []

    class Answer:
        def raise_for_status(self):
            pass

        def json(self):
            return {'data': [{'type': 'sell', 'platinum': 77, 'visible': True,
                               'user': {'status': 'ingame'}}], 'error': None}

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        return Answer()

    monkeypatch.setattr(market_api.requests, 'get', fake_get)
    return calls


# (case id, stored market_cache_hours or None, cache age, refetched)
GOAL_PLAN_CASES = [
    ('10 h old within a 24 h setting', '24', timedelta(hours=10), False),
    ('10 h old beyond the default 6 h', None, timedelta(hours=10), True),
    ('2 h old beyond a 1 h setting', '1', timedelta(hours=2), True),
    ('1 h old within the default 6 h', None, timedelta(hours=1), False),
]


@pytest.mark.parametrize('case', GOAL_PLAN_CASES, ids=lambda c: c[0])
def test_the_goal_plan_refetches_only_a_price_older_than_the_users_cache_setting(
        app, admin_client, admin_id, market_answers, case):
    _, hours, age, refetched = case
    with app.app_context():
        item = add_item('Braton Prime')
        cache(item, 33, ago(age))
        goal_id = pin(admin_id, item)
        if hours is not None:
            set_setting(admin_id, 'market_cache_hours', hours)

    response = admin_client.get(f'/farming/goals/{goal_id}/plan')

    assert response.status_code == 200
    plan = response.get_json()['plan']
    if refetched:
        assert len(market_answers) == 1
        assert plan['market_price'] == 77
    else:
        assert market_answers == []
        assert plan['market_price'] == 33


# ---------------------------------------------------------------------------
# One item's answer after a price refresh: GET /dashboard/farm-next/<id>
# ---------------------------------------------------------------------------

def add_drops(item_id, avg_runs=120):
    """One mission source: 120 runs at 5 min are 10 h of farming."""
    get_db().execute('INSERT INTO drop_sources (item_id, location, avg_runs) '
                     "VALUES (?, 'Earth/Mantle', ?)", (item_id, avg_runs))
    get_db().commit()


def answer_of(client, item_id):
    response = client.get(f'/dashboard/farm-next/{item_id}')
    return response.status_code, response.get_json()


def setting(settings, key, default=None):
    return int(settings[key]) if key in settings else default


# (case id, cached price or None, fetch age or None for no row, {setting: value}, drops)
ANSWER_CASES = [
    ('below threshold', 40, STALE_AGE, {'buy_threshold_plat': '50'}, False),
    ('at threshold', 50, FRESH_AGE, {'buy_threshold_plat': '50'}, False),
    ('above threshold', 60, STALE_AGE, {'buy_threshold_plat': '50'}, False),
    ('no settings', 40, STALE_AGE, {}, False),
    ('farming worth more', 150, STALE_AGE, {'plat_per_hour': '20'}, True),
    ('farming worth less', 250, STALE_AGE, {'plat_per_hour': '20'}, True),
    ('over the hour cap', 900, STALE_AGE, {'farm_hour_cap': '5'}, True),
    ('no price, over the hour cap', None, None, {'farm_hour_cap': '5'}, True),
    ('price NULL, threshold', None, FRESH_AGE, {'buy_threshold_plat': '50'}, True),
    ('old under a 24 h setting', 40, timedelta(hours=10),
     {'market_cache_hours': '24', 'buy_threshold_plat': '50'}, False),
]


@pytest.mark.parametrize('case', ANSWER_CASES, ids=lambda c: c[0])
def test_one_items_answer_is_the_one_the_dashboards_load_draws(
        app, admin_client, admin_id, requests_made, case):
    _, price, age, settings, drops = case
    with app.app_context():
        item = add_item('Braton Prime')
        if age is not None:
            cache(item, price, ago(age))
        if drops:
            add_drops(item)
        for key, value in settings.items():
            set_setting(admin_id, key, value)
        loaded = farm_next_items(admin_id, setting(settings, 'buy_threshold_plat'),
                                 setting(settings, 'plat_per_hour'), 10,
                                 setting(settings, 'market_cache_hours', 6),
                                 setting(settings, 'farm_hour_cap'))
        before = cache_rows()
    [row] = [i for i in loaded if i['id'] == item]

    status, body = answer_of(admin_client, item)

    assert status == 200
    assert body == {'ok': True, 'recommend_buy': row['recommend_buy'],
                    'recommend_reason': row['recommend_reason']}
    assert market_cell(farm_card(admin_client), 'Braton Prime').startswith('Buy ') \
        is body['recommend_buy']
    assert requests_made == []
    with app.app_context():
        assert cache_rows() == before


def test_the_answer_follows_the_cache_row_an_ok_refresh_writes(app, admin_client, admin_id,
                                                               requests_made):
    with app.app_context():
        item = add_item('Braton Prime')
        cache(item, 60, ago(STALE_AGE))
        set_setting(admin_id, 'buy_threshold_plat', '50')
    assert answer_of(admin_client, item)[1]['recommend_buy'] is False
    with app.app_context():
        get_db().execute('UPDATE market_cache SET platinum_price = 40, fetched_at = ? '
                         'WHERE item_id = ?', (ago(timedelta(0)), item))
        get_db().commit()
    status, body = answer_of(admin_client, item)
    assert (status, body['recommend_buy']) == (200, True)
    assert '50' in body['recommend_reason']
    with app.app_context():
        get_db().execute('UPDATE market_cache SET platinum_price = 70 WHERE item_id = ?', (item,))
        get_db().commit()
    assert answer_of(admin_client, item)[1]['recommend_buy'] is False
    assert requests_made == []


@pytest.mark.parametrize('which', ['unknown', 'no slug', 'zero', 'over 64 bits'])
def test_an_item_without_a_market_listing_has_no_answer(app, admin_client, requests_made, which):
    with app.app_context():
        item = add_item('Braton Prime', market_url_name=None)
        cache(item, 40, ago(FRESH_AGE))
    item_id = {'unknown': item + 1, 'no slug': item, 'zero': 0,
               'over 64 bits': 2 ** 63}[which]
    response = admin_client.get(f'/dashboard/farm-next/{item_id}')
    assert response.status_code == 404
    if which != 'over 64 bits':
        assert response.get_json()['ok'] is False
    assert requests_made == []


def test_the_answer_uses_the_callers_settings_only(app, admin_client, requests_made):
    with app.app_context():
        item = add_item('Braton Prime')
        cache(item, 40, ago(STALE_AGE))
        alice = User.create('alice', password='pw-alice-1').id
        set_setting(alice, 'buy_threshold_plat', '50')
    alice_client = type(admin_client)(app, app.response_class, use_cookies=True)
    assert alice_client.post('/auth/login', data={'username': 'alice',
                                                  'password': 'pw-alice-1'}).status_code == 302
    assert answer_of(admin_client, item)[1]['recommend_buy'] is False
    assert answer_of(alice_client, item)[1]['recommend_buy'] is True
    assert answer_of(admin_client, item)[1]['recommend_buy'] is False


def test_an_anonymous_request_is_refused_like_the_dashboard(app, client):
    with app.app_context():
        item = add_item('Braton Prime')
    dashboard = client.get('/dashboard')
    response = client.get(f'/dashboard/farm-next/{item}')
    assert response.status_code == dashboard.status_code == 302
    assert response.headers['Location'].split('?')[0] == \
        dashboard.headers['Location'].split('?')[0]


# ---------------------------------------------------------------------------
# The page script that follows a refresh (its run: the browser test)
# ---------------------------------------------------------------------------

def farm_next_script(page):
    start = page.index('// The Farm next Buy mark follows a refreshed price.')
    return page[start:page.index('// MR rank thresholds', start)]


def test_each_farm_next_row_carries_its_item_for_the_follow_up(app, admin_client):
    with app.app_context():
        item = add_item('Braton Prime')
        cache(item, 40, ago(STALE_AGE))
    assert f'<tr class="farm-next-row" data-item="{item}">' in farm_card(admin_client)


def test_the_dashboard_follows_only_an_ok_refresh_of_a_farm_next_row(admin_client):
    script = farm_next_script(admin_client.get('/dashboard').get_data(as_text=True))
    assert "$(document).on('wf-price-refreshed', function (e, itemId)" in script
    assert '"/dashboard/farm-next/0"' in script
    # the id is checked before it reaches a URL; rows are matched by attribute, not selector
    assert script.index('WF_ITEM_ID.test(id)') < script.index('url:')
    assert '[data-item' not in script
    # the answer is used only when it is current, ok and complete; a failure says nothing
    assert 'wfFarmNextSeq[id] !== seq || !r || r.ok !== true' in script
    assert "typeof r.recommend_buy !== 'boolean'" in script
    assert 'error:' not in script and 'M.toast' not in script
    # values go in as text
    assert "$mark.attr('title', reason)" in script
    assert '.html(' not in script and 'innerHTML' not in script


def test_with_pinned_goals_the_page_has_no_farm_next_row_to_follow(app, admin_client, admin_id):
    with app.app_context():
        pin(admin_id, add_item('Soma Prime', 'soma_prime_set'))
    assert 'farm-next-row' not in farm_card(admin_client)
