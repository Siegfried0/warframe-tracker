"""warframe.market prices: API v2 under the app's own client name, the price rule, and a failed
request that keeps the cached price.

A price comes from `GET /v2/orders/item/<slug>`: the lowest `platinum` among the orders with
`type` "sell", `visible` not false and `user.status` "ingame". A request that gets no usable
answer (no answer, an HTTP error status, a body that is not JSON or not v2's shape, an `error`
reported) is a failure, never "no price": it writes nothing to `market_cache`, the result keeps
the cached price and fetch time, and `GET /market/price/<id>` answers 502 with them. A
successful answer writes the price, or NULL for no sell order in game, with the current time.

The answers are hand-written in v2's shape, a few fields each (no market data is committed);
`requests.get` is replaced, so no test reaches the network.
"""
import itertools
import json
from datetime import datetime, timedelta

import pytest
import requests

from models.db import get_db
from models.item import Item
from models.user import User
from services import market_api
from services.farming_service import add_farming_goal

SLUG = 'thing_prime_set'
ORDERS_URL = f'https://api.warframe.market/v2/orders/item/{SLUG}'


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code, self._payload, self._text = status, payload, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error', response=self)

    def json(self):
        if self._text is not None:
            return json.loads(self._text)
        return self._payload


def order(platinum, type_='sell', status='ingame', visible=True):
    """One v2 order in the measured shape (a few of its fields only)."""
    found = {'id': 'o1', 'type': type_, 'platinum': platinum, 'quantity': 1, 'rank': 0,
             'user': {'id': 'u1', 'ingameName': 'Someone', 'status': status}}
    if visible is not ...:
        found['visible'] = visible
    return found


def answer(*orders):
    return {'apiVersion': '0.0.0', 'data': list(orders), 'error': None}


@pytest.fixture
def market(monkeypatch):
    """Serve `market.response` (a FakeResponse, or an exception to raise) to every request and
    record each as (url, headers, timeout)."""
    class Market:
        response = FakeResponse(payload=answer(order(40), order(25)))
        calls = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        Market.calls.append((url, headers, timeout))
        if isinstance(Market.response, BaseException):
            raise Market.response
        return Market.response

    monkeypatch.setattr(requests, 'get', fake_get)
    return Market


def add_item(slug=SLUG):
    Item.upsert('Thing Prime', 'warframe', source='prime')
    db = get_db()
    db.execute("UPDATE items SET market_url_name = ? WHERE name = 'Thing Prime'", (slug,))
    db.commit()
    return db.execute("SELECT id FROM items WHERE name = 'Thing Prime'").fetchone()['id']


def cache_row(item_id):
    row = get_db().execute('SELECT platinum_price, fetched_at FROM market_cache '
                           'WHERE item_id = ?', (item_id,)).fetchone()
    return None if row is None else (row['platinum_price'], row['fetched_at'])


def ago(hours):
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat()


# (case id, the cache row before as (price, fetched_at) or None, fresh under the default 6 h)
CACHE_STATES = [
    ('no row', None, False),
    ('fresh with a price', (30, ago(1)), True),
    ('stale with a price', (30, ago(10)), False),
    ('stale with a zero price', (0, ago(10)), False),
    ('stale without a price', (None, ago(10)), False),
    ('unreadable fetch time', (30, 'yesterday-ish'), False),
    ('empty fetch time', (30, ''), False),
]


@pytest.fixture(params=CACHE_STATES, ids=[c[0] for c in CACHE_STATES])
def cached(request, app):
    """An item with a slug and the cache state of the case; yields (item id, row, fresh)."""
    _, row, fresh = request.param
    with app.app_context():
        item_id = add_item()
        if row is not None:
            get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                             'VALUES (?, ?, ?)', (item_id, *row))
            get_db().commit()
    return item_id, row, fresh


# ── The request: v2, the app's own client name (toolchain) ───────────────────

def test_a_price_request_asks_v2_for_the_items_orders_under_the_apps_own_client_name(
        app, market):
    with app.app_context():
        assert market_api.get_price(add_item(), SLUG, 6)['platinum'] == 25
    assert [(url, timeout) for url, _, timeout in market.calls] == [(ORDERS_URL, 15)]
    headers = market.calls[0][1]
    assert headers['User-Agent'] == market_api.CLIENT_NAME
    assert headers['User-Agent'].strip()
    assert not headers['User-Agent'].lower().startswith('python-requests')
    assert (headers['Accept'], headers['Platform'], headers['Language']) == (
        'application/json', 'pc', 'en')


def test_the_client_name_is_a_fixed_product_name_and_version():
    name, _, version = market_api.CLIENT_NAME.partition('/')
    assert name.isascii() and name.isalnum()
    assert version and all(part.isdigit() for part in version.split('.'))


def test_no_other_module_of_the_app_calls_the_market():
    import pathlib
    app_root = pathlib.Path(market_api.__file__).resolve().parents[1]
    callers = sorted(str(p.relative_to(app_root)).replace('\\', '/')
                     for p in app_root.rglob('*.py')
                     if 'api.warframe.market' in p.read_text(encoding='utf-8'))
    assert callers == ['services/market_api.py']


# ── The price rule (axis 1: the order's fields) ──────────────────────────────

TYPES = {'sell': True, 'buy': False}
STATUSES = {'ingame': True, 'online': False, 'offline': False, 'invisible': False,
            None: False, 'InGame': False}
VISIBLE = {True: True, False: False, ...: True}       # ... = the field is absent
PLATINUM = {77: 77, 0: 0, -5: None, 12.0: None, True: None, '77': None}
ORDER_ROWS = list(itertools.product(TYPES, STATUSES, VISIBLE, PLATINUM))


def _row_id(row):
    type_, status, visible, platinum = row
    return (f'{type_}-{status or "missing"}-'
            f'{"absent" if visible is ... else visible}-{platinum!r}')


@pytest.mark.parametrize('row', ORDER_ROWS, ids=[_row_id(r) for r in ORDER_ROWS])
def test_only_a_visible_sell_order_in_game_with_a_whole_price_is_counted(row):
    type_, status, visible, platinum = row
    one = order(platinum, type_, status, visible)
    if status is None:
        del one['user']['status']
    counted = TYPES[type_] and STATUSES[status] and VISIBLE[visible]
    want = PLATINUM[platinum] if counted else None
    assert market_api.lowest_sell_price(answer(one)) == want
    # beside a counted order of 50, the lower one wins only when it is counted
    assert market_api.lowest_sell_price(answer(order(50), one)) == (
        50 if want is None else min(50, want))


@pytest.mark.parametrize('orders, want', [
    ([], None),
    ([order(40), order(25), order(31)], 25),
    ([order(10, status='online'), order(5, type_='buy'), order(20, visible=False)], None),
    ([order(30), order(30)], 30),
    (['not an order', None, 7, order(12)], 12),
    ([{**order(9), 'user': None}, {**order(8), 'user': 'ingame'}, order(15)], 15),
], ids=['no orders', 'the lowest of three', 'none counted', 'two equal', 'odd entries skipped',
        'user not an object'])
def test_the_lowest_counted_price_or_no_price(orders, want):
    assert market_api.lowest_sell_price(answer(*orders)) == want


# ── A failed request writes nothing (axis 4: dependency errors; axis 5) ──────

FAILURES = [
    ('unreachable', requests.ConnectionError('refused'), 'no answer'),
    ('timeout', requests.Timeout('slow'), 'no answer in time'),
    ('403 bot page', FakeResponse(status=403, text='<html><title>Just a moment</title></html>'),
     'HTTP 403'),
    ('403 deprecated', FakeResponse(status=403, text='Deprecated'), 'HTTP 403'),
    ('429', FakeResponse(status=429, payload={'error': 'slow down'}), 'HTTP 429'),
    ('500', FakeResponse(status=500), 'HTTP 500'),
    ('503', FakeResponse(status=503), 'HTTP 503'),
    ('200 with html', FakeResponse(text='<html>maintenance</html>'), 'the answer is not JSON'),
    ('200 empty body', FakeResponse(text=''), 'the answer is not JSON'),
    ('json without data', FakeResponse(payload={'apiVersion': '0.0.0', 'error': None}),
     'the answer is not in the expected shape'),
    ('the v1 shape', FakeResponse(payload={'payload': {'orders': [
        {'order_type': 'sell', 'platinum': 5, 'user': {'status': 'ingame'}}]}}),
     'the answer is not in the expected shape'),
    ('data an object', FakeResponse(payload={'data': {'sell': [order(5)]}, 'error': None}),
     'the answer is not in the expected shape'),
    ('data null', FakeResponse(payload={'data': None, 'error': None}),
     'the answer is not in the expected shape'),
    ('a json list', FakeResponse(payload=[order(5)]), 'the answer is not in the expected shape'),
    ('a json text', FakeResponse(payload='ok'), 'the answer is not in the expected shape'),
    ('error reported', FakeResponse(payload={'data': [order(5)], 'error': 'item not found'}),
     'the market reported an error'),
    ('error an object', FakeResponse(payload={'data': [], 'error': {'request': ['x']}}),
     'the market reported an error'),
]


@pytest.mark.parametrize('case, response, reason', FAILURES, ids=[c[0] for c in FAILURES])
def test_a_failed_request_keeps_the_cached_price_and_the_refresh_answers_502(
        app, admin_client, market, cached, case, response, reason):
    item_id, row, fresh = cached
    market.response = response
    with app.app_context():
        result = market_api.get_price(item_id, SLUG, 6)
        assert cache_row(item_id) == row
    if fresh:                                   # a fresh price asks nothing of the market
        assert market.calls == [] and result['from_cache'] is True
        assert 'failed' not in result
        return
    kept_price, kept_at = row if row else (None, None)
    kept_label = market_api._fetch_label(kept_at)
    assert result == {
        'platinum': kept_price, 'fetched_at': kept_at, 'fetched_label': kept_label,
        'from_cache': row is not None,
        'failed': True,
        'error': f'warframe.market gave no price ({reason}); the last price is kept.'}
    assert '<' not in result['error'] and 'Deprecated' not in result['error']

    response = admin_client.get(f'/market/price/{item_id}')
    assert response.status_code == 502
    assert response.get_json() == {'ok': False, 'error': result['error'],
                                   'platinum': kept_price, 'fetched_at': kept_at,
                                   'fetched_label': kept_label}
    with app.app_context():
        assert cache_row(item_id) == row
        assert not market_api.is_fresh(cache_row(item_id) and cache_row(item_id)[1], 6)
    assert len(market.calls) == 2


@pytest.mark.parametrize('orders, want', [((order(40), order(25)), 25),
                                          ((order(0),), 0),
                                          ((order(5, status='offline'),), None),
                                          ((), None)],
                         ids=['a price', 'a zero price', 'no sell order in game', 'no orders'])
def test_a_successful_answer_writes_the_price_and_the_current_time(
        app, admin_client, market, cached, orders, want):
    item_id, row, fresh = cached
    market.response = FakeResponse(payload=answer(*orders))
    before = datetime.utcnow()
    response = admin_client.get(f'/market/price/{item_id}')
    assert response.status_code == 200
    body = response.get_json()
    with app.app_context():
        stored = cache_row(item_id)
    if fresh:
        assert stored == row and market.calls == []
        assert body == {'ok': True, 'platinum': row[0], 'fetched_at': row[1],
                        'fetched_label': market_api._fetch_label(row[1]), 'from_cache': True}
        return
    assert stored[0] == want
    assert datetime.fromisoformat(stored[1]) >= before
    assert body == {'ok': True, 'platinum': want, 'fetched_at': stored[1],
                    'fetched_label': datetime.fromisoformat(stored[1]).strftime('%Y-%m-%d %H:%M') + ' UTC',
                    'from_cache': False}


# ── Two refreshes of one item (axis 6: concurrency) ──────────────────────────

def test_a_failure_that_ends_after_a_success_leaves_the_successs_row(app, market, monkeypatch):
    """Two refreshes of one stale item overlap: the first fails, but only after the second has
    fetched and stored a price. The failure writes nothing, so the success's row stays."""
    with app.app_context():
        item_id = add_item()
        get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                         'VALUES (?, 30, ?)', (item_id, ago(10)))
        get_db().commit()
        inner = {}

        def overlapping_get(url, headers=None, timeout=None, **kwargs):
            market.calls.append(url)
            if len(market.calls) == 1:          # the first request is still running ...
                market.response = FakeResponse(payload=answer(order(44)))
                inner['result'] = market_api.get_price(item_id, SLUG, 6)
                raise requests.ConnectionError('reset')   # ... and then fails
            return market.response

        monkeypatch.setattr(requests, 'get', overlapping_get)
        outer = market_api.get_price(item_id, SLUG, 6)
        assert inner['result']['platinum'] == 44
        assert (outer['failed'], outer['platinum']) == (True, 30)
        assert cache_row(item_id) == (44, inner['result']['fetched_at'])


# ── The farming plan's goal price ────────────────────────────────────────────

def test_the_goal_plan_shows_the_kept_price_after_a_failed_request(app, admin_client, market):
    with app.app_context():
        item_id = add_item()
        get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                         'VALUES (?, 30, ?)', (item_id, ago(10)))
        get_db().commit()
        goal_id = add_farming_goal(User.get_by_username('admin').id, item_id)['goal_id']
        row = cache_row(item_id)
    market.response = FakeResponse(status=403, text='Deprecated')
    response = admin_client.get(f'/farming/goals/{goal_id}/plan')
    assert response.status_code == 200
    assert response.get_json()['plan']['market_price'] == 30
    assert [url for url, _, _ in market.calls] == [ORDERS_URL]
    with app.app_context():
        assert cache_row(item_id) == row
