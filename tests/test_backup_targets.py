"""The consistent copy, and the local folder as a backup target. Every folder is under the
test's own temporary directory. The S3 target is tested in test_backup_targets_s3.py."""
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from unittest import mock

import pytest

from services import backup_targets as bt

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
NAME = 'warframe_tracker_20261114T120000Z.db'


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'live.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE t (v TEXT)')
    conn.execute("INSERT INTO t VALUES ('committed')")
    conn.commit()
    conn.close()
    return path


def rows(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute('SELECT v FROM t')]
    finally:
        conn.close()


@pytest.fixture
def folder(tmp_path):
    return tmp_path / 'backups'


# ── Configuration ────────────────────────────────────────────────────────────

def test_the_local_path_is_stripped_and_blank_means_not_configured(tmp_path):
    assert bt.local_folder({'BACKUP_LOCAL_PATH': f'  {tmp_path}{os.sep} '}) == tmp_path
    assert bt.local_folder({'BACKUP_LOCAL_PATH': '   '}) is None
    assert bt.local_folder({'BACKUP_LOCAL_PATH': ''}) is None
    assert bt.local_folder({}) is None


@pytest.mark.parametrize('raw', ['backups', './backups', '..', os.path.join('a', 'b')]
                         + (['\\backups', 'C:backups'] if sys.platform == 'win32' else []))
def test_a_relative_local_path_is_refused_never_resolved_against_the_working_directory(raw):
    folder = bt.local_folder({'BACKUP_LOCAL_PATH': f' {raw} '})
    with pytest.raises(ValueError, match='BACKUP_LOCAL_PATH must be an absolute path'):
        bt.check_local_folder(folder)


def test_an_absolute_local_path_is_accepted(tmp_path):
    assert bt.check_local_folder(tmp_path) == tmp_path



# ── The copy ─────────────────────────────────────────────────────────────────

def test_the_copy_holds_committed_rows_only(database, tmp_path):
    writer = sqlite3.connect(database, isolation_level=None)
    writer.execute('BEGIN IMMEDIATE')
    writer.execute("INSERT INTO t VALUES ('uncommitted')")
    try:
        bt.copy_database(database, tmp_path / 'copy.db')
    finally:
        writer.execute('ROLLBACK')
        writer.close()
    assert rows(tmp_path / 'copy.db') == ['committed']


def test_a_locked_database_fails_the_copy_after_the_busy_limit(database, tmp_path):
    holder = sqlite3.connect(database, isolation_level=None)
    holder.execute('BEGIN EXCLUSIVE')
    ticks = iter([0.0, 59.0, 60.0, 61.0])
    try:
        with pytest.raises(TimeoutError):
            bt.copy_database(database, tmp_path / 'copy.db', clock=lambda: next(ticks))
    finally:
        holder.execute('ROLLBACK')
        holder.close()


def test_a_locked_database_fails_the_copy_on_the_real_monotonic_clock(database, tmp_path):
    holder = sqlite3.connect(database, isolation_level=None)
    holder.execute('BEGIN EXCLUSIVE')
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            bt.copy_database(database, tmp_path / 'copy.db', busy_limit=0.5)
    finally:
        holder.execute('ROLLBACK')
        holder.close()
    assert 0.5 <= time.monotonic() - started < 10


def test_a_wal_database_with_no_connection_open_is_copied(tmp_path, folder):
    # The app's database between requests: WAL mode, every connection closed, no sidecars.
    source = tmp_path / 'wal.db'
    writer = sqlite3.connect(source)
    writer.execute('PRAGMA journal_mode = WAL')
    writer.execute('CREATE TABLE t (v TEXT)')
    writer.execute("INSERT INTO t VALUES ('checkpointed')")
    writer.commit()
    writer.close()
    assert sorted(os.listdir(tmp_path)) == ['wal.db']
    bt.write_local(folder, source, NOW)
    assert os.listdir(folder) == [NAME]
    assert rows(folder / NAME) == ['checkpointed']
    assert os.listdir(folder) == [NAME]


@pytest.mark.skipif(sys.platform != 'win32', reason='a Windows network path')
def test_a_database_on_a_windows_network_path_is_copied(database, tmp_path):
    drive, rest = os.path.splitdrive(str(database))
    unc = '\\\\localhost\\' + drive[0] + '$' + rest
    if not os.path.exists(unc):
        pytest.skip('the administrative share is not reachable here')
    bt.copy_database(unc, tmp_path / 'copy.db')
    assert rows(tmp_path / 'copy.db') == ['committed']


def test_a_copy_of_a_wal_database_leaves_no_sidecar_beside_the_backup(tmp_path, folder):
    source = tmp_path / 'wal.db'
    writer = sqlite3.connect(source)
    writer.execute('PRAGMA journal_mode = WAL')
    writer.execute('CREATE TABLE t (v TEXT)')
    writer.execute("INSERT INTO t VALUES ('in the wal')")
    writer.commit()
    try:
        bt.write_local(folder, source, NOW)
    finally:
        writer.close()
    assert os.listdir(folder) == [NAME]
    # Header bytes 18 and 19 are 1 for the rollback journal, 2 for WAL.
    assert (folder / NAME).read_bytes()[18:20] == b'\x01\x01'
    assert rows(folder / NAME) == ['in the wal']
    assert os.listdir(folder) == [NAME]      # reading the finished backup makes no sidecar


def test_a_missing_database_is_an_error_and_creates_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        bt.copy_database(tmp_path / 'absent.db', tmp_path / 'copy.db')
    assert not (tmp_path / 'absent.db').exists()


def test_a_database_gone_after_the_check_fails_the_copy_and_is_not_created(tmp_path,
                                                                            monkeypatch):
    absent = tmp_path / 'absent.db'
    monkeypatch.setattr(bt.Path, 'is_file', lambda self: True)
    with pytest.raises(sqlite3.OperationalError):
        bt.copy_database(absent, tmp_path / 'copy.db')
    assert not absent.exists()


def test_a_database_replaced_by_another_file_fails_the_copy(tmp_path):
    source = tmp_path / 'live.db'
    source.write_bytes(b'not a database at all, but long enough to be read as one' * 20)
    with pytest.raises(sqlite3.DatabaseError):
        bt.copy_database(source, tmp_path / 'copy.db')


def test_the_source_is_never_written(database, tmp_path, monkeypatch):
    before = database.read_bytes()
    bt.copy_database(database, tmp_path / 'copy.db')
    assert database.read_bytes() == before
    assert sorted(os.listdir(tmp_path)) == ['copy.db', 'live.db']


@pytest.mark.parametrize('name', ['a b.db', 'percent%20.db', 'hash#.db', 'ünï.db', 'semi;colon.db'])
def test_a_database_path_with_characters_a_uri_reserves_is_copied(tmp_path, name):
    source = tmp_path / name
    conn = sqlite3.connect(source)
    conn.execute('CREATE TABLE t (v TEXT)')
    conn.execute("INSERT INTO t VALUES ('odd name')")
    conn.commit()
    conn.close()
    bt.copy_database(source, tmp_path / 'copy.db')
    assert rows(tmp_path / 'copy.db') == ['odd name']


def test_the_read_only_uri_keeps_the_authority_empty():
    uri = bt._read_only_uri(os.path.abspath('x.db'))
    assert uri.startswith('file:///') and uri.endswith('x.db?mode=ro')


# ── The local folder ─────────────────────────────────────────────────────────

def test_a_backup_is_a_consistent_copy_under_its_final_name_and_leaves_others_alone(
        database, folder):
    (folder / 'sub').mkdir(parents=True)
    (folder / 'notes.txt').write_bytes(b'mine')
    written = bt.write_local(folder, database, NOW)
    assert written == str(folder / NAME)
    assert rows(folder / NAME) == ['committed']
    assert sorted(os.listdir(folder)) == sorted([NAME, 'notes.txt', 'sub'])
    assert (folder / 'notes.txt').read_bytes() == b'mine'


def test_a_missing_folder_is_created(database, tmp_path):
    folder = tmp_path / 'a' / 'b'
    bt.write_local(folder, database, NOW)
    assert (folder / NAME).is_file()


def test_another_process_writing_this_second_is_left_alone(database, folder):
    folder.mkdir()
    (folder / (NAME + '.partial')).write_bytes(b'theirs')
    with pytest.raises(bt.Skipped, match='being written in this second'):
        bt.write_local(folder, database, NOW)
    assert (folder / (NAME + '.partial')).read_bytes() == b'theirs'
    assert not (folder / NAME).exists()


def test_an_existing_backup_of_this_second_is_never_replaced(database, folder):
    folder.mkdir()
    (folder / NAME).write_bytes(b'first')
    with pytest.raises(bt.Skipped, match='already exists'):
        bt.write_local(folder, database, NOW)
    assert (folder / NAME).read_bytes() == b'first'
    assert os.listdir(folder) == [NAME]


@pytest.mark.parametrize('error', [OSError(28, 'No space left on device'), KeyboardInterrupt()],
                         ids=['disk full', 'interrupted'])
def test_a_copy_that_fails_leaves_neither_name(database, folder, monkeypatch, error):
    def failing(source, dest, **kwargs):
        open(dest, 'wb').write(b'half')
        raise error
    monkeypatch.setattr(bt, 'copy_database', failing)
    with pytest.raises(type(error)):
        bt.write_local(folder, database, NOW)
    assert os.listdir(folder) == []


def test_a_failed_copy_removes_its_own_sqlite_sidecars_too(database, folder, monkeypatch):
    def failing(source, dest, **kwargs):
        for suffix in ('', '-journal', '-wal', '-shm'):
            open(str(dest) + suffix, 'wb').close()
        raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(bt, 'copy_database', failing)
    with pytest.raises(sqlite3.OperationalError):
        bt.write_local(folder, database, NOW)
    assert os.listdir(folder) == []


def test_a_temporary_file_that_cannot_be_removed_is_logged_and_the_error_kept(
        database, folder, monkeypatch, caplog):
    monkeypatch.setattr(bt, 'copy_database',
                        mock.Mock(side_effect=OSError(28, 'No space left on device')))
    real_unlink = os.unlink

    def refusing(path, *args, **kwargs):
        if str(path).endswith('.partial'):
            raise PermissionError(13, 'in use')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(bt.os, 'unlink', refusing)
    with pytest.raises(OSError, match='No space'):
        bt.write_local(folder, database, NOW)
    assert 'Could not remove the temporary file' in caplog.text
    assert os.listdir(folder) == [NAME + '.partial']   # the sweep takes it an hour later


def test_a_refused_rename_removes_the_temporary_file(database, folder, monkeypatch):
    monkeypatch.setattr(bt.os, 'replace', mock.Mock(side_effect=PermissionError(13, 'in use')))
    with pytest.raises(PermissionError):
        bt.write_local(folder, database, NOW)
    assert os.listdir(folder) == []


def test_a_read_only_target_fails_the_backup(database, folder, monkeypatch):
    folder.mkdir()
    real_open = open

    def refusing(path, mode='r', *args, **kwargs):
        if str(path).endswith('.partial'):
            raise PermissionError(13, 'read-only')
        return real_open(path, mode, *args, **kwargs)
    monkeypatch.setattr('builtins.open', refusing)
    with pytest.raises(PermissionError):
        bt.write_local(folder, database, NOW)
    assert os.listdir(folder) == []


@pytest.mark.parametrize('stamp,removed', [
    ('20261114T110000', True), ('20261114T110001', False), ('20261114T130000', False),
], ids=['an hour old', 'younger than an hour', 'dated in the future'])
def test_leftover_temporary_files_are_removed_once_an_hour_old(database, folder, stamp,
                                                               removed):
    folder.mkdir()
    leftover = folder / f'warframe_tracker_{stamp}Z.db.partial'
    leftover.write_bytes(b'killed')
    bt.write_local(folder, database, NOW)
    assert leftover.exists() is not removed


@pytest.mark.parametrize('suffix', ['-journal', '-wal', '-shm'])
@pytest.mark.parametrize('stamp,removed', [('20261114T110000', True), ('20261114T113000', False)],
                         ids=['an hour old', 'younger than an hour'])
def test_leftover_sqlite_sidecars_of_a_killed_copy_are_removed_once_an_hour_old(
        database, folder, suffix, stamp, removed):
    folder.mkdir()
    leftover = folder / f'warframe_tracker_{stamp}Z.db.partial{suffix}'
    leftover.write_bytes(b'killed')
    bt.write_local(folder, database, NOW)
    assert leftover.exists() is not removed


def test_a_leftover_that_is_a_folder_or_has_another_name_stays(database, folder):
    (folder / 'warframe_tracker_20261101T000000Z.db.partial').mkdir(parents=True)
    others = ['Warframe_Tracker_20261103T000000Z.db.partial',
              'warframe_tracker_20261102T000000Z.db.partial-other',
              'warframe_tracker_20261102T000000Z.db.PARTIAL',
              'warframe_tracker_20261102T000000Z.db.partial.bak',
              'warframe_tracker_20260230T000000Z.db.partial',
              'warframe_tracker_２０２６1102T000000Z.db.partial']
    for name in others:
        (folder / name).write_bytes(b'x')
    bt.write_local(folder, database, NOW)
    assert len(os.listdir(folder)) == len(others) + 2


def test_a_leftover_that_cannot_be_removed_does_not_fail_the_backup(database, folder,
                                                                    monkeypatch, caplog):
    folder.mkdir()
    leftover = folder / 'warframe_tracker_20261101T000000Z.db.partial'
    leftover.write_bytes(b'killed')
    real_unlink = os.unlink

    def refusing(path, *args, **kwargs):
        if str(path) == str(leftover):
            raise PermissionError(13, 'in use')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(bt.os, 'unlink', refusing)
    assert bt.write_local(folder, database, NOW) == str(folder / NAME)
    assert leftover.exists() and 'Could not remove the leftover' in caplog.text


def test_a_missing_folder_is_an_empty_target_and_a_file_in_its_place_fails_the_listing(
        tmp_path):
    assert bt.list_local(tmp_path / 'absent') == []
    (tmp_path / 'file').write_bytes(b'x')
    with pytest.raises(NotADirectoryError, match='is not a folder'):
        bt.list_local(tmp_path / 'file')


def test_a_file_in_place_of_the_folder_fails_the_backup_and_is_left_alone(database, tmp_path):
    (tmp_path / 'file').write_bytes(b'mine')
    with pytest.raises(NotADirectoryError, match='is not a folder'):
        bt.write_local(tmp_path / 'file', database, NOW)
    assert (tmp_path / 'file').read_bytes() == b'mine'


@pytest.mark.parametrize('kind', ['symbolic link', 'junction'])
def test_a_folder_link_that_leads_nowhere_fails_the_listing_and_the_backup(database, tmp_path,
                                                                           kind):
    if kind == 'junction':
        if sys.platform != 'win32':
            pytest.skip('junctions are a Windows folder link')
        import _winapi
        (tmp_path / 'gone').mkdir()
        _winapi.CreateJunction(str(tmp_path / 'gone'), str(tmp_path / 'link'))
        (tmp_path / 'gone').rmdir()
    else:
        _link_or_skip(lambda: os.symlink(tmp_path / 'gone', tmp_path / 'link',
                                         target_is_directory=True))
    with pytest.raises(NotADirectoryError, match='is not a folder'):
        bt.list_local(tmp_path / 'link')
    with pytest.raises(NotADirectoryError, match='is not a folder'):
        bt.write_local(tmp_path / 'link', database, NOW)
    assert not (tmp_path / 'gone').exists()


def test_an_unreadable_folder_fails_the_listing(folder, monkeypatch):
    folder.mkdir()
    monkeypatch.setattr(bt.os, 'scandir', mock.Mock(side_effect=PermissionError(13, 'denied')))
    with pytest.raises(PermissionError):
        bt.list_local(folder)


def test_an_entry_that_vanishes_or_cannot_be_examined_is_skipped(folder, monkeypatch, caplog):
    folder.mkdir()
    names = ['warframe_tracker_20261101T000000Z.db', 'warframe_tracker_20261102T000000Z.db',
             NAME]
    for name in names:
        (folder / name).write_bytes(b'x')
    real_lstat = os.lstat

    def failing(path, *args, **kwargs):
        if str(path).endswith(names[0]):
            raise FileNotFoundError(2, 'gone')
        if str(path).endswith(names[1]):
            raise PermissionError(13, 'denied')
        return real_lstat(path, *args, **kwargs)
    monkeypatch.setattr(bt.os, 'lstat', failing)
    with caplog.at_level(logging.INFO):
        assert bt.list_local(folder) == [NAME]
    assert 'Skipped' in caplog.text


def test_a_zero_byte_file_with_a_backup_name_is_listed(folder):
    folder.mkdir()
    (folder / NAME).write_bytes(b'')
    assert bt.list_local(folder) == [NAME]


def test_a_second_write_in_the_same_second_is_skipped_and_the_first_kept(database, folder):
    bt.write_local(folder, database, NOW)
    first = (folder / NAME).read_bytes()
    with pytest.raises(bt.Skipped, match='already exists'):
        bt.write_local(folder, database, NOW)
    assert (folder / NAME).read_bytes() == first
    assert os.listdir(folder) == [NAME]


def _link_or_skip(make):
    try:
        make()
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'this system made no link here: {exc}')


def test_only_regular_files_directly_in_the_folder_are_listed(folder, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'target.db').write_bytes(b'outside')
    folder.mkdir()
    (folder / NAME).write_bytes(b'x')
    (folder / 'warframe_tracker_20261101T000000Z.db').mkdir()
    (folder / 'nested').mkdir()
    (folder / 'nested' / 'warframe_tracker_20261102T000000Z.db').write_bytes(b'x')
    os.link(folder / NAME, folder / 'warframe_tracker_20261103T000000Z.db')
    assert sorted(bt.list_local(folder)) == [
        'warframe_tracker_20261103T000000Z.db', NAME]


@pytest.mark.skipif(sys.platform != 'win32', reason='junctions are a Windows folder link')
def test_a_junction_named_like_a_backup_is_not_listed(folder, tmp_path):
    import _winapi
    outside = tmp_path / 'outside'
    outside.mkdir()
    folder.mkdir()
    _winapi.CreateJunction(str(outside), str(folder / 'warframe_tracker_20261101T000000Z.db'))
    assert bt.list_local(folder) == []


@pytest.mark.skipif(sys.platform != 'win32', reason='junctions are a Windows folder link')
def test_a_target_folder_that_is_a_junction_is_followed(database, tmp_path):
    import _winapi
    real = tmp_path / 'real'
    real.mkdir()
    _winapi.CreateJunction(str(real), str(tmp_path / 'link'))
    bt.write_local(tmp_path / 'link', database, NOW)
    assert os.listdir(real) == [NAME]
    assert bt.list_local(tmp_path / 'link') == [NAME]


def test_a_target_folder_that_is_a_symbolic_link_is_followed(database, tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    _link_or_skip(lambda: os.symlink(real, tmp_path / 'link', target_is_directory=True))
    bt.write_local(tmp_path / 'link', database, NOW)
    assert os.listdir(real) == [NAME]


def test_a_symbolic_link_named_like_a_backup_is_not_listed(folder, tmp_path):
    outside = tmp_path / 'outside.db'
    outside.write_bytes(b'outside')
    folder.mkdir()
    _link_or_skip(lambda: os.symlink(outside, folder / NAME))
    assert bt.list_local(folder) == []


def test_deleting_counts_removed_and_failed_and_continues(folder, monkeypatch, caplog):
    folder.mkdir()
    names = [f'warframe_tracker_2026110{d}T000000Z.db' for d in range(1, 5)]
    for name in names[:3]:
        (folder / name).write_bytes(b'x')
    (folder / names[3]).mkdir()            # became a folder since the listing
    real_unlink = os.unlink

    def refusing(path, *args, **kwargs):
        if str(path).endswith(names[1]):
            raise PermissionError(13, 'refused')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(bt.os, 'unlink', refusing)
    with caplog.at_level(logging.INFO):
        result = bt.delete_local(folder, names + ['warframe_tracker_20261109T000000Z.db'])
    assert result == (2, 2)                 # the gone one is neither
    assert sorted(os.listdir(folder)) == sorted([names[1], names[3]])
    assert 'already gone' in caplog.text


def test_deleting_a_hard_link_removes_only_that_name(folder):
    folder.mkdir()
    (folder / 'keep.db').write_bytes(b'data')
    os.link(folder / 'keep.db', folder / NAME)
    assert bt.delete_local(folder, [NAME]) == (1, 0)
    assert (folder / 'keep.db').read_bytes() == b'data'
