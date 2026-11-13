"""Every HTTP request the item catalogue sync makes, and the URLs it reads.

Every other module of the sync calls these through this module
(``wiki_sync_requests._fetch_wfcd(...)``), never by a name imported from it, so one
replacement of a name here replaces that request for every caller.
"""

import logging

import requests

# The sync's log lines keep the logger name an operator filters on.
log = logging.getLogger('services.wiki_sync')

_WFCD_BASE             = 'https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json'
_WIKI_API              = 'https://wiki.warframe.com/api.php'
_WARFRAMESTAT_DROPS    = 'https://drops.warframestat.us/data/all.json'

_WFCD_CDN        = 'https://cdn.warframestat.us/img'


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


def _fetch_drop_tables() -> dict | None:
    """The warframestat.us drop tables (``all.json``), fetched once per drop sync; None when
    the request fails or the answer is not a JSON object (non-fatal)."""
    try:
        resp = requests.get(_WARFRAMESTAT_DROPS, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning('Could not fetch warframestat drop tables (non-fatal): %s', exc)
        return None
    if not isinstance(data, dict):
        log.warning('Warframestat drop tables are not a JSON object (non-fatal)')
        return None
    return data
