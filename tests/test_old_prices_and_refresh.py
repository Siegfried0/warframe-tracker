"""Old market prices stay shown with their age, and each old or missing price has a refresh button.

Every place that shows a price (the market page, the dashboard's Market Watch panel and its
Farm next list, the farming plan's goal price, the collection browser's cards and table) shows
the cached price of any age: `—` only when none is cached or the market had no sell order in
game, `0p` for a cached zero. A price older than the user's `market_cache_hours` (an unreadable
fetch time included), or kept after a failed request, carries an "old" mark with its fetch time.
Beside an old or missing price of an item with a market slug a refresh button (the sprite's
`sync` icon in a `<button>`, named for the item) asks `GET /market/price/<id>` for that one item;
a fresh price has none. One page script (static/js/app.js) handles every button: it updates
every display of the item, ignores a click while that item's request runs, keeps the shown
price on any failure and inserts values from an answer as text only.

Loading a page requests no price (`requests.get` is patched to fail the test if called), except
the goal plan's one `get_price()` per open. Network refused (tests/conftest.py).
"""
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

from models.db import get_db
from models.item import Item
from models.user import User
from services import market_api
from services.farming_service import add_farming_goal
from services.market_api import price_view

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / 'app' / 'static' / 'js' / 'app.js'

FRESH = timedelta(minutes=30)
OLD = timedelta(hours=7)            # the default cache setting is 6 hours
OLD_AT = (datetime.utcnow() - OLD).replace(microsecond=0).isoformat()
OLD_LABEL = datetime.fromisoformat(OLD_AT).strftime('%Y-%m-%d %H:%M') + ' UTC'


def ago(age):
    return (datetime.utcnow() - age).isoformat()


# (case id, cache row as (price, fetched_at) or None, shown text, old mark text or None, button)
STATES = [
    ('none', None, '—', None, True),
    ('fresh', (120, ago(FRESH)), '120p', None, False),
    ('old', (95, OLD_AT), '95p', f'old ({OLD_LABEL})', True),
    ('NULL fresh', (None, ago(FRESH)), '—', None, False),
    ('NULL old', (None, OLD_AT), '—', f'old ({OLD_LABEL})', True),
    ('0p fresh', (0, ago(FRESH)), '0p', None, False),
    ('0p old', (0, OLD_AT), '0p', f'old ({OLD_LABEL})', True),
    ('large fresh', (2147483647, ago(FRESH)), '2147483647p', None, False),
    ('unreadable fetch time', (75, 'not a time'), '75p', 'old (not a time)', True),
]
PLACES = ['market page', 'Market Watch', 'Farm next', 'collection cards', 'collection table']
NAME = 'Braton Prime'


@pytest.fixture
def requests_made(monkeypatch):
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


def add_item(name, slug='braton_prime', tradable=True):
    Item.upsert(name, 'primary', source='standard', tradable=tradable)
    db = get_db()
    db.execute("UPDATE items SET market_url_name = ? WHERE name = ? AND category = 'primary'",
               (slug, name))
    db.commit()
    return db.execute("SELECT id FROM items WHERE name = ? AND category = 'primary'",
                      (name,)).fetchone()[0]


def cache(item_id, price, fetched_at):
    get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                     'VALUES (?, ?, ?)', (item_id, price, fetched_at))
    get_db().commit()


def watch(user_id, item_id):
    get_db().execute('INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
                     (user_id, item_id))
    get_db().commit()


def html_of(client, path):
    response = client.get(path)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def between(html, start, end):
    at = html.index(start)
    return html[at:html.index(end, at)]


def section(client, place):
    """The markup of one place that shows prices."""
    if place == 'market page':
        return html_of(client, '/market/')
    if place in ('Market Watch', 'Farm next'):
        html = html_of(client, '/dashboard')
        if place == 'Market Watch':
            return between(html, 'id="market-watch"', '<script')
        return between(html, 'Farm Next</span>', 'Farming Planner</a>')
    html = html_of(client, '/collection/primary')
    if place == 'collection cards':
        return between(html, 'id="collection-cards"', 'id="collection-table"')
    return between(html, 'id="collection-table"', '</table>')


def price_elements(markup, item_id):
    """The markup of every price display of the item: each balanced <span class="wf-price">."""
    found = []
    for m in re.finditer(rf'<span class="wf-price[^"]*" data-item="{item_id}">', markup):
        depth, at = 0, m.start()
        for tag in re.finditer(r'<(/?)span\b[^>]*>', markup[at:]):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                found.append(markup[at:at + tag.end()])
                break
    return found


def shown(element):
    """(price text, old mark text or None, the button's (data-item, accessible name) or None)."""
    value = re.search(r'class="wf-price-value">([^<]*)<', element).group(1)
    old = re.search(r'class="wf-price-old[^"]*"[^>]*>([^<]*)<', element)
    button = re.search(r'<button type="button" class="wf-price-refresh" data-item="(\d+)"'
                       r'[^>]*aria-label="([^"]*)"', element)
    return (value, old.group(1) if old else None,
            (int(button.group(1)), button.group(2)) if button else None)


# ── The matrix: place × cache state ──────────────────────────────────────────

@pytest.mark.parametrize('place', PLACES)
@pytest.mark.parametrize('state', STATES, ids=[s[0] for s in STATES])
def test_each_place_shows_the_cached_price_its_old_mark_and_the_refresh_button(
        app, admin_client, admin_id, requests_made, place, state):
    _, row, want_text, want_old, want_button = state
    with app.app_context():
        iid = add_item(NAME)
        watch(admin_id, iid)
        if row is not None:
            cache(iid, *row)
    [element] = price_elements(section(admin_client, place), iid)
    text, old, button = shown(element)
    assert (text, old) == (want_text, want_old)
    if want_button:
        assert button == (iid, f'Refresh the price of {NAME} from warframe.market')
        assert "#i-sync" in element
    else:
        assert button is None
    assert requests_made == []


@pytest.mark.parametrize('place', ['market page', 'Market Watch', 'collection cards',
                                   'collection table'])
def test_an_old_price_without_a_market_slug_has_no_button(app, admin_client, admin_id,
                                                          requests_made, place):
    """Tradable without a slug: shown and marked old, but there is nothing to request."""
    with app.app_context():
        iid = add_item(NAME, slug=None)
        watch(admin_id, iid)
        cache(iid, 40, OLD_AT)
    [element] = price_elements(section(admin_client, place), iid)
    assert shown(element) == ('40p', f'old ({OLD_LABEL})', None)


def test_the_cache_setting_of_the_user_decides_old(app, admin_client, admin_id, requests_made):
    """A 90-minute-old price is fresh at the default 6 h and old at a 1 h setting, everywhere."""
    with app.app_context():
        iid = add_item(NAME)
        watch(admin_id, iid)
        cache(iid, 30, ago(timedelta(minutes=90)))
    assert all(shown(price_elements(section(admin_client, p), iid)[0])[1] is None for p in PLACES)
    with app.app_context():
        get_db().execute("INSERT INTO settings (user_id, key, value) "
                         "VALUES (?, 'market_cache_hours', '1')", (admin_id,))
        get_db().commit()
    for place in PLACES:
        text, old, button = shown(price_elements(section(admin_client, place), iid)[0])
        assert old.startswith('old (') and button is not None, place


def test_the_dashboard_shows_one_item_twice_with_the_same_hook(app, admin_client, admin_id,
                                                               requests_made):
    """Watched and unowned: Market Watch and Farm next both show it, both under its id, so one
    refresh updates both."""
    with app.app_context():
        iid = add_item(NAME)
        watch(admin_id, iid)
        cache(iid, 95, OLD_AT)
    elements = price_elements(html_of(admin_client, '/dashboard'), iid)
    assert len(elements) == 2
    assert [shown(e) for e in elements] == [('95p', f'old ({OLD_LABEL})',
                                             (iid, f'Refresh the price of {NAME} from warframe.market'))] * 2
    assert requests_made == []


def test_the_market_page_shows_each_fetch_time_under_the_items_hook(app, admin_client, admin_id,
                                                                    requests_made):
    with app.app_context():
        iid = add_item(NAME)
        watch(admin_id, iid)
        cache(iid, 95, OLD_AT)
    html = html_of(admin_client, '/market/')
    assert re.search(rf'<td class="grey-text wf-price-fetched" data-item="{iid}"[^>]*>'
                     rf'{re.escape(OLD_LABEL)}</td>', html)
    assert 'Click a price to refresh' not in html and 'refresh-price' not in html


def test_a_hostile_item_name_is_drawn_as_text_in_the_buttons_name(app, admin_client, admin_id,
                                                                  requests_made):
    hostile = '<b>&Co" x'
    with app.app_context():
        iid = add_item(hostile, slug='hostile')
        watch(admin_id, iid)
    for place in PLACES:
        markup = section(admin_client, place)
        assert hostile not in markup, place
        [element] = price_elements(markup, iid)
        assert shown(element)[2] == (iid, 'Refresh the price of &lt;b&gt;&amp;Co&#34; x '
                                          'from warframe.market'), place


def test_an_empty_watchlist_and_an_empty_collection_show_no_price(app, admin_client,
                                                                  requests_made):
    assert 'class="wf-price' not in between(html_of(admin_client, '/dashboard'),
                                            'id="market-watch"', '<script')
    assert 'class="wf-price' not in html_of(admin_client, '/market/')
    assert 'class="wf-price"' not in html_of(admin_client, '/collection/primary')


# ── The price view ────────────────────────────────────────────────────────────

# (case id, cache row, failed, slug, want (stale, refresh, fetched_label))
VIEWS = [
    ('no row', None, False, 's', (False, True, None)),
    ('no row, failed', None, True, 's', (False, True, None)),
    ('fresh', {'platinum': 5, 'fetched_at': ago(FRESH)}, False, 's', (False, False, None)),
    ('fresh but kept after a failure', {'platinum': 5, 'fetched_at': OLD_AT}, True, 's',
     (True, True, OLD_LABEL)),
    ('old', {'platinum': 5, 'fetched_at': OLD_AT}, False, 's', (True, True, OLD_LABEL)),
    ('old, no slug', {'platinum': 5, 'fetched_at': OLD_AT}, False, None, (True, False, OLD_LABEL)),
    ('no row, no slug', None, False, '', (False, False, None)),
    ('empty fetch time', {'platinum': 5, 'fetched_at': ''}, False, 's',
     (True, True, 'at an unknown time')),
]


@pytest.mark.parametrize('case', VIEWS, ids=[v[0] for v in VIEWS])
def test_the_price_view_decides_old_and_the_button(case):
    _, row, failed, slug, (stale, refresh, label) = case
    view = price_view(row, 6, slug, failed=failed)
    assert (view['stale'], view['refresh'], view['failed']) == (stale, refresh, failed)
    if row is not None and label != OLD_LABEL:
        assert view['fetched_label'] == (label or market_api._fetch_label(row['fetched_at']))
    else:
        assert view['fetched_label'] == label
    assert view['platinum'] == (row['platinum'] if row else None)


def test_a_fresh_price_written_by_a_failed_requests_kept_row_is_still_old():
    """`failed` forces the mark even when the clock says fresh (a kept row is shown as old)."""
    view = price_view({'platinum': 0, 'fetched_at': ago(FRESH)}, 6, 's', failed=True)
    assert view['stale'] and view['refresh'] and view['platinum'] == 0


# ── The goal plan: one get_price() per open, its answer shown with the same view ──

class Answer:
    def __init__(self, platinum):
        self.platinum = platinum

    def raise_for_status(self):
        pass

    def json(self):
        return {'data': [{'type': 'sell', 'platinum': self.platinum, 'visible': True,
                          'user': {'status': 'ingame'}}], 'error': None}


# (case id, cache row, market: a price or 'fail' or None for not asked,
#  want (platinum, stale, failed, refresh, fetched_label or True for any))
PLAN_CASES = [
    ('fresh: not asked', (120, ago(FRESH)), None, (120, False, False, False, True)),
    ('fresh 0p: not asked', (0, ago(FRESH)), None, (0, False, False, False, True)),
    ('fresh NULL: not asked', (None, ago(FRESH)), None, (None, False, False, False, True)),
    ('old, answered', (95, OLD_AT), 77, (77, False, False, False, True)),
    ('old, failed: kept', (95, OLD_AT), 'fail', (95, True, True, True, OLD_LABEL)),
    ('old 0p, failed: kept', (0, OLD_AT), 'fail', (0, True, True, True, OLD_LABEL)),
    ('none, failed', None, 'fail', (None, False, True, True, None)),
    ('unreadable, failed', (75, 'not a time'), 'fail', (75, True, True, True, 'not a time')),
]


@pytest.mark.parametrize('case', PLAN_CASES, ids=[c[0] for c in PLAN_CASES])
def test_the_goal_plan_carries_the_price_view_of_its_one_request(
        app, admin_client, admin_id, monkeypatch, case):
    _, row, market, (platinum, stale, failed, refresh, label) = case
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        if market == 'fail':
            raise requests.ConnectionError('refused')
        return Answer(market)

    monkeypatch.setattr(market_api.requests, 'get', fake_get)
    with app.app_context():
        iid = add_item(NAME)
        if row is not None:
            cache(iid, *row)
        goal_id = add_farming_goal(admin_id, iid)['goal_id']
    plan = admin_client.get(f'/farming/goals/{goal_id}/plan').get_json()['plan']
    view = plan['market']
    assert plan['market_item_id'] == iid
    assert (view['platinum'], view['stale'], view['failed'], view['refresh']) == (
        platinum, stale, failed, refresh)
    assert plan['market_price'] == platinum
    if label is True:
        assert view['fetched_label']
    else:
        assert view['fetched_label'] == label
    if failed:
        assert view['error'] == ('warframe.market gave no price (no answer); '
                                 'the last price is kept.')
    assert len(calls) == (0 if market is None else 1)


def test_a_goal_without_a_market_slug_has_no_price_view(app, admin_client, admin_id,
                                                        requests_made):
    with app.app_context():
        iid = add_item(NAME, slug=None)
        goal_id = add_farming_goal(admin_id, iid)['goal_id']
    plan = admin_client.get(f'/farming/goals/{goal_id}/plan').get_json()['plan']
    assert plan['market'] is None and plan['market_price'] is None
    assert requests_made == []


def test_a_hostile_goal_name_reaches_the_plan_script_only_through_the_escaping_markup(
        app, admin_client, admin_id, requests_made):
    """The plan's JSON carries the name as data; its script draws it only as the button's
    name inside wfPriceMarkup, which escapes it (pinned by the page-script tests above), and
    as a component price button's name, through escText."""
    hostile = '<b>&Co" x'
    with app.app_context():
        iid = add_item(hostile, slug='hostile')
        cache(iid, 95, OLD_AT)
        goal_id = add_farming_goal(admin_id, iid)['goal_id']
    plan = admin_client.get(f'/farming/goals/{goal_id}/plan').get_json()['plan']
    assert plan['item_name'] == hostile
    assert plan['market']['refresh'] and plan['market_item_id'] == iid
    html = html_of(admin_client, '/farming/')
    uses = [m.start() for m in re.finditer(r'plan\.item_name', html)]
    shared = 'wfPriceMarkup(plan.market_item_id, plan.market, '
    before = [html[at - len(shared):at] for at in uses]
    assert before.count(shared) == 2 and len(uses) == 3
    assert [b for b in before if b != shared][0].endswith('escText(')


@pytest.mark.parametrize('spot', ["'Market price: ' + wfPriceMarkup(plan.market_item_id, "
                                  "plan.market, plan.item_name, true)",
                                  "'Market: ' + wfPriceMarkup(plan.market_item_id, plan.market, "
                                  "plan.item_name, true)"])
def test_both_goal_plan_spots_draw_the_price_through_the_shared_markup(admin_client, spot):
    html = html_of(admin_client, '/farming/')
    assert spot in html
    assert 'if (plan.market) {' in html and 'if (plan.market_price)' not in html


# ── The refresh answer ────────────────────────────────────────────────────────

def test_a_failed_refresh_answers_502_with_the_kept_price_and_its_label(app, admin_client,
                                                                        monkeypatch):
    def refused(url, *args, **kwargs):
        raise requests.ConnectionError('refused')

    monkeypatch.setattr(market_api.requests, 'get', refused)
    with app.app_context():
        iid = add_item(NAME)
        cache(iid, 0, OLD_AT)
    response = admin_client.get(f'/market/price/{iid}')
    assert response.status_code == 502
    body = response.get_json()
    assert (body['ok'], body['platinum'], body['fetched_at'], body['fetched_label']) == (
        False, 0, OLD_AT, OLD_LABEL)


def test_a_refresh_without_a_listing_answers_404_without_a_price(app, admin_client,
                                                                 requests_made):
    """The script keeps the shown price when the answer has no `platinum` key."""
    with app.app_context():
        iid = add_item(NAME, slug=None)
    response = admin_client.get(f'/market/price/{iid}')
    assert response.status_code == 404
    assert 'platinum' not in response.get_json()


def test_a_successful_refresh_answers_the_fresh_price_and_its_label(app, admin_client,
                                                                   monkeypatch):
    monkeypatch.setattr(market_api.requests, 'get', lambda url, *a, **k: Answer(0))
    with app.app_context():
        iid = add_item(NAME)
        cache(iid, 95, OLD_AT)
    body = admin_client.get(f'/market/price/{iid}').get_json()
    assert (body['ok'], body['platinum']) == (True, 0)
    assert body['fetched_label'].endswith(' UTC')


# ── The one page script ───────────────────────────────────────────────────────

def price_script():
    source = APP_JS.read_text(encoding='utf-8').replace('\r\n', '\n')
    return between(source, '// Market prices.', '// Every unsafe jQuery request')


SCRIPT_HOOKS = [
    # the URL of the one route, from the page
    "var meta = document.querySelector('meta[name=\"market-price-url\"]');",
    "return (meta ? meta.getAttribute('content') : '').replace(/\\/0$/, '/') + itemId;",
    # one delegated handler for every page; a click while the item's request runs is ignored
    "$(document).on('click', '.wf-price-refresh', function (e) {",
    "if (!WF_ITEM_ID.test(itemId) || wfPriceBusy[itemId]) return;",
    "wfPriceBusy[itemId] = true;",
    "delete wfPriceBusy[itemId];",
    # every display of the item
    "$('.wf-price[data-item=\"' + itemId + '\"]')",
    # only ok true is a success; ok false (502), a 404 and a network error keep the price
    "if (r && r.ok === true) {\n        wfPriceShow(itemId, r, true, null);",
    "} else failed(r, 'no answer.');",
    "error: function (xhr) { failed(xhr.responseJSON, 'the request failed.'); },",
    "var hasPrice = !!answer && Object.prototype.hasOwnProperty.call(answer, 'platinum');",
    "if (ok || hasPrice) $value.text(window.wfPriceText(answer.platinum));",
    "var $button = $p.find('.wf-price-refresh, .wf-comp-price-refresh');",
    "$p.find('.wf-price-old').add($button).remove();",
    "$old.text('old (' + label + ')').attr('title', 'Fetched ' + label);",
    "var $note = $('<span class=\"wf-price-failed red-text\">not refreshed</span>').attr('title', error);",
    "if (label) $('.wf-price-fetched[data-item=\"' + itemId + '\"]').text(label);",
    # the toast gets an element whose text is set, never a string of markup
    "note.textContent = 'The price was not refreshed: ' + error;",
    "M.toast({html: note});",
    # 0 is a price
    "return (typeof platinum === 'number' && isFinite(platinum)) ? platinum + 'p' : '—';",
]


@pytest.mark.parametrize('hook', SCRIPT_HOOKS)
def test_the_page_script_refreshes_one_item_everywhere_and_keeps_it_on_failure(hook):
    assert hook in price_script()


def test_only_an_ok_refresh_announces_the_item_to_the_page():
    """After an ok refresh the shared handler triggers 'wf-price-refreshed' on document with
    the item id (the Farming Planner follows it); a failure announces nothing."""
    script = price_script()
    handler = script[script.index("$(document).on('click', '.wf-price-refresh'"):]
    ok = handler[handler.index('if (r && r.ok === true) {'):handler.index("} else failed(r,")]
    assert "$(document).trigger('wf-price-refreshed', [itemId]);" in ok
    assert script.count('.trigger(') == 2                 # the focus move and the announcement
    failed = handler[handler.index('var failed = function'):handler.index('$.ajax(')]
    assert 'trigger' not in failed


def test_the_page_script_inserts_answer_values_as_text_only():
    script = price_script()
    assert '.html(' not in script and 'innerHTML' not in script
    assert '.append(' not in script and '.prepend(' not in script
    # the markup builder escapes every value it draws: price, label, error, name, URL
    for drawn in ('esc(window.wfPriceText(p.platinum))', "esc(p.fetched_label) + '\">'",
                  "'old (' + esc(p.fetched_label) + ')", "esc(p.error || '')", 'esc(name)',
                  'esc(p.market_url)'):
        assert drawn in script, drawn
    assert "if (!WF_ITEM_ID.test(id) || !p) return '';" in script


def test_the_shared_escape_covers_markup_and_both_quotes():
    source = APP_JS.read_text(encoding='utf-8')
    escape = between(source, 'window.wfEscape = function', '};')
    for pair in ("/&/g, '&amp;'", "/</g, '&lt;'", "/>/g, '&gt;'", "/\"/g, '&quot;'",
                 "/'/g, '&#39;'"):
        assert pair in escape, pair


@pytest.mark.parametrize('path', ['/dashboard', '/market/', '/collection/primary', '/farming/'])
def test_every_page_with_prices_has_the_route_and_no_handler_of_its_own(admin_client, path):
    html = html_of(admin_client, path)
    assert '<meta name="market-price-url" content="/market/price/0">' in html
    assert 'priceBusy' not in html and "on('click', '.refresh-price'" not in html
    assert "on('click', '.wf-price-refresh'" not in html
