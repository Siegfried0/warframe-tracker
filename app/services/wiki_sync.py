# file-length: exempt (split pending: T-011)
"""
Warframe item sync — fetches items from WFCD warframe-items dataset (primary) and
the Warframe wiki category API (K-Drive boards, Necramechs).

Primary source: https://github.com/WFCD/warframe-items
Wiki fallback:  https://wiki.warframe.com/api.php (standard query API, no Cargo extension needed)

Only catalogue data is written; user progress rows are never touched.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import requests

from models.db import get_db
from models.item import Item

log = logging.getLogger(__name__)

_WFCD_BASE             = 'https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json'
_WIKI_API              = 'https://wiki.warframe.com/api.php'
_WIKI_BASE             = 'https://wiki.warframe.com/w/'
_WARFRAMESTAT_DROPS    = 'https://drops.warframestat.us/data/all.json'

_SOURCE_KEYWORDS = ('prime', 'wraith', 'vandal', 'kuva', 'tenet')
_WFCD_CDN        = 'https://cdn.warframestat.us/img'

# Weapon sub-categories whose wiki component pages omit the "Blueprint" suffix
_WEAPON_CATEGORIES = frozenset({
    'primary', 'secondary', 'melee', 'arch-gun', 'arch-melee',
})

# Necramechs appear in WFCD Warframes.json but are not Warframes.
# They are synced under 'k-drive' by _sync_k_drives() instead.
_NECRAMECHS = frozenset({'Bonewidow', 'Voidrig'})


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
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_wfcd(filename: str) -> list[dict]:
    """Fetch a single WFCD category JSON file and return the parsed array."""
    resp = requests.get(f'{_WFCD_BASE}/{filename}', timeout=60)
    resp.raise_for_status()
    return resp.json()


def _wiki_category_members(category: str) -> list[str]:
    """Return all page titles in a wiki category using the standard MediaWiki query API.
    Does not require any wiki extensions (no Cargo, no SMW).
    """
    params = {
        'action':      'query',
        'list':        'categorymembers',
        'cmtitle':     f'Category:{category}',
        'cmlimit':     500,
        'cmnamespace': 0,       # main namespace only — no talk/module pages
        'format':      'json',
    }
    results = []
    while True:
        resp = requests.get(_WIKI_API, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if 'error' in data:
            raise RuntimeError(
                f"Wiki API error (category={category!r}): {data['error'].get('info', data['error'])}"
            )
        members = data.get('query', {}).get('categorymembers', [])
        results.extend(m['title'] for m in members)
        cont = data.get('continue', {})
        if not cont.get('cmcontinue'):
            break
        params['cmcontinue'] = cont['cmcontinue']
    return results


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


# ---------------------------------------------------------------------------
# Per-category sync functions
# ---------------------------------------------------------------------------

def _sync_warframes() -> int:
    log.info('Syncing warframes…')
    count = sum(
        _upsert_wfcd(item, 'warframe', tradable_override=False)
        for item in _fetch_wfcd('Warframes.json')
        if item.get('masterable') and item.get('name', '').strip() not in _NECRAMECHS
    )
    log.info('Warframes synced: %d', count)
    return count


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
                "UPDATE items SET category = 'resource', source = 'standard' WHERE id = ?",
                (row['id'],),
            )
        else:
            db.execute('DELETE FROM items WHERE id = ?', (row['id'],))
        cleaned += 1

    if cleaned:
        db.commit()
        log.info('%s/%s stale component rows cleaned up: %d', category, source, cleaned)

    return cleaned


def _sync_weapons() -> int:
    files = [
        ('Primary.json',    'primary'),
        ('Secondary.json',  'secondary'),
        ('Melee.json',      'melee'),
        ('Arch-Gun.json',   'arch-gun'),
        ('Arch-Melee.json', 'arch-melee'),
    ]
    total = 0
    # Collect Zaw names to identify stale component rows afterwards.
    # Kitgun chambers are handled separately by _sync_kitgun_chambers().
    synced_zaw_names: set[str] = set()

    for filename, category in files:
        log.info('Syncing %s…', category)
        count = 0
        for item in _fetch_wfcd(filename):
            if not item.get('masterable'):
                continue
            item_type = item.get('type', '').lower()
            if item_type == 'zaw':
                source_override = 'zaw'
                synced_zaw_names.add(item.get('name', '').strip())
            elif item_type in ('kitgun', 'secondary kitgun', 'kitgun chamber'):
                # Kitgun chambers are tracked by _sync_kitgun_chambers(); skip here
                # so we don't pollute the secondary category with source='standard'.
                continue
            else:
                source_override = None
            count += _upsert_wfcd(item, category, source_override=source_override)
        log.info('%s synced: %d', category, count)
        total += count

    # Remove stale Zaw component rows that WFCD may have previously exposed
    # as masterable but are actually component parts (strikes, grips, links…).
    _cleanup_source_components('melee', 'zaw', synced_zaw_names)

    return total


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
        for item in _fetch_wfcd('Secondary.json'):
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
        for item in _fetch_wfcd('Misc.json'):
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
            for title in _wiki_category_members(cat_name):
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
        title_images = _wiki_batch_images(canonical_wiki)
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


def _sync_companions() -> int:
    """Sync Pets (Kavats, Kubrows, Hounds, Vulpaphylas, Predasites) and Sentinels.
    MOAs are excluded here — they go to the k-drive category alongside K-Drive boards
    and Necramechs to match the app's category grouping.
    """
    log.info('Syncing companions…')
    count = 0

    for item in _fetch_wfcd('Pets.json'):
        if item.get('type') != 'Pets':
            continue
        if item.get('name', '').lower().endswith(' moa'):
            continue  # MOAs handled by _sync_k_drives
        count += _upsert_wfcd(item, 'companion')

    for item in _fetch_wfcd('Sentinels.json'):
        if item.get('masterable'):
            count += _upsert_wfcd(item, 'companion')

    log.info('Companions synced: %d', count)
    return count


def _sync_archwings() -> int:
    log.info('Syncing archwings…')
    count = sum(
        _upsert_wfcd(item, 'archwing', tradable_override=False)
        for item in _fetch_wfcd('Archwing.json')
        if item.get('masterable')
    )
    log.info('Archwings synced: %d', count)
    return count


# Words that identify non-item wiki pages (subpages, mod lists, skin articles, etc.)
_WIKI_SKIP_WORDS = frozenset(['/', 'Mod', 'Race', 'Chart', 'Skin', 'SFX'])

# K-Drive component parts that appear in Category:K-Drive but grant NO Mastery XP.
# Source: wiki.warframe.com/w/K-Drive#Components (verified 2026-06-06).
# The wiki has no "K-Drive Boards" subcategory, so this explicit exclusion list is
# the only reliable way to distinguish non-mastery parts from mastery-granting boards.
# Only the five "Board" components (Bad Baby, Flatbelly, Needlenose, Runway, Feverspine)
# grant mastery; all Reactors, Noses, and Jets do not.
_KDRIVE_NON_MASTERY = frozenset({
    # Reactors
    'Arc Twelve', 'Coldfusor', 'Gristlebuck', 'Highbrow', 'Hothead',
    # Noses
    'Beaky', 'Dink-A-Donk', 'Nodulite', 'Two-Sloops', 'Wingnut',
    # Jets
    'Fatboys', 'Steeba', 'Step Tens', 'Thugs', 'Twin Kavats',
})


def _sync_k_drives() -> int:
    """Sync K-Drive boards (wiki category), MOAs (WFCD Pets.json), and Necramechs (wiki category).

    Only the five Board components grant mastery; all Reactors, Noses, and Jets
    are excluded.  Any previously inserted non-mastery rows are cleaned up on sync.
    """
    log.info('Syncing K-Drives, MOAs, Necramechs…')
    count = 0
    wiki_titles: list[str] = []

    # --- MOAs: identified by name ending with " Moa" regardless of WFCD type field ---
    for item in _fetch_wfcd('Pets.json'):
        if item.get('name', '').lower().endswith(' moa'):
            count += _upsert_wfcd(item, 'k-drive')

    # Remove stale MOA rows that may have been inserted under 'companion' before
    # the exclusion was added — upsert uses (name, category) as the unique key so
    # the old companion rows survive alongside the new k-drive rows.
    db = get_db()
    db.execute("DELETE FROM items WHERE category = 'companion' AND LOWER(name) LIKE '% moa'")
    db.commit()

    # --- K-Drive boards: wiki Category:K-Drive, minus meta-pages and non-mastery parts ---
    for title in _wiki_category_members('K-Drive'):
        if title == 'K-Drive' or any(w in title for w in _WIKI_SKIP_WORDS):
            continue
        if title in _KDRIVE_NON_MASTERY:
            continue  # Reactor / Nose / Jet — no mastery, do not upsert
        count += _upsert_wiki(title, 'k-drive')
        wiki_titles.append(title)

    # --- Necramechs: wiki Category:Necramechs, minus meta-pages ---
    for title in _wiki_category_members('Necramechs'):
        if 'Necramech' in title or any(w in title for w in _WIKI_SKIP_WORDS):
            continue
        count += _upsert_wiki(title, 'k-drive')
        wiki_titles.append(title)

    # --- Clean up non-mastery K-Drive rows inserted by earlier syncs ---
    # Match by name against the authoritative exclusion list; do not rely on
    # grants_mastery since pre-migration rows all received DEFAULT 1.
    db = get_db()
    all_kdrive = db.execute(
        "SELECT id, name FROM items WHERE category = 'k-drive'"
    ).fetchall()
    cleaned = 0
    for row in all_kdrive:
        if row['name'] not in _KDRIVE_NON_MASTERY:
            continue
        has_progress = db.execute(
            "SELECT 1 FROM user_progress"
            " WHERE item_id = ? AND (mastered = 1 OR in_possession = 1) LIMIT 1",
            (row['id'],),
        ).fetchone()
        if has_progress:
            log.warning(
                'K-Drive part %r has user progress — moving to resource category to preserve data',
                row['name'],
            )
            db.execute(
                "UPDATE items SET category = 'resource', grants_mastery = 0 WHERE id = ?",
                (row['id'],),
            )
        else:
            db.execute('DELETE FROM items WHERE id = ?', (row['id'],))
        cleaned += 1
    if cleaned:
        db.commit()
        log.info('K-Drive non-mastery parts cleaned up: %d row(s)', cleaned)

    # Populate image_url for wiki-sourced items (no WFCD thumbnail available)
    if wiki_titles:
        title_images = _wiki_batch_images(wiki_titles)
        for title, thumb_url in title_images.items():
            db.execute(
                "UPDATE items SET image_url = ? WHERE name = ? AND category = 'k-drive'",
                (thumb_url, title),
            )
        if title_images:
            db.commit()
        log.info('K-Drive/Necramech wiki images fetched: %d / %d', len(title_images), len(wiki_titles))

    log.info('K-Drives/MOAs/Necramechs synced: %d', count)
    return count


# Wiki skip words for amp pages (same base set; amps have no extra meta-page patterns)
_AMP_SKIP_WORDS = _WIKI_SKIP_WORDS

# In WFCD (Misc.json) and on the wiki, each masterable amp is identified by its
# PRISM name — entries often appear as "Raplak Prism", "Shwaak Prism", etc.
# Stripping " Prism" gives the canonical amp name ("Raplak", "Shwaak", …).
# Scaffold and Brace entries exist in the same data but do NOT represent masterable
# amps and must be skipped entirely.
_AMP_PRISM_SUFFIX = ' Prism'
_AMP_SKIP_COMPONENTS = frozenset({'Scaffold', 'Brace'})  # NOT 'Prism' — handled via canonicalization

# Known amp prism names used as a last-resort seed when all live sources return nothing.
# Each prism type = one masterable amp regardless of scaffold/brace chosen.
_KNOWN_AMPS = frozenset({
    'Raplak', 'Shwaak', 'Granmu',              # tier 1–3, Quills (Unum)
    'Cantic', 'Certus', 'Klebrik', 'Lega',     # tier 4–7, Vox Solaris / Necraloid
    'Raplak Prime', 'Shwaak Prime', 'Granmu Prime',
    'Cantic Prime', 'Certus Prime', 'Klebrik Prime', 'Lega Prime',
})


def _sync_amps() -> int:
    """Sync Operator amps from WFCD (primary) or wiki Category:Amps (fallback).

    Each unique amp configuration grants 1 500 MR points (50 pts/rank × 30 ranks).

    WFCD (and the wiki) represent each masterable amp by its PRISM component, often
    named "<Name> Prism" (e.g. "Raplak Prism").  _upsert_amp_canonical() strips the
    " Prism" suffix so the DB stores the clean canonical name ("Raplak").  Scaffold
    and Brace entries are skipped entirely — they are not masterable in isolation.

    Sources tried in order:
      1. WFCD Amps.json (dedicated file, if it exists in the dataset)
      2. WFCD Misc.json filtered for type == 'Amp' (masterable flag not required)
      3. Wiki Category:Amps (filtered for meta pages)
      4. Hardcoded _KNOWN_AMPS list (last resort when all live sources return nothing)
    """
    log.info('Syncing amps…')
    count = 0
    synced_names: set[str] = set()

    def _canonical(raw: str) -> str:
        return raw[:-len(_AMP_PRISM_SUFFIX)] if raw.endswith(_AMP_PRISM_SUFFIX) else raw

    def _upsert_amp(raw_name: str, item: dict | None = None) -> bool:
        """Canonicalize, skip Scaffold/Brace, deduplicate, then upsert. Returns True if written."""
        if any(kw in raw_name for kw in _AMP_SKIP_COMPONENTS):
            return False
        canon = _canonical(raw_name)
        if canon in synced_names:
            return False
        wiki_url = (item.get('wikiaUrl') if item else None) or (_WIKI_BASE + canon.replace(' ', '_'))
        Item.upsert(
            name=canon,
            category='amp',
            source=_detect_source(canon),
            wiki_url=wiki_url,
            vaulted=bool(item.get('vaulted', False)) if item else False,
            tradable=False,
            image_url=_wfcd_image_url(item) if item else None,
            grants_mastery=True,
        )
        synced_names.add(canon)
        return True

    def _cleanup_amps() -> None:
        for src in ('standard', 'prime'):
            _cleanup_source_components('amp', src, synced_names)

    # --- Primary: WFCD dedicated Amps.json ---
    try:
        for item in _fetch_wfcd('Amps.json'):
            name = item.get('name', '').strip()
            if name and item.get('masterable') and _upsert_amp(name, item):
                count += 1
        if count:
            log.info('Amps synced from WFCD Amps.json: %d', count)
            _cleanup_amps()
            return count
        log.info('WFCD Amps.json returned no masterable entries — trying Misc.json')
    except Exception as exc:
        log.info('WFCD Amps.json not available (%s) — trying Misc.json', exc)

    # --- Secondary: WFCD Misc.json filtered by type == 'Amp' ---
    # In WFCD Misc.json, amp prism entries appear with type 'Amp' and are named
    # "<Name> Prism" (e.g. "Raplak Prism").  _upsert_amp() strips the suffix so
    # the canonical name "Raplak" is stored.  The masterable flag is not required
    # here — it is often absent for amp entries in Misc.json.
    try:
        for item in _fetch_wfcd('Misc.json'):
            if item.get('type', '').strip().lower() != 'amp':
                continue
            name = item.get('name', '').strip()
            if name and _upsert_amp(name, item):
                count += 1
        if count:
            log.info('Amps synced from WFCD Misc.json: %d', count)
            _cleanup_amps()
            return count
        log.info('No amp-typed entries in WFCD Misc.json — falling back to wiki')
    except Exception as exc:
        log.warning('Could not fetch WFCD Misc.json (%s) — falling back to wiki', exc)

    # --- Fallback: wiki Category:Amps ---
    # Wiki titles may include "Raplak Prism", "Raplak Scaffold", etc.
    # _upsert_amp() canonicalizes and skips non-masterable component entries.
    raw_wiki_titles: list[str] = []
    try:
        for title in _wiki_category_members('Amps'):
            if not any(w in title for w in _AMP_SKIP_WORDS):
                raw_wiki_titles.append(title)
    except Exception as exc:
        log.warning('Wiki Category:Amps lookup failed (%s) — using hardcoded fallback', exc)

    canonical_wiki: list[str] = []
    for title in raw_wiki_titles:
        if _upsert_amp(title):
            canonical_wiki.append(_canonical(title))
            count += 1

    if canonical_wiki:
        db = get_db()
        title_images = _wiki_batch_images(canonical_wiki)
        for title, thumb_url in title_images.items():
            db.execute(
                "UPDATE items SET image_url = ? WHERE name = ? AND category = 'amp'",
                (thumb_url, title),
            )
        if title_images:
            db.commit()
        log.info('Amp wiki images fetched: %d / %d', len(title_images), len(canonical_wiki))

    if count:
        log.info('Amps synced from wiki: %d', count)
        _cleanup_amps()
        return count

    # --- Last resort: hardcoded known amp prisms ---
    log.warning(
        'All amp sync sources returned nothing — seeding from hardcoded list (%d entries)',
        len(_KNOWN_AMPS),
    )
    for name in sorted(_KNOWN_AMPS):
        if _upsert_amp(name):
            count += 1
    log.info('Amps seeded from hardcoded fallback: %d', count)
    _cleanup_amps()
    return count


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
        mods_data = _fetch_wfcd('Mods.json')
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
        arc_data = _fetch_wfcd('Arcanes.json')
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
        misc_data = _fetch_wfcd('Misc.json')
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
        for title in _wiki_category_members('Arcanes'):
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
        title_images = _wiki_batch_images(wiki_titles)
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
        items_data = _fetch_wfcd('Resources.json')
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
        )
        count += 1

    log.info('Resources synced: %d', count)
    return count


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


def _sync_component_ingredients() -> int:
    """Rebuild component_ingredients table from WFCD item component data.

    Each WFCD component that has a nested components[] array represents a
    craftable sub-item. Those nested entries are the crafting ingredients
    (e.g., Alloy Plate × 500, Morphics × 2, etc.).
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
        log.warning('item_component_meta not found — restart the server to apply migration v5')

    count = 0
    for filename, category in _INGREDIENT_SOURCES:
        try:
            items_data = _fetch_wfcd(filename)
        except Exception as exc:
            log.warning('Skipping %s for ingredient sync: %s', filename, exc)
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


def _fetch_wiki_module(title: str) -> str:
    """Return the wikitext content of a MediaWiki module page."""
    params = {
        'action':   'query',
        'prop':     'revisions',
        'rvprop':   'content',
        'rvslots':  'main',
        'titles':   title,
        'format':   'json',
    }
    resp = requests.get(_WIKI_API, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    pages = data.get('query', {}).get('pages', {})
    for pid, page in pages.items():
        if pid == '-1':
            raise RuntimeError(f'Wiki module not found: {title}')
        revs = page.get('revisions', [])
        if not revs:
            raise RuntimeError(f'No revisions for wiki module: {title}')
        slots = revs[0].get('slots', {})
        if slots:
            return slots.get('main', {}).get('*', '')
        return revs[0].get('*', '')
    raise RuntimeError(f'Empty response for wiki module: {title}')


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
        content = _fetch_wiki_module(_MODULE_BLUEPRINTS)
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

def _images_dir() -> Path:
    """Return (and create) the local item image cache directory."""
    from flask import current_app
    d = Path(current_app.root_path) / 'static' / 'images' / 'items'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url_ext(url: str) -> str:
    """Infer a safe file extension from a URL, defaulting to .png."""
    _, ext = os.path.splitext(url.split('?')[0].rstrip('/'))
    return ext.lower() if ext.lower() in ('.png', '.jpg', '.jpeg', '.webp') else '.png'


def _download_image(url: str, dest: Path) -> bool:
    """Stream-download url to dest. Return True on success."""
    try:
        r = requests.get(url, timeout=15, stream=True)
        r.raise_for_status()
        with dest.open('wb') as fh:
            for chunk in r.iter_content(8192):
                fh.write(chunk)
        return True
    except Exception as exc:
        log.debug('Image download failed (%s): %s', url, exc)
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        return False


def _cached_local_url(item_id: int, images_dir: Path) -> str | None:
    """Return the /static/... URL if a local image already exists for this item."""
    for ext in ('.png', '.jpg', '.jpeg', '.webp'):
        if (images_dir / f'{item_id}{ext}').exists():
            return f'/static/images/items/{item_id}{ext}'
    return None


def _wiki_batch_images(titles: list[str]) -> dict[str, str]:
    """Batch-fetch thumbnail URLs from the wiki for a list of page titles.
    Returns {title: thumbnail_url}. Uses up to 50 titles per API call.
    """
    if not titles:
        return {}
    result: dict[str, str] = {}
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        params = {
            'action':      'query',
            'titles':      '|'.join(batch),
            'prop':        'pageimages',
            'pithumbsize': 200,
            'format':      'json',
        }
        try:
            resp = requests.get(_WIKI_API, params=params, timeout=20)
            resp.raise_for_status()
            for page in resp.json().get('query', {}).get('pages', {}).values():
                title = page.get('title', '')
                thumb = page.get('thumbnail', {}).get('source')
                if title and thumb:
                    result[title] = thumb
        except Exception as exc:
            log.warning('Wiki batch image fetch failed: %s', exc)
    return result


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

    title_images = _wiki_batch_images(list(title_to_id.keys()))
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

    title_images = _wiki_batch_images(list(title_to_id.keys()))

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
    - Items whose local file already exists from a previous sync are updated in
      the DB without re-downloading.
    Returns (downloaded, already_cached) counts.
    """
    rows = db.execute(
        "SELECT id, image_url FROM items WHERE image_url IS NOT NULL AND image_url NOT LIKE '/static/%'"
    ).fetchall()
    if not rows:
        return 0, 0

    images_dir = _images_dir()
    downloaded = 0
    already_cached = 0

    for row in rows:
        item_id = row['id']
        url = row['image_url']

        local_url = _cached_local_url(item_id, images_dir)
        if local_url:
            db.execute('UPDATE items SET image_url = ? WHERE id = ?', (local_url, item_id))
            already_cached += 1
            continue

        ext = _url_ext(url)
        dest = images_dir / f'{item_id}{ext}'
        if _download_image(url, dest):
            local_url = f'/static/images/items/{item_id}{ext}'
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

    title_images = _wiki_batch_images(list(title_map.keys()))

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

def _components_images_dir() -> Path:
    """Return (and create) the local component image cache directory."""
    from flask import current_app
    d = Path(current_app.root_path) / 'static' / 'images' / 'components'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _comp_slug(component_name: str) -> str:
    """Safe filename segment for a component name."""
    return component_name.lower().replace(' ', '_').replace('/', '-')


def _sync_component_images(db) -> tuple[int, int]:
    """Download and locally cache images for all component entries in item_component_meta
    that still have a remote URL.

    Returns (downloaded, already_cached) counts.
    """
    rows = db.execute(
        "SELECT item_id, component_name, image_url FROM item_component_meta "
        "WHERE image_url IS NOT NULL AND image_url NOT LIKE '/static/%'"
    ).fetchall()
    if not rows:
        return 0, 0

    images_dir = _components_images_dir()
    downloaded = 0
    already_cached = 0

    for row in rows:
        item_id = row['item_id']
        comp_name = row['component_name']
        url = row['image_url']
        slug = _comp_slug(comp_name)

        # Check if already downloaded
        local_url = None
        for ext in ('.png', '.jpg', '.jpeg', '.webp'):
            p = images_dir / f'{item_id}__{slug}{ext}'
            if p.exists():
                local_url = f'/static/images/components/{item_id}__{slug}{ext}'
                break

        if local_url:
            db.execute(
                'UPDATE item_component_meta SET image_url = ? WHERE item_id = ? AND component_name = ?',
                (local_url, item_id, comp_name),
            )
            already_cached += 1
            continue

        ext = _url_ext(url)
        dest = images_dir / f'{item_id}__{slug}{ext}'
        if _download_image(url, dest):
            local_url = f'/static/images/components/{item_id}__{slug}{ext}'
            db.execute(
                'UPDATE item_component_meta SET image_url = ? WHERE item_id = ? AND component_name = ?',
                (local_url, item_id, comp_name),
            )
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
    """Return item_id if drop_name starts with a known item name, else None."""
    for item_name, item_id in prefix_lookup:
        if drop_name == item_name or drop_name.startswith(item_name + ' '):
            return item_id
    return None


def _sync_resource_drops(db) -> int:
    """Insert drop_sources for resource items from WFCD Resources.json."""
    try:
        items_data = _fetch_wfcd('Resources.json')
    except Exception as exc:
        log.warning('Could not fetch Resources.json for drop sync: %s', exc)
        return 0

    count = 0
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
            location = drop.get('location', '').strip()
            chance   = drop.get('chance')
            if not location or not chance:
                continue
            avg_runs = round(100.0 / chance, 1) if chance > 0 else None
            db.execute(
                '''INSERT INTO drop_sources
                       (item_id, mission_type, location, drop_chance,
                        avg_runs, source_type, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (item_id, drop.get('type', 'Unknown'), location,
                 round(chance, 4), avg_runs, 'resource', name),
            )
            count += 1
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
            items_data = _fetch_wfcd(filename)
        except Exception as exc:
            log.warning('Skipping %s for drop sync: %s', filename, exc)
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
                comp_name = comp.get('name', '')
                for drop in comp.get('drops', []):
                    location = drop.get('location', '').strip()
                    chance   = drop.get('chance')
                    if not location or not chance:
                        continue
                    avg_runs = round(100.0 / chance, 1) if chance > 0 else None
                    db.execute(
                        '''INSERT INTO drop_sources
                               (item_id, mission_type, location, drop_chance,
                                avg_runs, source_type, notes)
                           VALUES (?, ?, ?, ?, ?, ?, ?)''',
                        (item_id, drop.get('type', 'Unknown'), location,
                         round(chance, 4), avg_runs, 'component', comp_name),
                    )
                    count += 1
    return count


def _sync_mission_reward_drops(db,
                               prefix_lookup: list[tuple[str, int]]) -> int:
    """Insert drop_sources from warframestat.us mission reward tables.

    Matches each drop entry name against our items catalogue using prefix
    matching (longest item name first), so 'Ash Prime Neuroptics Blueprint'
    correctly resolves to 'Ash Prime' and not 'Ash'.
    """
    try:
        resp = requests.get(_WARFRAMESTAT_DROPS, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning('Could not fetch warframestat drop tables (non-fatal): %s', exc)
        return 0

    count = 0
    for planet, nodes in data.get('missionRewards', {}).items():
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
            else:
                reward_list = [
                    (rot, lst)
                    for rot, lst in rewards.items()
                    if isinstance(lst, list)
                ]

            for rotation, items in reward_list:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    drop_name = item.get('itemName', '').strip()
                    chance    = item.get('chance')
                    if not drop_name or not chance:
                        continue
                    item_id = _match_drop_to_item(drop_name, prefix_lookup)
                    if not item_id:
                        continue
                    avg_runs = round(100.0 / chance, 1) if chance > 0 else None
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
    """Rebuild drop_sources from WFCD component data, resource drops, and warframestat.us mission tables."""
    log.info('Syncing drop sources…')
    db = get_db()
    db.execute('DELETE FROM drop_sources')

    count_comp = _sync_wfcd_component_drops(db)
    log.info('WFCD component drops: %d', count_comp)

    count_res = _sync_resource_drops(db)
    log.info('Resource drops: %d', count_res)

    prefix_lookup  = _build_item_prefix_lookup(db)
    count_missions = _sync_mission_reward_drops(db, prefix_lookup)
    log.info('Mission reward drops: %d', count_missions)

    db.commit()
    total = count_comp + count_res + count_missions
    log.info('Drop sources total: %d', total)
    return total


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_sync(app=None):
    """Run a full item sync. Pass the Flask app when calling outside a request context."""
    from flask import has_app_context
    ctx = app.app_context() if (app and not has_app_context()) else None
    try:
        if ctx:
            ctx.push()
        log.info('Sync started at %s', datetime.utcnow().isoformat())
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
            'ingredients':  _sync_component_ingredients(),
            'drop_sources': _sync_drop_sources(),
        }
        db = get_db()
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
        total = sum(v for k, v in counts.items() if not k.startswith('images_'))
        log.info('Sync complete at %s — %d total items', datetime.utcnow().isoformat(), total)
        return {'ok': True, 'counts': counts, 'total': total}
    except Exception as exc:
        log.exception('Sync failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
    finally:
        if ctx:
            ctx.pop()
