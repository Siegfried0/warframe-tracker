"""
Backup service — local filesystem and S3-compatible targets.

Retention strategies
--------------------
interval_count : keep at most N backups; don't create a new one if the
                 most recent is less than X days old.
gfs            : Grandfather-Father-Son rotation (daily / weekly / monthly).
"""

import io
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)


def _timestamp() -> str:
    return datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')


def _db_path(app) -> Path:
    return Path(app.config['DATABASE_PATH'])


# ------------------------------------------------------------------
# Local backups
# ------------------------------------------------------------------

def _local_backups(target_dir: Path) -> list[Path]:
    return sorted(target_dir.glob('warframe_tracker_*.db'))


def backup_local(app, *, force: bool = False,
                 min_days: int = 1, keep: int = 7) -> dict:
    target_dir = Path(app.config.get('BACKUP_LOCAL_PATH') or '')
    if not target_dir or not str(target_dir).strip():
        return {'ok': False, 'error': 'BACKUP_LOCAL_PATH not configured'}

    target_dir.mkdir(parents=True, exist_ok=True)
    existing = _local_backups(target_dir)

    if not force and existing:
        last_mtime = datetime.utcfromtimestamp(existing[-1].stat().st_mtime)
        if datetime.utcnow() - last_mtime < timedelta(days=min_days):
            return {'ok': True, 'skipped': True,
                    'reason': f'last backup is less than {min_days} day(s) old'}

    dest = target_dir / f'warframe_tracker_{_timestamp()}.db'
    shutil.copy2(_db_path(app), dest)
    log.info('Local backup written: %s', dest)

    # Prune old backups
    existing = _local_backups(target_dir)
    while len(existing) > keep:
        existing.pop(0).unlink(missing_ok=True)

    return {'ok': True, 'path': str(dest)}


# ------------------------------------------------------------------
# S3-compatible backups
# ------------------------------------------------------------------

def backup_s3(app, *, force: bool = False) -> dict:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return {'ok': False, 'error': 'boto3 not installed (pip install boto3)'}

    cfg = app.config
    endpoint = cfg.get('BACKUP_S3_ENDPOINT')
    bucket = cfg.get('BACKUP_S3_BUCKET')
    prefix = cfg.get('BACKUP_S3_PREFIX', 'warframe-tracker/')
    access_key = cfg.get('BACKUP_S3_ACCESS_KEY')
    secret_key = cfg.get('BACKUP_S3_SECRET_KEY')

    if not bucket:
        return {'ok': False, 'error': 'BACKUP_S3_BUCKET not configured'}

    key = f'{prefix}warframe_tracker_{_timestamp()}.db'
    try:
        session = boto3.session.Session()
        client = session.client(
            's3',
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        client.upload_file(str(_db_path(app)), bucket, key)
        log.info('S3 backup uploaded: s3://%s/%s', bucket, key)
        return {'ok': True, 'key': key}
    except Exception as exc:
        log.error('S3 backup failed: %s', exc)
        return {'ok': False, 'error': str(exc)}


def backup_now(app, force: bool = True) -> dict:
    results = {}
    if app.config.get('BACKUP_LOCAL_PATH'):
        results['local'] = backup_local(app, force=force)
    if app.config.get('BACKUP_S3_BUCKET'):
        results['s3'] = backup_s3(app, force=force)
    if not results:
        return {'ok': False, 'error': 'No backup targets configured'}
    return results
