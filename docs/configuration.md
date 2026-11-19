# Configuration

The app is configured by environment variables only; the same code runs as a private server
and on Azure App Service, and the mode is chosen by these variables, with no code change. The
variables are read when the app starts: restart it after changing one.

On a private server only `SECRET_KEY` is required; SQLite and the built-in login forms are used
automatically. For Azure, see [Deploy to Azure App Service](azure.md).

A value read as a number (the `*_DAYS` and `*_HOURS` variables) must be a whole number. A
switch read as `true` (`EASYAUTH_ENABLED`) is on only for the word `true` in any letter case;
anything else, `1` included, is off.

**Auto-sync, the sync interval and the session lifetime** (`AUTO_SYNC_ON_STARTUP`,
`SYNC_INTERVAL_HOURS`, `SESSION_LIFETIME_DAYS`) each come from one of three places, in this
order:

1. **The environment variable, when it is set.** It pins the setting: the app uses it and
   nothing stored overrides it. A set variable must hold a usable value: a whole number in
   digits only within the setting's range (blanks around it are ignored), or for
   `AUTO_SYNC_ON_STARTUP` the word `true` or `false` in any letter case. Anything else, an
   empty value included, and the app does not start, with a message naming the variable and
   what it accepts. (In PowerShell, `$env:NAME = ''` sets the variable to an empty value; remove
   it with `Remove-Item Env:NAME`.)
2. **The app's stored setting**, an app-wide row in the database, when the variable is not set.
   An admin sets it in **Settings → Sync & Sessions** (see
   [Settings and preferences](features/settings.md#sync--sessions-admins-only)); a setting
   pinned by its variable is shown there read-only, with the variable to remove. A stored value
   that is not usable counts as the default (logged once) and stays stored until an admin saves.
3. **The default** in the tables below.

The sync interval and the session lifetime stored in the database apply without a restart (the
interval from the next scheduled check, at most 15 minutes later; the lifetime within a
minute); auto-sync on startup applies from the next start.

## The app

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | — (**required**) | Signs logins and form tokens: a long random value (`python -c "import secrets; print(secrets.token_hex(32))"`); the app does not start without one, with a blank one or with `dev-change-me-in-production` |
| `DATABASE_PATH` | `warframe_tracker.db` in `app/` | Path of the SQLite file (an absolute path is safest; a relative one is taken from `app/`, the working directory of both `python run.py` and the Gunicorn command, not from the folder you start them in); created on the first start |
| `MSSQL_CONNECTION_STRING` | — | A full pyodbc connection string: switches the database from SQLite to Azure SQL / MSSQL (`DATABASE_PATH` is then not used, and no file backup is made) |
| `SESSION_LIFETIME_DAYS` | `30` | How long a session lasts without a request, in days (1 to 365): every sign-in ends after this idle time. Every sign-in also ends three times this long after it was made (90 days with the default), however often it is used; you then sign in again. "Remember me" (and every sign-in through setup, registration or an OpenID Connect provider) keeps the session when the browser closes; without it the session ends with the browser. See the three sources above |
| `SESSION_COOKIE_SAMESITE` | `Lax` | The session cookie's `SameSite` attribute (`Lax`, `Strict`, or `None`, which needs HTTPS), in any letter case; empty means `Lax`; any other value and the app does not start, with a message naming the setting and its three values |
| `FLASK_RUN_HOST` | `127.0.0.1` | Development server only (`python run.py`): the address to listen on; ignored without a usable `SECRET_KEY` (see [Install and run](install.md)) |
| `FLASK_DEBUG` | off | Development server only: `1` or `true` turns on the debugger and reloader; refused with a host other than `127.0.0.1`, `localhost` or `::1` |

## Wiki sync

| Variable | Default | Meaning |
|---|---|---|
| `AUTO_SYNC_ON_STARTUP` | `false` | `true` or `false`: run a [wiki sync](features/wiki-sync.md) 2 minutes after every server start, whether one is due or not. See the three sources above |
| `SYNC_INTERVAL_HOURS` | `24` | Hours (0 to 8760) after the last finished sync (successful or not) before the scheduled sync runs again; the time is stored, so restarts do not reset it (0 = off). See the three sources above |
| `RESURGENCE_FALLBACK_DAYS` | `7` | Days the [Prime Resurgence](features/prime-resurgence.md) stock counts after a sync when the wiki page shows no end date (a value below 1 means 7) |

## Sign-in

| Variable | Default | Meaning |
|---|---|---|
| `OIDC_CLIENT_ID` | — | Optional sign-in through an OpenID Connect provider: set all four of `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_ISSUER_URL`, `OIDC_REDIRECT_URI` |
| `OIDC_CLIENT_SECRET` | — | The client secret the provider issued; kept out of the database |
| `OIDC_ISSUER_URL` | — | The provider's issuer URL (`https`) |
| `OIDC_REDIRECT_URI` | — | `https://<your host>/auth/oidc/callback` |
| `OIDC_ALLOWED_EMAIL_DOMAINS` | — | Optional: only emails of these domains may sign in through OIDC (comma-separated, for example `example.com, example.org`) |
| `OIDC_ALLOWED_GROUPS` | — | Optional: only members of these provider groups may sign in through OIDC (comma-separated, exact names) |
| `EASYAUTH_ENABLED` | `false` | `true`: Azure App Service EasyAuth signs users in; the login forms are off and OIDC sign-in is off |

How these work together is on the [sign-in page](sign-in.md).

## Backup

| Variable | Default | Meaning |
|---|---|---|
| `BACKUP_LOCAL_PATH` | — | Local backup directory, an absolute path (created if missing; a relative path is refused); setting it turns on scheduled backups |
| `BACKUP_S3_ENDPOINT` | — | S3-compatible endpoint URL |
| `BACKUP_S3_REGION` | — | S3 region (blanks removed; unset or blank = no region, the S3 client's default) |
| `BACKUP_S3_CA_BUNDLE` | — | Absolute path of a PEM file with the certificate authority of a self-hosted store (for example MinIO behind your own CA); it replaces the built-in certificates for the S3 connection. Unset or blank = the built-in certificates. A relative path or one that is not a file fails the S3 backup only; certificate checks are never switched off |
| `BACKUP_S3_BUCKET` | — | S3 bucket name; setting it with both keys below turns on scheduled backups to S3 |
| `BACKUP_S3_PREFIX` | `warframe-tracker/` | S3 key prefix (leading `/` removed, a trailing `/` added; empty = the bucket root) |
| `BACKUP_S3_ACCESS_KEY` | — | S3 access key; required with the bucket (no other credentials are ever used) |
| `BACKUP_S3_SECRET_KEY` | — | S3 secret key; required with the bucket |

How backups are made, kept and restored is on the [backup page](backup.md).
