"""Item and component pictures: wiki thumbnail fallbacks and the local picture cache."""

import logging
from urllib.parse import unquote

from services import image_cache, wiki_sync_requests
from services.wiki_sync_catalogue import _WEAPON_CATEGORIES

# The sync's log lines keep the logger name an operator filters on.
log = logging.getLogger('services.wiki_sync')

# ---------------------------------------------------------------------------
# Image caching
# ---------------------------------------------------------------------------

# Naming, adoption of older files, downloads, the repair and the sweep live in
# services/image_cache.py.


def _title_from_wiki_url(wiki_url: str) -> str:
    """Extract the MediaWiki page title from a wiki URL.

    Handles both the current wiki.warframe.com/w/ format and the legacy
    warframe.fandom.com/wiki/ format that WFCD's wikiaUrl field may contain.

    Examples:
      https://wiki.warframe.com/w/Ash%2FPrime        →  Ash/Prime
      https://wiki.warframe.com/w/Ack_%26_Brunt      →  Ack_&_Brunt
      https://warframe.fandom.com/wiki/Alloy_Plate   →  Alloy_Plate
    """
    if not wiki_url:
        return ''
    # Prefer the canonical /w/ path on wiki.warframe.com
    if 'wiki.warframe.com' in wiki_url and '/w/' in wiki_url:
        return unquote(wiki_url.split('/w/', 1)[1])
    # Fandom URL: .../wiki/<title>  (note: split on '/wiki/' not '/w/')
    if '/wiki/' in wiki_url:
        return unquote(wiki_url.split('/wiki/', 1)[1])
    return ''


def _backfill_null_item_images(db, categories: list[str]) -> int:
    """Fetch wiki thumbnails for items in the given categories where image_url IS NULL.

    Uses the wiki_url-derived page title so the API query targets the correct
    wiki page (e.g. "Sporelacer_Chamber" rather than the canonical name
    "Sporelacer").  Falls back to the item name when wiki_url is absent.
    """
    if not categories:
        return 0
    placeholders = ','.join('?' * len(categories))
    rows = db.execute(
        f'SELECT id, name, wiki_url FROM items WHERE category IN ({placeholders}) AND image_url IS NULL',
        categories,
    ).fetchall()
    if not rows:
        return 0

    title_to_id: dict[str, int] = {}
    for r in rows:
        title = (_title_from_wiki_url(r['wiki_url']) if r['wiki_url']
                 else r['name'].replace(' ', '_'))
        if title:
            title_to_id[title] = r['id']
    if not title_to_id:
        return 0

    title_images = wiki_sync_requests._wiki_batch_images(list(title_to_id.keys()))
    normalised = {t.replace('_', ' ').lower(): iid for t, iid in title_to_id.items()}

    updated = 0
    for wiki_title, thumb_url in title_images.items():
        item_id = (title_to_id.get(wiki_title)
                   or normalised.get(wiki_title.replace('_', ' ').lower()))
        if item_id:
            db.execute('UPDATE items SET image_url = ? WHERE id = ?', (thumb_url, item_id))
            updated += 1

    if updated:
        db.commit()
    return updated


def _sync_wiki_images_for_missing(db) -> int:
    """For items where image_url IS NULL, fetch a thumbnail from the wiki via
    prop=pageimages (keyed by page title derived from wiki_url) and set image_url
    to the returned thumbnail URL so _sync_item_images can then download it.
    """
    rows = db.execute(
        'SELECT id, name, wiki_url FROM items WHERE image_url IS NULL'
    ).fetchall()
    if not rows:
        return 0

    title_to_id: dict[str, int] = {}
    for r in rows:
        if r['wiki_url']:
            title = _title_from_wiki_url(r['wiki_url'])
        else:
            title = r['name'].replace(' ', '_')
        if title:
            title_to_id[title] = r['id']

    if not title_to_id:
        return 0

    title_images = wiki_sync_requests._wiki_batch_images(list(title_to_id.keys()))

    # Build a normalised lookup (space↔underscore, case-insensitive) to handle
    # any title normalisation the wiki API may apply in its response.
    normalised: dict[str, int] = {
        t.replace('_', ' ').lower(): iid for t, iid in title_to_id.items()
    }

    updated = 0
    for wiki_title, thumb_url in title_images.items():
        item_id = title_to_id.get(wiki_title) or normalised.get(wiki_title.replace('_', ' ').lower())
        if item_id:
            db.execute('UPDATE items SET image_url = ? WHERE id = ?', (thumb_url, item_id))
            updated += 1

    if updated:
        db.commit()
    return updated


def _sync_item_images(db) -> tuple[int, int]:
    """Download and locally cache images for all items that still have a remote URL.

    - Items already pointing at /static/... are skipped.
    - An item uses a cached file only when it was made for its current URL (or is an
      older file not older than the item, adopted under the new name); otherwise the
      picture is downloaded.
    Returns (downloaded, already_cached) counts.
    """
    rows = db.execute(
        "SELECT id, image_url, date_added FROM items "
        "WHERE image_url IS NOT NULL AND image_url NOT LIKE '/static/%'"
    ).fetchall()
    if not rows:
        return 0, 0

    images_dir = image_cache.folder(image_cache.ITEMS)
    downloaded = 0
    already_cached = 0

    for row in rows:
        item_id = row['id']
        url = row['image_url']
        name = image_cache.item_file(item_id, url)
        local_url = image_cache.static_url(image_cache.ITEMS, name)

        if image_cache.resolve(images_dir, name, image_cache.legacy_item_files(item_id),
                               image_cache.parse_added(row['date_added'])):
            db.execute('UPDATE items SET image_url = ? WHERE id = ?', (local_url, item_id))
            already_cached += 1
            continue

        if image_cache.download(url, images_dir / name):
            db.execute('UPDATE items SET image_url = ? WHERE id = ?', (local_url, item_id))
            downloaded += 1

    if downloaded or already_cached:
        db.commit()
    return downloaded, already_cached


# ---------------------------------------------------------------------------
# Component wiki-image fallback
# ---------------------------------------------------------------------------

def _sync_component_wiki_images(db) -> int:
    """For components in item_component_meta that still have no image URL,
    fetch thumbnails from the Warframe wiki using the component blueprint page.

    Naming conventions used:
      • Warframes, archwings, companions: "{item} {comp} Blueprint"
          e.g. "Ash Prime Chassis Blueprint"
      • Weapons (primary/secondary/melee/arch-gun/arch-melee): "{item} {comp}"
          e.g. "Lex Prime Barrel"
    """
    try:
        rows = db.execute(
            '''SELECT m.item_id, m.component_name, i.name AS item_name, i.category
                 FROM item_component_meta m
                 JOIN items i ON i.id = m.item_id
                WHERE m.image_url IS NULL'''
        ).fetchall()
    except Exception as exc:
        log.warning('Could not query item_component_meta (migration pending?): %s', exc)
        return 0

    if not rows:
        return 0

    # Map wiki-title → (item_id, component_name); try both patterns when unsure
    title_map: dict[str, tuple[int, str]] = {}
    for r in rows:
        iid   = r['item_id']
        cname = r['component_name']
        iname = r['item_name']
        cat   = r['category']
        suffix = '' if cat in _WEAPON_CATEGORIES else ' Blueprint'
        title = f'{iname} {cname}{suffix}'
        if title not in title_map:
            title_map[title] = (iid, cname)

    title_images = wiki_sync_requests._wiki_batch_images(list(title_map.keys()))

    # Normalised lookup for title casing/underscore differences
    normalised: dict[str, tuple[int, str]] = {
        t.replace('_', ' ').lower(): v for t, v in title_map.items()
    }

    updated = 0
    for wiki_title, thumb_url in title_images.items():
        entry = title_map.get(wiki_title) or normalised.get(wiki_title.replace('_', ' ').lower())
        if entry:
            item_id, comp_name = entry
            db.execute(
                'UPDATE item_component_meta SET image_url = ? WHERE item_id = ? AND component_name = ?',
                (thumb_url, item_id, comp_name),
            )
            updated += 1

    if updated:
        db.commit()
    log.info('Component wiki images fetched: %d / %d', updated, len(rows))
    return updated


# ---------------------------------------------------------------------------
# Component image caching
# ---------------------------------------------------------------------------

def _sync_component_images(db) -> tuple[int, int]:
    """Download and locally cache images for all component entries in item_component_meta
    that still have a remote URL (the same rule as items; the date is the item's).

    Returns (downloaded, already_cached) counts.
    """
    rows = db.execute(
        "SELECT m.item_id, m.component_name, m.image_url, i.date_added "
        "FROM item_component_meta m LEFT JOIN items i ON i.id = m.item_id "
        "WHERE m.image_url IS NOT NULL AND m.image_url NOT LIKE '/static/%'"
    ).fetchall()
    if not rows:
        return 0, 0

    images_dir = image_cache.folder(image_cache.COMPONENTS)
    downloaded = 0
    already_cached = 0

    for row in rows:
        item_id = row['item_id']
        comp_name = row['component_name']
        url = row['image_url']
        name = image_cache.component_file(item_id, comp_name, url)
        local_url = image_cache.static_url(image_cache.COMPONENTS, name)
        legacy = image_cache.legacy_component_files(item_id, comp_name)

        if image_cache.resolve(images_dir, name, legacy,
                               image_cache.parse_added(row['date_added'])):
            cached = True
        elif image_cache.download(url, images_dir / name):
            cached = False
        else:
            continue
        db.execute(
            'UPDATE item_component_meta SET image_url = ? WHERE item_id = ? AND component_name = ?',
            (local_url, item_id, comp_name),
        )
        if cached:
            already_cached += 1
        else:
            downloaded += 1

    if downloaded or already_cached:
        db.commit()
    return downloaded, already_cached
