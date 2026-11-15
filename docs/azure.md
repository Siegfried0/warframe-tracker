# Deploy to Azure App Service

The same code runs on Azure App Service; the mode is selected entirely by environment
variables (app settings), with no code change.

## Settings

Set these three variables; everything else is optional and identical to the private-server
settings in [Configuration](configuration.md).

| Variable | Required value | Effect |
|---|---|---|
| `SECRET_KEY` | A long random value (`python -c "import secrets; print(secrets.token_hex(32))"`) | **Required** — the app does not start without one; set it as an app setting (the App Service filesystem is ephemeral) |
| `MSSQL_CONNECTION_STRING` | Full pyodbc connection string | Switches database backend from SQLite to Azure SQL |
| `EASYAUTH_ENABLED` | `true` | Disables login forms; identity is read from EasyAuth HTTP headers |

With an MSSQL database the app makes no file backup: the database's own backups apply (see
[Backup and restore](backup.md#mssql)).

## EasyAuth

EasyAuth must also be enabled at the Azure portal level (App Service → Authentication blade)
independently of this environment variable. The variable tells the app to trust the injected
headers; it does not configure the Azure platform itself.

When `EASYAUTH_ENABLED=true`:
- The app's login, register, and first-run setup pages redirect to `/.auth/login`; the menu's
  logout entry links to `/.auth/logout`.
- The first user to access the app is auto-provisioned as `admin`; later users as `user`.
- An EasyAuth sign-in only ever matches an account EasyAuth created. When another account
  already has the same name, or a local or OpenID Connect account of an earlier setup has the
  same id, the request is refused (403) and nothing is created; an admin can delete that
  account.
- Account creation via the admin panel is hidden (accounts are created automatically on first
  EasyAuth access); role assignment remains available.
- OpenID Connect sign-in is off (see [Sign-in and accounts](sign-in.md)).

## Deployment

Zip the contents of `app/` and deploy via zip-deploy. The default Gunicorn startup command
requires no modification:

```
gunicorn --bind=0.0.0.0 --timeout 600 app:app
```

`app.py` exposes a module-level `app` object that Gunicorn picks up, so no custom startup
command is needed.
