"""Component ingredients: the crafting recipes of each item's components (WFCD, wiki)."""

import logging

from models.db import get_db
from models.item import Item
from services import wiki_sync_requests
from services.wiki_sync_catalogue import _NECRAMECHS, _WIKI_BASE, _wfcd_image_url

# The sync's log lines keep the logger name an operator filters on.
log = logging.getLogger('services.wiki_sync')

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
                            grants_mastery=False,
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
                    grants_mastery=False,
                )

    log.info('Warframe component ingredients from wiki: %d', count)
    return count
