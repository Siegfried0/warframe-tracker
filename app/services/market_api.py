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
        fetched = datetime.fromisoformat(row['fetched_at'])
        # Compared in seconds, not as a timedelta: a cache duration of any whole number of
        # hours (up to the 64-bit bound a setting allows) must not overflow the comparison.
        age = (datetime.utcnow() - fetched).total_seconds()
        if age < cache_hours * 3600:
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
