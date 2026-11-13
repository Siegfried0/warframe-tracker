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
in ``wiki_sync_amps``; mods, arcanes and resources in ``wiki_sync_mods``; component
ingredients in ``wiki_sync_ingredients``; the image passes (wiki thumbnail fallbacks, the
local picture cache) in ``wiki_sync_images``.
"""

import logging
import math
from datetime import datetime

from models.db import get_db
from services import image_cache, relic_sync, sync_schedule, wiki_sync_requests
from services.market_slugs import sync_market_slugs
from services.resurgence_sync import sync_resurgence
from services.wiki_sync_amps import _sync_amps
from services.wiki_sync_catalogue import _NECRAMECHS
from services.wiki_sync_gear import (
    _sync_archwings, _sync_companions, _sync_k_drives, _sync_warframes, _sync_weapons,
)
from services.wiki_sync_images import (
    _backfill_null_item_images, _sync_component_images, _sync_component_wiki_images,
    _sync_item_images, _sync_wiki_images_for_missing,
)
from services.wiki_sync_ingredients import _sync_component_ingredients
from services.wiki_sync_kitguns import _sync_kitgun_chambers
from services.wiki_sync_mods import _sync_arcanes, _sync_mods, _sync_resources

log = logging.getLogger(__name__)


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
