"""The picture cache: a cached file belongs to the row and the source URL it was downloaded
from, never to an item ID alone (SQLite reuses the ID of a row deleted at the top).

Covered: the reused-ID case for items and components, files named by older versions
(adopted only when not older than the item), the repair of a stored stale picture in one
sync, the end-of-sync sweep and when it is skipped, repetition, hostile URLs and component
names, interrupted and refused downloads, links out of the cache, and the sync page's
coverage counts. Every download is mocked; the cache lives in a temporary folder.
"""
import os
import sys
from datetime import datetime, timezone

import pytest
import requests

from models.db import get_db
from models.item import Item
from services import image_cache
from services import wiki_sync

ADDED = '2026-11-08 12:00:00'
ADDED_TS = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc).timestamp()
OLDER = datetime(2026, 6, 6, 9, 0, 0, tzinfo=timezone.utc).timestamp()
NEWER = ADDED_TS + 3600
OLD_URL = 'https://cdn.example.test/img/old-frame.png'
NEW_URL = 'https://cdn.example.test/img/citrine-prime.png'
COMP_OLD_URL = 'https://cdn.example.test/img/old-neuroptics.png'
COMP_NEW_URL = 'https://cdn.example.test/img/citrine-neuroptics.png'


# ── Fixtures and helpers ─────────────────────────────────────────────────────

class FakeImage:
    def __init__(self, url, status=200, body=None, break_after_first=False):
        self.url, self.status_code = url, status
        self.body = url.encode() if body is None else body
        self.break_after_first = break_after_first

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error', response=self)

    def iter_content(self, size):
        if self.body:
            yield self.body
        if self.break_after_first:
            raise requests.ConnectionError('connection reset mid-body')


@pytest.fixture
def net(monkeypatch):
    """Every picture download, recorded; `net.responses[url]` overrides one URL's answer
    (a FakeImage, or an exception to raise)."""
    class Net:
        calls = []
        responses = {}

    def fake_get(url, timeout=None, stream=False, **kwargs):
        Net.calls.append(url)
        answer = Net.responses.get(url)
        if isinstance(answer, BaseException):
            raise answer
        return answer or FakeImage(url)

    monkeypatch.setattr(requests, 'get', fake_get)
    return Net


@pytest.fixture
def db(app):
    with app.app_context():
        yield get_db()


@pytest.fixture
def items_dir(image_cache_root):
    d = image_cache_root / 'items'
    d.mkdir(parents=True)
    return d


@pytest.fixture
def comps_dir(image_cache_root):
    d = image_cache_root / 'components'
    d.mkdir(parents=True)
    return d


def add_item(db, name, url, added=ADDED, category='warframe'):
    Item.upsert(name, category, source='standard', image_url=url)
    db.execute('UPDATE items SET date_added = ? WHERE name = ? AND category = ?',
               (added, name, category))
    db.commit()
    return db.execute('SELECT id FROM items WHERE name = ? AND category = ?',
                      (name, category)).fetchone()['id']


def add_component(db, item_id, comp_name, url):
    db.execute('INSERT INTO item_component_meta (item_id, component_name, image_url) '
               'VALUES (?, ?, ?)', (item_id, comp_name, url))
    db.commit()


def put(path, data=b'picture', mtime=None):
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def image_url(db, item_id):
    return db.execute('SELECT image_url FROM items WHERE id = ?', (item_id,)).fetchone()[0]


def comp_url(db, item_id, comp_name):
    return db.execute('SELECT image_url FROM item_component_meta '
                      'WHERE item_id = ? AND component_name = ?',
                      (item_id, comp_name)).fetchone()[0]


def names(directory):
    return sorted(p.name for p in directory.iterdir())


# ── A reused ID never inherits a deleted row's picture ───────────────────────

def test_a_new_item_with_a_reused_id_downloads_its_own_picture(db, net, items_dir):
    old_id = add_item(db, 'Old Frame', OLD_URL)
    put(items_dir / f'{old_id}.png', b'deleted item', OLDER)
    put(items_dir / image_cache.item_file(old_id, OLD_URL), b'deleted item', OLDER)
    db.execute('DELETE FROM items WHERE id = ?', (old_id,))
    db.commit()

    new_id = add_item(db, 'Citrine Prime', NEW_URL)
    assert new_id == old_id                       # SQLite gave the new row the old ID

    assert wiki_sync._sync_item_images(db) == (1, 0)
    assert net.calls == [NEW_URL]
    name = image_cache.item_file(new_id, NEW_URL)
    assert image_url(db, new_id) == f'/static/images/items/{name}'
    assert (items_dir / name).read_bytes() == NEW_URL.encode()
    assert not (items_dir / f'{old_id}.png').exists()


def test_a_component_of_an_item_with_a_reused_id_downloads_its_own_picture(
        db, net, comps_dir):
    old_id = add_item(db, 'Old Frame', OLD_URL)
    add_component(db, old_id, 'Neuroptics', COMP_OLD_URL)
    put(comps_dir / f'{old_id}__neuroptics.png', b'deleted component', OLDER)
    put(comps_dir / image_cache.component_file(old_id, 'Neuroptics', COMP_OLD_URL),
        b'deleted component', OLDER)
    db.execute('DELETE FROM item_component_meta WHERE item_id = ?', (old_id,))
    db.execute('DELETE FROM items WHERE id = ?', (old_id,))
    db.commit()

    new_id = add_item(db, 'Citrine Prime', NEW_URL)
    assert new_id == old_id
    add_component(db, new_id, 'Neuroptics', COMP_NEW_URL)

    assert wiki_sync._sync_component_images(db) == (1, 0)
    assert net.calls == [COMP_NEW_URL]
    name = image_cache.component_file(new_id, 'Neuroptics', COMP_NEW_URL)
    assert comp_url(db, new_id, 'Neuroptics') == f'/static/images/components/{name}'
    assert (comps_dir / name).read_bytes() == COMP_NEW_URL.encode()
    assert not (comps_dir / f'{old_id}__neuroptics.png').exists()


# ── Files named by older versions ────────────────────────────────────────────

LEGACY_CASES = {
    # case: (legacy bytes or None for no file, legacy mtime, date_added, adopted)
    'newer than the item':        (b'kept picture', NEWER, ADDED, True),
    'the same second as the item': (b'kept picture', ADDED_TS, ADDED, True),
    'older than the item':        (b'stale picture', OLDER, ADDED, False),
    'missing':                    (None, None, ADDED, False),
    'empty and newer':            (b'', NEWER, ADDED, False),
    'with an unreadable date':    (b'kept picture', NEWER, 'not a date', False),
}


@pytest.mark.parametrize('kind', ['item', 'component'])
@pytest.mark.parametrize('case', LEGACY_CASES)
def test_a_legacy_file_is_adopted_only_when_not_older_than_its_item(
        db, net, items_dir, comps_dir, kind, case):
    data, mtime, added, adopted = LEGACY_CASES[case]
    item_id = add_item(db, 'Ash', NEW_URL, added=added)
    if kind == 'item':
        directory, legacy = items_dir, f'{item_id}.png'
        url, name = NEW_URL, image_cache.item_file(item_id, NEW_URL)
        run, stored = wiki_sync._sync_item_images, lambda: image_url(db, item_id)
    else:
        add_component(db, item_id, 'Neuroptics Blueprint', COMP_NEW_URL)
        directory, legacy = comps_dir, f'{item_id}__neuroptics_blueprint.png'
        url = COMP_NEW_URL
        name = image_cache.component_file(item_id, 'Neuroptics Blueprint', COMP_NEW_URL)
        run = wiki_sync._sync_component_images
        stored = lambda: comp_url(db, item_id, 'Neuroptics Blueprint')  # noqa: E731
    if data is not None:
        put(directory / legacy, data, mtime)

    result = run(db)

    assert result == ((0, 1) if adopted else (1, 0))
    assert net.calls == ([] if adopted else [url])
    assert (directory / name).read_bytes() == (data if adopted else url.encode())
    assert stored() == f'/static/images/{directory.name}/{name}'
    assert not (directory / legacy).exists()


# ── The full sync: repair, repetition, the sweep ─────────────────────────────

def fake_catalogue(monkeypatch, items, components=(), component_status_ok=True,
                   failing=None):
    """Replace the sync's fetching passes: `items` are (name, url) catalogue rows,
    `components` (item name, component, url) rows rebuilt on every run."""
    def warframes():
        for name, url in items:
            Item.upsert(name, 'warframe', source='standard', image_url=url)
        return len(items)

    def ingredients(status=None):
        db = get_db()
        db.execute('DELETE FROM item_component_meta')
        for item_name, comp, url in components:
            item_id = db.execute("SELECT id FROM items WHERE name = ? AND category = 'warframe'",
                                 (item_name,)).fetchone()['id']
            db.execute('INSERT INTO item_component_meta (item_id, component_name, image_url) '
                       'VALUES (?, ?, ?)', (item_id, comp, url))
        db.commit()
        if not component_status_ok:
            status['complete'] = False
        return len(components)

    monkeypatch.setattr(wiki_sync, '_sync_warframes', warframes)
    monkeypatch.setattr(wiki_sync, '_sync_component_ingredients', ingredients)
    for name in ('_sync_weapons', '_sync_kitgun_chambers', '_sync_companions',
                 '_sync_archwings', '_sync_k_drives', '_sync_amps', '_sync_mods',
                 '_sync_arcanes', '_sync_resources', '_sync_drop_sources'):
        monkeypatch.setattr(wiki_sync, name, lambda: 0)
    monkeypatch.setattr(wiki_sync, '_backfill_null_item_images', lambda db, cats: 0)
    monkeypatch.setattr(wiki_sync, '_sync_wiki_images_for_missing', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_component_wiki_images', lambda db: 0)
    monkeypatch.setattr(wiki_sync, 'sync_resurgence', lambda: {'ok': True})
    monkeypatch.setattr(wiki_sync, 'sync_market_slugs', lambda: {'ok': True, 'matched': 0})
    if failing:
        def broken(*args, **kwargs):
            raise RuntimeError('source down')
        monkeypatch.setattr(wiki_sync, failing, broken)


@pytest.mark.parametrize('data, mtime, repaired', [
    (b'deleted item', OLDER, True),
    (b'', NEWER, True),
    (None, None, True),
    (b'its own picture', NEWER, False),
], ids=['older legacy file', 'empty file', 'missing file', 'newer legacy file'])
def test_a_stored_stale_picture_is_replaced_in_one_sync_and_a_good_one_is_kept(
        app, db, net, items_dir, monkeypatch, data, mtime, repaired):
    item_id = add_item(db, 'Citrine Prime', NEW_URL)
    if data is not None:
        put(items_dir / f'{item_id}.png', data, mtime)
    legacy_url = f'/static/images/items/{item_id}.png'
    db.execute('UPDATE items SET image_url = ? WHERE id = ?', (legacy_url, item_id))
    db.commit()
    fake_catalogue(monkeypatch, [('Citrine Prime', NEW_URL)])

    result = wiki_sync.run_sync(app)

    assert result['ok'] is True
    assert result['counts']['images_repaired'] == (1 if repaired else 0)
    if repaired:
        name = image_cache.item_file(item_id, NEW_URL)
        assert net.calls == [NEW_URL]
        assert image_url(db, item_id) == f'/static/images/items/{name}'
        assert names(items_dir) == [name]
    else:
        assert net.calls == []
        assert image_url(db, item_id) == legacy_url
        assert (items_dir / f'{item_id}.png').read_bytes() == data


def test_a_second_sync_downloads_nothing_and_removes_nothing(
        app, db, net, items_dir, comps_dir, monkeypatch):
    fake_catalogue(monkeypatch, [('Ash', OLD_URL), ('Citrine Prime', NEW_URL)],
                   [('Ash', 'Chassis', COMP_OLD_URL), ('Citrine Prime', 'Neuroptics', COMP_NEW_URL)])

    first = wiki_sync.run_sync(app)
    calls_after_first = list(net.calls)
    files = (names(items_dir), names(comps_dir))
    second = wiki_sync.run_sync(app)

    assert first['counts']['images_downloaded'] == 2
    assert first['counts']['comp_images_downloaded'] == 2
    assert net.calls == calls_after_first
    assert second['counts']['images_downloaded'] == 0
    assert second['counts']['comp_images_downloaded'] == 0
    assert second['counts']['comp_images_already_cached'] == 2
    assert second['counts']['images_swept'] == 0
    assert second['counts']['images_comp_swept'] == 0
    assert (names(items_dir), names(comps_dir)) == files


def make_sweep_fixture(db, items_dir, comps_dir):
    """A referenced file in each folder, plus unreferenced cache files and files that are
    no cache file of ours."""
    item_id = add_item(db, 'Ash', NEW_URL)
    kept_item = image_cache.item_file(item_id, NEW_URL)
    put(items_dir / kept_item)
    db.execute('UPDATE items SET image_url = ? WHERE id = ?',
               (f'/static/images/items/{kept_item}', item_id))
    add_component(db, item_id, 'Chassis', COMP_NEW_URL)
    kept_comp = image_cache.component_file(item_id, 'Chassis', COMP_NEW_URL)
    put(comps_dir / kept_comp)
    db.execute('UPDATE item_component_meta SET image_url = ?',
               (f'/static/images/components/{kept_comp}',))
    db.commit()
    for name in ('77-0123456789ab.png', '78.jpg', '79-0123456789ab.webp.part'):
        put(items_dir / name)
    for name in ('77__chassis-0123456789ab.png', '78__old_name.png'):
        put(comps_dir / name)
    for d in (items_dir, comps_dir):
        put(d / 'readme.txt')
        put(d / 'notes.png')
        (d / '90.png').mkdir()
        (d / '91__folder.png').mkdir()
    return (sorted([kept_item, 'readme.txt', 'notes.png', '90.png', '91__folder.png']),
            sorted([kept_comp, 'readme.txt', 'notes.png', '90.png', '91__folder.png']))


def test_the_sweep_removes_only_unreferenced_cache_files(db, items_dir, comps_dir):
    items_left, comps_left = make_sweep_fixture(db, items_dir, comps_dir)
    before = (names(items_dir), names(comps_dir))

    assert image_cache.sweep(db, image_cache.ITEMS) == 3
    assert image_cache.sweep(db, image_cache.COMPONENTS) == 2
    assert (names(items_dir), names(comps_dir)) == (items_left, comps_left)
    assert image_cache.sweep(db, image_cache.ITEMS) == 0      # nothing removed twice
    assert len(before[0]) == len(items_left) + 3


@pytest.mark.parametrize('failure, items_swept, comps_swept', [
    (dict(component_status_ok=False), True, False),
    (dict(failing='_sync_component_images'), False, False),
    (dict(failing='_sync_item_images'), False, False),
    (dict(failing='_sync_mods'), False, False),
], ids=['component rows incomplete', 'component pass raised', 'item pass raised',
        'catalogue pass raised'])
def test_a_folder_whose_rows_were_not_rebuilt_is_not_swept(
        app, db, net, items_dir, comps_dir, monkeypatch, failure, items_swept, comps_swept):
    items_left, comps_left = make_sweep_fixture(db, items_dir, comps_dir)
    before = (names(items_dir), names(comps_dir))
    fake_catalogue(monkeypatch, [], [('Ash', 'Chassis', COMP_NEW_URL)], **failure)

    result = wiki_sync.run_sync(app)

    assert result['ok'] is (items_swept or comps_swept)
    assert names(items_dir) == (items_left if items_swept else before[0])
    assert names(comps_dir) == (comps_left if comps_swept else before[1])


def _link_dir(target, link):
    if sys.platform == 'win32':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_the_sweep_never_follows_a_link_out_of_the_cache(db, image_cache_root, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = put(outside / '5.png', b'not ours')
    image_cache_root.mkdir()
    _link_dir(outside, image_cache_root / 'items')            # the folder itself is a link

    assert image_cache.sweep(db, image_cache.ITEMS) == 0
    assert image_cache.repair_stale_items(db) == 0
    assert victim.read_bytes() == b'not ours'


def test_the_sweep_leaves_a_linked_file_and_its_target(db, items_dir, tmp_path):
    target = put(tmp_path / 'target.png', b'not ours')
    try:
        os.symlink(target, items_dir / '6.png')
    except OSError:
        pytest.skip('this machine cannot create a file symlink')
    assert image_cache.sweep(db, image_cache.ITEMS) == 0
    assert target.read_bytes() == b'not ours'
    assert os.path.islink(items_dir / '6.png')


# ── Input spellings ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('url, ext', [
    ('https://cdn.example.test/a/Frame.PNG', '.png'),
    ('https://cdn.example.test/a/Frame.png?width=200&v=3', '.png'),
    ('https://cdn.example.test/a/Frame.JPEG', '.jpeg'),
    ('https://cdn.example.test/a/Frame.webp/', '.webp'),
    ('https://cdn.example.test/a/Frame', '.png'),
    ('https://cdn.example.test/a/Frame.gif', '.png'),
    ('https://cdn.example.test/a/Frame.png/../../evil.exe', '.png'),
])
def test_a_file_name_is_the_id_and_a_hash_of_the_whole_url(url, ext):
    name = image_cache.item_file(42, url)
    assert name == f'42-{image_cache.url_hash(url)}{ext}'
    assert image_cache.is_cache_file(image_cache.ITEMS, name)


def test_a_query_string_is_part_of_the_source():
    plain = 'https://cdn.example.test/a/Frame.png'
    assert image_cache.item_file(1, plain) != image_cache.item_file(1, plain + '?v=2')


@pytest.mark.parametrize('comp_name', [
    'Neuroptics Blueprint', 'Blade / Hilt', 'Blade\\Hilt', '..', '../../evil', '..\\..\\evil',
    '   ', 'C:evil', 'Ünïcode Part', 'Name\x00Null',
])
def test_a_component_name_gives_a_plain_name_inside_the_folder(
        db, net, comps_dir, image_cache_root, comp_name):
    item_id = add_item(db, 'Ash', NEW_URL)
    add_component(db, item_id, comp_name, COMP_NEW_URL)
    name = image_cache.component_file(item_id, comp_name, COMP_NEW_URL)

    assert wiki_sync._sync_component_images(db) == (1, 0)
    assert names(comps_dir) == [name]
    assert sorted(p.name for p in image_cache_root.iterdir()) == ['components']
    for legacy in image_cache.legacy_component_files(item_id, comp_name):
        assert '\\' not in legacy and '/' not in legacy and ':' not in legacy


def test_two_items_with_the_same_url_each_get_their_own_file(db, net, items_dir):
    first = add_item(db, 'Ash', NEW_URL)
    second = add_item(db, 'Ash Prime', NEW_URL)
    assert wiki_sync._sync_item_images(db) == (2, 0)
    assert names(items_dir) == sorted([image_cache.item_file(first, NEW_URL),
                                       image_cache.item_file(second, NEW_URL)])


# ── Interruption and dependency errors ───────────────────────────────────────

@pytest.mark.parametrize('answer', [
    requests.ConnectionError('refused'),
    requests.Timeout('slow'),
    FakeImage(NEW_URL, status=404),
    FakeImage(NEW_URL, break_after_first=True),
    FakeImage(NEW_URL, body=b''),
], ids=['refused', 'timed out', 'HTTP 404', 'broken mid-body', 'empty body'])
def test_a_failed_download_leaves_no_file_and_no_stale_picture(db, net, items_dir, answer):
    item_id = add_item(db, 'Citrine Prime', NEW_URL)
    put(items_dir / f'{item_id}.png', b'deleted item', OLDER)
    net.responses[NEW_URL] = answer

    assert wiki_sync._sync_item_images(db) == (0, 0)
    assert image_url(db, item_id) == NEW_URL
    assert names(items_dir) == []


def test_a_legacy_file_that_cannot_be_renamed_is_not_used(db, net, items_dir, monkeypatch):
    item_id = add_item(db, 'Ash', NEW_URL)
    put(items_dir / f'{item_id}.png', b'current picture', NEWER)
    real_replace = os.replace

    def replace(src, dst):
        if not str(src).endswith('.part'):
            raise PermissionError('file in use')
        return real_replace(src, dst)
    monkeypatch.setattr(os, 'replace', replace)

    assert wiki_sync._sync_item_images(db) == (1, 0)
    name = image_cache.item_file(item_id, NEW_URL)
    assert image_url(db, item_id) == f'/static/images/items/{name}'
    assert (items_dir / name).read_bytes() == NEW_URL.encode()
    assert image_cache.sweep(db, image_cache.ITEMS) == 1      # the leftover goes at the end
    assert names(items_dir) == [name]


# ── Toolchain: the stored date ───────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('2026-11-08 12:00:00', ADDED_TS),
    ('2026-11-08T12:00:00', ADDED_TS),
    ('2026-11-08 12:00:00.250', ADDED_TS),
    (' 2026-11-08 12:00:00 ', ADDED_TS),
    ('2026-11-08', None),
    ('not a date', None),
    ('', None),
    (None, None),
    (20261108, None),
])
def test_date_added_is_read_as_utc_or_not_at_all(text, expected):
    assert image_cache.parse_added(text) == expected


# ── The sync page's coverage ─────────────────────────────────────────────────

def test_image_coverage_counts_missing_files_and_unreferenced_cache_files(
        app, admin_client, items_dir):
    with app.app_context():
        db = get_db()
        hashed = add_item(db, 'Ash', NEW_URL)
        legacy = add_item(db, 'Banshee', OLD_URL)
        missing = add_item(db, 'Citrine', OLD_URL)
        add_item(db, 'Dagath', NEW_URL)                      # still remote
        hashed_name = image_cache.item_file(hashed, NEW_URL)
        put(items_dir / hashed_name)
        put(items_dir / f'{legacy}.png')
        put(items_dir / '999-0123456789ab.png')                # no row references it
        put(items_dir / 'readme.txt')
        for item_id, url in ((hashed, f'/static/images/items/{hashed_name}'),
                             (legacy, f'/static/images/items/{legacy}.png'),
                             (missing, f'/static/images/items/{missing}-0123456789ab.png')):
            db.execute('UPDATE items SET image_url = ? WHERE id = ?', (url, item_id))
        db.commit()

    body = admin_client.get('/sync/image-coverage').get_json()

    assert set(body) == {'ok', 'categories', 'broken', 'orphans', 'disk_count', 'missing_items'}
    assert (body['ok'], body['broken'], body['orphans'], body['disk_count']) == (True, 1, 1, 4)
    assert body['categories'] == [{'category': 'warframe', 'total': 4, 'missing': 0,
                                   'cached': 3, 'remote': 1}]
