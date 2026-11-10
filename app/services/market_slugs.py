"""Market slugs: each catalogue item's warframe.market slug, read once per sync.

Source: warframe.market's own item list (``GET /v2/items``, the API and version the price
client uses, ``services/market_api.py``), which maps every listed item's English name
(``i18n.en.name``) to its slug (``slug``, the ``<slug>`` of ``/v2/orders/item/<slug>`` and of
the listing link). The official wiki and the WFCD data the catalogue sync reads carry no slug.

Matching, per catalogue row: the listed name equal to the item's name, compared
case-insensitively after trimming the outer blanks; failing that, the listed name equal to
``<name> Set`` (prime warframes and weapons trade as sets). Nothing else is tried: an item
without a match has no slug. ``tradable`` is neither read nor changed.

Sync writes catalogue rows only: ``items.market_url_name``, and only here (``Item.upsert``
inserts NULL and keeps the stored slug). After a good read every matched row gets its slug
and every other row NULL, in one transaction. A failed request, an answer that is not the
expected shape (one malformed entry is enough), an empty list or a failed write changes
nothing: the last known slugs stay, and the result says why.
"""
import logging
import re

from models.db import get_db
from services import market_api

log = logging.getLogger(__name__)

# A slug as the market spells one: ASCII letters, digits, "_" and "-", and no longer than the
# column holds on MSSQL (NVARCHAR(255)). It goes into the price request's path and the
# listing link, so nothing that could change either (a "/", "?", "." or blank) is accepted.
_SLUG = re.compile(r'[A-Za-z0-9_-]{1,255}')
_SET_SUFFIX = ' set'


class ListUnreadable(Exception):
    """The answer is not the item list's expected shape; nothing is written."""


def _key(name: str) -> str:
    return name.strip().casefold()


def _english_name(entry: dict):
    i18n = entry.get('i18n')
    english = i18n.get('en') if isinstance(i18n, dict) else None
    return english.get('name') if isinstance(english, dict) else None


def parse_item_list(data) -> dict[str, str]:
    """The listed names (as matching keys) mapped to their slugs.

    Expects ``{"data": [{"slug": str, "i18n": {"en": {"name": str}}}, ...]}`` with at least
    one entry and no ``error`` that is not empty; raises ``ListUnreadable`` otherwise, naming
    the first entry at fault. A name listed twice keeps the slug of its first entry.
    """
    if isinstance(data, dict) and data.get('error'):
        raise ListUnreadable('the market reported an error')
    items = data.get('data') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ListUnreadable('no data list')
    if not items:
        raise ListUnreadable('the item list is empty')
    slugs: dict[str, str] = {}
    for pos, entry in enumerate(items):
        if not isinstance(entry, dict):
            raise ListUnreadable(f'entry {pos} is not an object')
        name, slug = _english_name(entry), entry.get('slug')
        if not isinstance(name, str) or not name.strip():
            raise ListUnreadable(f'entry {pos} has no i18n.en.name')
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise ListUnreadable(f'entry {pos} has no valid slug')
        slugs.setdefault(_key(name), slug)
    return slugs


def slug_for(name: str, slugs: dict[str, str]) -> str | None:
    """The slug of the listed name equal to `name`, else of `<name> Set`, else None."""
    key = _key(name)
    return slugs.get(key) or slugs.get(key + _SET_SUFFIX)


def sync_market_slugs() -> dict:
    """Read the market's item list and store every catalogue row's slug; never raises.

    Returns ``{'ok': True, 'matched': n}`` (the rows that have a slug now) or
    ``{'ok': False, 'error': ...}``, in which case no slug changed.
    """
    try:
        data = market_api.fetch_item_list()
    except Exception as exc:  # timeout, unreachable, HTTP error status, a body not JSON
        log.warning('warframe.market item list fetch failed: %s', exc)
        return {'ok': False, 'error': f'fetch failed: {exc}'}
    try:
        slugs = parse_item_list(data)
    except ListUnreadable as exc:
        log.warning('warframe.market item list unreadable: %s', exc)
        return {'ok': False, 'error': f'list unreadable: {exc}'}

    db = get_db()
    try:
        matched = 0
        rows = db.execute('SELECT id, name, market_url_name FROM items ORDER BY id').fetchall()
        for row in rows:
            slug = slug_for(row['name'], slugs)
            if slug is not None:
                matched += 1
            if row['market_url_name'] != slug:
                db.execute('UPDATE items SET market_url_name = ? WHERE id = ?',
                           (slug, row['id']))
        db.commit()
    except Exception as exc:
        db.rollback()
        log.warning('Market slug write failed: %s', exc)
        return {'ok': False, 'error': f'write failed: {exc}'}
    log.info('Market slugs: %d of %d items listed', matched, len(rows))
    return {'ok': True, 'matched': matched}
