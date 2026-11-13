"""The catalogue helpers the item sync's category groups share.

The source a name implies, the image URL a WFCD entry gives, the upsert of one item from
a WFCD entry or a wiki page title, the cleanup of stale component rows, and the constants
more than one group reads. Only catalogue rows are written; a stale row that holds user
progress is moved, never deleted.
"""

import logging

from models.db import get_db
from models.item import Item
from services.wiki_sync_requests import _WFCD_CDN

# The sync's log lines keep the logger name an operator filters on.
log = logging.getLogger('services.wiki_sync')

_WIKI_BASE             = 'https://wiki.warframe.com/w/'

_SOURCE_KEYWORDS = ('prime', 'wraith', 'vandal', 'kuva', 'tenet')

# Weapon sub-categories whose wiki component pages omit the "Blueprint" suffix
_WEAPON_CATEGORIES = frozenset({
    'primary', 'secondary', 'melee', 'arch-gun', 'arch-melee',
})

# Necramechs appear in WFCD Warframes.json but are not Warframes.
# They are synced under 'k-drive' by _sync_k_drives() instead.
_NECRAMECHS = frozenset({'Bonewidow', 'Voidrig'})

# Words that identify non-item wiki pages (subpages, mod lists, skin articles, etc.)
_WIKI_SKIP_WORDS = frozenset(['/', 'Mod', 'Race', 'Chart', 'Skin', 'SFX'])


def _detect_source(name: str) -> str:
    lower = name.lower()
    for src in _SOURCE_KEYWORDS:
        if src in lower:
            return src
    return 'standard'


def _wfcd_image_url(obj: dict) -> str | None:
    """Return the best available image URL from a WFCD item or component dict.

    Priority: wikiaThumbnail → imageUrl → CDN URL constructed from imageName.
    Returns None when no image information is present.
    """
    url = obj.get('wikiaThumbnail') or obj.get('imageUrl')
    if not url:
        name = obj.get('imageName', '')
        if name:
            url = f'{_WFCD_CDN}/{name}'
    return url or None


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

def _upsert_wfcd(item: dict, category: str, tradable_override=None,
                 source_override=None) -> bool:
    """Upsert one item from a WFCD JSON entry. Returns True if written."""
    name = item.get('name', '').strip()
    if not name:
        return False
    tradable = tradable_override if tradable_override is not None else bool(item.get('tradable', False))
    wiki_url = item.get('wikiaUrl') or (_WIKI_BASE + name.replace(' ', '_'))
    image_url = _wfcd_image_url(item)
    source = source_override if source_override is not None else _detect_source(name)
    Item.upsert(
        name=name,
        category=category,
        source=source,
        wiki_url=wiki_url,
        vaulted=bool(item.get('vaulted', False)),
        tradable=tradable,
        image_url=image_url,
    )
    return True


def _upsert_wiki(name: str, category: str, tradable: bool = False,
                 grants_mastery: bool = True) -> bool:
    """Upsert one item from a wiki page title. Returns True if written."""
    name = name.strip()
    if not name:
        return False
    Item.upsert(
        name=name,
        category=category,
        source=_detect_source(name),
        wiki_url=_WIKI_BASE + name.replace(' ', '_'),
        vaulted=False,
        tradable=tradable,
        grants_mastery=grants_mastery,
    )
    return True


def _cleanup_source_components(category: str, source: str,
                               valid_names: set[str]) -> int:
    """Remove items in a (category, source) slot that are not in valid_names.

    Called after a weapons sync run with the full set of masterable item names
    fetched from WFCD.  Any DB row not in that set is a stale component part
    inserted by an earlier sync before the filtering was correct.

    - Rows with no user progress are deleted outright.
    - Rows with user progress are moved to category='resource' so the data is
      preserved, and a warning is logged.

    Returns the number of rows cleaned up.
    """
    if not valid_names:
        # Nothing was synced this run — don't purge blindly.
        return 0

    db = get_db()
    all_rows = db.execute(
        'SELECT id, name FROM items WHERE category = ? AND source = ?',
        (category, source),
    ).fetchall()

    cleaned = 0
    for row in all_rows:
        if row['name'] in valid_names:
            continue
        has_progress = db.execute(
            'SELECT 1 FROM user_progress'
            ' WHERE item_id = ? AND (mastered = 1 OR in_possession = 1) LIMIT 1',
            (row['id'],),
        ).fetchone()
        if has_progress:
            log.warning(
                '%s component %r has user progress — moving to resource category to preserve data',
                source, row['name'],
            )
            db.execute(
                "UPDATE items SET category = 'resource', source = 'standard', grants_mastery = 0"
                " WHERE id = ?",
                (row['id'],),
            )
        else:
            db.execute('DELETE FROM items WHERE id = ?', (row['id'],))
        cleaned += 1

    if cleaned:
        db.commit()
        log.info('%s/%s stale component rows cleaned up: %d', category, source, cleaned)

    return cleaned
