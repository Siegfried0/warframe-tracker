# Warframe Progress Tracker

A locally-hosted, multi-user companion app for Warframe that answers the question *"What should I focus on this session — and what is the most efficient way to get there?"* at a glance. It tracks your collection and mastery progress, optimises farming routes across multiple target items, surfaces current market prices as a buy-vs-farm alternative, and keeps item data current via the Warframe wiki.

---

## Key Features

- **Dashboard** — **MR rank widget** that calculates your current Mastery Rank from tracked mastered items and shows a progress bar to the next rank (mirroring the in-game UI so mismatches are immediately obvious); immediate-gain items (already owned, not yet mastered); next-best farming targets; live market prices for your want-list.
- **Collection browser** — card-based default view per category (portrait image, status badges, vaulted flag) with a sortable/filterable table view as an alternative; view preference is remembered per category. Inline toggles (mastered, owned, subsumed, Prime Resurgence watch), notes, and wiki links work in both views. Subsuming a Warframe (non-Prime Warframes only) marks it mastered and turns *owned* off, since the Helminth destroys the copy; mark a re-built copy owned again with one click. Un-mastering a subsumed Warframe also clears *subsumed*. A persistent **filter bar** lets you combine filters for ownership (owned/unowned), mastery status (mastered/unmastered), subsume status (Warframes only), source variant (Prime / non-Prime / Kuva / Tenet / …), vault status, and Prime Resurgence watch/availability. A **"Plan it"** button on every item card or table row adds the item directly to the Farming Planner. Portrait images fall back to a placeholder silhouette when no image is available.
- **Farming Planner** — goal-card system where each target item is shown as a card with portrait image (with placeholder fallback), category, and an overall completion progress bar. Goals can be added, removed, pinned (surfaced on the dashboard), and reordered. Each goal supports two display modes: **Tree View** (full crafting hierarchy with expandable/collapsible nodes and per-ingredient checkboxes) and **Shopping List View** (flat list with numeric quantity inputs and a per-resource progress bar). An **All Goals aggregate** collapses all active goals into a single shopping list, summing shared resources across goals. All checkbox states, quantity inputs, and display-mode preferences persist across sessions. Buy-vs-farm recommendation shown inline when market price beats expected farming time.
- **Live Session Advisor** — set a "Currently farming" mission node to surface secondary drop opportunities at that node across all your active goals. Each opportunity shows the drop chance at the current node, the optimal farming node's drop chance, and an efficiency rating (Optimal / Acceptable / Poor). A guaranteed-drop flag highlights when the node's primary drop is near-certain, making even low-probability side drops essentially free. A collapsible harvest summary estimates expected yields per run across all opportunities at the node.
- **Prime Resurgence** — each sync reads Varzia's current stock from the wiki's Prime Resurgence page. Items in it count as available in the collection's Resurgence filter; watched ones get a badge and a highlight, and unowned watched ones appear in the dashboard's "Available Now!" alert. The stock counts until the rotation's end date on the page (18:00 UTC); once that has passed, or before the first successful read, nothing is shown as available and a small notice says the Prime Resurgence data is missing or out of date. In the Farming Planner a vaulted item's components say they can be obtained by trade or through Prime Resurgence, and "in Prime Resurgence now" while it is in the current stock. If the page cannot be read (the wiki unreachable, or its layout changed) the sync still succeeds, keeps the last stock and says so in its result.
- **Market integration** — read-only price data from [warframe.market](https://warframe.market); cached locally with a configurable TTL.
- **Wiki sync** — one-click (or scheduled) sync from [wiki.warframe.com](https://wiki.warframe.com) to add new items, update vaulted / Prime Resurgence status, import crafting recipes, populate resource farm data, and fetch item portrait images — without touching user progress.
- **Multi-user** — independent progress records per account, local or OIDC or Azure EasyAuth authentication, role-based access (admin / user). Admins can create, deactivate, and delete accounts, assign roles, and reset passwords for local-auth accounts directly from the admin panel.
- **Preferences** — on the Settings page: market cache duration (hours, default 6), buy threshold (platinum) and platinum per hour. Each field takes a whole number (0 or more) or stays empty for the default; any other value is refused with a message naming the field, and nothing of the form is saved.
- **Backup** — local filesystem and/or S3-compatible bucket, with interval+count or GFS retention strategies.
- **Export / import** — download all your data as a ZIP of CSV files you can open in a spreadsheet: per-category progress (owned, mastered, subsumed, resurgence watch, notes, arcane ranks), farming goals with their component and resource progress (every component's state: pending, blueprint or crafted), the farming checklist, the market watchlist and your personal settings. Importing a ZIP (older exports too) checks every cell first, then merges it into your data in one step: rows in the file overwrite yours for the same item, new items are added, everything else is kept; one invalid cell refuses the whole file and the message names the file, line and column.

---

## Installation (Private Server)

```bash
# 1 — create and activate the virtual environment (one-time)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 2 — install dependencies
pip install -r app/requirements.txt

# 3 — jQuery 3.7 and Materialize CSS 1.0 are included in app/static/vendor/ (MIT licence)

# 4 — start the server
python run.py
```

Open `http://localhost:5000`. On first run with an empty database the setup wizard creates the initial admin account.

To run the tests (offline; each test uses its own temporary database):

```bash
python -m pip install -r app/requirements.txt -r requirements-dev.txt
python -m pytest
```

GitHub Actions runs the same two commands on every push and pull request (Ubuntu 24.04, Python 3.11; `.github/workflows/tests.yml`).

For long-running installations a **systemd service unit** is provided. Scheduled wiki syncs run automatically at the configured interval when running as a service.

---

## Deployment Modes

The same codebase handles both targets. Mode is selected entirely by environment variables — no code changes needed.

### Private Server (default)

No mandatory environment variables. SQLite and built-in login forms are used automatically.

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | *(insecure dev default)* | **Change this in production** |
| `DATABASE_PATH` | `warframe_tracker.db` | Absolute path to SQLite file |
| `SESSION_LIFETIME_DAYS` | `30` | Session cookie lifetime |
| `MARKET_CACHE_HOURS` | `6` | Market price cache TTL |
| `AUTO_SYNC_ON_STARTUP` | `false` | Run wiki sync at server start |
| `SYNC_INTERVAL_HOURS` | `24` | Background sync interval (0 = off) |
| `RESURGENCE_FALLBACK_DAYS` | `7` | Days the Prime Resurgence stock counts after a sync when the wiki page shows no end date |
| `OIDC_CLIENT_ID` | — | Optional OIDC integration |
| `OIDC_CLIENT_SECRET` | — | |
| `OIDC_ISSUER_URL` | — | |
| `OIDC_REDIRECT_URI` | — | |
| `BACKUP_LOCAL_PATH` | — | Local backup directory |
| `BACKUP_S3_ENDPOINT` | — | S3-compatible endpoint URL |
| `BACKUP_S3_BUCKET` | — | S3 bucket name |
| `BACKUP_S3_PREFIX` | `warframe-tracker/` | S3 key prefix |
| `BACKUP_S3_ACCESS_KEY` | — | S3 credentials |
| `BACKUP_S3_SECRET_KEY` | — | |

### Azure App Service

Set these three variables; everything else is optional and identical to the private-server table above.

| Variable | Required value | Effect |
|---|---|---|
| `SECRET_KEY` | A strong random value | **Required** — the App Service filesystem is ephemeral |
| `MSSQL_CONNECTION_STRING` | Full pyodbc connection string | Switches database backend from SQLite to Azure SQL |
| `EASYAUTH_ENABLED` | `true` | Disables login forms; identity is read from EasyAuth HTTP headers |

**EasyAuth setup note**: EasyAuth must also be enabled at the Azure portal level (App Service → Authentication blade) independently of this environment variable. The variable tells the app to trust the injected headers; it does not configure the Azure platform itself.

When `EASYAUTH_ENABLED=true`:
- The app's login, register, and first-run setup pages redirect to `/.auth/login` and `/.auth/logout`.
- The first user to access the app is auto-provisioned as `admin`.
- Account creation via the admin panel is hidden (accounts are created automatically on first EasyAuth access); role assignment remains available.

**Deployment**: zip the contents of `app/` and deploy via zip-deploy. The default Gunicorn startup command requires no modification:
```
gunicorn --bind=0.0.0.0 --timeout 600 app:app
```

---

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python · Flask |
| Database | SQLite (default) or Azure SQL / MSSQL (`MSSQL_CONNECTION_STRING`) |
| Frontend | HTML / CSS / JS · Materialize CSS · jQuery |
| Wiki data | wiki.warframe.com Cargo API |
| Market data | warframe.market public REST API |

---

## Out of Scope

- Automatic inventory sync via an official Warframe API (not publicly available for player data).
- Acting as a trade intermediary — market integration is read-only price data only.
- Mobile-native app — the responsive web UI is the intended interface on all devices.

---

## Licence

MIT, see [`LICENSE`](LICENSE). The vendored jQuery and Materialize CSS in `app/static/vendor/` keep their own MIT notices.
