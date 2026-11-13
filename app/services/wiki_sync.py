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
ingredients in ``wiki_sync_ingredients``; drop sources in ``wiki_sync_drops``; the image passes
(wiki thumbnail fallbacks, the local picture cache) in ``wiki_sync_images``. This module holds
only the entry point.
"""

import logging
from datetime import datetime

from models.db import get_db
from services import image_cache, sync_schedule
from services.market_slugs import sync_market_slugs
from services.resurgence_sync import sync_resurgence
from services.wiki_sync_amps import _sync_amps
from services.wiki_sync_drops import (
    _sync_drop_sources, _sync_resource_drops, _sync_wfcd_component_drops,
)
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
