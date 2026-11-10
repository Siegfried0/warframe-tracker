"""The sync's market step for components: each component's warframe.market slug from the
market's item list it already reads, and the two tables that hold component slugs and prices.

Oracles: the matching as a table (``<item> <component>``, ``... Blueprint``, case and blank
spellings, no match, two components of one item); a failed or refused list and a failed write
(nothing changes); the migration on SQLite (an old database, a rerun) and the MSSQL
statements. Every request is mocked at ``requests.get``; no test reaches the network.
"""
import inspect

import pytest
import requests

from models import db as db_module
from models.db import get_connection, get_db
from services import component_prices as cp
from services import market_slugs as ms
from component_price_fixtures import (  # noqa: F401  (fixtures: market, admin_id, refused)
    OLD_AT, FakeResponse, add_item, admin_id, ash_goal, cache_slug, components_of, drop,
    item_list, listed, market, meta, now_text, refused, set_component_slug, slug_cache,
    store_setting, stored_component_slugs)


# ── The slug step matches components (axis 1: spellings) ─────────────────────

# (case id, item name, component as item_component_meta names it, listed entries, slug)
COMPONENT_ROWS = [
    ('item and component', 'Ash Prime', 'Neuroptics',
     [listed('Ash Prime Neuroptics', 'ash_prime_neuroptics')], 'ash_prime_neuroptics'),
    ('listed as the blueprint', 'Ash Prime', 'Neuroptics',
     [listed('Ash Prime Neuroptics Blueprint', 'ash_prime_neuroptics_blueprint')],
     'ash_prime_neuroptics_blueprint'),
    ('both listed: the plain name first', 'Ash Prime', 'Neuroptics',
     [listed('Ash Prime Neuroptics Blueprint', 'bp'), listed('Ash Prime Neuroptics', 'plain')],
     'plain'),
    ('another case in the list', 'Ash Prime', 'Neuroptics',
     [listed('ASH PRIME neuroptics', 'upper')], 'upper'),
    ('outer blanks in the list', 'Ash Prime', 'Neuroptics',
     [listed('  Ash Prime Neuroptics  ', 'blanks')], 'blanks'),
    ('outer blanks on the component', 'Ash Prime', ' Neuroptics ',
     [listed('Ash Prime Neuroptics', 'trimmed')], 'trimmed'),
    ('a component already named Blueprint', 'Ash Prime', 'Blueprint',
     [listed('Ash Prime Blueprint', 'ash_prime_blueprint')], 'ash_prime_blueprint'),
    ('an inner double blank is no match', 'Ash Prime', 'Neuroptics',
     [listed('Ash Prime  Neuroptics', 'double')], None),
    ('no other fuzzing: a set is no part', 'Ash Prime', 'Neuroptics',
     [listed('Ash Prime Neuroptics Set', 'set')], None),
    ('not listed', 'Ash Prime', 'Neuroptics', [listed('Ash Prime Set', 'ash_prime_set')], None),
]


@pytest.mark.parametrize('row', COMPONENT_ROWS, ids=lambda r: r[0])
def test_the_slug_step_gives_each_component_the_slug_its_name_matches(app, market, row):
    _, item_name, component, entries, want = row
    market.items = FakeResponse(payload=item_list(entries))
    with app.app_context():
        item = add_item(item_name)
        meta(item, component)
        result = ms.sync_market_slugs()
        assert result['ok'] is True
        assert result['components_matched'] == (0 if want is None else 1)
        assert stored_component_slugs() == ({} if want is None else {(item, component): want})


def test_two_components_of_one_item_and_a_second_item_each_get_their_own_slug(app, market):
    market.items = FakeResponse(payload=item_list([
        listed('Ash Prime Neuroptics', 'ash_n'), listed('Ash Prime Chassis Blueprint', 'ash_c'),
        listed('Braton Prime Barrel', 'braton_b'), listed('Ash Prime Set', 'ash_set')]))
    with app.app_context():
        ash, braton = add_item('Ash Prime'), add_item('Braton Prime', 'primary')
        meta(ash, 'Neuroptics', 'Chassis', 'Systems')
        meta(braton, 'Barrel')
        assert ms.sync_market_slugs() == {'ok': True, 'matched': 1, 'components_matched': 3}
        assert stored_component_slugs() == {(ash, 'Neuroptics'): 'ash_n',
                                            (ash, 'Chassis'): 'ash_c',
                                            (braton, 'Barrel'): 'braton_b'}


def test_a_good_read_replaces_the_component_slugs_and_a_rerun_writes_nothing(app, market):
    with app.app_context():
        ash = add_item('Ash Prime')
        meta(ash, 'Neuroptics', 'Chassis')
        set_component_slug(ash, 'Neuroptics', 'old_slug')        # changed
        set_component_slug(ash, 'Chassis', 'gone_slug')          # no longer listed
        set_component_slug(ash, 'Orphan', 'orphan_slug')         # no longer a component
        market.items = FakeResponse(payload=item_list([listed('Ash Prime Neuroptics', 'new_n')]))
        assert ms.sync_market_slugs()['components_matched'] == 1
        assert stored_component_slugs() == {(ash, 'Neuroptics'): 'new_n'}
        db = get_db()
        before = db.total_changes
        ms.sync_market_slugs()
        assert db.total_changes == before


# ── A failed or refused list keeps the component slugs (axes 4, 5) ───────────

FAILED_READS = [
    ('timeout', requests.Timeout('slow')),
    ('unreachable', requests.ConnectionError('refused')),
    ('too many requests', FakeResponse(status=429)),
    ('server error', FakeResponse(status=503)),
    ('not json', FakeResponse(payload='<html>maintenance</html>')),
    ('not v2 shape', FakeResponse(payload={'payload': {'items': []}})),
    ('empty list', FakeResponse(payload=item_list([]))),
    ('one malformed entry', FakeResponse(payload=item_list(
        [listed('Ash Prime Neuroptics', 'ash_n'), {'slug': 'x', 'i18n': {}}]))),
    ('a slug that is a path', FakeResponse(payload=item_list(
        [listed('Ash Prime Neuroptics', '../orders')]))),
]


@pytest.mark.parametrize('case, response', FAILED_READS, ids=[c[0] for c in FAILED_READS])
def test_a_failed_or_refused_list_changes_no_component_slug(app, market, case, response):
    with app.app_context():
        ash = add_item('Ash Prime', slug='ash_prime_set')
        meta(ash, 'Neuroptics', 'Chassis')
        set_component_slug(ash, 'Neuroptics', 'kept_slug')
        market.items = response
        result = ms.sync_market_slugs()
        assert result['ok'] is False
        assert stored_component_slugs() == {(ash, 'Neuroptics'): 'kept_slug'}


def test_a_failed_component_write_keeps_the_item_slugs_too(app, market):
    market.items = FakeResponse(payload=item_list([
        listed('Ash Prime Set', 'ash_set'), listed('Ash Prime Neuroptics', 'ash_n')]))
    with app.app_context():
        ash = add_item('Ash Prime', slug='old_item_slug')
        meta(ash, 'Neuroptics')
        db = get_db()
        db.execute("CREATE TRIGGER refuse BEFORE INSERT ON component_market_slugs "
                   "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        db.commit()
        assert ms.sync_market_slugs() == {'ok': False, 'error': 'write failed: disk full'}
        assert db.execute('SELECT market_url_name FROM items WHERE id = ?',
                          (ash,)).fetchone()[0] == 'old_item_slug'
        assert stored_component_slugs() == {}


def test_the_sync_page_shows_the_matched_components(admin_client):
    page = admin_client.get('/sync/').get_data(as_text=True)
    assert "if (typeof ms.components_matched === 'number') {" in page
    assert "msg += ', ' + ms.components_matched + ' components';" in page


# ── The migration (axis 8: both backends) ────────────────────────────────────

def _user_data(db):
    return {table: sorted(tuple(r) for r in db.execute(f'SELECT * FROM {table}'))
            for table in ('users', 'user_progress', 'farming_goals', 'settings', 'items',
                          'market_cache', 'item_component_meta')}


def test_a_database_of_the_previous_version_gains_both_tables_and_keeps_its_data(app,
                                                                               admin_id):
    with app.app_context():
        db = get_db()
        ash = add_item('Ash Prime', slug='ash_set')
        meta(ash, 'Neuroptics')
        db.execute("INSERT INTO market_cache (item_id, platinum_price, fetched_at) "
                   "VALUES (?, 50, ?)", (ash, OLD_AT))
        db.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)', (admin_id, ash))
        db.execute('DROP TABLE component_market_slugs')
        db.execute('DROP TABLE market_slug_cache')
        db.execute('UPDATE schema_version SET version = 10')
        db.commit()
        before = _user_data(db)

        conn = get_connection()
        db_module._apply_migrations(conn)
        conn.close()

        assert db.execute('SELECT version FROM schema_version').fetchone()[0] == 11
        assert db.execute('SELECT COUNT(*) FROM component_market_slugs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM market_slug_cache').fetchone()[0] == 0
        assert _user_data(db) == before


def test_the_migration_runs_again_without_harm(app):
    with app.app_context():
        ash = add_item('Ash Prime')
        set_component_slug(ash, 'Neuroptics', 'ash_n')
        cache_slug('ash_n', 3, OLD_AT)
        db_module._migrate_v11(get_db())
        assert stored_component_slugs() == {(ash, 'Neuroptics'): 'ash_n'}
        assert slug_cache() == {'ash_n': (3, OLD_AT)}


def test_a_component_slug_goes_with_its_item(app):
    with app.app_context():
        ash = add_item('Ash Prime')
        set_component_slug(ash, 'Neuroptics', 'ash_n')
        get_db().execute('DELETE FROM items WHERE id = ?', (ash,))
        get_db().commit()
        assert stored_component_slugs() == {}


class _Recorder:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        assert params == ()
        self.statements.append(' '.join(sql.split()))

    def commit(self):
        pass


def test_the_mssql_migration_creates_both_tables_once_each():
    db = _Recorder()
    db_module._migrate_v11(db, 'mssql')
    slugs, cache = db.statements
    assert slugs.startswith("IF OBJECT_ID('component_market_slugs', 'U') IS NULL "
                            "CREATE TABLE component_market_slugs")
    assert 'item_id INT NOT NULL REFERENCES items(id) ON DELETE CASCADE' in slugs
    assert 'component_name NVARCHAR(500) NOT NULL' in slugs
    assert 'market_url_name NVARCHAR(255) NOT NULL' in slugs
    assert 'PRIMARY KEY (item_id, component_name)' in slugs
    assert cache.startswith("IF OBJECT_ID('market_slug_cache', 'U') IS NULL "
                            "CREATE TABLE market_slug_cache")
    assert 'market_url_name NVARCHAR(255) PRIMARY KEY' in cache
    assert 'platinum_price INT' in cache and 'fetched_at NVARCHAR(50) NOT NULL' in cache


def test_the_component_slug_write_uses_no_sqlite_only_sql():
    """The component slug write runs on both backends: no clause MSSQL lacks."""
    fn = ms._write_component_slugs
    source = inspect.getsource(fn).replace(fn.__doc__ or '', '')
    for sqlite_only in ('LIMIT ', "datetime('now')", 'ON CONFLICT', 'INSERT OR ',
                        'executescript'):
        assert sqlite_only not in source, sqlite_only


def test_the_component_price_sql_runs_on_both_backends():
    """The component price reads and writes use no SQLite-only clause (MSSQL has none)."""
    for fn in (cp.get_slug_price, cp._store_slug_price, cp.component_slugs,
               cp.component_price_rows):
        source = inspect.getsource(fn).replace(fn.__doc__ or '', '')
        for sqlite_only in ('LIMIT ', "datetime('now')", 'ON CONFLICT', 'INSERT OR ',
                            'executescript'):
            assert sqlite_only not in source, (fn.__name__, sqlite_only)


def test_the_slug_step_still_writes_the_catalogue_only(app, admin_id, market):
    """Component slugs are catalogue rows; no user's data and no price changes."""
    market.items = FakeResponse(payload=item_list([listed('Ash Prime Neuroptics', 'ash_n')]))
    with app.app_context():
        db = get_db()
        ash = add_item('Ash Prime')
        meta(ash, 'Neuroptics')
        cache_slug('ash_n', 5, OLD_AT)
        db.execute('INSERT INTO farming_goals (user_id, item_id) VALUES (?, ?)', (admin_id, ash))
        db.commit()
        names = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN "
            "('items', 'component_market_slugs') AND name NOT LIKE 'sqlite_%'")]
        before = {n: sorted(tuple(r) for r in db.execute(f'SELECT * FROM "{n}"')) for n in names}
        assert ms.sync_market_slugs()['components_matched'] == 1
        assert {n: sorted(tuple(r) for r in db.execute(f'SELECT * FROM "{n}"'))
                for n in names} == before
