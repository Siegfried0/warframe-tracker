# Backup and restore

The app backs up its SQLite database to a local folder, to an S3-compatible bucket, or to both.
Each target is backed up on its own schedule and keeps its backups by its own retention.

## Targets

Where backups go is set by the environment only, so the S3 keys never enter the database;
restart the app after changing them (every variable is in [Configuration](configuration.md#backup)):

- **Local folder**: `BACKUP_LOCAL_PATH`, an absolute path (created if missing; a relative path
  is refused).
- **S3-compatible bucket**: `BACKUP_S3_BUCKET` with both `BACKUP_S3_ACCESS_KEY` and
  `BACKUP_S3_SECRET_KEY`; optionally `BACKUP_S3_ENDPOINT`, `BACKUP_S3_REGION`,
  `BACKUP_S3_PREFIX` (default `warframe-tracker/`) and `BACKUP_S3_CA_BUNDLE`. S3 backups need
  `boto3`, which `app/requirements.txt` installs.

No `AWS_*` setting (region, endpoint, profile, certificates, retries, ...) and no file under
`~/.aws` configures the S3 backup (TLS is checked against the certificate bundle installed with
the app's Python packages, or the one `BACKUP_S3_CA_BUNDLE` names); the proxy variables
(`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`) do apply.

## When a backup is made

While the app runs it checks every 15 minutes (the first check 2 minutes after the start)
whether a configured target is due, and backs it up then. Each backup is a consistent copy of
the SQLite database, made while the app runs.

**Back up now**, on the [Settings page](features/settings.md#backup-admins-only) (admins only), backs up every configured target at once
and reports one line per target: the backup written and how many old backups were removed (or
could not be), or why it was skipped or failed.

## Retention

How long each target keeps its backups is chosen by an admin in **Settings → Backup**, one form
per configured target. The section shows where each target writes (the folder; the S3 endpoint,
bucket and prefix, never a key) and, for a target that is not configured, which variables
configure it. Users who are not admins see no backup section. Choose per target:

- *Interval + count* (the default: 1 day, 7 kept), for a desktop or development setup that
  starts and stops often: a backup is made when the newest is at least that many days old
  (0 = at every check), and the given number of newest backups is kept (1–1000; days 0–365).
- *Grandfather-father-son* (by default 7 daily, 4 weekly, 6 monthly), for an instance that runs
  all the time as a service: the newest backup of each of the last days, weeks and months that
  have one is kept (each count 0–1000, at least one above 0).

A save checks every field first and changes nothing when one is out of range, naming each field
and its range. The new retention applies from that target's next backup; switching strategies
deletes nothing by itself.

Old backups are removed only after a new one was written, and only files named
`warframe_tracker_YYYYMMDDTHHMMSSZ.db` directly in the folder or under the prefix: anything else
you keep there is left alone.

## MSSQL

With an MSSQL database (`MSSQL_CONNECTION_STRING` set) no file backup is made, scheduled or by
**Back up now**, and there is no retention to choose: the database's own backups apply.

## What a backup file is

A backup is one complete SQLite database file, a copy of the app's database as it was at that
moment: every account, its progress, goals, watchlist and settings, and the item catalogue.

- Its name is `warframe_tracker_YYYYMMDDTHHMMSSZ.db`, the time it was made in UTC (for example
  `warframe_tracker_20260301T031500Z.db`).
- In the local folder it is written under a temporary name ending in `.partial` and renamed
  only when complete, so a file with the final name is always a whole backup. A `.partial`
  file is a backup that did not finish: never restore one.
- In the bucket it is the object `<prefix><name>`, uploaded in one piece.

## Restoring a backup

The app has no restore function: a backup is put back by hand, while the app is stopped.

1. **Stop the app** (the development server, Gunicorn or the service that runs it), so nothing
   writes to the database while you replace it.
2. **Keep the current database file aside**: rename or move the file `DATABASE_PATH` names
   (by default `app/warframe_tracker.db`), together with any `-journal`, `-wal` or `-shm` file
   beside it, so you can go back to it.
3. **Put the backup in its place**: copy the backup file you chose (download it from the bucket
   first for an S3 backup) to the path `DATABASE_PATH` names, under that file name (a relative
   path is inside `app/`).
4. **Start the app.** It opens the restored database; a backup made by an older version of the
   app is brought up to date when the app starts, as any older database is.

Everything written after the backup was made is not in it: progress, goals and settings changed
since then are lost unless you export them first with the app's [export and import](features/export-import.md), which moves
one user's data rather than the whole database.
