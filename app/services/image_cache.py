"""The local picture cache: file names, adoption of older files, repair, sweep, coverage.

Pictures of items and of their components are downloaded once by the sync and served from
``static/images/items`` and ``static/images/components``. A row's ID alone cannot name its
file: SQLite reuses the ID of a row deleted at the top of the table, so a file named only
after an ID may belong to a deleted item. A file is therefore named after its row *and*
the source URL it was downloaded from:

- an item:      ``<id>-<h><ext>``
- a component:  ``<id>__<slug>-<h><ext>``

where ``<h>`` is the first 12 hex digits of the SHA-256 of the source URL. A row with a
remote URL uses a file only when that file was made for that URL.

Files named by an older version (``<id><ext>``, ``<id>__<slug><ext>``) carry no source. Such
a file counts for its row only when it is not older than the item's ``date_added``: it was
then written while the row existed. A current one is adopted (renamed to the new name, no
download); an older one belonged to a deleted row and is removed.

The app deletes files only here, only directly inside the two cache folders, only regular
files (never a link, a junction or a folder) whose names match the cache's own patterns.
"""

import hashlib
import logging
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

ITEMS = 'items'
COMPONENTS = 'components'

_EXTS = ('.png', '.jpg', '.jpeg', '.webp')
_HASH_LEN = 12
_PART = '.part'

# A download is written under the final name plus this suffix, then renamed; a leftover
# from an interrupted download matches the pattern too and is swept.
_EXT_RE = r'\.(?:png|jpg|jpeg|webp)(?:\.part)?'
_ITEM_FILE_RE = re.compile(r'\d+(?:-[0-9a-f]{%d})?%s' % (_HASH_LEN, _EXT_RE))
_LEGACY_ITEM_RE = re.compile(r'\d+\.(?:png|jpg|jpeg|webp)')
# Older component names kept the component name almost as it was; any plain file name
# that starts with an ID and a double underscore is a component cache file.
_COMP_FILE_RE = re.compile(r'\d+__[^\\/:\x00]+%s' % _EXT_RE)
_PATTERNS = {ITEMS: _ITEM_FILE_RE, COMPONENTS: _COMP_FILE_RE}


# ---------------------------------------------------------------------------
# Places and names
# ---------------------------------------------------------------------------

def cache_root() -> Path:
    """The folder that holds the two cache folders (``static/images`` of the app)."""
    from flask import current_app
    return Path(current_app.root_path) / 'static' / 'images'


def folder(kind: str) -> Path:
    """Return (and create) the cache folder of one kind."""
    d = cache_root() / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def static_url(kind: str, filename: str) -> str:
    return f'/static/images/{kind}/{filename}'


def url_ext(url: str) -> str:
    """A safe file extension from a URL's path (the query left out), default ``.png``."""
    _, ext = os.path.splitext(url.split('?')[0].rstrip('/'))
    return ext.lower() if ext.lower() in _EXTS else '.png'


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode('utf-8')).hexdigest()[:_HASH_LEN]


def comp_slug(component_name: str) -> str:
    """A file-name segment for a component: lower-case letters, digits and underscores."""
    slug = re.sub(r'[^a-z0-9]+', '_', component_name.lower()).strip('_')
    return slug or 'component'


def _legacy_comp_slug(component_name: str) -> str:
    """The segment older versions used (only blanks and ``/`` were mapped)."""
    return component_name.lower().replace(' ', '_').replace('/', '-')


def item_file(item_id: int, url: str) -> str:
    return f'{item_id}-{url_hash(url)}{url_ext(url)}'


def component_file(item_id: int, component_name: str, url: str) -> str:
    return f'{item_id}__{comp_slug(component_name)}-{url_hash(url)}{url_ext(url)}'


def legacy_item_files(item_id: int) -> list[str]:
    return [f'{item_id}{ext}' for ext in _EXTS]


def legacy_component_files(item_id: int, component_name: str) -> list[str]:
    """The older names of a component's file; none when that name would not be a plain
    file name inside the folder (a separator, a drive colon, a NUL)."""
    names = [f'{item_id}__{_legacy_comp_slug(component_name)}{ext}' for ext in _EXTS]
    return [n for n in names if _COMP_FILE_RE.fullmatch(n)]


def is_cache_file(kind: str, filename: str) -> bool:
    return bool(_PATTERNS[kind].fullmatch(filename))


# ---------------------------------------------------------------------------
# File checks
# ---------------------------------------------------------------------------

def _is_link(path: Path) -> bool:
    """A symbolic link, or on Windows any reparse point (a junction)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, 'st_file_attributes', 0)
    return bool(attrs & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _regular_size(path: Path) -> int | None:
    """The size of a regular file that is no link; None for anything else or nothing."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or _is_link(path):
        return None
    return st.st_size


def parse_added(text) -> float | None:
    """``date_added`` as a POSIX time, read as UTC; None when it does not parse.

    Both backends store ``YYYY-MM-DD HH:MM:SS`` (SQLite's ``datetime('now')`` is UTC;
    the MSSQL default is the server's clock, UTC on Azure SQL).
    """
    if not isinstance(text, str):
        return None
    try:
        added = datetime.strptime(text.strip().replace('T', ' ')[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    return added.replace(tzinfo=timezone.utc).timestamp()


def _legacy_is_current(path: Path, added_ts: float | None) -> bool:
    """A legacy file counts only when it is not empty and not older than its row."""
    size = _regular_size(path)
    if not size or added_ts is None:
        return False
    try:
        return os.lstat(path).st_mtime >= added_ts
    except OSError:
        return False


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError as exc:
        if path.exists():
            log.warning('Could not remove cache file %s: %s', path.name, exc)
        return False


# ---------------------------------------------------------------------------
# Resolving a row's file, downloading
# ---------------------------------------------------------------------------

def resolve(directory: Path, final_name: str, legacy_names: list[str],
            added_ts: float | None) -> bool:
    """True when ``final_name`` holds the row's picture without a download.

    The file is there already, or a current legacy file was renamed to it. A legacy file
    older than the row (or empty, or with no usable date) is removed; one that cannot be
    renamed is left for the sweep and not used.
    """
    final = directory / final_name
    if _regular_size(final):
        return True
    for name in legacy_names:
        legacy = directory / name
        if _regular_size(legacy) is None:
            continue
        if _legacy_is_current(legacy, added_ts):
            try:
                os.replace(legacy, final)
                return True
            except OSError as exc:
                log.warning('Could not adopt cache file %s: %s', name, exc)
                continue
        _unlink(legacy)
    return False


def download(url: str, dest: Path) -> bool:
    """Stream ``url`` to a partial file beside ``dest``, then rename it to ``dest``.

    No file appears under ``dest`` unless the whole, non-empty body arrived.
    """
    part = dest.with_name(dest.name + _PART)
    try:
        r = requests.get(url, timeout=15, stream=True)
        r.raise_for_status()
        written = 0
        with part.open('wb') as fh:
            for chunk in r.iter_content(8192):
                fh.write(chunk)
                written += len(chunk)
        if not written:
            raise ValueError('empty body')
        os.replace(part, dest)
        return True
    except Exception as exc:
        log.debug('Image download failed (%s): %s', url, exc)
        try:
            part.unlink()
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# Repair, sweep, coverage
# ---------------------------------------------------------------------------

def _referenced_name(kind: str, image_url) -> str | None:
    """The cache file a stored ``/static/`` URL names, when it names one."""
    prefix = static_url(kind, '')
    if not isinstance(image_url, str) or not image_url.startswith(prefix):
        return None
    name = image_url[len(prefix):]
    return name if is_cache_file(kind, name) else None


def repair_stale_items(db) -> int:
    """Clear the picture of every item whose stored file is a legacy file older than the
    item (it came from a deleted row with the same ID), an empty file, or missing.

    The file is removed and ``image_url`` set to NULL, so the same sync fills in the
    catalogue's or the wiki's URL and downloads the picture again. Returns the count.
    """
    rows = db.execute(
        "SELECT id, image_url, date_added FROM items WHERE image_url LIKE '/static/images/items/%'"
    ).fetchall()
    directory = cache_root() / ITEMS
    if _is_link(directory):
        return 0
    repaired = 0
    for row in rows:
        name = _referenced_name(ITEMS, row['image_url'])
        if name is None:
            continue
        path = directory / name
        if not os.path.lexists(path):
            stale = True                              # the file is gone: download again
        else:
            size = _regular_size(path)
            if size is None:
                continue                              # a link or a folder: left alone
            stale = size == 0 or (
                _LEGACY_ITEM_RE.fullmatch(name)
                and not _legacy_is_current(path, parse_added(row['date_added']))
            )
            if stale:
                _unlink(path)
        if not stale:
            continue
        db.execute('UPDATE items SET image_url = NULL WHERE id = ?', (row['id'],))
        repaired += 1
    if repaired:
        db.commit()
        log.info('Image cache: %d stale picture(s) cleared for download', repaired)
    return repaired


def _referenced(db, kind: str) -> set[str]:
    if kind == ITEMS:
        sql = "SELECT image_url FROM items WHERE image_url LIKE '/static/images/items/%'"
    else:
        sql = ("SELECT image_url FROM item_component_meta "
               "WHERE image_url LIKE '/static/images/components/%'")
    names = set()
    for row in db.execute(sql).fetchall():
        name = _referenced_name(kind, row['image_url'])
        if name:
            names.add(name)
    return names


def _cache_entries(kind: str):
    """The regular, non-link files directly in a cache folder whose names are the
    cache's own; nothing when the folder is missing or is itself a link."""
    directory = cache_root() / kind
    if _is_link(directory) or not directory.is_dir():
        return []
    found = []
    with os.scandir(directory) as it:
        for entry in it:
            if not is_cache_file(kind, entry.name):
                continue
            path = directory / entry.name
            if _regular_size(path) is None:
                continue
            found.append(path)
    return found


def sweep(db, kind: str) -> int:
    """Remove the cache files of one kind that no row references. Returns the count."""
    referenced = _referenced(db, kind)
    removed = 0
    for path in _cache_entries(kind):
        if path.name not in referenced and _unlink(path):
            removed += 1
    if removed:
        log.info('Image cache: %d unreferenced %s file(s) removed', removed, kind)
    return removed


def coverage(db) -> dict:
    """The sync page's picture coverage of items (resources left out of the categories).

    ``broken``: items whose stored ``/static/`` picture file is missing. ``orphans``: cache
    files in the item folder no item references. ``disk_count``: files in that folder.
    """
    cat_rows = db.execute('''
        SELECT category,
               COUNT(*)                                                          AS total,
               SUM(CASE WHEN image_url IS NULL            THEN 1 ELSE 0 END)    AS missing,
               SUM(CASE WHEN image_url LIKE '/static/%'   THEN 1 ELSE 0 END)    AS cached,
               SUM(CASE WHEN image_url IS NOT NULL
                         AND image_url NOT LIKE '/static/%' THEN 1 ELSE 0 END)  AS remote
          FROM items
         WHERE category != 'resource'
         GROUP BY category
         ORDER BY category
    ''').fetchall()

    directory = cache_root() / ITEMS
    disk_count = 0
    if directory.is_dir() and not _is_link(directory):
        with os.scandir(directory) as it:
            disk_count = sum(1 for e in it if e.is_file(follow_symlinks=False))

    broken = 0
    for row in db.execute("SELECT image_url FROM items WHERE image_url LIKE '/static/%'"):
        name = _referenced_name(ITEMS, row['image_url'])
        if name is None or not _regular_size(directory / name):
            broken += 1

    referenced = _referenced(db, ITEMS)
    orphans = sum(1 for p in _cache_entries(ITEMS) if p.name not in referenced)

    missing_rows = db.execute(
        "SELECT id, name, category FROM items WHERE image_url IS NULL AND category != 'resource' "
        "ORDER BY category, name"
    ).fetchall()
    return {
        'categories':    [dict(r) for r in cat_rows],
        'broken':        broken,
        'orphans':       orphans,
        'disk_count':    disk_count,
        'missing_items': [dict(r) for r in missing_rows],
    }
