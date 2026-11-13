"""Kitgun chambers: the per-chamber masterable secondary weapons, from WFCD, the wiki and
a curated list."""

import logging

from models.db import get_db
from models.item import Item
from services import wiki_sync_requests
from services.wiki_sync_catalogue import (
    _WIKI_BASE, _WIKI_SKIP_WORDS, _cleanup_source_components, _wfcd_image_url,
)

# The sync's log lines keep the logger name an operator filters on.
log = logging.getLogger('services.wiki_sync')


# ---------------------------------------------------------------------------
# Kitgun chamber sync
# ---------------------------------------------------------------------------

# In WFCD / the wiki, kitgun chambers appear with a " Chamber" suffix
# (e.g. "Catchmoon Chamber").  Stripping that suffix gives the canonical name
# ("Catchmoon") which matches how the mastery screen labels the weapon.
# The ONLY reliable positive signal for a chamber is this suffix — negative
# word-lists ("not a grip/loader/…") are fragile because part names can be
# arbitrary (e.g. "Haymaker" does not contain the word "Grip").
_KITGUN_CHAMBER_SUFFIX = ' Chamber'

# Comprehensive list of known kitgun chambers used as a guaranteed supplement.
# This runs unconditionally so chambers absent from WFCD (e.g. newer Deimos entries)
# are always inserted regardless of what live sources returned.
# Source: https://wiki.warframe.com/w/Kitgun (verified 2026-06-06)
_KNOWN_KITGUN_CHAMBERS = frozenset({
    # Vox Solaris (Fortuna, Venus)
    'Catchmoon', 'Tombfinger', 'Rattleguts', 'Gaze',
    # Entrati (Deimos, Cambion Drift)
    'Sporelacer', 'Vermisplicer',
})


def _sync_kitgun_chambers() -> int:
    """Sync Kitgun chambers — the per-chamber masterable secondary weapons.

    In Warframe, mastery XP is granted once per unique chamber type when a kitgun
    built with that chamber is maxed.  Grip and Loader choices do not affect
    mastery tracking.

    The ONLY reliable way to identify a chamber entry in WFCD or the wiki is
    the ' Chamber' name suffix.  Negative word-lists ('not a grip/loader') are
    fragile because part names can be arbitrary strings with no shared pattern.

    Source strategy:
      Both WFCD passes (Secondary.json and Misc.json) always run so that chambers
      from different factions/updates (e.g. Fortuna vs. Deimos) are each captured
      from whichever file WFCD places them in.
      The wiki and hardcoded seed are only used when WFCD found nothing at all.

      1. WFCD Secondary.json — components[] arrays of all secondary weapon entries;
         only sub-entries whose name ends in ' Chamber' are accepted.
         Top-level entries are also checked when type == 'Kitgun Chamber' exactly.
      2. WFCD Misc.json — entries with type == 'Kitgun Chamber' exactly.
         (Always runs even if Secondary.json already found chambers.)
      3. Wiki Category:Kitgun Chambers → Category:Kitguns (Chamber-suffix filter).
      4. Hardcoded _KNOWN_KITGUN_CHAMBERS seed (last resort).

    All entries are stored as category='secondary', source='kitgun', grants_mastery=True.
    The canonical name has ' Chamber' stripped (e.g. "Catchmoon Chamber" → "Catchmoon").
    """
    log.info('Syncing Kitgun chambers…')
    count = 0
    synced_names: set[str] = set()

    def _canonical(raw: str) -> str:
        return raw[:-len(_KITGUN_CHAMBER_SUFFIX)] if raw.endswith(_KITGUN_CHAMBER_SUFFIX) else raw

    def _upsert_chamber(raw_name: str, item: dict | None = None) -> bool:
        """Strip ' Chamber' suffix, deduplicate, upsert.  No negative word-list gating —
        callers are responsible for only passing valid chamber names."""
        canon = _canonical(raw_name)
        if not canon or canon in synced_names:
            return False
        # Point the wiki URL at the ' Chamber' page (e.g. "Catchmoon_Chamber")
        # since that is the actual wiki article, not "Catchmoon".
        wiki_name = raw_name if raw_name.endswith(_KITGUN_CHAMBER_SUFFIX) else canon + _KITGUN_CHAMBER_SUFFIX
        wiki_url = (item.get('wikiaUrl') if item else None) or (
            _WIKI_BASE + wiki_name.replace(' ', '_')
        )
        Item.upsert(
            name=canon,
            category='secondary',
            source='kitgun',
            wiki_url=wiki_url,
            vaulted=False,
            tradable=False,
            image_url=_wfcd_image_url(item) if item else None,
            grants_mastery=True,
        )
        synced_names.add(canon)
        return True

    # --- Pass 1: WFCD Secondary.json ---
    # (a) Component sub-entries: name MUST end in ' Chamber' — definitive filter.
    # (b) Top-level entries: type MUST be 'Kitgun Chamber' exactly AND name ends in ' Chamber'.
    # No masterable-flag check; WFCD is inconsistent about this field for modular parts.
    try:
        sec_count = 0
        for item in wiki_sync_requests._fetch_wfcd('Secondary.json'):
            item_type = item.get('type', '').strip().lower()
            name = item.get('name', '').strip()
            # (a) top-level with unambiguous type
            if item_type == 'kitgun chamber' and name.endswith(_KITGUN_CHAMBER_SUFFIX):
                if _upsert_chamber(name, item):
                    sec_count += 1
            # (b) component sub-entries — scan every secondary weapon's parts list
            for comp in item.get('components', []):
                cname = comp.get('name', '').strip()
                if cname.endswith(_KITGUN_CHAMBER_SUFFIX):
                    if _upsert_chamber(cname, comp):
                        sec_count += 1
        if sec_count:
            log.info('Kitgun chambers from WFCD Secondary.json: %d', sec_count)
        count += sec_count
    except Exception as exc:
        log.info('WFCD Secondary.json kitgun scan failed (%s)', exc)

    # --- Pass 2: WFCD Misc.json (always runs — may carry chambers from different factions) ---
    # Accept any entry whose type contains 'kitgun' AND whose name ends in ' Chamber'.
    # The name suffix is the reliable positive filter; the type string is inconsistent
    # across WFCD updates (may be 'Kitgun', 'Kitgun Chamber', 'Secondary Kitgun', etc.)
    # Grips, Loaders, and Braces never have names ending in ' Chamber', so they are
    # safely excluded by the suffix check alone.
    try:
        misc_count = 0
        for item in wiki_sync_requests._fetch_wfcd('Misc.json'):
            if 'kitgun' not in item.get('type', '').strip().lower():
                continue
            name = item.get('name', '').strip()
            if name.endswith(_KITGUN_CHAMBER_SUFFIX) and _upsert_chamber(name, item):
                misc_count += 1
        if misc_count:
            log.info('Kitgun chambers from WFCD Misc.json: %d', misc_count)
        count += misc_count
    except Exception as exc:
        log.info('WFCD Misc.json kitgun scan failed (%s)', exc)

    # --- Pass 3: wiki category lookup (always runs — supplements WFCD, not a fallback) ---
    # WFCD only covers items present in its dataset; chambers from newer factions
    # (e.g. Deimos) may only exist on the wiki.  Running this pass unconditionally
    # ensures full coverage.  _upsert_chamber deduplicates, so Fortuna chambers
    # already found by WFCD are silently skipped here.
    # Title MUST end in ' Chamber' — the only reliable positive signal.
    raw_wiki_titles: list[str] = []
    for cat_name in ('Kitgun Chambers', 'Kitguns'):
        try:
            for title in wiki_sync_requests._wiki_category_members(cat_name):
                if any(w in title for w in _WIKI_SKIP_WORDS):
                    continue
                if title.endswith(_KITGUN_CHAMBER_SUFFIX):
                    raw_wiki_titles.append(title)
            if raw_wiki_titles:
                log.info('Wiki Category:%s — %d Chamber page(s) found', cat_name, len(raw_wiki_titles))
                break   # found results; don't double-count from the other category
        except Exception as exc:
            log.warning('Wiki Category:%s lookup failed (%s)', cat_name, exc)

    wiki_count = 0
    canonical_wiki: list[str] = []
    for title in raw_wiki_titles:
        if _upsert_chamber(title):
            canonical_wiki.append(_canonical(title))
            wiki_count += 1

    if canonical_wiki:
        db = get_db()
        title_images = wiki_sync_requests._wiki_batch_images(canonical_wiki)
        for title, thumb_url in title_images.items():
            db.execute(
                "UPDATE items SET image_url = ? WHERE name = ? AND category = 'secondary' AND source = 'kitgun'",
                (thumb_url, title),
            )
        if title_images:
            db.commit()
        log.info('Kitgun chamber wiki images: %d / %d', len(title_images), len(canonical_wiki))

    if wiki_count:
        log.info('Kitgun chambers from wiki (new beyond WFCD): %d', wiki_count)
    count += wiki_count

    # --- Pass 4: hardcoded supplement (always runs) ---
    # _KNOWN_KITGUN_CHAMBERS is a curated list that covers chambers absent from WFCD
    # or not yet categorised on the wiki (e.g. recently added Deimos chambers).
    # _upsert_chamber deduplicates, so chambers already found above are no-ops here.
    seed_count = 0
    for name in sorted(_KNOWN_KITGUN_CHAMBERS):
        if _upsert_chamber(name):
            seed_count += 1
    if seed_count:
        log.info('Kitgun chambers added from hardcoded supplement: %d', seed_count)
        count += seed_count
    elif not count:
        log.warning('No kitgun chambers found from any source — hardcoded list also empty')

    # Remove stale kitgun rows inserted by any previous incorrect sync run.
    _cleanup_source_components('secondary', 'kitgun', synced_names)

    log.info('Kitgun chambers total: %d', count)
    return count
