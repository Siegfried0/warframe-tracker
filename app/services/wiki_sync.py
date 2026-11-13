# file-length: exempt -- over the length limit; a split into smaller modules is planned
"""
Warframe item sync — fetches items from WFCD warframe-items dataset (primary) and
the Warframe wiki category API (K-Drive boards, Necramechs).

Primary source: https://github.com/WFCD/warframe-items
Wiki fallback:  https://wiki.warframe.com/api.php (standard query API, no Cargo extension needed)

Only catalogue data is written; user progress rows are never touched.

This module is the entry point (``run_sync``) and binds every step it runs as a name of its
own, so replacing ``wiki_sync._sync_<step>`` replaces that step. Every HTTP request lives in
``wiki_sync_requests`` and is called through that module's attribute; the helpers several
category groups share live in ``wiki_sync_catalogue``; warframes, weapons, companions,
archwings and K-Drives in ``wiki_sync_gear``; kitgun chambers in ``wiki_sync_kitguns``; amps
in ``wiki_sync_amps``; mods, arcanes and resources in ``wiki_sync_mods``.
"""

import logging
import math
from datetime import datetime
from urllib.parse import unquote

from models.db import get_db
from models.item import Item
from services import image_cache, relic_sync, sync_schedule, wiki_sync_requests
from services.market_slugs import sync_market_slugs
from services.resurgence_sync import sync_resurgence
from services.wiki_sync_amps import _sync_amps
from services.wiki_sync_catalogue import (
    _NECRAMECHS, _WEAPON_CATEGORIES, _WIKI_BASE, _wfcd_image_url,
)
from services.wiki_sync_gear import (
    _sync_archwings, _sync_companions, _sync_k_drives, _sync_warframes, _sync_weapons,
)
from services.wiki_sync_kitguns import _sync_kitgun_chambers
from services.wiki_sync_mods import _sync_arcanes, _sync_mods, _sync_resources

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Component ingredient sync
# ---------------------------------------------------------------------------

# Files to extract crafting ingredients from (all items that have components)
_INGREDIENT_SOURCES = [
    ('Warframes.json',  'warframe'),
    ('Primary.json',    'primary'),
    ('Secondary.json',  'secondary'),
    ('Melee.json',      'melee'),
    ('Arch-Gun.json',   'arch-gun'),
    ('Arch-Melee.json', 'arch-melee'),
    ('Archwing.json',   'archwing'),
    ('Sentinels.json',  'companion'),
    ('Pets.json',       'companion'),
]

# Component names that are themselves craftable (have sub-ingredients) and
# whose blueprint drops can be tracked. Skip "Blueprint" — that's the root
# blueprint; we only record crafting ingredients for sub-components.
_SKIP_COMPONENT_NAMES = frozenset({'Forma', 'Exilus Adapter'})


def _sync_component_ingredients(status: dict | None = None) -> int:
    """Rebuild component_ingredients table from WFCD item component data.

    Each WFCD component that has a nested components[] array represents a
    craftable sub-item. Those nested entries are the crafting ingredients
    (e.g., Alloy Plate × 500, Morphics × 2, etc.).

    ``status['complete']`` is set to False when item_component_meta could not be
    rebuilt in full (a source failed to load): the component picture sweep then
    skips this run, so the files of the rows not rebuilt are kept.
    """
    log.info('Syncing component ingredients…')
    db = get_db()
    db.execute('DELETE FROM component_ingredients')
    try:
        db.execute('DELETE FROM item_component_meta')
    except Exception:
        # Table may not exist yet if the server hasn't restarted since migration v5.
        # Silently skip — component images won't be populated this run, but the
        # ingredient sync itself can still complete successfully.
        if status is not None:
            status['complete'] = False
        log.warning('item_component_meta not found — restart the server to apply migration v5')

    count = 0
    for filename, category in _INGREDIENT_SOURCES:
        try:
            items_data = wiki_sync_requests._fetch_wfcd(filename)
        except Exception as exc:
            log.warning('Skipping %s for ingredient sync: %s', filename, exc)
            if status is not None:
                status['complete'] = False
            continue

        for item_data in items_data:
            name = item_data.get('name', '').strip()
            if not name or not item_data.get('masterable'):
                continue
            if category == 'warframe' and name in _NECRAMECHS:
                continue

            row = db.execute(
                'SELECT id FROM items WHERE name = ? AND category = ?',
                (name, category),
            ).fetchone()
            if not row:
                continue
            item_id = row['id']

            for comp in item_data.get('components', []):
                comp_name = comp.get('name', '').strip()
                if not comp_name or comp_name in _SKIP_COMPONENT_NAMES:
                    continue

                # Store component image URL — always insert a row so the wiki
                # fallback (_sync_component_wiki_images) can later fill in NULLs.
                img_url = _wfcd_image_url(comp)
                try:
                    db.execute(
                        '''INSERT OR IGNORE INTO item_component_meta
                               (item_id, component_name, image_url)
                           VALUES (?, ?, ?)''',
                        (item_id, comp_name, img_url),
                    )
                except Exception:
                    pass  # table missing — migration not yet applied

                # Nested components = crafting ingredients for this component
                ingredients = comp.get('components') or []
                for ing in ingredients:
                    ing_name = ing.get('name', '').strip()
                    ing_qty  = int(ing.get('itemCount', 1))
                    if not ing_name or ing_name in _SKIP_COMPONENT_NAMES:
                        continue
                    db.execute(
                        '''INSERT INTO component_ingredients
                               (item_id, component_name, ingredient_name, ingredient_qty)
                           VALUES (?, ?, ?, ?)''',
                        (item_id, comp_name, ing_name, ing_qty),
                    )
                    count += 1
                    # Register ingredient as a resource item so image lookup works.
                    # INSERT OR IGNORE preserves existing entries (including
                    # already-downloaded /static/ image_urls).
                    existing = db.execute(
                        "SELECT id FROM items WHERE name = ? AND category = 'resource'",
                        (ing_name,),
                    ).fetchone()
                    if not existing:
                        ing_img   = _wfcd_image_url(ing)
                        ing_wiki  = (_WIKI_BASE + ing_name.replace(' ', '_'))
                        Item.upsert(
                            name=ing_name,
                            category='resource',
                            source='standard',
                            wiki_url=ing_wiki,
                            vaulted=False,
                            tradable=False,
                            image_url=ing_img,
                        )

    # WFCD Warframes.json does not embed crafting ingredients for sub-components
    # (Chassis, Neuroptics, Systems).  Use the wiki Blueprints module instead.
    count += _sync_warframe_component_ingredients_from_wiki(db)

    db.commit()
    log.info('Component ingredients synced: %d', count)
    return count


# ---------------------------------------------------------------------------
# Warframe component ingredient sync from wiki Module:Blueprints/data
# ---------------------------------------------------------------------------

# Component-name suffixes that identify a warframe (or archwing/K-drive) sub-part
_WARFRAME_COMPONENT_TYPES = frozenset({
    'Chassis', 'Neuroptics', 'Systems',   # standard warframes
    'Harness', 'Wings', 'Legs',           # K-drives
})

_MODULE_BLUEPRINTS = 'Module:Blueprints/data'


def _parse_wiki_blueprints(content: str) -> list[dict]:
    """Parse the Lua table returned by Module:Blueprints/data.

    Returns a list of dicts::

        {'key': str, 'result': str, 'parts': [{'count': int, 'name': str}]}
    """
    import re

    blueprints = []
    key_re = re.compile(r'\["([^"]+)"\]\s*=\s*\{')

    for m in key_re.finditer(content):
        key = m.group(1)
        # Walk forward to find the matching closing brace for this entry.
        start = m.end() - 1   # position of the opening '{'
        depth = 0
        i = start
        while i < len(content):
            c = content[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = content[start : i + 1]

        result_m = re.search(r'Result\s*=\s*"([^"]+)"', body)
        result = result_m.group(1) if result_m else ''

        parts: list[dict] = []
        parts_m = re.search(r'Parts\s*=\s*\{', body)
        if parts_m:
            pb_start = body.index('{', parts_m.start())
            depth2 = 0
            j = pb_start
            while j < len(body):
                c = body[j]
                if c == '{':
                    depth2 += 1
                elif c == '}':
                    depth2 -= 1
                    if depth2 == 0:
                        break
                j += 1
            parts_body = body[pb_start : j + 1]
            for part_m in re.finditer(r'\{([^}]+)\}', parts_body):
                pb = part_m.group(1)
                cnt_m  = re.search(r'Count\s*=\s*(\d+)', pb)
                name_m = re.search(r'Name\s*=\s*"([^"]+)"', pb)
                if cnt_m and name_m:
                    parts.append({
                        'count': int(cnt_m.group(1)),
                        'name':  name_m.group(1),
                    })

        blueprints.append({'key': key, 'result': result, 'parts': parts})

    return blueprints


def _sync_warframe_component_ingredients_from_wiki(db) -> int:
    """Populate component_ingredients for warframe sub-components from the wiki.

    WFCD Warframes.json omits the per-component crafting recipes.
    Module:Blueprints/data on the Warframe wiki contains the full set.

    A blueprint entry whose Result matches ``"{WarframeName} {ComponentType}"``
    (e.g. "Wisp Chassis") is mapped to the parent warframe item and the named
    component, and its Parts are inserted as ingredient rows.
    """
    log.info('Syncing warframe component ingredients from wiki…')
    try:
        content = wiki_sync_requests._fetch_wiki_module(_MODULE_BLUEPRINTS)
    except Exception as exc:
        log.warning('Could not fetch wiki blueprints module (non-fatal): %s', exc)
        return 0

    blueprints = _parse_wiki_blueprints(content)
    count = 0

    for bp in blueprints:
        result = bp.get('result', '')
        if not result:
            continue

        # Split "Wisp Chassis" → item_name="Wisp", component_name="Chassis"
        parts = result.rsplit(' ', 1)
        if len(parts) != 2:
            continue
        item_name, component_name = parts

        if component_name not in _WARFRAME_COMPONENT_TYPES:
            continue

        row = db.execute(
            'SELECT id FROM items WHERE name = ? AND category = ?',
            (item_name, 'warframe'),
        ).fetchone()
        if not row:
            # Try k-drive category for Harness/Wings/Legs
            row = db.execute(
                'SELECT id FROM items WHERE name = ? AND category = ?',
                (item_name, 'k-drive'),
            ).fetchone()
        if not row:
            continue

        item_id = row['id']
        for part in bp['parts']:
            db.execute(
                '''INSERT INTO component_ingredients
                       (item_id, component_name, ingredient_name, ingredient_qty)
                   VALUES (?, ?, ?, ?)''',
                (item_id, component_name, part['name'], part['count']),
            )
            count += 1
            # Register the ingredient as a resource item so the image pipeline
            # can populate its image_url on the next sync pass.
            existing = db.execute(
                "SELECT id FROM items WHERE name = ? AND category = 'resource'",
                (part['name'],),
            ).fetchone()
            if not existing:
                Item.upsert(
                    name=part['name'],
                    category='resource',
                    source='standard',
                    wiki_url=(_WIKI_BASE + part['name'].replace(' ', '_')),
                    vaulted=False,
                    tradable=False,
                    image_url=None,
                )

    log.info('Warframe component ingredients from wiki: %d', count)
    return count


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


# ---------------------------------------------------------------------------
# Drop-source sync
# ---------------------------------------------------------------------------

def _build_item_prefix_lookup(db) -> list[tuple[str, int]]:
    """Return (name, id) pairs sorted longest-name-first for prefix matching.

    Longest first ensures 'Ash Prime' is tested before 'Ash', so a drop name
    like 'Ash Prime Neuroptics Blueprint' correctly resolves to Ash Prime.
    """
    rows = db.execute(
        'SELECT id, name FROM items ORDER BY length(name) DESC'
    ).fetchall()
    return [(r['name'], r['id']) for r in rows]


def _match_drop_to_item(drop_name: str,
                        prefix_lookup: list[tuple[str, int]]) -> int | None:
    """Return item_id if drop_name starts with a known item name, else None.

    A Prime the catalogue lacks is no match: when the rest of the name begins with the
    word 'Prime' and the item's name does not end in it ('Gyre Prime Systems Blueprint'
    with Gyre but no Gyre Prime), the drop is skipped rather than filed under the plain
    item; any shorter name it also starts with would be wrong too.
    """
    for item_name, item_id in prefix_lookup:
        if drop_name == item_name or drop_name.startswith(item_name + ' '):
            rest = drop_name[len(item_name):].split()
            if rest and rest[0] == 'Prime' and item_name.split()[-1:] != ['Prime']:
                return None
            return item_id
    return None


def _wfcd_chance_percent(value) -> float | None:
    """A WFCD drop chance in percent, or None.

    WFCD's warframe-items gives each ``drops[].chance`` as a fraction from 0 to 1 (0.2533),
    while ``drop_sources.drop_chance`` holds percent (25.33), as the warframestat drop tables
    give it. Only a number above 0 and at most 1 is read; anything else (0, a negative, a
    value above 1 such as a percent, a bool, a string, None, NaN, infinity, an integer too
    large for a float) is None, and the caller skips the drop rather than guess its scale. A
    fraction so small that its percent rounds to 0 at 4 decimals (below 5e-7) is None too.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:       # an integer too large for a float
        return None
    if not math.isfinite(value) or value <= 0 or value > 1:
        return None
    percent = round(value * 100, 4)
    # A fraction below 5e-7 rounds to 0 percent; the row's avg_runs (100 / chance) has no
    # value then, so the drop is skipped like any other unusable chance.
    return percent if percent > 0 else None


def _insert_wfcd_drop(db, item_id, drop, source_type, notes) -> bool:
    """Insert one WFCD drop as a drop_sources row, its chance converted to percent; False
    (nothing written) when the drop names no location or its chance is not a fraction."""
    location = drop.get('location', '').strip()
    chance   = _wfcd_chance_percent(drop.get('chance'))
    if not location or chance is None:
        return False
    db.execute(
        '''INSERT INTO drop_sources
               (item_id, mission_type, location, drop_chance,
                avg_runs, source_type, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (item_id, drop.get('type', 'Unknown'), location,
         chance, round(100.0 / chance, 1), source_type, notes),
    )
    return True


def _log_skipped_wfcd_drops(filename: str, skipped: int) -> None:
    if skipped:
        log.warning('%s: skipped %d drop(s) without a location or a chance between 0 and 1',
                    filename, skipped)


def _sync_resource_drops(db) -> int:
    """Insert drop_sources for resource items from WFCD Resources.json."""
    try:
        items_data = wiki_sync_requests._fetch_wfcd('Resources.json')
    except Exception as exc:
        log.warning('Could not fetch Resources.json for drop sync: %s', exc)
        return 0

    count = skipped = 0
    for item_data in items_data:
        name = item_data.get('name', '').strip()
        if not name:
            continue
        row = db.execute(
            "SELECT id FROM items WHERE name = ? AND category = 'resource'",
            (name,),
        ).fetchone()
        if not row:
            continue
        item_id = row['id']
        for drop in item_data.get('drops', []):
            if _insert_wfcd_drop(db, item_id, drop, 'resource', name):
                count += 1
            else:
                skipped += 1
    _log_skipped_wfcd_drops('Resources.json', skipped)
    return count


def _sync_wfcd_component_drops(db) -> int:
    """Insert drop_sources from component.drops arrays embedded in WFCD item JSONs."""
    targets = [
        ('Warframes.json',  'warframe'),
        ('Primary.json',    'primary'),
        ('Secondary.json',  'secondary'),
        ('Melee.json',      'melee'),
        ('Arch-Gun.json',   'arch-gun'),
        ('Arch-Melee.json', 'arch-melee'),
        ('Archwing.json',   'archwing'),
        ('Sentinels.json',  'companion'),
        ('Pets.json',       'companion'),
    ]
    count = 0
    for filename, category in targets:
        try:
            items_data = wiki_sync_requests._fetch_wfcd(filename)
        except Exception as exc:
            log.warning('Skipping %s for drop sync: %s', filename, exc)
            continue

        skipped = 0
        for item_data in items_data:
            name = item_data.get('name', '').strip()
            if not name or not item_data.get('masterable'):
                continue
            if category == 'warframe' and name in _NECRAMECHS:
                continue

            row = db.execute(
                'SELECT id FROM items WHERE name = ? AND category = ?',
                (name, category),
            ).fetchone()
            if not row:
                continue

            item_id = row['id']
            for comp in item_data.get('components', []):
                comp_name = comp.get('name', '')
                for drop in comp.get('drops', []):
                    if _insert_wfcd_drop(db, item_id, drop, 'component', comp_name):
                        count += 1
                    else:
                        skipped += 1
        _log_skipped_wfcd_drops(filename, skipped)
    return count


def _sync_mission_reward_drops(db, prefix_lookup: list[tuple[str, int]],
                               tables: dict | None) -> int:
    """Insert drop_sources from warframestat.us mission reward tables.

    Matches each drop entry name against our items catalogue using prefix
    matching (longest item name first), so 'Ash Prime Neuroptics Blueprint'
    correctly resolves to 'Ash Prime' and not 'Ash'. Entries without a name, or without
    a chance above 0 and at most 100, are skipped; a section of another shape adds nothing.
    """
    mission_rewards = tables.get('missionRewards') if isinstance(tables, dict) else None
    if not isinstance(mission_rewards, dict):
        return 0

    count = 0
    for planet, nodes in mission_rewards.items():
        if not isinstance(nodes, dict):
            continue
        for node, node_data in nodes.items():
            if not isinstance(node_data, dict):
                continue
            game_mode = node_data.get('gameMode', 'Unknown')
            location  = f'{node} — {planet}'
            rewards   = node_data.get('rewards', {})

            # Rotational missions have rewards keyed by rotation letter (A/B/C).
            # Non-rotational missions have a flat list.
            if isinstance(rewards, list):
                reward_list = [(None, rewards)]
            elif isinstance(rewards, dict):
                reward_list = [
                    (rot, lst)
                    for rot, lst in rewards.items()
                    if isinstance(lst, list)
                ]
            else:
                reward_list = []

            for rotation, items in reward_list:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    drop_name = relic_sync.clean(item.get('itemName'))
                    chance    = relic_sync.drop_chance(item.get('chance'))
                    if not drop_name or chance is None:
                        continue
                    item_id = _match_drop_to_item(drop_name, prefix_lookup)
                    if not item_id:
                        continue
                    avg_runs = round(100.0 / chance, 1)
                    notes    = (f'Rot. {rotation}: {drop_name}'
                                if rotation else drop_name)
                    db.execute(
                        '''INSERT INTO drop_sources
                               (item_id, mission_type, location, drop_chance,
                                avg_runs, source_type, notes)
                           VALUES (?, ?, ?, ?, ?, ?, ?)''',
                        (item_id, game_mode, location,
                         round(chance, 4), avg_runs, 'mission', notes),
                    )
                    count += 1
    return count


def _sync_drop_sources() -> int:
    """Rebuild drop_sources and relic_drops in one transaction from WFCD component data,
    resource drops, and the warframestat.us drop tables (mission rewards, bounty rewards and
    relics)."""
    log.info('Syncing drop sources…')
    db = get_db()
    try:
        db.execute('DELETE FROM drop_sources')

        count_comp = _sync_wfcd_component_drops(db)
        log.info('WFCD component drops: %d', count_comp)

        count_res = _sync_resource_drops(db)
        log.info('Resource drops: %d', count_res)

        tables         = wiki_sync_requests._fetch_drop_tables()
        prefix_lookup  = _build_item_prefix_lookup(db)
        count_missions = _sync_mission_reward_drops(db, prefix_lookup, tables)
        log.info('Mission reward drops: %d', count_missions)

        count_relics = relic_sync.sync_relic_drops(db, tables)
        db.commit()
    except Exception:
        db.rollback()
        raise
    total = count_comp + count_res + count_missions + count_relics
    log.info('Drop sources total: %d', total)
    return total


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_sync(app=None):
    """Run a full item sync. Pass the Flask app when calling outside a request context.

    One sync runs at a time, and each records its start, finish and result; a sync asked for
    while one runs starts nothing and answers ``running`` (services/sync_schedule.py)."""
    from flask import has_app_context
    ctx = app.app_context() if (app and not has_app_context()) else None
    try:
        if ctx:
            ctx.push()
        return sync_schedule.run_recorded(_run_sync_steps)
    finally:
        if ctx:
            ctx.pop()


def _run_sync_steps():
    """Every step of a full sync, inside an app context; a failure is a result, not raised."""
    try:
        log.info('Sync started at %s', datetime.utcnow().isoformat())
        db = get_db()
        # A stored picture that came from a deleted row with the same ID is cleared first,
        # so the catalogue pass below gives the item its URL again.
        repaired = image_cache.repair_stale_items(db)
        component_rows = {'complete': True}
        counts = {
            'warframes':        _sync_warframes(),
            'weapons':          _sync_weapons(),
            'kitgun_chambers':  _sync_kitgun_chambers(),
            'companions':       _sync_companions(),
            'archwings':    _sync_archwings(),
            'k_drives':     _sync_k_drives(),
            'amps':         _sync_amps(),
            'mods':         _sync_mods(),
            'arcanes':      _sync_arcanes(),
            'resources':    _sync_resources(),
            'ingredients':  _sync_component_ingredients(component_rows),
            'drop_sources': _sync_drop_sources(),
        }
        counts['images_repaired'] = repaired
        # Targeted backfill for weapon categories where WFCD may omit thumbnail
        # fields (confirmed: Sporelacer, Vermisplicer, and other kitgun/melee items).
        # Also covers primary/arch weapons with no wikiaThumbnail in the payload.
        _weapon_cats = ['primary', 'secondary', 'melee', 'arch-gun', 'arch-melee']
        n = _backfill_null_item_images(db, _weapon_cats)
        if n:
            log.info('Weapon null-image backfill: %d URL(s) fetched from wiki', n)
        counts['weapon_images_backfilled'] = n

        log.info('Fetching wiki images for items with no thumbnail…')
        counts['images_wiki_fetched'] = _sync_wiki_images_for_missing(db)
        log.info('Wiki image URLs fetched: %d', counts['images_wiki_fetched'])
        log.info('Caching item images locally…')
        downloaded, already_cached = _sync_item_images(db)
        counts['images_downloaded'] = downloaded
        counts['images_already_cached'] = already_cached
        log.info('Images: %d downloaded, %d already cached', downloaded, already_cached)
        log.info('Fetching wiki images for components with no thumbnail…')
        counts['comp_images_wiki_fetched'] = _sync_component_wiki_images(db)
        log.info('Caching component images locally…')
        comp_downloaded, comp_cached = _sync_component_images(db)
        counts['comp_images_downloaded'] = comp_downloaded
        counts['comp_images_already_cached'] = comp_cached
        log.info('Component images: %d downloaded, %d already cached', comp_downloaded, comp_cached)
        # Files no row references are removed once the rows are rebuilt; a failure above
        # ends the sync before this point, so no folder is swept after a failed pass.
        counts['images_swept'] = image_cache.sweep(db, image_cache.ITEMS)
        counts['images_comp_swept'] = (
            image_cache.sweep(db, image_cache.COMPONENTS) if component_rows['complete'] else 0)
        # Prime Resurgence's current stock: a failure is reported, never a failed sync
        log.info('Reading Prime Resurgence stock…')
        resurgence = sync_resurgence()
        # Each item's warframe.market slug, from the market's item list: likewise reported
        log.info('Reading the warframe.market item list…')
        market_slugs = sync_market_slugs()
        total = sum(v for k, v in counts.items() if not k.startswith('images_'))
        log.info('Sync complete at %s — %d total items', datetime.utcnow().isoformat(), total)
        return {'ok': True, 'counts': counts, 'total': total, 'resurgence': resurgence,
                'market_slugs': market_slugs}
    except Exception as exc:
        log.exception('Sync failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
