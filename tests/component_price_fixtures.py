"""Shared doubles and catalogue helpers of the component price tests: a market that answers
hand-written copies of the item list's and the orders' shapes (every request mocked at
``requests.get``), and an item with priced and unpriced parts."""
import json
from datetime import datetime

import pytest
import requests

from models.db import get_db
from models.item import Item
from models.user import User
from services import market_api
from services.farming_service import add_farming_goal

OLD_AT = '2026-01-01T10:00:00'


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error', response=self)

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


def listed(name, slug):
    return {'id': 'x' + slug[:20], 'slug': slug, 'i18n': {'en': {'name': name}}}


def item_list(entries):
    return {'apiVersion': '0.0.0', 'data': entries, 'error': None}


def orders(*prices):
    return {'apiVersion': '0.0.0', 'error': None, 'data': [
        {'type': 'sell', 'platinum': p, 'visible': True, 'user': {'status': 'ingame'}}
        for p in prices]}


@pytest.fixture
def market(monkeypatch):
    """`market.items` answers the item list, `market.orders` a price request (a FakeResponse,
    or an exception to raise); every requested URL is recorded in `market.calls`."""
    class Market:
        items = FakeResponse(payload=item_list([]))
        orders = FakeResponse(payload=orders(30, 22))
        calls = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        Market.calls.append(url)
        answer = Market.orders if '/orders/item/' in url else Market.items
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(requests, 'get', fake_get)
    return Market


@pytest.fixture
def admin_id(app, admin_client):
    with app.app_context():
        return User.get_by_username('admin').id


# ── Catalogue helpers ────────────────────────────────────────────────────────

def add_item(name, category='warframe', slug=None):
    Item.upsert(name, category, source='prime', tradable=True)
    db = get_db()
    db.execute('UPDATE items SET market_url_name = ? WHERE name = ? AND category = ?',
               (slug, name, category))
    db.commit()
    return db.execute('SELECT id FROM items WHERE name = ? AND category = ?',
                      (name, category)).fetchone()[0]


def meta(item_id, *components):
    for component in components:
        get_db().execute('INSERT INTO item_component_meta (item_id, component_name) VALUES (?, ?)',
                         (item_id, component))
    get_db().commit()


def drop(item_id, part, location='Ceres/Draco (Capture)', runs=10):
    get_db().execute(
        '''INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs,
                                     source_type, notes) VALUES (?, ?, ?, 10, ?, 'component', ?)''',
        (item_id, f'Ash Prime {part}', location, runs, part))
    get_db().commit()


def stored_component_slugs():
    return {(r['item_id'], r['component_name']): r['market_url_name'] for r in get_db().execute(
        'SELECT item_id, component_name, market_url_name FROM component_market_slugs')}


def set_component_slug(item_id, component, slug):
    get_db().execute('INSERT INTO component_market_slugs (item_id, component_name, '
                     'market_url_name) VALUES (?, ?, ?)', (item_id, component, slug))
    get_db().commit()


def cache_slug(slug, price, fetched_at):
    get_db().execute('INSERT INTO market_slug_cache (market_url_name, platinum_price, fetched_at) '
                     'VALUES (?, ?, ?)', (slug, price, fetched_at))
    get_db().commit()


def slug_cache():
    return {r['market_url_name']: (r['platinum_price'], r['fetched_at']) for r in get_db().execute(
        'SELECT market_url_name, platinum_price, fetched_at FROM market_slug_cache')}


def now_text():
    return datetime.utcnow().isoformat()


@pytest.fixture
def refused(monkeypatch):
    """Every market request fails the test."""
    def fake_get(url, *args, **kwargs):
        raise AssertionError(f'unexpected market request: {url}')
    monkeypatch.setattr(market_api.requests, 'get', fake_get)


def ash_goal(user_id, item_slug=None):
    """Ash Prime with four parts: Neuroptics (no cache row), Chassis (fresh, 40p), Systems
    (old, 15p), Blueprint (fresh, no sell order); Helmet without a slug. Each part 10 runs of
    Capture: 30 minutes."""
    ash = add_item('Ash Prime', slug=item_slug)
    for part in ('Neuroptics', 'Chassis', 'Systems', 'Blueprint', 'Helmet'):
        drop(ash, part)
    for part, slug in (('Neuroptics', 'ash_n'), ('Chassis', 'ash_c'), ('Systems', 'ash_s'),
                       ('Blueprint', 'ash_bp')):
        set_component_slug(ash, part, slug)
    cache_slug('ash_c', 40, now_text())
    cache_slug('ash_s', 15, OLD_AT)
    cache_slug('ash_bp', None, now_text())
    return ash, add_farming_goal(user_id, ash)['goal_id']


def components_of(plan):
    return {c['component_name']: c for c in plan['components']}


def store_setting(user_id, key, value):
    get_db().execute('INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)',
                     (user_id, key, value))
    get_db().commit()


