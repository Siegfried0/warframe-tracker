# Install and run

Every command below runs from the project root (the folder holding `run.py` and `app/`).

## Quick start

```bash
# 1 — create and activate the virtual environment (one-time)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 2 — install dependencies
pip install -r app/requirements.txt

# 3 — jQuery 3.7 and Materialize CSS 1.0 are included in app/static/vendor/ (MIT licence);
#     the icons are the app's own drawings: no page loads a font or asset from another server

# 4 — make a secret key once and set it (it signs logins and form tokens; keep it private)
python -c "import secrets; print(secrets.token_hex(32))"
$env:SECRET_KEY = '<the printed value>'    # Windows (PowerShell)
# export SECRET_KEY='<the printed value>'  # macOS / Linux

# 5 — start the development server (on this machine only: http://localhost:5000)
python run.py
```

`app/requirements.txt` also installs `boto3` (only needed for backups to S3) and `pyodbc` (only
needed for an MSSQL database).

## The secret key

The app does not start without `SECRET_KEY`, with a blank one, or with the example value
older versions used (`dev-change-me-in-production`, published with the source): the message
says how to make a key. Keep the same key across restarts, or everyone is logged out.

## The development server

`python run.py` (or `python app.py` from inside `app/`, the same thing) is the development
server, not a production one. It listens on `127.0.0.1` port 5000 only, with Flask's debugger
off. Two settings change that:

- `FLASK_RUN_HOST`: another address to listen on, e.g. `0.0.0.0` to reach it from your
  network (only when `SECRET_KEY` is set).
- `FLASK_DEBUG`: `1` or `true` turns on the interactive debugger and the reloader. The
  debugger runs code for whoever reaches it, so with `FLASK_DEBUG` on the server refuses to
  start unless `FLASK_RUN_HOST` is unset, `127.0.0.1`, `localhost` or `::1`.

Without a usable `SECRET_KEY` the development server still starts, for development: it then
makes a random key for that run, listens on `127.0.0.1` only (ignoring `FLASK_RUN_HOST`), and
logs a warning (logins end when it stops).

The database is the SQLite file `app/warframe_tracker.db`, created on the first start;
`DATABASE_PATH` puts it elsewhere (see [Configuration](configuration.md)).

## First run

Open `http://localhost:5000`. On first run with an empty database the setup wizard creates the
initial admin account. How the setup page behaves, and the other ways to sign in, are on the
[sign-in page](sign-in.md).

## Running it for longer

For a long-running installation, run the app under a WSGI server such as Gunicorn rather than
the development server. Gunicorn is not in `app/requirements.txt`: install it with
`pip install gunicorn`, then start it from the project root (`--chdir app` makes `app/` its
working directory), for example:

```bash
gunicorn --chdir app --bind=0.0.0.0 --timeout 600 app:app
```

`app.py` exposes a module-level `app` object that Gunicorn picks up. Unlike the development
server, `app:app` never starts without a usable `SECRET_KEY`. Any service manager can keep that
command running; the app ships no service definition of its own.

While the app runs, scheduled [wiki syncs](features/wiki-sync.md#scheduled-syncs) run in it: the app stores when the last sync finished
and checks every 15 minutes (first 2 minutes after it starts) whether `SYNC_INTERVAL_HOURS`
have passed since then, so a restart neither delays nor repeats the sync. Scheduled backups are
checked on the same rhythm (see [Backup and restore](backup.md)).

For Azure App Service, see [Deploy to Azure App Service](azure.md).

## Running the tests

The tests run offline; each test uses its own temporary database:

```bash
python -m pip install -r app/requirements.txt -r requirements-dev.txt
python -m pytest
```

GitHub Actions runs the same two commands on every push and pull request (Ubuntu 24.04, Python
3.11; `.github/workflows/tests.yml`).
