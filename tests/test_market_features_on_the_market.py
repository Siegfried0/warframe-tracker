"""Which items get the market features: those warframe.market trades, whatever WFCD's flag says.

An item is on the market when it is tradable or has a market slug (`market_url_name`); an empty
slug counts as none. Prime warframes are the case that matters: the market sells them as sets,
so they have a slug while WFCD marks them not tradable. The collection shows the market-watch
toggle, in the card and the table view of every tab, for exactly the items on the market (the
table shows a `—` for the others). Farm next's cached price and buy recommendation and the goal
plan's price and link need only the slug: an item with one gets them whatever its `tradable`,
an item without one gets none. Showing these never changes `tradable`.

Network refused (tests/conftest.py); the one goal-plan price request is answered by a stub.
"""
import re

import pytest

from models.db import get_db
from models.item import Item, on_market
from models.user import User
from services import market_api
from services.farming_service import add_farming_goal, enrich_with_cached_market

CATEGORIES = ('warframe', 'primary', 'mod')
VIEWS = ('cards', 'table')

# (case id, tradable, market_url_name, on the market, priced by slug)
CASES = [
    ('tradable with slug', True, 'thing_set', True, True),
    ('not tradable with slug', False, 'thing_set', True, True),   # a prime warframe
    ('tradable without slug', True, None, True, False),
    ('not tradable without slug', False, None, False, False),
    ('tradable empty slug', True, '', True, False),
    ('not tradable empty slug', False, '', False, False),
]

TOGGLE = re.compile(r'<a href="#" class="wf-market-watch [^"]*"[^>]*>', re.S)


def add_item(name, category, tradable, slug):
    Item.upsert(name, category, source='prime' if category == 'warframe' else 'standard',
                tradable=tradable)
    db = get_db()
    db.execute('UPDATE items SET market_url_name = ? WHERE name = ? AND category = ?',
               (slug, name, category))
    db.commit()
    return db.execute('SELECT id FROM items WHERE name = ? AND category = ?',
                      (name, category)).fetchone()['id']


def tradable_flags():
    return {r['id']: r['tradable'] for r in get_db().execute('SELECT id, tradable FROM items')}


@pytest.fixture
def catalogue(app, admin_client):
    """Every case in every category; returns {(category, case id): item id}."""
    ids = {}
    with app.app_context():
        for category in CATEGORIES:
            for case_id, tradable, slug, _on, _priced in CASES:
                ids[category, case_id] = add_item(f'{case_id} {category}', category,
                                                  tradable, slug)
    return ids


def view_chunks(html, view):
    """{item id: the markup of its card (cards) or row (table)} in one view."""
    cards_at = html.index('id="collection-cards"')
    table_at = html.index('id="collection-table"')
    section = (html[cards_at:table_at] if view == 'cards'
               else html[table_at:html.index('</table>', table_at)])
    parts = section.split('data-item-id="')[1:]
    return {int(part[:part.index('"')]): part for part in parts}


@pytest.mark.parametrize('view', VIEWS)
@pytest.mark.parametrize('category', CATEGORIES)
def test_the_toggle_shows_for_exactly_the_items_on_the_market(
        app, admin_client, catalogue, category, view):
    with app.app_context():
        before = tradable_flags()
    response = admin_client.get(f'/collection/{category}')
    assert response.status_code == 200
    chunks = view_chunks(response.get_data(as_text=True), view)
    for case_id, _tradable, _slug, on, _priced in CASES:
        chunk = chunks[catalogue[category, case_id]]
        assert len(TOGGLE.findall(chunk)) == (1 if on else 0), (category, view, case_id)
        if view == 'table' and not on:
            assert '<span class="grey-text">—</span>' in chunk, (category, case_id)
    with app.app_context():
        assert tradable_flags() == before


@pytest.mark.parametrize('case', CASES, ids=lambda c: c[0])
def test_on_market_is_tradable_or_a_non_empty_slug(case):
    _, tradable, slug, on, _priced = case
    assert on_market({'tradable': int(tradable), 'market_url_name': slug}) is on


@pytest.mark.parametrize('case', CASES, ids=lambda c: c[0])
def test_farm_next_prices_an_item_by_its_slug_alone(app, monkeypatch, case):
    case_id, tradable, slug, _on, priced = case
    monkeypatch.setattr(market_api.requests, 'get',
                        lambda url, *a, **k: pytest.fail(f'unexpected request: {url}'))
    with app.app_context():
        iid = add_item('Thing Prime', 'warframe', tradable, slug)
        get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                         "VALUES (?, 40, '2026-11-01T00:00:00')", (iid,))
        get_db().commit()
        items = [dict(get_db().execute('SELECT * FROM items WHERE id = ?', (iid,)).fetchone())]
        enrich_with_cached_market(items, 50, None)
    item = items[0]
    if priced:
        assert item['market_price'] == 40, case_id
        assert item['market_url'] == f'https://warframe.market/items/{slug}'
        assert item['recommend_buy'] is True
    else:
        assert 'market_price' not in item and 'recommend_buy' not in item, case_id


@pytest.mark.parametrize('case', CASES, ids=lambda c: c[0])
def test_the_goal_plan_prices_and_links_an_item_by_its_slug_alone(
        app, admin_client, monkeypatch, case):
    case_id, tradable, slug, _on, priced = case
    calls = []

    class Answer:
        def raise_for_status(self):
            pass

        def json(self):
            return {'payload': {'orders': [
                {'order_type': 'sell', 'platinum': 77, 'user': {'status': 'ingame'}}]}}

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        return Answer()

    monkeypatch.setattr(market_api.requests, 'get', fake_get)
    with app.app_context():
        iid = add_item('Thing Prime', 'warframe', tradable, slug)
        goal_id = add_farming_goal(User.get_by_username('admin').id, iid)['goal_id']

    response = admin_client.get(f'/farming/goals/{goal_id}/plan')

    assert response.status_code == 200
    plan = response.get_json()['plan']
    if priced:
        assert plan['market_price'] == 77, case_id
        assert plan['market_url'] == f'https://warframe.market/items/{slug}'
        assert len(calls) == 1 and slug in calls[0]
    else:
        assert plan['market_price'] is None and plan['market_url'] is None, case_id
        assert calls == []
