"""The dashboard's market watch panel shows the caller's watchlist with its cached prices.

The panel reads the price cache only: loading the dashboard requests no price. Each row is
the item's name, its cached price (a dash when none), a stale mark with the fetch time when
the price is older than the user's cache setting, and the item's warframe.market link when it
has a listing. At most ten rows, by name, with a link to the market page; an empty watchlist
says so. The watchlist is per user: another user's items and prices never show.

Network refused (tests/conftest.py); the price request function is replaced by a recorder.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest

from models.db import get_db
from models.item import Item
from models.user import User
from services import market_api
from services.market_api import is_fresh, watchlist_prices

FRESH_AGE = timedelta(hours=1)    # younger than the default 6 h
STALE_AGE = timedelta(hours=7)    # older than the default 6 h


def ago(age):
    return (datetime.utcnow() - age).isoformat()


def label(fetched_at):
    return datetime.fromisoformat(fetched_at).strftime('%Y-%m-%d %H:%M') + ' UTC'


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
def users(app, admin_client):
    with app.app_context():
        admin = User.get_by_username('admin').id
        alice = User.create('alice', password='pw-alice-1').id
    return {'admin': admin, 'alice': alice}


def add_item(name, market_url_name=None, owner=None):
    """A tradable catalogue item; owned by `owner` so the dashboard's farm list skips it."""
    Item.upsert(name, 'primary', source='standard')
    db = get_db()
    db.execute("UPDATE items SET market_url_name = ? WHERE name = ? AND category = 'primary'",
               (market_url_name, name))
    item_id = db.execute("SELECT id FROM items WHERE name = ? AND category = 'primary'",
                         (name,)).fetchone()[0]
    if owner is not None:
        db.execute('INSERT INTO user_progress (user_id, item_id, in_possession) VALUES (?, ?, 1)',
                   (owner, item_id))
    db.commit()
    return item_id


def watch(user_id, item_id):
    get_db().execute('INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
                     (user_id, item_id))
    get_db().commit()


def cache(item_id, price, fetched_at):
    get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                     'VALUES (?, ?, ?)', (item_id, price, fetched_at))
    get_db().commit()


def panel(client):
    response = client.get('/dashboard')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    start = html.index('id="market-watch"')
    return html[start:html.index('<script', start)]


def rows(panel_html):
    return re.findall(r'<tr class="market-watch-row">(.*?)</tr>', panel_html, re.S)


def cell(row_html, cls):
    match = re.search(rf'class="{cls}"[^>]*>(.*?)</td>', row_html, re.S)
    return match.group(1) if match else None


def text(fragment):
    return ' '.join(re.sub(r'<[^>]+>', ' ', fragment).split())


# ---------------------------------------------------------------------------
# The panel's states: cache state x listing
# ---------------------------------------------------------------------------

# (case id, cached price, fetch age or None for no cache row, expected price text, stale)
CACHE_STATES = [
    ('no row', None, None, '—', False),
    ('fresh', 120, FRESH_AGE, '120p', False),
    ('stale', 95, STALE_AGE, '95p', True),
    ('price NULL fresh', None, FRESH_AGE, '—', False),
    ('price NULL stale', None, STALE_AGE, '—', True),
    ('price 0 fresh', 0, FRESH_AGE, '0p', False),
]
LISTINGS = [('with listing', 'braton_prime_set'), ('without listing', None)]


@pytest.mark.parametrize('listing', LISTINGS, ids=lambda l: l[0])
@pytest.mark.parametrize('state', CACHE_STATES, ids=lambda s: s[0])
def test_a_watched_item_shows_its_cached_price_stale_mark_and_link(
        app, admin_client, users, requests_made, state, listing):
    _, price, age, want_price, want_stale = state
    _, url_name = listing
    with app.app_context():
        item = add_item('Braton Prime Set', url_name, owner=users['admin'])
        watch(users['admin'], item)
        fetched_at = ago(age) if age is not None else None
        if fetched_at:
            cache(item, price, fetched_at)

    [row] = rows(panel(admin_client))
    assert text(cell(row, 'market-watch-name')) == 'Braton Prime Set'
    price_text = text(cell(row, 'market-watch-price'))
    if want_stale:
        assert price_text == f'{want_price} stale ({label(fetched_at)})'
        assert 'market-watch-stale' in row
    else:
        assert price_text == want_price
        assert 'market-watch-stale' not in row
    if url_name:
        assert f'href="https://warframe.market/items/{url_name}"' in row
    else:
        assert 'market-watch-link' not in row
    assert requests_made == []


def test_the_users_cache_setting_decides_stale(app, admin_client, users, requests_made):
    """A two-hour-old price is fresh at the default 6 h and stale at a 1 h setting."""
    with app.app_context():
        item = add_item('Soma Prime Set', 'soma_prime_set', owner=users['admin'])
        watch(users['admin'], item)
        cache(item, 40, ago(timedelta(hours=2)))
    assert 'market-watch-stale' not in panel(admin_client)
    with app.app_context():
        get_db().execute("INSERT INTO settings (user_id, key, value) "
                         "VALUES (?, 'market_cache_hours', '1')", (users['admin'],))
        get_db().commit()
    assert 'market-watch-stale' in panel(admin_client)
    assert requests_made == []


def test_an_empty_watchlist_says_so_and_points_to_the_market_page(app, admin_client, users,
                                                                    requests_made):
    html = panel(admin_client)
    assert rows(html) == []
    assert 'Your watchlist is empty' in html
    assert 'href="/market/"' in html
    assert 'All watched items' not in html


def test_eleven_watched_items_show_the_first_ten_by_name_and_the_link(app, admin_client, users,
                                                                     requests_made):
    names = [f'Item {n:02d}' for n in range(1, 12)]
    with app.app_context():
        for name in reversed(names):
            item = add_item(name, name.lower().replace(' ', '_'), owner=users['admin'])
            watch(users['admin'], item)
            cache(item, 10, ago(FRESH_AGE))
    html = panel(admin_client)
    shown = [text(cell(r, 'market-watch-name')) for r in rows(html)]
    assert shown == names[:10]
    assert re.search(r'<a href="/market/"[^>]*id="market-watch-all">All watched items</a>', html)
    assert requests_made == []


def test_another_users_watchlist_and_prices_never_show(app, admin_client, users, requests_made):
    with app.app_context():
        mine = add_item('Lex Prime Set', 'lex_prime_set', owner=users['admin'])
        theirs = add_item('Kronen Prime Set', 'kronen_prime_set', owner=users['admin'])
        watch(users['admin'], mine)
        watch(users['alice'], theirs)
        cache(mine, 31, ago(FRESH_AGE))
        cache(theirs, 777, ago(FRESH_AGE))
    html = panel(admin_client)
    assert [text(cell(r, 'market-watch-name')) for r in rows(html)] == ['Lex Prime Set']
    assert 'Kronen' not in html and '777' not in html and 'kronen_prime_set' not in html


def test_a_user_whose_watchlist_is_empty_sees_nobody_elses(app, admin_client, users,
                                                         requests_made):
    with app.app_context():
        theirs = add_item('Kronen Prime Set', 'kronen_prime_set', owner=users['admin'])
        watch(users['alice'], theirs)
        cache(theirs, 777, ago(FRESH_AGE))
    html = panel(admin_client)
    assert rows(html) == []
    assert 'Your watchlist is empty' in html and 'Kronen' not in html


def test_a_hostile_name_and_listing_render_as_text(app, admin_client, users, requests_made):
    name = '<script>alert("x")</script> & \'q\''
    url_name = 'a"><img src=x onerror=alert(1)>'
    with app.app_context():
        item = add_item(name, url_name, owner=users['admin'])
        watch(users['admin'], item)
        cache(item, 5, ago(FRESH_AGE))
    html = panel(admin_client)
    assert '<script>alert' not in html and '<img src=x' not in html
    assert ('&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt; &amp; &#39;q&#39;'
            in cell(rows(html)[0], 'market-watch-name'))
    assert 'href="https://warframe.market/items/a&#34;&gt;&lt;img src=x' in html


def test_loading_the_dashboard_requests_no_price_for_any_watched_item(app, admin_client, users,
                                                                     requests_made):
    """Every cache state at once, the stale and missing ones included: still no request."""
    with app.app_context():
        for n, (_, price, age, _, _) in enumerate(CACHE_STATES):
            item = add_item(f'Watched {n}', f'watched_{n}', owner=users['admin'])
            watch(users['admin'], item)
            if age is not None:
                cache(item, price, ago(age))
    assert len(rows(panel(admin_client))) == len(CACHE_STATES)
    assert requests_made == []


# ---------------------------------------------------------------------------
# The service and the market page that shares it
# ---------------------------------------------------------------------------

def test_the_service_returns_only_the_callers_rows_in_name_order(app, users, requests_made):
    with app.app_context():
        b = add_item('Bo Prime Set', 'bo_prime_set')
        a = add_item('Akbolto Prime Set', 'akbolto_prime_set')
        c = add_item('Cernos Prime Set', None)
        for item in (b, a, c):
            watch(users['admin'], item)
        watch(users['alice'], b)
        cache(a, 12, ago(FRESH_AGE))
        cache(b, 34, ago(STALE_AGE))
        got = watchlist_prices(users['admin'], 6)
        limited = watchlist_prices(users['admin'], 6, limit=2)
        alices = watchlist_prices(users['alice'], 6)
    assert [(r['name'], r['platinum_price'], r['fresh'], r['stale'], r.get('market_url'))
            for r in got] == [
        ('Akbolto Prime Set', 12, True, False,
         'https://warframe.market/items/akbolto_prime_set'),
        ('Bo Prime Set', 34, False, True, 'https://warframe.market/items/bo_prime_set'),
        ('Cernos Prime Set', None, False, False, None),
    ]
    assert [r['name'] for r in limited] == ['Akbolto Prime Set', 'Bo Prime Set']
    assert [r['name'] for r in alices] == ['Bo Prime Set']
    assert requests_made == []


NOW = datetime(2026, 11, 9, 12, 0, 0)


@pytest.mark.parametrize('fetched_at,hours,want', [
    (None, 6, False),
    ('', 6, False),
    ('not a time', 6, False),
    ('2026-11-09T11:00:00', 6, True),
    ('2026-11-09 11:00:00', 6, True),                 # the MSSQL default's spelling
    ('2026-11-09T06:00:01', 6, True),                 # one second inside the window
    ('2026-11-09T06:00:00', 6, False),                # exactly the window: stale
    ('2026-11-09T11:59:59', 0, False),                # a 0 h setting: never fresh
    ('2000-01-01T00:00:00', 9223372036854775807, True),   # the largest setting: no overflow
    ('2026-11-09T12:30:00+02:00', 2, True),           # an offset counts: 10:30 UTC
    ('2026-11-09T09:30:00+00:00', 2, False),
], ids=['none', 'empty', 'garbage', 'iso', 'space', 'inside', 'boundary', 'zero-hours',
        'max-hours', 'offset-fresh', 'offset-stale'])
def test_freshness_by_the_cache_setting(fetched_at, hours, want):
    assert is_fresh(fetched_at, hours, now=NOW) is want


def test_freshness_reads_an_aware_time_as_utc():
    stamp = datetime(2026, 11, 9, 11, 0, tzinfo=timezone.utc).isoformat()
    assert is_fresh(stamp, 2, now=NOW) is True


def test_the_market_page_still_lists_every_watched_item_with_its_cache(app, admin_client, users,
                                                                       requests_made):
    names = [f'Item {n:02d}' for n in range(1, 12)]
    with app.app_context():
        for name in reversed(names):
            item = add_item(name, name.lower().replace(' ', '_'))
            watch(users['admin'], item)
            cache(item, 10, '2026-11-01T08:00:00')
        theirs = add_item('Kronen Prime Set', 'kronen_prime_set')
        watch(users['alice'], theirs)
    response = admin_client.get('/market/')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    found = re.findall(r'<td class="white-text">\s*(.*?)\s*</td>', html, re.S)
    assert found == names
    assert html.count('2026-11-01T08:00:00') == 11
    assert 'Kronen' not in html
    assert 'href="https://warframe.market/items/item_01"' in html
    assert requests_made == []
