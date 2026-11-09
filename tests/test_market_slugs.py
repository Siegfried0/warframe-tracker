"""The market slug sync step: each catalogue item's warframe.market slug from the market's item
list, read once per sync — the matching, the one-transaction write that only this step makes,
the failures that change nothing, and what a matched item gains (its price and its link).

The item list is a hand-written, minimal copy of the answer's shape (no market data is
committed); every request is mocked at ``requests.get``, so no test reaches the network.
"""
import json

import pytest
import requests

from models.db import get_db
from models.item import Item
from services import market_slugs as ms
from services import wiki_sync

# (name, category) of the catalogue every test starts with
CATALOGUE = [
    ('Braton Prime', 'primary'),        # listed only as "Braton Prime Set"
    ('Soma Prime', 'primary'),          # listed under both forms: the exact name wins
    ('Serration', 'mod'),               # listed in another case
    ('Vitality', 'mod'),                # listed with outer blanks
    (' Hornet Strike ', 'mod'),         # the catalogue name has outer blanks
    ('Ōmen', 'warframe'),               # non-ASCII, listed upper-cased
    ('Lato  Vandal', 'secondary'),      # an inner double blank: not listed that way
    ('Saryn’s Gift', 'mod'),            # a typographic apostrophe: listed with "'"
    ('Detron', 'secondary'),            # the same name in two categories,
    ('Detron', 'arch-gun'),             # listed twice with different slugs
    ('Excalibur Umbra', 'warframe'),    # not listed at all
]

ITEMS = [
    {'item_name': 'Braton Prime Set', 'url_name': 'braton_prime_set'},
    {'item_name': 'Soma Prime Set', 'url_name': 'soma_prime_set'},
    {'item_name': 'Soma Prime', 'url_name': 'soma_prime'},
    {'item_name': 'SERRATION', 'url_name': 'serration'},
    {'item_name': '  Vitality  ', 'url_name': 'vitality'},
    {'item_name': 'Hornet Strike', 'url_name': 'hornet_strike'},
    {'item_name': 'ŌMEN', 'url_name': 'omen'},
    {'item_name': 'Lato Vandal', 'url_name': 'lato_vandal'},
    {'item_name': "Saryn's Gift", 'url_name': 'saryns_gift'},
    {'item_name': 'Detron', 'url_name': 'detron'},
    {'item_name': 'detron', 'url_name': 'detron_duplicate'},
    {'item_name': 'Orokin Cell', 'url_name': 'orokin-cell_2'},
]

# (case id, catalogue name, category, the slug a good read stores)
EXPECTED = [
    ('only with Set', 'Braton Prime', 'primary', 'braton_prime_set'),
    ('exact before Set', 'Soma Prime', 'primary', 'soma_prime'),
    ('case', 'Serration', 'mod', 'serration'),
    ('outer blanks in the list', 'Vitality', 'mod', 'vitality'),
    ('outer blanks in the catalogue', ' Hornet Strike ', 'mod', 'hornet_strike'),
    ('non-ASCII case', 'Ōmen', 'warframe', 'omen'),
    ('inner double blank', 'Lato  Vandal', 'secondary', None),
    ('other apostrophe', 'Saryn’s Gift', 'mod', None),
    ('two categories, first of a duplicate', 'Detron', 'secondary', 'detron'),
    ('two categories, the other one', 'Detron', 'arch-gun', 'detron'),
    ('not listed', 'Excalibur Umbra', 'warframe', None),
]
MATCHED = sum(1 for case in EXPECTED if case[3] is not None)


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


def item_list(items):
    return {'payload': {'items': items}}


@pytest.fixture
def market(monkeypatch):
    """Serve `market.response` (a FakeResponse, or an exception to raise) for the item list,
    and `market.orders` for a price request; every request is recorded."""
    class Market:
        response = FakeResponse(payload=item_list(ITEMS))
        orders = {'payload': {'orders': [
            {'order_type': 'sell', 'platinum': 40, 'user': {'status': 'ingame'}},
            {'order_type': 'sell', 'platinum': 25, 'user': {'status': 'ingame'}},
            {'order_type': 'sell', 'platinum': 10, 'user': {'status': 'offline'}},
            {'order_type': 'buy', 'platinum': 5, 'user': {'status': 'ingame'}},
        ]}}
        calls = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        Market.calls.append((url, headers, timeout))
        if url.endswith('/orders'):
            return FakeResponse(payload=Market.orders)
        if isinstance(Market.response, BaseException):
            raise Market.response
        return Market.response

    monkeypatch.setattr(requests, 'get', fake_get)
    return Market


@pytest.fixture
def ctx(app):
    with app.app_context():
        for name, category in CATALOGUE:
            Item.upsert(name, category, source='standard')
        yield get_db()


def slugs(db):
    return {(r['name'], r['category']): r['market_url_name']
            for r in db.execute('SELECT name, category, market_url_name FROM items')}


def seed(db, slug='seeded_slug'):
    db.execute('UPDATE items SET market_url_name = ?', (slug,))
    db.commit()


# ── Matching (axis 1: spellings; axis 3: the same name twice) ────────────────

@pytest.mark.parametrize('case, name, category, want', EXPECTED, ids=[c[0] for c in EXPECTED])
def test_a_good_read_gives_each_item_the_slug_its_name_matches(ctx, market, case, name,
                                                              category, want):
    seed(ctx)                                   # an unmatched row is cleared, not kept
    assert ms.sync_market_slugs() == {'ok': True, 'matched': MATCHED}
    assert slugs(ctx)[(name, category)] == want


def test_the_list_is_read_once_from_the_price_clients_api(ctx, market):
    ms.sync_market_slugs()
    assert [(url, timeout) for url, _, timeout in market.calls] == [
        ('https://api.warframe.market/v1/items', 60)]
    assert market.calls[0][1]['Language'] == 'en'


def test_a_new_item_starts_without_a_slug_and_a_catalogue_upsert_keeps_it(ctx, market):
    ms.sync_market_slugs()
    for name, category in CATALOGUE:            # the category passes of the next sync
        Item.upsert(name, category, source='prime', vaulted=True, tradable=False)
    Item.upsert('Gara Prime', 'warframe', source='prime')
    stored = slugs(ctx)
    assert stored.pop(('Gara Prime', 'warframe')) is None
    assert stored == {(name, category): want for _, name, category, want in EXPECTED}


def test_a_second_sync_in_a_row_writes_no_row(ctx, market):
    first = ms.sync_market_slugs()
    before = ctx.total_changes
    assert ms.sync_market_slugs() == first
    assert ctx.total_changes == before


def test_a_slug_of_the_longest_length_the_column_holds_is_stored(ctx, market):
    long_slug = 'a' * 255
    market.response = FakeResponse(payload=item_list(
        [{'item_name': 'Excalibur Umbra', 'url_name': long_slug}]))
    assert ms.sync_market_slugs() == {'ok': True, 'matched': 1}
    assert slugs(ctx)[('Excalibur Umbra', 'warframe')] == long_slug


def test_the_tradable_flag_is_neither_read_nor_changed(ctx, market):
    ctx.execute("UPDATE items SET tradable = 0 WHERE name = 'Braton Prime'")
    ctx.commit()
    ms.sync_market_slugs()
    row = ctx.execute("SELECT tradable, market_url_name FROM items "
                      "WHERE name = 'Braton Prime'").fetchone()
    assert (row['tradable'], row['market_url_name']) == (0, 'braton_prime_set')


# ── Failures change nothing (axis 4: dependency errors) ──────────────────────

def _entry(**fields):
    return item_list(ITEMS + [fields])


FAILURES = [
    ('timeout', requests.Timeout('slow'), 'fetch failed: slow'),
    ('unreachable', requests.ConnectionError('refused'), 'fetch failed: refused'),
    ('http error', FakeResponse(status=503), 'fetch failed: 503 error'),
    ('not json', FakeResponse(text='<html>maintenance</html>'), 'fetch failed: '),
    ('a json list', FakeResponse(payload=[ITEMS]), 'list unreadable: no payload.items list'),
    ('no payload', FakeResponse(payload={'items': ITEMS}),
     'list unreadable: no payload.items list'),
    ('payload null', FakeResponse(payload={'payload': None}),
     'list unreadable: no payload.items list'),
    ('items null', FakeResponse(payload=item_list(None)),
     'list unreadable: no payload.items list'),
    ('items an object', FakeResponse(payload=item_list({'a': ITEMS[0]})),
     'list unreadable: no payload.items list'),
    ('empty list', FakeResponse(payload=item_list([])),
     'list unreadable: the item list is empty'),
    ('entry not an object', FakeResponse(payload=item_list(ITEMS + ['detron'])),
     'list unreadable: entry 12 is not an object'),
    ('no item_name', FakeResponse(payload=_entry(url_name='x')),
     'list unreadable: entry 12 has no item_name'),
    ('item_name a number', FakeResponse(payload=_entry(item_name=7, url_name='x')),
     'list unreadable: entry 12 has no item_name'),
    ('item_name blank', FakeResponse(payload=_entry(item_name='  ', url_name='x')),
     'list unreadable: entry 12 has no item_name'),
    ('no url_name', FakeResponse(payload=_entry(item_name='X')),
     'list unreadable: entry 12 has no valid url_name'),
    ('url_name null', FakeResponse(payload=_entry(item_name='X', url_name=None)),
     'list unreadable: entry 12 has no valid url_name'),
    ('url_name a number', FakeResponse(payload=_entry(item_name='X', url_name=12)),
     'list unreadable: entry 12 has no valid url_name'),
    ('url_name empty', FakeResponse(payload=_entry(item_name='X', url_name='')),
     'list unreadable: entry 12 has no valid url_name'),
    ('url_name a path', FakeResponse(payload=_entry(item_name='X', url_name='../orders')),
     'list unreadable: entry 12 has no valid url_name'),
    ('url_name a query', FakeResponse(payload=_entry(item_name='X', url_name='x?y=1')),
     'list unreadable: entry 12 has no valid url_name'),
    ('url_name a blank', FakeResponse(payload=_entry(item_name='X', url_name='x y')),
     'list unreadable: entry 12 has no valid url_name'),
    ('url_name a newline', FakeResponse(payload=_entry(item_name='X', url_name='x\n')),
     'list unreadable: entry 12 has no valid url_name'),
    ('url_name non-ASCII', FakeResponse(payload=_entry(item_name='X', url_name='ōmen')),
     'list unreadable: entry 12 has no valid url_name'),
    ('url_name too long', FakeResponse(payload=_entry(item_name='X', url_name='a' * 256)),
     'list unreadable: entry 12 has no valid url_name'),
    ('bad entry first', FakeResponse(payload=item_list([{'item_name': 'X'}] + ITEMS)),
     'list unreadable: entry 0 has no valid url_name'),
]


@pytest.mark.parametrize('case, response, error', FAILURES, ids=[c[0] for c in FAILURES])
def test_a_failed_or_malformed_read_changes_no_slug(ctx, market, case, response, error):
    seed(ctx)
    before = slugs(ctx)
    market.response = response
    result = ms.sync_market_slugs()
    assert result['ok'] is False and set(result) == {'ok', 'error'}
    assert result['error'].startswith(error)
    assert slugs(ctx) == before


# ── The write path (axis 7) ──────────────────────────────────────────────────

def test_a_failure_mid_write_leaves_the_old_slugs(ctx, market):
    seed(ctx)
    before = slugs(ctx)
    last = ctx.execute('SELECT MAX(id) FROM items').fetchone()[0]
    ctx.execute(f'''CREATE TRIGGER refuse_last BEFORE UPDATE OF market_url_name ON items
                    WHEN NEW.id = {int(last)} BEGIN SELECT RAISE(ABORT, 'disk full'); END''')
    ctx.commit()
    assert ms.sync_market_slugs() == {'ok': False, 'error': 'write failed: disk full'}
    assert slugs(ctx) == before


def _other_tables(db):
    names = [r['name'] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'items' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: sorted(tuple(r) for r in db.execute(f'SELECT * FROM "{name}"'))
            for name in names}


def test_the_step_writes_the_catalogue_only(app, admin_client, ctx, market):
    admin = ctx.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    braton = ctx.execute("SELECT id FROM items WHERE name = 'Braton Prime'").fetchone()[0]
    ctx.execute('INSERT INTO user_progress (user_id, item_id, in_possession, note) '
                'VALUES (?, ?, 1, ?)', (admin, braton, 'kept'))
    ctx.execute('INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
                (admin, braton))
    ctx.execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                "VALUES (?, 30, '2026-11-09T10:00:00')", (braton,))
    ctx.execute("INSERT INTO settings (user_id, key, value) VALUES (?, 'buy_threshold_plat', '9')",
                (admin,))
    ctx.commit()
    before = _other_tables(ctx)
    assert ms.sync_market_slugs()['ok'] is True
    assert _other_tables(ctx) == before


# ── The full sync and what a matched item gains ──────────────────────────────

@pytest.fixture
def quick_sync(monkeypatch):
    """`run_sync` with every catalogue, image and Prime Resurgence pass replaced: one row each."""
    for name in ('_sync_warframes', '_sync_weapons', '_sync_kitgun_chambers', '_sync_companions',
                 '_sync_archwings', '_sync_k_drives', '_sync_amps', '_sync_mods', '_sync_arcanes',
                 '_sync_resources', '_sync_component_ingredients', '_sync_drop_sources'):
        monkeypatch.setattr(wiki_sync, name, lambda *args: 1)
    monkeypatch.setattr(wiki_sync, '_backfill_null_item_images', lambda db, cats: 0)
    monkeypatch.setattr(wiki_sync, '_sync_wiki_images_for_missing', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_item_images', lambda db: (0, 0))
    monkeypatch.setattr(wiki_sync, '_sync_component_wiki_images', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_component_images', lambda db: (0, 0))
    monkeypatch.setattr(wiki_sync, 'sync_resurgence', lambda: {'ok': False, 'error': 'mocked'})


def test_the_full_sync_reports_the_slug_step_and_a_failed_read_is_no_failed_sync(
        app, ctx, market, quick_sync):
    result = wiki_sync.run_sync(app)
    assert result['ok'] is True and result['market_slugs'] == {'ok': True, 'matched': MATCHED}
    market.response = requests.Timeout('slow')
    result = wiki_sync.run_sync(app)
    assert result['ok'] is True and result['total'] == 12
    assert result['market_slugs'] == {'ok': False, 'error': 'fetch failed: slow'}
    assert slugs(ctx)[('Braton Prime', 'primary')] == 'braton_prime_set'


def test_a_matched_item_gets_its_price_and_link_after_a_sync(app, admin_client, ctx, market,
                                                             quick_sync):
    ids = {(r['name'], r['category']): r['id'] for r in ctx.execute('SELECT * FROM items')}
    braton, umbra = ids[('Braton Prime', 'primary')], ids[('Excalibur Umbra', 'warframe')]
    assert admin_client.get(f'/market/price/{braton}').status_code == 404
    assert wiki_sync.run_sync(app)['ok'] is True

    response = admin_client.get(f'/market/price/{braton}')
    assert response.status_code == 200
    assert (response.get_json()['ok'], response.get_json()['platinum']) == (True, 25)
    assert market.calls[-1][0] == 'https://api.warframe.market/v1/items/braton_prime_set/orders'
    assert admin_client.get(f'/market/price/{umbra}').status_code == 404

    assert admin_client.post('/market/watch', json={'item_id': braton}).status_code == 200
    page = admin_client.get('/market/').get_data(as_text=True)
    assert 'https://warframe.market/items/braton_prime_set' in page


# ── The sync page (axis 9) ───────────────────────────────────────────────────

def test_the_sync_page_shows_the_slug_steps_result_for_both_outcomes(admin_client):
    page = admin_client.get('/sync/').get_data(as_text=True)
    assert "var ms = r.market_slugs || {};" in page
    assert "msg += ' · Market listings: ' + ms.matched + ' items matched';" in page
    assert ("msg += ' · Market listings not updated (' + (ms.error || 'not read') +\n"
            "                 '); the last ones are kept.';") in page.replace('\r\n', '\n')
