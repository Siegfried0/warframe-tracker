"""The file and object work of a backup: the consistent copy, and listing, writing and deleting
in the local folder and under the S3 prefix.

The copy is made with SQLite's online backup API in one step; a database locked for more than
60 seconds fails the backup instead of waiting for ever. The local backup is written to its
temporary name (``<name>.partial``, created exclusively) and renamed only when complete, so a
killed backup never leaves a name that counts, and two processes writing in the same second
never replace each other's backup. Only regular files directly in the folder are listed: a
link, a junction or a sub-folder is never a backup and is never deleted. The S3 backup
uploads such a copy from a temporary folder; nothing temporary is written to the bucket.
"""
import logging
import os
import re
import sqlite3
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from services.backup_retention import TIME_FORMAT, backup_name, partial_name

log = logging.getLogger(__name__)

BUSY_LIMIT_SECONDS = 60                    # a locked database fails the backup after this
LEFTOVER_AGE = timedelta(hours=1)          # a temporary file this old is a killed backup's
DELETE_BATCH = 1000                        # S3 DeleteObjects takes at most 1000 keys
_BUSY = (5, 6)                             # SQLITE_BUSY, SQLITE_LOCKED
SIDECARS = ('-journal', '-wal', '-shm')        # files SQLite may leave beside a killed copy
# A killed backup's temporary file, or a SQLite sidecar of it.
LEFTOVER_PATTERN = re.compile(
    r'warframe_tracker_([0-9]{8}T[0-9]{6}Z)\.db\.partial(?:-journal|-wal|-shm)?')
ABSOLUTE_PATH_REASON = 'BACKUP_LOCAL_PATH must be an absolute path'
S3_KEYS_REASON = 'BACKUP_S3_ACCESS_KEY and BACKUP_S3_SECRET_KEY must both be set'


class Skipped(Exception):
    """The backup was not made, and not because of an error (another one has this name)."""


# ── Configuration ────────────────────────────────────────────────────────────

def local_folder(config):
    """The configured folder (blanks stripped), or None when unset or blank. A relative path is
    returned too: ``check_local_folder`` refuses it."""
    text = (config.get('BACKUP_LOCAL_PATH') or '').strip()
    return Path(text) if text else None


def check_local_folder(folder: Path) -> Path:
    """The folder, when it can be a target: never resolved against the working directory."""
    if not folder.is_absolute():
        raise ValueError(ABSOLUTE_PATH_REASON)
    return folder


def s3_bucket(config):
    return (config.get('BACKUP_S3_BUCKET') or '').strip() or None


def s3_prefix(config) -> str:
    """Blanks and leading ``/`` stripped; empty is the bucket root, else ends in one ``/``."""
    text = (config.get('BACKUP_S3_PREFIX') or '').strip().lstrip('/').rstrip('/')
    return text + '/' if text else ''


def s3_keys_missing(config):
    """The reason the S3 target is not configured although a bucket is (an access key or the
    secret key missing or blank), else None. Credentials are never taken from anywhere else
    (``AWS_*`` variables, ``~/.aws``, the instance-metadata endpoint)."""
    if not (config.get('BACKUP_S3_ACCESS_KEY') or '').strip() \
            or not (config.get('BACKUP_S3_SECRET_KEY') or '').strip():
        return S3_KEYS_REASON
    return None


def _s3_client(config):
    """The S3 client of the configured endpoint and keys (the tests replace this seam). Its
    timeouts and retries bound an unreachable endpoint to about a minute. Without both keys
    it raises before boto3 is imported, so no other credentials are ever looked up.

    The target is configured by these settings alone: the session reads no profile
    (``AWS_PROFILE``), no AWS config or credentials file, and no endpoint from the
    environment (``AWS_ENDPOINT_URL``, ``AWS_ENDPOINT_URL_S3``), so settings meant for
    another tool neither send the database elsewhere nor stop the backup."""
    reason = s3_keys_missing(config)
    if reason:
        raise ValueError(reason)
    import boto3
    import botocore.session
    from botocore.config import Config
    # (name in the config file, environment variable, default, converter): no file, no variable.
    core = botocore.session.Session(session_vars={
        'profile': (None, None, None, None),
        'config_file': (None, None, os.devnull, None),
        'credentials_file': (None, None, os.devnull, None),
    })
    return boto3.session.Session(botocore_session=core).client(
        's3',
        endpoint_url=(config.get('BACKUP_S3_ENDPOINT') or '').strip() or None,
        aws_access_key_id=config['BACKUP_S3_ACCESS_KEY'].strip(),
        aws_secret_access_key=config['BACKUP_S3_SECRET_KEY'].strip(),
        config=Config(connect_timeout=10, read_timeout=30, retries={'max_attempts': 2},
                      ignore_configured_endpoint_urls=True),
    )


# ── The consistent copy ──────────────────────────────────────────────────────

def _read_only_uri(path) -> str:
    """A SQLite URI that opens ``path`` read-only. The path is made absolute and every
    character a URI reserves is escaped (``%``, ``#``, ``?``, blanks, non-ASCII); the authority
    stays empty, so a Windows network path (``\\\\server\\share``) opens too."""
    text = os.path.abspath(path).replace(os.sep, '/')
    if not text.startswith('/'):
        text = '/' + text            # a drive path: file:///C:/...
    return 'file://' + quote(text, safe='/:') + '?mode=ro'


def copy_database(source, dest, clock=time.monotonic, busy_limit=BUSY_LIMIT_SECONDS):
    """Copy the SQLite database ``source`` into the file ``dest`` (absent or empty).

    The source is opened read-only, so a missing database fails the copy instead of creating
    an empty one. The copy is switched to the rollback journal before it is closed, so the
    finished file never asks for a ``-wal`` or ``-shm`` beside it."""
    if not Path(source).is_file():
        raise FileNotFoundError(f'the database file {source} does not exist')
    busy_since = []

    def progress(status, remaining, total):
        if status in _BUSY:
            busy_since[:1] = busy_since[:1] or [clock()]
            if clock() - busy_since[0] >= busy_limit:
                raise TimeoutError(f'the database stayed locked for {busy_limit} seconds')

    # No busy timeout of its own: the backup loop retries a locked step every 250 ms and the
    # progress callback above bounds the wait.
    src = sqlite3.connect(_read_only_uri(source), timeout=0, uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst, pages=-1, progress=progress)
            dst.execute('PRAGMA journal_mode=DELETE')
        finally:
            dst.close()
    finally:
        src.close()


# ── The local folder ─────────────────────────────────────────────────────────

def _is_regular(path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError as exc:      # removed since the folder was read, or cannot be examined
        log.info('Skipped %s while listing: %s', path, exc)
        return False


def _regular_files(folder: Path):
    with os.scandir(folder) as entries:
        return [e.name for e in entries if _is_regular(e.path)]


def list_local(folder: Path) -> list:
    """The names of the regular files directly in the folder. Nothing at the path is an empty
    target (the backup creates the folder); a path that is not a folder (a file, a link that
    leads nowhere) or a folder that cannot be read raises. A folder that is a link is
    followed."""
    if not os.path.lexists(folder):
        return []
    if not os.path.isdir(folder):
        raise NotADirectoryError(f'BACKUP_LOCAL_PATH {folder} is not a folder')
    try:
        return _regular_files(folder)
    except FileNotFoundError:   # removed since it was looked at
        return []


def _remove(path: Path, what: str) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        log.info('%s %s was already gone', what, path)
    except OSError as exc:
        log.warning('Could not remove %s %s: %s', what, path, exc)
    return False


def _leftover_time(name):
    match = LEFTOVER_PATTERN.fullmatch(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _remove_leftovers(folder: Path, now):
    """Remove temporary files of killed backups and their SQLite sidecars, once an hour old by
    their name time (a younger one may be another process's backup in progress)."""
    try:
        names = _regular_files(folder)
    except OSError as exc:
        log.warning('Could not list %s for leftover temporary files: %s', folder, exc)
        return
    for name in names:
        moment = _leftover_time(name)
        if moment is not None and moment <= now - LEFTOVER_AGE:
            _remove(folder / name, 'the leftover temporary file')


def write_local(folder: Path, database, now) -> str:
    """Write a backup into the folder; returns its path. Raises ``Skipped`` when another
    backup has this second's name, any other error when the backup failed."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except FileExistsError:     # a file, or a link that leads nowhere, holds the name
        raise NotADirectoryError(f'BACKUP_LOCAL_PATH {folder} is not a folder') from None
    _remove_leftovers(folder, now)
    final, partial = folder / backup_name(now), folder / partial_name(now)
    try:
        open(partial, 'xb').close()
    except FileExistsError:
        raise Skipped('another backup of this target is being written in this second') from None
    try:
        if os.path.lexists(final):
            raise Skipped('a backup with this name already exists')
        copy_database(database, partial)
        os.replace(partial, final)
    except BaseException:
        _remove(partial, 'the temporary file')
        for suffix in SIDECARS:
            if os.path.lexists(str(partial) + suffix):
                _remove(Path(str(partial) + suffix), 'the temporary file')
        raise
    log.info('Local backup written: %s', final)
    return str(final)


def delete_local(folder: Path, names) -> tuple:
    """Delete the named regular files of the folder; returns (removed, failed)."""
    removed = failed = 0
    for name in names:
        path = folder / name
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                log.warning('Not deleted, no longer a regular file: %s', path)
                failed += 1
                continue
            os.unlink(path)
            removed += 1
        except FileNotFoundError:
            log.info('Old backup %s was already gone', path)
        except OSError as exc:
            log.warning('Could not delete old backup %s: %s', path, exc)
            failed += 1
    return removed, failed


# ── The S3 prefix ────────────────────────────────────────────────────────────

def list_s3(client, bucket: str, prefix: str) -> list:
    """The names of the objects directly under the prefix, every page. A truncated page
    without a continuation token raises: a partial listing must delete nothing."""
    names, token = [], None
    while True:
        request = {'Bucket': bucket, 'Prefix': prefix, 'Delimiter': '/'}
        if token:
            request['ContinuationToken'] = token
        page = client.list_objects_v2(**request)
        for entry in page.get('Contents', []):
            key = entry.get('Key', '')
            if key.startswith(prefix):
                names.append(key[len(prefix):])
        if not page.get('IsTruncated'):
            return names
        token = page.get('NextContinuationToken')
        if not token:
            raise RuntimeError('the listing was cut short without a continuation token')


def write_s3(client, bucket: str, prefix: str, database, now) -> str:
    """Upload a consistent copy as ``<prefix><name>`` in one ``PutObject`` (never multipart, which
    could leave an incomplete upload behind); returns the key."""
    key = prefix + backup_name(now)
    with tempfile.TemporaryDirectory(prefix='warframe_tracker_backup_') as staging:
        copy = Path(staging) / backup_name(now)
        copy_database(database, copy)
        with open(copy, 'rb') as body:
            client.put_object(Bucket=bucket, Key=key, Body=body)
    log.info('S3 backup uploaded: s3://%s/%s', bucket, key)
    return key


def delete_s3(client, bucket: str, prefix: str, names) -> tuple:
    """Delete the named objects in batches; returns (removed, failed)."""
    keys = [prefix + name for name in names]
    removed = failed = 0
    for start in range(0, len(keys), DELETE_BATCH):
        batch = keys[start:start + DELETE_BATCH]
        try:
            answer = client.delete_objects(
                Bucket=bucket, Delete={'Objects': [{'Key': key} for key in batch]})
        except Exception as exc:  # noqa: BLE001 (a refused batch: the next one still runs)
            log.warning('Could not delete %d old backups in s3://%s: %s', len(batch), bucket, exc)
            failed += len(batch)
            continue
        errors = answer.get('Errors') or []
        for error in errors:
            log.warning('Could not delete old backup s3://%s/%s: %s', bucket,
                        error.get('Key'), error.get('Code'))
        failed += len(errors)
        removed += len(batch) - len(errors)
    return removed, failed
