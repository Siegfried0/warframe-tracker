"""
warframe.market public REST API client — read-only price lookups.
Docs: https://warframe.market/api_doc
"""

import logging
from datetime import datetime

import requests

from models.db import get_db

log = logging.getLogger(__name__)

_BASE = 'https://api.warframe.market/v1'
_HEADERS = {
    'Accept': 'application/json',
    'Platform': 'pc',
    'Language': 'en',
}


def _get(path: str) -> dict | None:
    try:
        resp = requests.get(f'{_BASE}{path}', headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning('warframe.market request failed for %s: %s', path, exc)
        return None


def fetch_lowest_price(market_url_name: str) -> int | None:
    """Return the lowest current sell-order price in platinum, or None on failure."""
    data = _get(f'/items/{market_url_name}/orders')
    if not data:
        return None
    orders = data.get('payload', {}).get('orders', [])
    sell_prices = [
        o['platinum']
        for o in orders
        if o.get('order_type') == 'sell'
        and o.get('user', {}).get('status') == 'ingame'
    ]
    return min(sell_prices) if sell_prices else None


def get_price(item_id: int, market_url_name: str, cache_hours: int = 6) -> dict:
    """
    Return cached price if fresh, otherwise fetch from API and cache result.
    Returns {'platinum': int|None, 'fetched_at': str, 'from_cache': bool}
    """
    db = get_db()
    row = db.execute(
        'SELECT platinum_price, fetched_at FROM market_cache WHERE item_id = ?',
        (item_id,),
    ).fetchone()

    if row:
        # Compared in seconds, not as a timedelta: a cache duration of any whole number of
        # hours (up to the 64-bit bound a setting allows) must not overflow the comparison.
        if is_fresh(row['fetched_at'], cache_hours):
            return {
                'platinum': row['platinum_price'],
                'fetched_at': row['fetched_at'],
                'from_cache': True,
            }

    price = fetch_lowest_price(market_url_name)
    now = datetime.utcnow().isoformat()
    db.execute(
        '''INSERT INTO market_cache (item_id, platinum_price, fetched_at)
           VALUES (?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
               platinum_price = excluded.platinum_price,
               fetched_at     = excluded.fetched_at''',
        (item_id, price, now),
    )
    db.commit()
    return {'platinum': price, 'fetched_at': now, 'from_cache': False}


def get_prices_bulk(items: list[dict], cache_hours: int = 6) -> dict[int, dict]:
    """
    Fetch prices for a list of {'id': int, 'market_url_name': str} dicts.
    Returns {item_id: price_dict}.
    """
    return {
        item['id']: get_price(item['id'], item['market_url_name'], cache_hours)
        for item in items
        if item.get('market_url_name')
    }


def market_url_for(market_url_name: str) -> str:
    return f'https://warframe.market/items/{market_url_name}'


def is_fresh(fetched_at, cache_hours: int, now: datetime | None = None) -> bool:
    """Whether a price fetched at `fetched_at` (UTC, ISO text) is younger than `cache_hours`.

    A missing or unreadable fetch time is never fresh. The age is compared in seconds, so any
    whole number of hours a setting accepts works without overflow.
    """
    if not fetched_at:
        return False
    try:
        fetched = datetime.fromisoformat(str(fetched_at))
    except ValueError:
        return False
    if fetched.tzinfo is not None:
        fetched = fetched.replace(tzinfo=None) - fetched.utcoffset()
    age = ((now or datetime.utcnow()) - fetched).total_seconds()
    return age < cache_hours * 3600


def _fetch_label(fetched_at) -> str | None:
    """The fetch time as shown to the user: `YYYY-MM-DD HH:MM UTC`, or the text as stored."""
    if not fetched_at:
        return None
    try:
        return datetime.fromisoformat(str(fetched_at)).strftime('%Y-%m-%d %H:%M') + ' UTC'
    except ValueError:
        return str(fetched_at)


def watchlist_prices(user_id: int, cache_hours: int, limit: int | None = None) -> list[dict]:
    """The caller's watched items, ordered by name, each with its cached price.

    Reads only the cache: never requests a price. Each dict holds the item's columns plus
    `platinum_price` and `fetched_at` (None without a cache row), `fresh` (the cached price
    is younger than `cache_hours`), `stale` (a cache row exists and is not fresh),
    `fetched_label` and, for an item with a listing, `market_url`. `limit` keeps the first
    rows only.
    """
    rows = get_db().execute('''
        SELECT i.*, mc.platinum_price, mc.fetched_at
        FROM user_market_watchlist w
        JOIN items i ON i.id = w.item_id
        LEFT JOIN market_cache mc ON mc.item_id = i.id
        WHERE w.user_id = ?
        ORDER BY i.name, i.id
    ''', (user_id,)).fetchall()
    watched = [dict(r) for r in rows]
    if limit is not None:
        watched = watched[:limit]
    now = datetime.utcnow()
    for item in watched:
        if item.get('market_url_name'):
            item['market_url'] = market_url_for(item['market_url_name'])
        item['fresh'] = is_fresh(item['fetched_at'], cache_hours, now)
        item['stale'] = bool(item['fetched_at']) and not item['fresh']
        item['fetched_label'] = _fetch_label(item['fetched_at'])
    return watched
