"""Warframes, weapons, companions, archwings and K-Drives (with MOAs and Necramechs): the
masterable gear the sync reads from WFCD and the wiki category API."""

import logging

from models.db import get_db
from services import wiki_sync_requests
from services.wiki_sync_catalogue import (
    _NECRAMECHS, _WIKI_SKIP_WORDS, _cleanup_source_components, _upsert_wfcd, _upsert_wiki,
)

# The sync's log lines keep the logger name an operator filters on.
log = logging.getLogger('services.wiki_sync')


# ---------------------------------------------------------------------------
# Per-category sync functions
# ---------------------------------------------------------------------------

def _sync_warframes() -> int:
    log.info('Syncing warframes…')
    count = sum(
        _upsert_wfcd(item, 'warframe', tradable_override=False)
        for item in wiki_sync_requests._fetch_wfcd('Warframes.json')
        if item.get('masterable') and item.get('name', '').strip() not in _NECRAMECHS
    )
    log.info('Warframes synced: %d', count)
    return count


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
        for item in wiki_sync_requests._fetch_wfcd(filename):
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


def _sync_companions() -> int:
    """Sync Pets (Kavats, Kubrows, Hounds, Vulpaphylas, Predasites) and Sentinels.
    MOAs are excluded here — they go to the k-drive category alongside K-Drive boards
    and Necramechs to match the app's category grouping.
    """
    log.info('Syncing companions…')
    count = 0

    for item in wiki_sync_requests._fetch_wfcd('Pets.json'):
        if item.get('type') != 'Pets':
            continue
        if item.get('name', '').lower().endswith(' moa'):
            continue  # MOAs handled by _sync_k_drives
        count += _upsert_wfcd(item, 'companion')

    for item in wiki_sync_requests._fetch_wfcd('Sentinels.json'):
        if item.get('masterable'):
            count += _upsert_wfcd(item, 'companion')

    log.info('Companions synced: %d', count)
    return count


def _sync_archwings() -> int:
    log.info('Syncing archwings…')
    count = sum(
        _upsert_wfcd(item, 'archwing', tradable_override=False)
        for item in wiki_sync_requests._fetch_wfcd('Archwing.json')
        if item.get('masterable')
    )
    log.info('Archwings synced: %d', count)
    return count


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
    for item in wiki_sync_requests._fetch_wfcd('Pets.json'):
        if item.get('name', '').lower().endswith(' moa'):
            count += _upsert_wfcd(item, 'k-drive')

    # Remove stale MOA rows that may have been inserted under 'companion' before
    # the exclusion was added — upsert uses (name, category) as the unique key so
    # the old companion rows survive alongside the new k-drive rows.
    db = get_db()
    db.execute("DELETE FROM items WHERE category = 'companion' AND LOWER(name) LIKE '% moa'")
    db.commit()

    # --- K-Drive boards: wiki Category:K-Drive, minus meta-pages and non-mastery parts ---
    for title in wiki_sync_requests._wiki_category_members('K-Drive'):
        if title == 'K-Drive' or any(w in title for w in _WIKI_SKIP_WORDS):
            continue
        if title in _KDRIVE_NON_MASTERY:
            continue  # Reactor / Nose / Jet — no mastery, do not upsert
        count += _upsert_wiki(title, 'k-drive')
        wiki_titles.append(title)

    # --- Necramechs: wiki Category:Necramechs, minus meta-pages ---
    for title in wiki_sync_requests._wiki_category_members('Necramechs'):
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
        title_images = wiki_sync_requests._wiki_batch_images(wiki_titles)
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
