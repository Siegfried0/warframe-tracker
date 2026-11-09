"""The market-watch toggle in the collection browser, and the watchlist add and remove behind it.

Every tradable item (`items.tradable = 1`) shows a market-watch toggle in both collection views
(card grid and table) on every category tab, mods and arcanes included; an item that is not
tradable shows none. The toggle shows the caller's own watchlist state on page load and is
told apart from the Prime Resurgence watch by its icon and title. A watched item is listed on
the market page and in the dashboard's Market Watch panel; an unwatched one on neither.

Adding an item that is already watched leaves the one row and answers ok, also when the insert
loses a race to the UNIQUE (user_id, item_id) constraint; any other constraint failure is
raised. Removing is idempotent. Another user's watchlist is never read into, or changed by, the
caller's toggle.

Network refused (tests/conftest.py); no price is ever requested.
"""
import re
import sqlite3

import pyodbc
import pytest

from models.db import get_db
from models.item import Item
from models.user import User
from services import market_api
from services.market_api import unwatch_item, watch_item

CATEGORIES = ('warframe', 'primary', 'mod', 'arcane')
VIEWS = ('cards', 'table')

# (tradable, watched by the caller, watched by the other user)
ITEMS = {
    'Watched Tradable': (True, True, False),
    'Plain Tradable': (True, False, True),
    'Watched Untradable': (False, True, False),
    'Plain Untradable': (False, False, True),
}

WATCHED_TITLE = 'On your market watchlist (click to remove)'
UNWATCHED_TITLE = 'Add to market watchlist'

TOGGLE = re.compile(r'<a href="#" class="wf-market-watch [^"]*"[^>]*>\s*'
                    r'<i class="material-icons[^"]*">\w+</i>', re.S)


def attrs(toggle):
    """The attributes of the toggle's `<a>` tag (not of its icon)."""
    return dict(re.findall(r'([\w-]+)="([^"]*)"', toggle[:toggle.index('>')]))


@pytest.fixture
def users(app, admin_client):
    with app.app_context():
        admin = User.get_by_username('admin').id
        alice = User.create('alice', password='pw-alice-1').id
    return {'admin': admin, 'alice': alice}


def add_watch(user_id, item_id):
    get_db().execute('INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
                     (user_id, item_id))
    get_db().commit()


def watch_rows(user_id, item_id=None):
    sql = 'SELECT item_id FROM user_market_watchlist WHERE user_id = ?'
    params = [user_id]
    if item_id is not None:
        sql += ' AND item_id = ?'
        params.append(item_id)
    return [r['item_id'] for r in get_db().execute(sql, params).fetchall()]


def item_id(name, category):
    return get_db().execute('SELECT id FROM items WHERE name = ? AND category = ?',
                            (name, category)).fetchone()['id']


@pytest.fixture
def catalogue(app, users):
    """The four kinds of item in every category; returns {(category, name): id}."""
    ids = {}
    with app.app_context():
        for category in CATEGORIES:
            for name, (tradable, mine, theirs) in ITEMS.items():
                full = f'{name} {category}'
                Item.upsert(full, category, source='prime' if category == 'warframe' else 'standard',
                            tradable=tradable)
                ids[category, name] = item_id(full, category)
                if mine:
                    add_watch(users['admin'], ids[category, name])
                if theirs:
                    add_watch(users['alice'], ids[category, name])
    return ids


def view_chunks(html, view):
    """{item id: the markup of its card (cards) or row (table)} in one view."""
    cards_at = html.index('id="collection-cards"')
    table_at = html.index('id="collection-table"')
    section = html[cards_at:table_at] if view == 'cards' else html[table_at:html.index('</table>', table_at)]
    parts = section.split('data-item-id="')[1:]
    return {int(part[:part.index('"')]): part for part in parts}


@pytest.mark.parametrize('view', VIEWS)
@pytest.mark.parametrize('category', CATEGORIES)
def test_exactly_the_tradable_items_show_the_toggle_with_the_callers_state(
        admin_client, catalogue, category, view):
    response = admin_client.get(f'/collection/{category}')
    assert response.status_code == 200
    chunks = view_chunks(response.get_data(as_text=True), view)
    for name, (tradable, mine, _theirs) in ITEMS.items():
        iid = catalogue[category, name]
        toggles = TOGGLE.findall(chunks[iid])
        if not tradable:
            assert toggles == [], (category, view, name)
            continue
        assert len(toggles) == 1, (category, view, name)
        a = attrs(toggles[0])
        icon = re.search(r'>(\w+)</i>', toggles[0]).group(1)
        assert a['data-item'] == str(iid)
        assert a['data-watched'] == ('1' if mine else '0'), (category, view, name)
        assert a['aria-pressed'] == ('true' if mine else 'false')
        assert a['role'] == 'button'
        assert a['title'] == (WATCHED_TITLE if mine else UNWATCHED_TITLE)
        assert icon == ('bookmark' if mine else 'bookmark_border')
        assert ('green-text' in a['class']) is mine


@pytest.mark.parametrize('view', VIEWS)
def test_the_toggle_is_not_the_prime_resurgence_watch(admin_client, catalogue, view):
    chunks = view_chunks(admin_client.get('/collection/warframe').get_data(as_text=True), view)
    chunk = chunks[catalogue['warframe', 'Plain Tradable']]
    assert 'data-field="resurgence_watch"' in chunk          # a Prime: both watches show
    toggle = TOGGLE.findall(chunk)[0]
    assert 'notifications' not in toggle and 'Resurgence' not in toggle
    assert 'wf-toggle' not in toggle and 'data-field' not in toggle


def test_the_table_has_a_market_column_on_every_tab(admin_client, catalogue):
    for category in CATEGORIES:
        html = admin_client.get(f'/collection/{category}').get_data(as_text=True)
        head = html[html.index('id="collection-table"'):html.index('</thead>')]
        assert head.count('<th class="center-align">Market</th>') == 1, category


def test_one_delegated_handler_serves_both_views_and_keeps_the_state_on_failure(admin_client):
    html = admin_client.get('/collection/warframe').get_data(as_text=True)
    assert html.count(".on('click', '.wf-market-watch'") == 1
    assert "$(document).on('click', '.wf-market-watch'" in html
    handler = html[html.index("$(document).on('click', '.wf-market-watch'"):]
    handler = handler[:handler.index('// Farm it')]
    error = handler[handler.index('error:'):handler.index('complete:')]
    assert 'M.toast' in error and 'showMarketWatch' not in error
    not_ok = handler[handler.index('if (!r || !r.ok)'):handler.index('showMarketWatch(itemId, !watched)')]
    assert 'M.toast' in not_ok and 'return;' in not_ok


def market_page_names(client):
    return client.get('/market/').get_data(as_text=True)


def dashboard_panel(client):
    html = client.get('/dashboard').get_data(as_text=True)
    start = html.index('id="market-watch"')
    return html[start:html.index('<script', start)]


def test_watch_then_unwatch_shows_and_hides_the_item_on_the_market_page_and_dashboard(
        app, admin_client, catalogue, users):
    iid = catalogue['primary', 'Plain Tradable']
    name = 'Plain Tradable primary'
    assert name not in market_page_names(admin_client)
    assert name not in dashboard_panel(admin_client)

    assert admin_client.post('/market/watch', json={'item_id': iid}).get_json() == {'ok': True}
    assert name in market_page_names(admin_client)
    assert name in dashboard_panel(admin_client)
    chunks = view_chunks(admin_client.get('/collection/primary').get_data(as_text=True), 'cards')
    assert attrs(TOGGLE.findall(chunks[iid])[0])['data-watched'] == '1'

    assert admin_client.post('/market/unwatch', json={'item_id': iid}).get_json() == {'ok': True}
    assert name not in market_page_names(admin_client)
    assert name not in dashboard_panel(admin_client)
    with app.app_context():
        assert watch_rows(users['alice'], iid) == [iid]       # the other user's row stays


@pytest.mark.parametrize('steps, rows_after', [
    (('watch', 'watch'), 1),
    (('unwatch', 'unwatch'), 0),
    (('watch', 'unwatch', 'watch'), 1),
    (('watch', 'unwatch', 'unwatch'), 0),
])
def test_repeated_adds_and_removes_answer_ok_and_leave_at_most_one_row(
        app, admin_client, catalogue, users, steps, rows_after):
    iid = catalogue['mod', 'Plain Tradable']
    for step in steps:
        response = admin_client.post(f'/market/{step}', json={'item_id': iid})
        assert response.status_code == 200
        assert response.get_json() == {'ok': True}
    with app.app_context():
        assert watch_rows(users['admin'], iid) == [iid] * rows_after
        assert watch_rows(users['alice'], iid) == [iid]


def test_the_toggle_never_changes_another_users_watchlist(app, admin_client, catalogue, users):
    with app.app_context():
        before = sorted(watch_rows(users['alice']))
    for (_category, _name), iid in catalogue.items():
        admin_client.post('/market/watch', json={'item_id': iid})
        admin_client.post('/market/unwatch', json={'item_id': iid})
    with app.app_context():
        assert sorted(watch_rows(users['alice'])) == before
        assert watch_rows(users['admin']) == []


class RacedConnection:
    """The real connection, except that just before the watchlist INSERT a rival request adds
    the same row (and commits) through a connection of its own; `raise_instead` then raises
    that error in place of running the INSERT (the MSSQL driver's error class)."""

    def __init__(self, real, rival, raise_instead=None):
        self._real = real
        self._rival = rival
        self._raise = raise_instead
        self.inserts = 0

    def execute(self, sql, params=()):
        if sql.lstrip().startswith('INSERT INTO user_market_watchlist'):
            self.inserts += 1
            if self._rival is not None:
                self._rival()
            if self._raise is not None:
                raise self._raise
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def rival_insert(app, user_id, item_id):
    def insert():
        conn = sqlite3.connect(app.config['DATABASE_PATH'])
        try:
            conn.execute('INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
                         (user_id, item_id))
            conn.commit()
        finally:
            conn.close()
    return insert


@pytest.mark.parametrize('driver_error', [
    None,  # the SQLite backend raises its own UNIQUE error
    pyodbc.IntegrityError('23000', '[23000] Violation of UNIQUE KEY constraint (2627)'),
], ids=['sqlite', 'mssql-driver'])
def test_an_insert_that_loses_the_race_counts_as_watched(
        app, admin_client, catalogue, users, monkeypatch, driver_error):
    iid = catalogue['arcane', 'Plain Tradable']
    made = []

    def raced_db():
        made.append(RacedConnection(get_db(), rival_insert(app, users['admin'], iid), driver_error))
        return made[-1]

    monkeypatch.setattr(market_api, 'get_db', raced_db)
    response = admin_client.post('/market/watch', json={'item_id': iid})
    assert response.status_code == 200
    assert response.get_json() == {'ok': True}
    assert sum(conn.inserts for conn in made) == 1          # the race did happen
    monkeypatch.undo()
    with app.app_context():
        assert watch_rows(users['admin'], iid) == [iid]


def test_a_constraint_failure_that_leaves_no_row_is_raised(app, catalogue, users, monkeypatch):
    iid = catalogue['primary', 'Plain Tradable']
    with app.test_request_context():
        failing = RacedConnection(get_db(), None, sqlite3.IntegrityError('FOREIGN KEY constraint failed'))
        monkeypatch.setattr(market_api, 'get_db', lambda: failing)
        with pytest.raises(sqlite3.IntegrityError):
            watch_item(users['admin'], iid)
        monkeypatch.undo()
        assert watch_rows(users['admin'], iid) == []


def test_watch_item_twice_and_unwatch_item_of_an_unwatched_item(app, catalogue, users):
    iid = catalogue['warframe', 'Plain Tradable']
    with app.test_request_context():
        assert watch_item(users['admin'], iid) is None
        assert watch_item(users['admin'], iid) is None
        assert watch_rows(users['admin'], iid) == [iid]
        unwatch_item(users['admin'], iid)
        unwatch_item(users['admin'], iid)
        assert watch_rows(users['admin'], iid) == []
        assert watch_rows(users['alice'], iid) == [iid]
