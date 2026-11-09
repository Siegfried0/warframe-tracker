# Warframe Progress Tracker

A locally-hosted, multi-user companion app for Warframe that answers the question *"What should I focus on this session — and what is the most efficient way to get there?"* at a glance. It tracks your collection and mastery progress, optimises farming routes across multiple target items, surfaces current market prices as a buy-vs-farm alternative, and keeps item data current via the Warframe wiki.

---

## Key Features

- **Dashboard** — **MR rank widget** that calculates your current Mastery Rank from tracked mastered items and shows a progress bar to the next rank (mirroring the in-game UI so mismatches are immediately obvious); immediate-gain items (already owned, not yet mastered); next-best farming targets (shown when no goal is pinned) with their last cached market price and a buy hint; a **Market Watch** panel with the last cached prices of up to ten items on your market watchlist (by name, with a link to the full list), each marked stale with its fetch time once it is older than your market cache duration. The dashboard never fetches prices itself: refresh them on the market page.
- **Collection browser** — card-based default view per category (portrait image, status badges, vaulted flag) with a sortable/filterable table view as an alternative; view preference is remembered per category. Inline toggles (mastered, owned, subsumed, Prime Resurgence watch), notes, and wiki links work in both views. Every item on warframe.market (tradable, or listed there, such as prime warframes sold as sets), mods and arcanes included, has a **market watch** toggle (a bookmark icon) that adds it to or removes it from your market watchlist with one click; watched items appear on the Market page and in the dashboard's Market Watch panel. Subsuming a Warframe (non-Prime Warframes only) marks it mastered and turns *owned* off, since the Helminth destroys the copy; mark a re-built copy owned again with one click. Un-mastering a subsumed Warframe also clears *subsumed*. A persistent **filter bar** lets you combine filters for ownership (owned/unowned), mastery status (mastered/unmastered), subsume status (Warframes only), source variant (Prime / non-Prime / Kuva / Tenet / …), vault status, and Prime Resurgence watch/availability. A **"Plan it"** button on every item card or table row adds the item directly to the Farming Planner, and a **drop sources** button beside it (the only one on mods and arcanes) shows where the item drops and, for an item built from parts, the relics holding each part and where those relics drop. Portrait images fall back to a placeholder silhouette when no image is available.
- **Farming Planner** — goal-card system where each target item is shown as a card with portrait image (with placeholder fallback), category, and an overall completion progress bar. Goals can be added, removed, pinned (surfaced on the dashboard), and reordered: the arrow buttons on a card move it one place up or down, pinned goals always staying above the others, and the dashboard lists pinned goals in the same order. Each goal supports two display modes: **Tree View** (full crafting hierarchy with expandable/collapsible nodes and per-ingredient checkboxes) and **Shopping List View** (flat list with numeric quantity inputs and a per-resource progress bar). An **All Goals aggregate** collapses all active goals into a single shopping list, summing shared resources across goals. All checkbox states, quantity inputs, and display-mode preferences persist across sessions. Buy-vs-farm recommendation shown inline when market price beats expected farming time. A component that comes from void relics (most Prime parts) lists the relics that hold it, each with its Intact drop chance and the missions where that relic drops (the three needing the fewest runs); a relic that drops from no mission now is marked vaulted.
- **Live Session Advisor** — set a "Currently farming" mission node to surface secondary drop opportunities at that node across all your active goals. Each opportunity shows the drop chance at the current node, the optimal farming node's drop chance, and an efficiency rating (Optimal / Acceptable / Poor). A guaranteed-drop flag highlights when the node's primary drop is near-certain, making even low-probability side drops essentially free. A collapsible harvest summary estimates expected yields per run across all opportunities at the node.
- **Prime Resurgence** — each sync reads Varzia's current stock from the wiki's Prime Resurgence page. Items in it count as available in the collection's Resurgence filter; watched ones get a badge and a highlight, and unowned watched ones appear in the dashboard's "Available Now!" alert. The stock counts until the rotation's end date on the page (18:00 UTC); once that has passed, or before the first successful read, nothing is shown as available and a small notice says the Prime Resurgence data is missing or out of date. In the Farming Planner a vaulted item's components say they can be obtained by trade or through Prime Resurgence, and "in Prime Resurgence now" while it is in the current stock. If the page cannot be read (the wiki unreachable, or its layout changed) the sync still succeeds, keeps the last stock and says so in its result.
- **Market integration** — read-only price data from [warframe.market](https://warframe.market); cached locally with a configurable TTL. Each sync looks every item up in warframe.market's item list (by its name, or its name with " Set" for items traded as sets) to find its listing; items found there get prices and warframe.market links, items not listed have none. If the list cannot be read the sync still succeeds, keeps the listings it found before and says so in its result.
- **Wiki sync** — one-click (or scheduled) sync from [wiki.warframe.com](https://wiki.warframe.com) to add new items, update vaulted / Prime Resurgence status, import crafting recipes, populate resource farm data, fetch item portrait images, and look up each item's warframe.market listing — without touching user progress.
- **Multi-user** — independent progress records per account, local or OIDC or Azure EasyAuth authentication, role-based access (admin / user). Admins can create, deactivate, and delete accounts, assign roles, and reset passwords for local-auth accounts directly from the admin panel.
- **Preferences** — on the Settings page: market cache duration (hours, default 6), buy threshold (platinum) and platinum per hour. Each field takes a whole number (0 or more) or stays empty for the default; any other value is refused with a message naming the field, and nothing of the form is saved.
- **Backup** — local filesystem and/or S3-compatible bucket, with interval+count or GFS retention strategies.
- **Export / import** — download all your data as a ZIP of CSV files you can open in a spreadsheet: per-category progress (owned, mastered, subsumed, resurgence watch, notes, arcane ranks), farming goals with their component and resource progress (every component's state: pending, blueprint or crafted), the farming checklist, the market watchlist and your personal settings. A cell that starts with `=`, `+`, `-`, `@`, a tab, a carriage return or `'` is written with a leading `'` so that a spreadsheet shows it as text instead of running it as a formula; importing the ZIP removes that `'` again, so your notes and names come back exactly as they were. Importing a ZIP (older exports too) checks every cell first, then merges it into your data in one step: rows in the file overwrite yours for the same item, new items are added, everything else is kept; one invalid cell refuses the whole file and the message names the file, line and column. You can also import one CSV file of an export on its own (after editing it in a spreadsheet, say), without zipping it again: keep its export name (`warframe.csv`, `farming_goals.csv`, `settings.csv` and so on; a copy suffix such as `warframe (1).csv` is fine) and only that file's data changes.

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

# 4 — make a secret key once and set it (it signs logins and form tokens; keep it private)
python -c "import secrets; print(secrets.token_hex(32))"
$env:SECRET_KEY = '<the printed value>'    # Windows (PowerShell)
# export SECRET_KEY='<the printed value>'  # macOS / Linux

# 5 — start the development server (on this machine only: http://localhost:5000)
python run.py
```

The app does not start without `SECRET_KEY`, with a blank one, or with the example value
older versions used (`dev-change-me-in-production`, published with the source): the message
says how to make a key. Keep the same key across restarts, or everyone is logged out.

`python run.py` (or `python app.py` from inside `app/`, the same thing) is the development
server, not a production one; production runs Gunicorn (see Deployment below). It listens on
`127.0.0.1` only, with Flask's debugger off. Two settings change that:

- `FLASK_RUN_HOST`: another address to listen on, e.g. `0.0.0.0` to reach it from your
  network (only when `SECRET_KEY` is set).
- `FLASK_DEBUG`: `1` or `true` turns on the interactive debugger and the reloader. The
  debugger runs code for whoever reaches it, so with `FLASK_DEBUG` on the server refuses to
  start unless `FLASK_RUN_HOST` is unset, `127.0.0.1`, `localhost` or `::1`.

Without a usable `SECRET_KEY` the development server still starts, for development: it then
makes a random key for that run, listens on `127.0.0.1` only (ignoring `FLASK_RUN_HOST`), and
logs a warning (logins end when it stops).

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

One mandatory environment variable: `SECRET_KEY`. SQLite and built-in login forms are used automatically.

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | — (**required**) | Signs logins and form tokens: a long random value (`python -c "import secrets; print(secrets.token_hex(32))"`); the app does not start without one, with a blank one or with `dev-change-me-in-production` |
| `FLASK_RUN_HOST` | `127.0.0.1` | Development server only (`python run.py`): the address to listen on; ignored without a usable `SECRET_KEY` |
| `FLASK_DEBUG` | off | Development server only: `1` or `true` turns on the debugger and reloader; refused with a host other than `127.0.0.1`, `localhost` or `::1` |
| `DATABASE_PATH` | `warframe_tracker.db` | Absolute path to SQLite file |
| `SESSION_LIFETIME_DAYS` | `30` | Session cookie lifetime |
| `SESSION_COOKIE_SAMESITE` | `Lax` | The session cookie's `SameSite` attribute (`Lax`, `Strict`, or `None`, which needs HTTPS) |
| `MARKET_CACHE_HOURS` | `6` | Market price cache TTL |
| `AUTO_SYNC_ON_STARTUP` | `false` | Run wiki sync at server start |
| `SYNC_INTERVAL_HOURS` | `24` | Background sync interval (0 = off) |
| `RESURGENCE_FALLBACK_DAYS` | `7` | Days the Prime Resurgence stock counts after a sync when the wiki page shows no end date |
| `OIDC_CLIENT_ID` | — | Optional sign-in through an OpenID Connect provider: set all four `OIDC_*` (see below) |
| `OIDC_CLIENT_SECRET` | — | The client secret the provider issued; kept out of the database |
| `OIDC_ISSUER_URL` | — | The provider's issuer URL (`https`) |
| `OIDC_REDIRECT_URI` | — | `https://<your host>/auth/oidc/callback` |
| `BACKUP_LOCAL_PATH` | — | Local backup directory |
| `BACKUP_S3_ENDPOINT` | — | S3-compatible endpoint URL |
| `BACKUP_S3_BUCKET` | — | S3 bucket name |
| `BACKUP_S3_PREFIX` | `warframe-tracker/` | S3 key prefix |
| `BACKUP_S3_ACCESS_KEY` | — | S3 credentials |
| `BACKUP_S3_SECRET_KEY` | — | |

**Sign-in with an OpenID Connect provider** (Keycloak, Authentik, Google, …). Beside the local
login, the app can sign users in through any OpenID Connect provider:

1. Register the app at your provider as a *confidential* client using the authorization code
   flow, with the redirect URI `https://<your host>/auth/oidc/callback` (the exact address users
   reach the app at).
2. Set all four variables: `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_ISSUER_URL` (the
   provider's issuer, for example `https://id.example.com/realms/home`; the app reads
   `<issuer>/.well-known/openid-configuration`) and `OIDC_REDIRECT_URI` (the same redirect URI).
   They are read from the environment only, so the secret never ends up in the database, a
   backup or an export.
3. Restart the app. The login page (and, on an empty database, the setup page) shows **Sign in
   with OpenID Connect**.

Notes:
- With only some of the four set, OIDC sign-in stays off and the startup log names the missing
  variables. With `EASYAUTH_ENABLED=true`, OIDC sign-in is off.
- The provider must be reached over `https`; plain `http` is accepted only for a provider on the
  same machine (`localhost`, `127.0.0.1`, `::1`). The app trusts the ID token because it comes
  straight from the provider's token endpoint over TLS, so `OIDC_ISSUER_URL` must name a provider
  you trust.
- **Who can sign up:** anyone your provider signs in gets a `user` account on their first sign-in,
  as with self-registration. With a public provider (Google, GitHub) that is anyone with an
  account there, so prefer a provider or client limited to your own users. An admin can
  deactivate or delete such accounts.
- On an empty database the first account, local or OIDC, becomes the admin.
- An OIDC account is never linked to another account, not even one with the same name or
  email. On the first sign-in the username is the provider's `preferred_username` (else the
  email); when another account already has that name, the new account gets the first free of
  `name-2`, `name-3`, … (at most 64 characters). When another account already uses the email,
  the new account is created without an email. When an Azure EasyAuth account of an earlier
  setup holds the same provider id, the sign-in is refused. Passwords cannot be reset for OIDC
  accounts.
- Logging out ends the session in this app only; you stay signed in at your provider.
- Pointing `OIDC_ISSUER_URL` at another provider later gives its users new accounts; the old
  ones remain until an admin removes them.

**Return page after login.** After a login (local or OpenID Connect) the app returns to the
page that asked for it only when that is a page of this app; any other address (another site,
`//host`, a backslash, a `javascript:` link, or an encoded form of these) goes to the
dashboard.

**First-run setup.** The setup page creates the admin only while the database has no account
at all. If another setup or a first OpenID Connect sign-in made the first account in the
meantime, the form is refused and you are sent to the login page.

**Forms and cross-site requests.** Every form and every change the pages send carries a
per-session security token, and the server refuses a change without it (protection against
cross-site request forgery). Logging out is a button in the menu that sends it too. A form left
open across a login, a logout or an expired session is refused once with a page saying
"Reload the page and try again"; after a reload it works. A script or tool outside the browser
that posts to the app must first read the token from a page (`<meta name="csrf-token">`) and
send it in the `X-CSRF-Token` header with the same session cookie.

### Azure App Service

Set these three variables; everything else is optional and identical to the private-server table above.

| Variable | Required value | Effect |
|---|---|---|
| `SECRET_KEY` | A long random value (`python -c "import secrets; print(secrets.token_hex(32))"`) | **Required** — the app does not start without one; set it as an app setting (the App Service filesystem is ephemeral) |
| `MSSQL_CONNECTION_STRING` | Full pyodbc connection string | Switches database backend from SQLite to Azure SQL |
| `EASYAUTH_ENABLED` | `true` | Disables login forms; identity is read from EasyAuth HTTP headers |

**EasyAuth setup note**: EasyAuth must also be enabled at the Azure portal level (App Service → Authentication blade) independently of this environment variable. The variable tells the app to trust the injected headers; it does not configure the Azure platform itself.

When `EASYAUTH_ENABLED=true`:
- The app's login, register, and first-run setup pages redirect to `/.auth/login`; the menu's logout entry links to `/.auth/logout`.
- The first user to access the app is auto-provisioned as `admin`; later users as `user`.
- An EasyAuth sign-in only ever matches an account EasyAuth created. When another account
  already has the same name, or a local or OpenID Connect account of an earlier setup has the
  same id, the request is refused (403) and nothing is created; an admin can delete that
  account.
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
