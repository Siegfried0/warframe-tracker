"""Mods, arcanes and resources: the catalogue's upgrades and crafting materials."""

import logging

from models.db import get_db
from models.item import Item
from services import wiki_sync_requests
from services.wiki_sync_catalogue import _WIKI_BASE, _WIKI_SKIP_WORDS, _wfcd_image_url

# The sync's log lines keep the logger name an operator filters on.
log = logging.getLogger('services.wiki_sync')


# ---------------------------------------------------------------------------
# Mod sync
# ---------------------------------------------------------------------------

# Riven mod type suffixes — used to detect and deduplicate individual riven entries
# down to one generic "X Riven Mod" entry per weapon class.
_RIVEN_SUFFIX = 'riven mod'


def _sync_mods() -> int:
    """Sync mods from WFCD Mods.json.

    - Stores mod_type, mod_polarity, mod_max_rank.
    - Rivens are collapsed to one generic entry per weapon class (e.g. 'Rifle Riven Mod')
      because individual riven stats are not trackable in a collection.
    - grants_mastery is always False (mods contribute 0 MR).
    """
    log.info('Syncing mods…')
    try:
        mods_data = wiki_sync_requests._fetch_wfcd('Mods.json')
    except Exception as exc:
        log.warning('Could not fetch Mods.json: %s', exc)
        return 0

    count = 0
    seen_riven_classes: set[str] = set()

    for item in mods_data:
        name = item.get('name', '').strip()
        if not name:
            continue

        raw_type   = item.get('type', '').strip()
        polarity   = item.get('polarity', '').strip()
        max_rank   = item.get('fusionLimit')
        if max_rank is None:
            max_rank = item.get('ranks')

        # Rivens: deduplicate to one generic entry per weapon class.
        if _RIVEN_SUFFIX in raw_type.lower():
            riven_class = raw_type  # e.g. "Rifle Riven Mod"
            if riven_class in seen_riven_classes:
                continue
            seen_riven_classes.add(riven_class)
            Item.upsert(
                name=riven_class,
                category='mod',
                source='standard',
                wiki_url=_WIKI_BASE + 'Riven_Mod',
                vaulted=False,
                tradable=True,
                image_url=_wfcd_image_url(item),
                grants_mastery=False,
                mod_type=raw_type,
                mod_polarity='Any',
                mod_max_rank=0,
            )
            count += 1
            continue

        Item.upsert(
            name=name,
            category='mod',
            source='standard',
            wiki_url=item.get('wikiaUrl') or (_WIKI_BASE + name.replace(' ', '_')),
            vaulted=False,
            tradable=bool(item.get('tradable', False)),
            image_url=_wfcd_image_url(item),
            grants_mastery=False,
            mod_type=raw_type or None,
            mod_polarity=polarity or None,
            mod_max_rank=int(max_rank) if max_rank is not None else None,
        )
        count += 1

    log.info('Mods synced: %d', count)
    return count


# ---------------------------------------------------------------------------
# Arcane sync
# ---------------------------------------------------------------------------

# Keywords that identify arcane enhancement entries in WFCD Misc.json.
# Type values seen in practice: 'Arcane Enhancement', 'Arcane'
_ARCANE_TYPES = frozenset({'arcane enhancement', 'arcane'})

# Hardcoded seed when all live sources fail.
_KNOWN_ARCANES = frozenset({
    'Arcane Acceleration', 'Arcane Agility', 'Arcane Arachne',
    'Arcane Avenger', 'Arcane Awakening', 'Arcane Barrier',
    'Arcane Blessing', 'Arcane Bodyguard', 'Arcane Consequence',
    'Arcane Courage', 'Arcane Deflection', 'Arcane Eruption',
    'Arcane Excellia', 'Arcane Force', 'Arcane Fury',
    'Arcane Grace', 'Arcane Guardian', 'Arcane Healing',
    'Arcane Ice', 'Arcane Intention', 'Arcane Invigoration',
    'Arcane Momentum', 'Arcane Nullifier', 'Arcane Phantasm',
    'Arcane Precision', 'Arcane Proximity', 'Arcane Pulse',
    'Arcane Rage', 'Arcane Resistance', 'Arcane Rise',
    'Arcane Serenity', 'Arcane Steadfast', 'Arcane Strike',
    'Arcane Tanker', 'Arcane Tempo', 'Arcane Trickery',
    'Arcane Ultimatum', 'Arcane Velocity', 'Arcane Victory',
    'Arcane Warmth', 'Arcane Wyrm',
})


def _sync_arcanes() -> int:
    """Sync Arcane Enhancements.

    Sources tried in order:
      1. WFCD Arcanes.json (if it exists)
      2. WFCD Misc.json filtered for arcane-type entries
      3. Wiki Category:Arcanes (filtered for meta pages)
      4. Hardcoded _KNOWN_ARCANES seed

    mod_max_rank is populated from WFCD fusionLimit (typically 3 or 5).
    grants_mastery is always False.
    """
    log.info('Syncing arcanes…')
    count = 0

    def _upsert_arcane(item: dict) -> bool:
        name = item.get('name', '').strip()
        if not name:
            return False
        max_rank = item.get('fusionLimit')
        if max_rank is None:
            max_rank = item.get('ranks')
        Item.upsert(
            name=name,
            category='arcane',
            source='standard',
            wiki_url=item.get('wikiaUrl') or (_WIKI_BASE + name.replace(' ', '_')),
            vaulted=False,
            tradable=bool(item.get('tradable', True)),
            image_url=_wfcd_image_url(item),
            grants_mastery=False,
            mod_type=None,
            mod_polarity=None,
            mod_max_rank=int(max_rank) if max_rank is not None else None,
        )
        return True

    # --- Primary: WFCD dedicated Arcanes.json ---
    try:
        arc_data = wiki_sync_requests._fetch_wfcd('Arcanes.json')
        for item in arc_data:
            count += _upsert_arcane(item)
        if count:
            log.info('Arcanes synced from WFCD Arcanes.json: %d', count)
            return count
        log.info('WFCD Arcanes.json empty — trying Misc.json')
    except Exception as exc:
        log.info('WFCD Arcanes.json not available (%s) — trying Misc.json', exc)

    # --- Secondary: WFCD Misc.json filtered for arcane type ---
    try:
        misc_data = wiki_sync_requests._fetch_wfcd('Misc.json')
        for item in misc_data:
            if item.get('type', '').strip().lower() in _ARCANE_TYPES:
                count += _upsert_arcane(item)
        if count:
            log.info('Arcanes synced from WFCD Misc.json: %d', count)
            return count
        log.info('No arcane entries in WFCD Misc.json — falling back to wiki')
    except Exception as exc:
        log.warning('Could not fetch WFCD Misc.json (%s) — falling back to wiki', exc)

    # --- Fallback: wiki Category:Arcanes ---
    wiki_titles: list[str] = []
    try:
        for title in wiki_sync_requests._wiki_category_members('Arcanes'):
            if any(w in title for w in _WIKI_SKIP_WORDS):
                continue
            wiki_titles.append(title)
    except Exception as exc:
        log.warning('Wiki Category:Arcanes lookup failed (%s) — using hardcoded fallback', exc)

    for title in wiki_titles:
        Item.upsert(
            name=title,
            category='arcane',
            source='standard',
            wiki_url=_WIKI_BASE + title.replace(' ', '_'),
            vaulted=False,
            tradable=True,
            grants_mastery=False,
        )
        count += 1

    if wiki_titles:
        db = get_db()
        title_images = wiki_sync_requests._wiki_batch_images(wiki_titles)
        for title, thumb_url in title_images.items():
            db.execute(
                "UPDATE items SET image_url = ? WHERE name = ? AND category = 'arcane'",
                (thumb_url, title),
            )
        if title_images:
            db.commit()
        log.info('Arcane wiki images fetched: %d / %d', len(title_images), len(wiki_titles))

    if count:
        log.info('Arcanes synced from wiki: %d', count)
        return count

    # --- Last resort: hardcoded seed ---
    log.warning('All arcane sources returned nothing — seeding from hardcoded list')
    for name in sorted(_KNOWN_ARCANES):
        Item.upsert(
            name=name,
            category='arcane',
            source='standard',
            wiki_url=_WIKI_BASE + name.replace(' ', '_'),
            vaulted=False,
            tradable=True,
            grants_mastery=False,
        )
        count += 1
    log.info('Arcanes seeded from hardcoded fallback: %d', count)
    return count


# ---------------------------------------------------------------------------
# Resource sync
# ---------------------------------------------------------------------------

# WFCD resource types we want in our catalogue; ignore Orokin Cells etc. listed
# under other categories that are not farmable items.
_RESOURCE_TYPES = frozenset({
    'Resource', 'OrokinReactor', 'OrokinCatalyst',  # typed resources
})

def _sync_resources() -> int:
    """Sync farmable resources from WFCD Resources.json."""
    log.info('Syncing resources…')
    count = 0
    try:
        items_data = wiki_sync_requests._fetch_wfcd('Resources.json')
    except Exception as exc:
        log.warning('Could not fetch Resources.json: %s', exc)
        return 0

    for item in items_data:
        name = item.get('name', '').strip()
        if not name:
            continue
        wiki_url = item.get('wikiaUrl') or (_WIKI_BASE + name.replace(' ', '_'))
        Item.upsert(
            name=name,
            category='resource',
            source='standard',
            wiki_url=wiki_url,
            vaulted=False,
            tradable=bool(item.get('tradable', False)),
            image_url=_wfcd_image_url(item),
            grants_mastery=False,
        )
        count += 1

    log.info('Resources synced: %d', count)
    return count
