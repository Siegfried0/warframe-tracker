"""
Component prices: each component's warframe.market slug (`component_market_slugs`) and the
last price of a slug that is no catalogue item (`market_slug_cache`).

The fresh-or-fetch rule and the failure rule (a failed request writes nothing) are the item
prices' own (`market_api.cached_or_fetched()`); only the store differs.
"""

from models.db import INTEGRITY_ERRORS, get_db
from services.market_api import cached_or_fetched
from services.relic_sync import name_key


def get_slug_price(market_url_name: str, cache_hours: int = 6) -> dict:
    """The price of a market slug that is no catalogue item (a component of one), from
    `market_slug_cache`: as `get_price()`, the cached one while it is fresh, otherwise asked
    of the market; a successful answer is written, a failed request writes nothing and the
    result holds the kept row's values with `failed` and `error`."""
    db = get_db()
    row = db.execute(
        'SELECT platinum_price, fetched_at FROM market_slug_cache WHERE market_url_name = ?',
        (market_url_name,),
    ).fetchone()

    def store(price, now):
        _store_slug_price(db, market_url_name, price, now)

    return cached_or_fetched(row, market_url_name, cache_hours, store)


def _store_slug_price(db, market_url_name: str, price, now: str) -> None:
    """Write one slug's price and fetch time, and commit. Statements both backends run (no
    ``ON CONFLICT``): an update, else an insert; when another request inserted the row in
    between, the insert breaks the primary key and the update is made instead."""
    params = (price, now, market_url_name)
    update = 'UPDATE market_slug_cache SET platinum_price = ?, fetched_at = ? WHERE market_url_name = ?'
    if db.execute(update, params).rowcount == 0:
        try:
            db.execute('INSERT INTO market_slug_cache (platinum_price, fetched_at, '
                       'market_url_name) VALUES (?, ?, ?)', params)
        except INTEGRITY_ERRORS:
            db.rollback()
            db.execute(update, params)
    db.commit()


def component_slugs(item_id: int, db=None) -> dict[str, str]:
    """The market slug of each component of an item that has one, keyed by the component's
    name key (case-folded, blanks cleaned: the planner's and the catalogue's spellings of a
    part meet there). Reads `component_market_slugs` only."""
    rows = (db or get_db()).execute(
        'SELECT component_name, market_url_name FROM component_market_slugs WHERE item_id = ? '
        'ORDER BY component_name',
        (item_id,),
    ).fetchall()
    slugs: dict[str, str] = {}
    for row in rows:
        slugs.setdefault(name_key(row['component_name']), row['market_url_name'])
    return slugs


def get_component_price(item_id: int, component_name: str, cache_hours: int = 6) -> dict | None:
    """One component's price, as `get_slug_price()` answers for its slug (fetched only when
    the cached one is not fresh); None when the item has no component of that name (any
    spelling of case and blanks) with a market slug, or no such item."""
    slug = component_slugs(item_id).get(name_key(component_name))
    return None if slug is None else get_slug_price(slug, cache_hours)


def component_price_rows(item_id: int, db=None) -> dict[str, dict]:
    """Each component of an item that has a market slug, with its cached price of any age:
    `{name key: {'market_url_name', 'cache'}}`, `cache` the slug's cache row (`platinum`,
    `fetched_at`) or None without one. One query; reads only, never requests a price."""
    rows = (db or get_db()).execute(
        'SELECT s.component_name, s.market_url_name, c.platinum_price, c.fetched_at '
        'FROM component_market_slugs s '
        'LEFT JOIN market_slug_cache c ON c.market_url_name = s.market_url_name '
        'WHERE s.item_id = ? ORDER BY s.component_name',
        (item_id,),
    ).fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        cache = ({'platinum': row['platinum_price'], 'fetched_at': row['fetched_at']}
                 if row['fetched_at'] is not None else None)
        result.setdefault(name_key(row['component_name']),
                          {'market_url_name': row['market_url_name'], 'cache': cache})
    return result
