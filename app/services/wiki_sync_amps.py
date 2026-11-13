"""Operator amps: one masterable amp per prism, from WFCD, the wiki and a curated list."""

import logging

from models.db import get_db
from models.item import Item
from services import wiki_sync_requests
from services.wiki_sync_catalogue import (
    _WIKI_BASE, _WIKI_SKIP_WORDS, _cleanup_source_components, _detect_source, _wfcd_image_url,
)

# The sync's log lines keep the logger name an operator filters on.
log = logging.getLogger('services.wiki_sync')


# ---------------------------------------------------------------------------
# Amp sync
# ---------------------------------------------------------------------------

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
        for item in wiki_sync_requests._fetch_wfcd('Amps.json'):
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
        for item in wiki_sync_requests._fetch_wfcd('Misc.json'):
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
        for title in wiki_sync_requests._wiki_category_members('Amps'):
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
        title_images = wiki_sync_requests._wiki_batch_images(canonical_wiki)
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
