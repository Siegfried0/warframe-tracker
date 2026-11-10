"""
warframe.market public REST API client (v2): read-only price lookups and the item list.

Every request names the app as its client (`User-Agent`): the market's CDN refuses the HTTP
library's default name, and its v1 API is retired.
"""

import logging
from datetime import datetime

import requests

from models.db import INTEGRITY_ERRORS, get_db

log = logging.getLogger(__name__)

_BASE = 'https://api.warframe.market/v2'
# The app's own client name: a fixed product name and version, nothing that names a user, the
# owner, the instance or its address. Raise the version when the client's requests change.
CLIENT_NAME = 'WarframeProgressTracker/2.0'
_HEADERS = {
    'User-Agent': CLIENT_NAME,
    'Accept': 'application/json',
    'Platform': 'pc',
    'Language': 'en',
}


# The item list is a few MB, so its read may take longer than a price request.
_TIMEOUT = 15
_ITEM_LIST_TIMEOUT = 60


class MarketRequestFailed(Exception):
    """A price request without a usable answer: none (unreachable, a timeout), an HTTP error
    status, a body that is not JSON or not v2's shape, or an answer reporting an error. Never
    the same as "no sell order in game"."""


def _request(path: str, timeout: int = _TIMEOUT):
    """The decoded JSON body of a GET; raises on a failed request, an HTTP error status or
    a body that is not JSON."""
    resp = requests.get(f'{_BASE}{path}', headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_item_list():
    """The market's item list (`GET /v2/items`) as decoded, unchecked: every listed item's
    slug and English name under `data`. Raises when the request fails or the body is not
    JSON; the caller checks the shape."""
    return _request('/items', timeout=_ITEM_LIST_TIMEOUT)


def _counted(order) -> int | None:
    """The platinum of a sell order, visible, of a player in game, when it is a whole number of
    at least 0; None for any other order."""
    if not isinstance(order, dict) or order.get('type') != 'sell':
        return None
    if order.get('visible') is False:
        return None
    user = order.get('user')
    if not isinstance(user, dict) or user.get('status') != 'ingame':
        return None
    platinum = order.get('platinum')
    if isinstance(platinum, int) and not isinstance(platinum, bool) and platinum >= 0:
        return platinum
    return None


def lowest_sell_price(answer) -> int | None:
    """The lowest price in a decoded `GET /v2/orders/item/<slug>` answer, or None when it lists
    no sell order of a player in game.

    Counted: an order with `type` "sell", `visible` not false and `user.status` "ingame"
    (exact spellings) whose `platinum` is a whole number of at least 0; any other order is not
    counted. Raises `MarketRequestFailed` when the answer is not v2's shape (an object with a
    `data` list) or its `error` is not empty.
    """
    if not isinstance(answer, dict):
        raise MarketRequestFailed('the answer is not in the expected shape')
    if answer.get('error'):
        raise MarketRequestFailed('the market reported an error')
    orders = answer.get('data')
    if not isinstance(orders, list):
        raise MarketRequestFailed('the answer is not in the expected shape')
    prices = [p for p in map(_counted, orders) if p is not None]
    return min(prices) if prices else None


def fetch_lowest_price(market_url_name: str) -> int | None:
    """The lowest current sell price in platinum (`GET /v2/orders/item/<slug>`), or None when
    the market lists no sell order of a player in game.

    Raises `MarketRequestFailed` on a failed request: no answer (unreachable, a timeout), an
    HTTP error status (403, 429, 5xx, ...), a body that is not JSON or not v2's shape.
    """
    path = f'/orders/item/{market_url_name}'
    try:
        answer = _request(path)
    except requests.HTTPError as exc:
        status = getattr(exc.response, 'status_code', None)
        log.warning('warframe.market request failed for %s: HTTP %s', path, status)
        raise MarketRequestFailed(f'HTTP {status}' if status else 'an HTTP error') from exc
    except requests.Timeout as exc:
        log.warning('warframe.market request timed out for %s', path)
        raise MarketRequestFailed('no answer in time') from exc
    except ValueError as exc:                     # the body is not JSON
        log.warning('warframe.market answer for %s is not JSON', path)
        raise MarketRequestFailed('the answer is not JSON') from exc
    except Exception as exc:                      # unreachable, refused, any other failure
        log.warning('warframe.market request failed for %s: %s', path, exc)
        raise MarketRequestFailed('no answer') from exc
    try:
        return lowest_sell_price(answer)
    except MarketRequestFailed as exc:
        log.warning('warframe.market answer for %s refused: %s', path, exc)
        raise


def get_price(item_id: int, market_url_name: str, cache_hours: int = 6) -> dict:
    """The item's price: the cached one while it is fresh, otherwise asked of the market.

    Returns `{'platinum': int|None, 'fetched_at': str|None, 'from_cache': bool}`. A successful
    answer is written to the cache: the price, or NULL for no sell order in game, with the
    current time. A failed request writes nothing: the cache row keeps its price and fetch
    time, and the result holds those kept values (None without a row; `from_cache` True when a
    row exists) plus `failed: True` and `error`, a short text for the user. Only a failed
    result has the `failed` key.
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

    try:
        price = fetch_lowest_price(market_url_name)
    except MarketRequestFailed as exc:
        return {
            'platinum': row['platinum_price'] if row else None,
            'fetched_at': row['fetched_at'] if row else None,
            'from_cache': row is not None,
            'failed': True,
            'error': f'warframe.market gave no price ({exc}); the last price is kept.',
        }
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


def cached_prices(item_ids: list[int]) -> dict[int, int | None]:
    """The cached price of each listed item that has a cache row, of any age.

    Reads only the cache, in one query, and writes nothing: never requests a price. An item
    without a cache row is absent from the result; a row without a price maps to None.
    """
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return {}
    placeholders = ','.join('?' for _ in ids)
    rows = get_db().execute(
        f'SELECT item_id, platinum_price FROM market_cache WHERE item_id IN ({placeholders})',
        ids,
    ).fetchall()
    return {row['item_id']: row['platinum_price'] for row in rows}


def page_prices(rows: list[dict], cache_hours: int) -> dict[int, dict]:
    """The price shown for each item on the market among a collection page's rows.

    Reads only the cache, in one query over the page's on-market items (none when the page
    has none), and writes nothing: never requests a price. Keyed by item id, each dict holds
    `platinum` (None when no price is cached), `cached` (a cache row exists), `stale` (a cache
    row exists and is not younger than `cache_hours`; an unreadable fetch time counts as
    stale), `fetched_label`, `market_slug` (the item's market slug, empty when it has none)
    and `market_url` (None without a slug). A row not on the market (`on_market` 0) has no
    entry.
    """
    listed = [row for row in rows if row.get('on_market')]
    ids = list(dict.fromkeys(row['id'] for row in listed))
    cache = {}
    if ids:
        placeholders = ','.join('?' for _ in ids)
        cache = {r['item_id']: r for r in get_db().execute(
            'SELECT item_id, platinum_price, fetched_at FROM market_cache '
            f'WHERE item_id IN ({placeholders})',
            ids,
        ).fetchall()}
    now = datetime.utcnow()
    prices = {}
    for row in listed:
        cached = cache.get(row['id'])
        slug = row.get('market_url_name') or ''
        prices[row['id']] = {
            'platinum': cached['platinum_price'] if cached else None,
            'cached': cached is not None,
            'stale': cached is not None and not is_fresh(cached['fetched_at'], cache_hours, now),
            'fetched_label': _fetch_label(cached['fetched_at']) if cached else None,
            'market_slug': slug,
            'market_url': market_url_for(slug) if slug else None,
        }
    return prices


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


def _is_watched(db, user_id: int, item_id: int) -> bool:
    return db.execute(
        'SELECT 1 FROM user_market_watchlist WHERE user_id = ? AND item_id = ?',
        (user_id, item_id),
    ).fetchone() is not None


def watch_item(user_id: int, item_id: int) -> None:
    """Put an existing item on the user's market watchlist; already watched is no change.

    Plain SQL that runs on both backends (`added_at` comes from the column's default). A
    second add of the same item leaves the one row: it is seen by the read before the
    insert, or, when another request inserted the row in between, the insert breaks the
    UNIQUE (user_id, item_id) constraint and the row that won counts as this add. Any other
    constraint failure (the row is still absent afterwards) is raised.
    """
    db = get_db()
    if _is_watched(db, user_id, item_id):
        return
    try:
        db.execute(
            'INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
            (user_id, item_id),
        )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        if not _is_watched(db, user_id, item_id):
            raise


def unwatch_item(user_id: int, item_id: int) -> None:
    """Remove an item from the user's market watchlist; not watched is no change."""
    db = get_db()
    db.execute(
        'DELETE FROM user_market_watchlist WHERE user_id = ? AND item_id = ?',
        (user_id, item_id),
    )
    db.commit()


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
