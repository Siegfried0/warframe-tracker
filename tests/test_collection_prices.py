"""Market prices in the collection browser: the price column and the card price badge.

Every item on the market (tradable, or with a market slug) shows its price in both views (the
table's "Price" column and the right-hand side of its card's head, beside the name) on every
category tab: the cached price or `—`
when none is cached, an "old" mark with the fetch time when the cache row is older than the
user's `market_cache_hours` (an unreadable fetch time counts as old), and a warframe.market
link (new tab, `rel="noopener"`) when the item has a market slug. With a slug an old or missing
price has a refresh button (`GET /market/price/<id>`), a fresh one none; without a slug there is
nothing to request, so no button. An item not on the market shows no price.

Loading a page requests no price: one cache-only query over the page's on-market items reads
their prices, whatever the page size, and nothing is written. Network refused
(tests/conftest.py); `requests.get` is also patched to fail the test if called.
"""
import os
import re
from datetime import datetime, timedelta

import pytest

from models import db as db_module
from models.db import get_db
from models.item import Item
from models.user import User
from services import market_api
from services.market_api import page_prices

CATEGORIES = ('warframe', 'mod', 'arcane')
VIEWS = ('cards', 'table')

FRESH = timedelta(minutes=30)
STALE = timedelta(hours=7)          # the default cache setting is 6 hours

STALE_AT = (datetime.utcnow() - STALE).replace(microsecond=0)


def ago(age):
    return (datetime.utcnow() - age).isoformat()


# name: (tradable, slug, cache row as (price, fetched_at) or None,
#        expected (on market, shown text, stale mark text or None, has slug))
STALE_TEXT = 'old (' + STALE_AT.strftime('%Y-%m-%d %H:%M') + ' UTC)'
CASES = {
    'Off Market':        (False, None, None, (False, None, None, False)),
    'Off Market Cached': (False, None, (50, ago(FRESH)), (False, None, None, False)),
    'Tradable No Slug':  (True, None, None, (True, '—', None, False)),
    'Tradable Empty':    (True, '', (40, ago(FRESH)), (True, '40p', None, False)),
    'Slug Only':         (False, 'slug_only', None, (True, '—', None, True)),
    'Uncached':          (True, 'uncached', None, (True, '—', None, True)),
    'Fresh':             (True, 'fresh', (120, ago(FRESH)), (True, '120p', None, True)),
    'Stale':             (True, 'stale', (120, STALE_AT.isoformat()), (True, '120p', STALE_TEXT, True)),
    'Fresh Null':        (True, 'fresh_null', (None, ago(FRESH)), (True, '—', None, True)),
    'Stale Null':        (True, 'stale_null', (None, STALE_AT.isoformat()), (True, '—', STALE_TEXT, True)),
    'Zero':              (True, 'zero', (0, ago(FRESH)), (True, '0p', None, True)),
    'Large':             (True, 'large', (2147483647, ago(FRESH)), (True, '2147483647p', None, True)),
    'Unreadable Time':   (True, 'unreadable', (75, 'not a time'), (True, '75p', 'old (not a time)', True)),
}


@pytest.fixture
def requests_made(monkeypatch):
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        raise AssertionError(f'unexpected market request: {url}')

    monkeypatch.setattr(market_api.requests, 'get', fake_get)
    return calls


def add_item(name, category, tradable=True, slug=None):
    Item.upsert(name, category, source='standard', tradable=tradable)
    db = get_db()
    db.execute('UPDATE items SET market_url_name = ? WHERE name = ? AND category = ?',
               (slug, name, category))
    db.commit()
    return db.execute('SELECT id FROM items WHERE name = ? AND category = ?',
                      (name, category)).fetchone()['id']


def cache(item_id, price, fetched_at):
    get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                     'VALUES (?, ?, ?)', (item_id, price, fetched_at))
    get_db().commit()


@pytest.fixture
def catalogue(app, admin_client):
    """Every case in every tested category; returns {(category, name): id}."""
    ids = {}
    with app.app_context():
        for category in CATEGORIES:
            for name, (tradable, slug, row, _expected) in CASES.items():
                iid = add_item(f'{name} {category}', category, tradable,
                               f'{slug}_{category}' if slug else slug)
                if row is not None:
                    cache(iid, *row)
                ids[category, name] = iid
    return ids


def view_chunks(html, view):
    """{item id: the markup of its card (cards) or row (table)} in one view."""
    cards_at = html.index('id="collection-cards"')
    table_at = html.index('id="collection-table"')
    section = html[cards_at:table_at] if view == 'cards' else html[table_at:html.index('</table>', table_at)]
    parts = section.split('data-item-id="')[1:]
    return {int(part[:part.index('"')]): part for part in parts}


def div_inner(markup, cls):
    """The inner markup of the first `<div class="cls">` in markup (nested divs balanced), or
    None when there is none."""
    start = re.search(rf'<div class="{cls}">', markup)
    if start is None:
        return None
    depth, at = 1, start.end()
    for tag in re.finditer(r'<(/?)div\b', markup[at:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return markup[at:at + tag.start()]
    raise AssertionError(f'unclosed <div class="{cls}">')


def price_markup(chunk, view):
    """The price markup of one card or row, or None when it shows no price. A card shows it in
    its head's right-hand column."""
    if view == 'cards':
        head = div_inner(chunk, 'coll-card-head')
        assert head is not None
        return div_inner(head, 'coll-card-market')
    cell = re.search(r'<td class="wf-price-cell">(.*?)</td>', chunk, re.S).group(1)
    return cell if 'class="wf-price"' in cell else None


def text_of(markup, cls):
    match = re.search(rf'class="{cls}[^"]*"[^>]*>([^<]*)<', markup)
    return match.group(1) if match else None


def page(client, category, **params):
    response = client.get(f'/collection/{category}', query_string=params)
    assert response.status_code == 200
    return response.get_data(as_text=True)


# ── The matrix: case × view × tab ─────────────────────────────────────────────

@pytest.mark.parametrize('view', VIEWS)
@pytest.mark.parametrize('category', CATEGORIES)
def test_each_item_shows_its_cached_price_stale_mark_and_link(
        admin_client, catalogue, requests_made, category, view):
    chunks = view_chunks(page(admin_client, category), view)
    for name, (_tradable, slug, row, expected) in CASES.items():
        listed, shown, stale, has_slug = expected
        iid = catalogue[category, name]
        markup = price_markup(chunks[iid], view)
        where = (category, view, name)
        if not listed:
            assert markup is None, where
            assert 'wf-price' not in chunks[iid].split('<td class="wf-price-cell">')[0], where
            if view == 'table':
                cell = re.search(r'<td class="wf-price-cell">(.*?)</td>', chunks[iid], re.S).group(1)
                assert '—' in cell, where
            continue
        assert markup is not None, where
        assert f'data-item="{iid}"' in markup, where
        assert text_of(markup, 'wf-price-value') == shown, where
        assert text_of(markup, 'wf-price-old') == stale, where
        link = re.search(r'<a href="([^"]*)" target="_blank" rel="noopener" class="wf-price-link', markup)
        refresh = re.search(r'<button type="button" class="wf-price-refresh" data-item="(\d+)"'
                            r'[^>]*aria-label="([^"]*)"', markup)
        cached = row is not None
        if has_slug:
            assert link.group(1) == f'https://warframe.market/items/{slug}_{category}', where
            if cached and stale is None:            # fresh: no button
                assert refresh is None, where
            else:
                assert int(refresh.group(1)) == iid, where
                assert refresh.group(2) == (f'Refresh the price of {name} {category} '
                                            'from warframe.market'), where
        else:
            assert link is None and refresh is None, where
    assert requests_made == []


@pytest.mark.parametrize('view', VIEWS)
def test_another_users_cache_setting_does_not_decide_stale(app, admin_client, catalogue, view):
    """The caller's own `market_cache_hours` decides: at 1 hour a 90-minute price is stale."""
    with app.app_context():
        admin = User.get_by_username('admin').id
        iid = add_item('Ninety Minutes', 'warframe', True, 'ninety')
        cache(iid, 30, ago(timedelta(minutes=90)))
        get_db().execute("INSERT INTO settings (user_id, key, value) "
                         "VALUES (?, 'market_cache_hours', '1')", (admin,))
        get_db().commit()
    markup = price_markup(view_chunks(page(admin_client, 'warframe'), view)[iid], view)
    assert text_of(markup, 'wf-price-old').startswith('old (')


def test_the_table_has_a_price_column_on_every_tab(admin_client, catalogue):
    for category in CATEGORIES:
        html = page(admin_client, category)
        head = html[html.index('id="collection-table"'):html.index('</thead>')]
        assert '<th>Price</th>' in head, category


def test_an_empty_page_renders_without_prices(admin_client, requests_made):
    html = page(admin_client, 'warframe')
    assert 'No items found.' in html
    assert 'class="wf-price"' not in html
    assert requests_made == []


def test_a_hostile_fetch_time_renders_as_text(app, admin_client):
    with app.app_context():
        iid = add_item('Hostile Time', 'warframe', True, 'hostile')
        cache(iid, 10, '<b>&"x')
    html = page(admin_client, 'warframe')
    assert '<b>&"x' not in html
    assert 'old (&lt;b&gt;&amp;&#34;x)' in html


# ── The card's head: name and labels on the left, the price on the right ──────

# tab: (source, mod type, mod polarity, the label lines the card shows)
HEAD_TABS = {
    'warframe':  ('prime', None, None, ['prime']),
    'primary':   ('prime', None, None, ['prime']),          # a prime weapon's tab
    'companion': ('standard', None, None, ['standard']),
    'mod':       ('standard', 'Warframe', 'Madurai', ['Warframe', 'Madurai · R5']),
    'arcane':    ('eidolon', None, None, ['Eidolon']),
    'k-drive':   (None, None, None, []),                 # no source label
}


def add_labelled(name, category, on_market, source):
    _src, mod_type, polarity, _labels = HEAD_TABS[category]
    Item.upsert(name, category, source=source, tradable=on_market, mod_type=mod_type,
                mod_polarity=polarity, mod_max_rank=5 if polarity else None)
    db = get_db()
    db.execute('UPDATE items SET market_url_name = ? WHERE name = ? AND category = ?',
               ('head_slug' if on_market else None, name, category))
    db.commit()
    return db.execute('SELECT id FROM items WHERE name = ? AND category = ?',
                      (name, category)).fetchone()['id']


def head_parts(chunk):
    """(the head's left column, its price column or None, the card markup after the head)."""
    head = div_inner(chunk, 'coll-card-head')
    assert head is not None
    after = chunk[chunk.index(head) + len(head):]
    return div_inner(head, 'coll-card-title'), div_inner(head, 'coll-card-market'), after


def labels_in(markup):
    return re.findall(r'<p class="grey-text coll-card-source"[^>]*>([^<]*)</p>', markup)


@pytest.mark.parametrize('on_market', [True, False], ids=['on-market', 'off-market'])
@pytest.mark.parametrize('category', list(HEAD_TABS))
def test_a_card_head_holds_the_name_labels_and_price_on_every_tab(
        app, admin_client, requests_made, category, on_market):
    source, _type, _polarity, labels = HEAD_TABS[category]
    with app.app_context():
        iid = add_labelled('Head Item', category, on_market, source)
        cache(iid, 12345, ago(FRESH))
    html = page(admin_client, category)
    chunk = view_chunks(html, 'cards')[iid]
    title, market, after = head_parts(chunk)
    assert '<p class="white-text coll-card-name" title="Head Item">Head Item</p>' in title
    assert labels_in(title) == labels
    for text in labels:                               # a truncated label keeps its full text
        assert f'title="{text}">{text}</p>' in title
    assert labels_in(after) == []                      # no label line outside the head
    assert 'coll-card-price' not in html
    assert '<div class="coll-card-icons">' in after   # the icon row stays below the head
    if category == 'arcane':                          # the rank bar too, full width
        assert 'arc-rank-btn' in after and 'arc-rank-btn' not in title
    if on_market:
        assert text_of(market, 'wf-price-value') == '12345p'
        assert 'wf-price-link' in market
        assert 'wf-price' not in after
    else:
        assert market is None
        assert 'wf-price' not in chunk
    assert requests_made == []


@pytest.mark.parametrize('on_market', [True, False], ids=['on-market', 'off-market'])
def test_a_card_without_a_label_line_still_has_its_head(app, admin_client, on_market):
    with app.app_context():
        iid = add_labelled('No Label', 'warframe', on_market, None)
    title, market, _after = head_parts(view_chunks(page(admin_client, 'warframe'), 'cards')[iid])
    assert 'title="No Label">No Label</p>' in title
    assert labels_in(title) == []
    assert (market is not None) == on_market


@pytest.mark.parametrize('name, shown', [
    ('Excalibur Umbra Prime With A Name Far Longer Than Any Card Is Wide',
     'Excalibur Umbra Prime With A Name Far Longer Than Any Card Is Wide'),
    ('Fish <b> & "Chips"', 'Fish &lt;b&gt; &amp; &#34;Chips&#34;'),
])
def test_a_long_or_hostile_name_sits_escaped_in_the_head_beside_its_price(
        app, admin_client, name, shown):
    with app.app_context():
        iid = add_labelled(name, 'warframe', True, 'prime')
        cache(iid, 120, STALE_AT.isoformat())
    html = page(admin_client, 'warframe')
    title, market, _after = head_parts(view_chunks(html, 'cards')[iid])
    assert f'<p class="white-text coll-card-name" title="{shown}">{shown}</p>' in title
    assert '<b>' not in title
    assert text_of(market, 'wf-price-old') == STALE_TEXT
    refresh = re.search(r'class="wf-price-refresh"[^>]*aria-label="([^"]*)"', market)
    assert refresh.group(1) == f'Refresh the price of {shown} from warframe.market'


def css_rule(css, selector):
    """The declarations of the stylesheet's rule for exactly this selector."""
    match = re.search(r'(?m)^' + re.escape(selector) + r'\s*\{([^}]*)\}', css)
    assert match, selector
    body = re.sub(r'/\*.*?\*/', '', match.group(1), flags=re.S)
    return dict(re.findall(r'([\w-]+)\s*:\s*([^;]+);', body))


def test_the_stylesheet_lays_the_head_out_as_a_row_with_a_reserved_height(app):
    with open(os.path.join(app.root_path, 'static', 'css', 'app.css'), encoding='utf-8') as fh:
        css = fh.read()
    head = css_rule(css, '.coll-card-head')
    assert head['display'] == 'flex'
    assert head.get('flex-direction', 'row') == 'row'
    assert re.fullmatch(r'[\d.]+(rem|em|px)', head['min-height'])
    assert float(re.match(r'[\d.]+', head['min-height']).group()) > 0
    title = css_rule(css, '.coll-card-title')
    assert title['min-width'].strip() == '0'                 # lets the name truncate
    label = css_rule(css, '.coll-card-head .coll-card-source')
    assert label['white-space'] == 'nowrap'                  # a label never wraps the head
    assert label['overflow'] == 'hidden'
    assert label['text-overflow'] == 'ellipsis'
    market = css_rule(css, '.coll-card-market')
    shrink = market.get('flex-shrink') or market['flex'].split()[1]
    assert shrink == '0'
    assert market['text-align'] == 'right'
    assert market['max-width'].endswith('%')
    assert '.coll-card-price' not in css


# ── Page load: no market request, one cache query, no write ──────────────────

@pytest.fixture
def statements(monkeypatch):
    """Every SQL statement run on a connection opened while the fixture is active."""
    seen = []
    real = db_module.get_connection

    def traced():
        conn = real()
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(db_module, 'get_connection', traced)
    return seen


@pytest.mark.parametrize('per_page', [24, 200])
def test_loading_a_page_reads_the_cache_once_and_requests_no_price(
        app, admin_client, requests_made, statements, per_page):
    with app.app_context():
        for n in range(150):
            iid = add_item(f'Item {n:03d}', 'primary', True, f'item_{n:03d}')
            if n % 2:
                cache(iid, n, ago(STALE))
    del statements[:]
    html = page(admin_client, 'primary', per_page=per_page)
    reads = [s for s in statements if 'FROM market_cache' in s]
    assert len(reads) == 1
    assert reads[0].startswith('SELECT item_id, platinum_price, fetched_at FROM market_cache')
    assert not any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE'))
                   for s in statements)
    assert html.count('class="wf-price"') == 2 * min(per_page, 150)
    assert requests_made == []


def test_a_page_with_no_item_on_the_market_reads_no_price(app, admin_client, statements):
    with app.app_context():
        add_item('Untradable', 'primary', False, None)
    del statements[:]
    page(admin_client, 'primary')
    assert [s for s in statements if 'FROM market_cache' in s] == []


class RecordingDb:
    """Stands in for either backend's connection: records each statement and its parameters."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, list(params)))
        return self

    def fetchall(self):
        return []


def test_the_cache_read_binds_every_id_as_a_parameter(monkeypatch):
    """Plain SQL for both backends: `IN (?, …)` with one placeholder per distinct id (200, the
    largest page), the ids bound as parameters, never written into the statement."""
    recorder = RecordingDb()
    monkeypatch.setattr(market_api, 'get_db', lambda: recorder)
    rows = [{'id': 1000 + n, 'on_market': 1, 'market_url_name': None} for n in range(200)]
    rows.append({'id': 1000, 'on_market': 1, 'market_url_name': None})   # a repeated id
    rows.append({'id': 9999, 'on_market': 0, 'market_url_name': None})   # not on the market
    prices = page_prices(rows, 6)
    assert len(recorder.calls) == 1
    sql, params = recorder.calls[0]
    assert sql == ('SELECT item_id, platinum_price, fetched_at FROM market_cache WHERE item_id IN ('
                   + ','.join(['?'] * 200) + ')')
    assert params == [1000 + n for n in range(200)]
    assert 9999 not in prices and len(prices) == 200
    assert prices[1000] == {'platinum': None, 'cached': False, 'stale': False,
                            'fetched_label': None, 'failed': False, 'market_slug': '',
                            'market_url': None, 'refresh': False}


def test_no_item_on_the_market_means_no_query(monkeypatch):
    recorder = RecordingDb()
    monkeypatch.setattr(market_api, 'get_db', lambda: recorder)
    assert page_prices([{'id': 1, 'on_market': 0}], 6) == {}
    assert page_prices([], 6) == {}
    assert recorder.calls == []


# ── The refresh route ─────────────────────────────────────────────────────────

def test_a_refresh_writes_the_cache_and_the_next_load_shows_it_fresh(app, admin_client, monkeypatch):
    with app.app_context():
        iid = add_item('Refreshed', 'warframe', True, 'refreshed')
        cache(iid, 90, STALE_AT.isoformat())
    monkeypatch.setattr(market_api, 'fetch_lowest_price', lambda slug: 77 if slug == 'refreshed' else None)
    answer = admin_client.get(f'/market/price/{iid}').get_json()
    assert (answer['ok'], answer['platinum'], answer['from_cache']) == (True, 77, False)
    for view in VIEWS:
        markup = price_markup(view_chunks(page(admin_client, 'warframe'), view)[iid], view)
        assert text_of(markup, 'wf-price-value') == '77p'
        assert text_of(markup, 'wf-price-old') is None
        assert 'wf-price-refresh' not in markup


def test_refreshing_an_item_without_a_slug_answers_404_and_writes_nothing(app, admin_client,
                                                                           requests_made):
    with app.app_context():
        iid = add_item('No Slug', 'warframe', True, None)
    response = admin_client.get(f'/market/price/{iid}')
    assert response.status_code == 404
    assert response.get_json()['ok'] is False
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM market_cache').fetchone()[0] == 0
    assert requests_made == []
