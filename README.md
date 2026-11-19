# Warframe Progress Tracker

A locally-hosted, multi-user companion app for Warframe that answers the question *"What should I focus on this session — and what is the most efficient way to get there?"* at a glance. It tracks your collection and mastery progress, optimises farming routes across multiple target items, surfaces current market prices as a buy-vs-farm alternative, and keeps item data current from the community item data and the Warframe wiki.

The full documentation is in [`docs/`](docs/README.md).

---

## Key Features

- **[Dashboard](docs/features/dashboard.md)** — your Mastery Rank and the progress to the next, items to master now, what to farm next and the prices you watch.
- **[Collection browser](docs/features/collection.md)** — every item per category as cards or a table, with owned / mastered / subsumed toggles, filters, notes and prices.
- **[Farming Planner](docs/features/farming-planner.md)** — goals with their crafting tree or shopping list, relic and mission sources, and a buy-or-farm answer for each.
- **[Live Session Advisor](docs/features/live-session-advisor.md)** — the node you are farming now, and what else your goals need that drops there.
- **[Prime Resurgence](docs/features/prime-resurgence.md)** — Varzia's current stock, read from the wiki at each sync, with an alert for the items you watch.
- **[Market](docs/features/market.md)** — read-only prices from [warframe.market](https://warframe.market), cached, never dropped when old, refreshed one item at a time.
- **[Wiki sync](docs/features/wiki-sync.md)** — one-click or scheduled update of the item catalogue, recipes, drop sources, images and market listings, never touching your progress.
- **[Settings and preferences](docs/features/settings.md)** — market cache duration, buy threshold, platinum per hour and farming hour cap; your password; clearing your progress or deleting your account; for admins, sync on startup, the scheduled sync interval and the session lifetime.
- **[Export and import](docs/features/export-import.md)** — all your data as a ZIP of CSV files that opens in a spreadsheet, and back again.
- **[Accounts and administration](docs/features/accounts.md)** — independent progress per account, local, OpenID Connect or Azure EasyAuth sign-in, admin and user roles.
- **[Backup](docs/backup.md)** — scheduled and on-demand copies of the database to a local folder and/or an S3-compatible bucket, each with its own retention.

---

## Quick start

```bash
# 1 — create and activate the virtual environment (one-time)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 2 — install dependencies
pip install -r app/requirements.txt

# 3 — make a secret key once and set it (it signs logins and form tokens; keep it private)
python -c "import secrets; print(secrets.token_hex(32))"
$env:SECRET_KEY = '<the printed value>'    # Windows (PowerShell)
# export SECRET_KEY='<the printed value>'  # macOS / Linux

# 4 — start the development server (on this machine only: http://localhost:5000)
python run.py
```

Open `http://localhost:5000`. On first run with an empty database the setup wizard creates the
initial admin account. `SECRET_KEY` is the one setting you must make; every other one has a
default and is listed in [Configuration](docs/configuration.md). The development server, its two
settings, a production server and running the tests are on the [install page](docs/install.md).

---

## Backups

Where backups go is set by environment variables: `BACKUP_LOCAL_PATH` for a local folder, and
`BACKUP_S3_BUCKET` with `BACKUP_S3_ACCESS_KEY` and `BACKUP_S3_SECRET_KEY` for an S3-compatible
bucket (restart the app after changing them). An admin chooses how long each target keeps its
backups in **Settings → Backup**, and **Back up now** there backs up every configured target at
once. With an MSSQL database no file backup is made: the database's own backups apply. The app
has no restore button; how to restore a backup by hand, the retention strategies and every
backup setting are on the [backup page](docs/backup.md).

---

## Documentation

- [Documentation index](docs/README.md)
- How-to: [Install and run](docs/install.md) · [Configuration](docs/configuration.md) ·
  [Sign-in and accounts](docs/sign-in.md) · [Backup and restore](docs/backup.md) ·
  [Deploy to Azure App Service](docs/azure.md)
- Features: [Dashboard](docs/features/dashboard.md) ·
  [Collection browser](docs/features/collection.md) ·
  [Farming Planner](docs/features/farming-planner.md) ·
  [Live Session Advisor](docs/features/live-session-advisor.md) ·
  [Prime Resurgence](docs/features/prime-resurgence.md) · [Market](docs/features/market.md) ·
  [Wiki sync](docs/features/wiki-sync.md) · [Settings and preferences](docs/features/settings.md) ·
  [Export and import](docs/features/export-import.md) ·
  [Accounts and administration](docs/features/accounts.md)

---

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python · Flask |
| Database | SQLite (default) or Azure SQL / MSSQL (`MSSQL_CONNECTION_STRING`) |
| Frontend | HTML / CSS / JS · Materialize CSS · jQuery · the app's own SVG icons (every asset served locally, no remote fonts) |
| Item data | The WFCD `warframe-items` data set (items, components, recipes); the Warframe wiki's MediaWiki API (`wiki.warframe.com/api.php`: category lists, blueprint recipes, the Prime Resurgence page); the warframestat.us drop tables and image CDN |
| Market data | warframe.market public REST API (version 2) |

---

## Out of Scope

- Automatic inventory sync via an official Warframe API (not publicly available for player data).
- Acting as a trade intermediary — market integration is read-only price data only.
- Mobile-native app — the responsive web UI is the intended interface on all devices.

---

## Licence

MIT, see [`LICENSE`](LICENSE). The vendored jQuery and Materialize CSS in `app/static/vendor/` keep their own MIT notices.
