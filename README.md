# Warframe Progress Tracker

A locally-hosted, multi-user companion app for Warframe that answers the question *"What should I focus on this session — and what is the most efficient way to get there?"* at a glance. It tracks your collection and mastery progress, optimises farming routes across multiple target items, surfaces current market prices as a buy-vs-farm alternative, and keeps item data current via the Warframe wiki.

---

## Key Features

- **Dashboard** — **MR rank widget** that calculates your current Mastery Rank from tracked mastered items and shows a progress bar to the next rank (mirroring the in-game UI so mismatches are immediately obvious); immediate-gain items (already owned, not yet mastered); next-best farming targets (shown when no goal is pinned) with their last cached market price and a buy hint; a **Market Watch** panel with the last cached prices of up to ten items on your market watchlist (by name, with a link to the full list). Opening the dashboard never fetches prices: a price older than your market cache duration stays shown, marked *old* with its fetch time, and a small refresh button beside an old or missing price asks warframe.market for that one item and updates it wherever the page shows it.
- **Collection browser** — card-based default view per category (portrait image, status badges, vaulted flag) with a sortable/filterable table view as an alternative; view preference is remembered per category. Inline toggles (mastered, owned, subsumed, Prime Resurgence watch), notes, and wiki links work in both views; each toggle's icon and its screen-reader name follow its state (*Mark as owned* / *Owned (click to unmark)*). Every item on warframe.market (tradable, or listed there, such as prime warframes sold as sets), mods and arcanes included, has a **market watch** toggle (a bookmark icon) that adds it to or removes it from your market watchlist with one click; watched items appear on the Market page and in the dashboard's Market Watch panel. The same items show their **market price**, a line on the card and a *Price* column in the table: the last cached price (or `—` when none is cached yet, or the market had no seller in game), marked *old* with its fetch time once it is older than your market cache duration, and a link to the item on warframe.market. Opening a page never fetches prices; the refresh button beside an old or missing price asks for that one item. Subsuming a Warframe (non-Prime Warframes only) marks it mastered and turns *owned* off, since the Helminth destroys the copy; mark a re-built copy owned again with one click. Un-mastering a subsumed Warframe also clears *subsumed*. A persistent **filter bar** lets you combine filters for ownership (owned/unowned), mastery status (mastered/unmastered), subsume status (Warframes only), source variant (Prime / non-Prime / Kuva / Tenet / …), vault status, and Prime Resurgence watch/availability. A **"Plan it"** button on every item card or table row adds the item directly to the Farming Planner, and a **drop sources** button beside it (the only one on mods and arcanes) shows where the item drops and, for an item built from parts, the relics holding each part and where those relics drop (missions and bounties). Portrait images fall back to a placeholder silhouette when no image is available.
- **Farming Planner** — goal-card system where each target item is shown as a card with portrait image (with placeholder fallback), category, and an overall completion progress bar. Goals can be added, removed, pinned (surfaced on the dashboard), and reordered: the arrow buttons on a card move it one place up or down, pinned goals always staying above the others, and the dashboard lists pinned goals in the same order. Each goal supports two display modes: **Tree View** (full crafting hierarchy with expandable/collapsible nodes and per-ingredient checkboxes) and **Shopping List View** (flat list with numeric quantity inputs and a per-resource progress bar). An **All Goals aggregate** collapses all active goals into a single shopping list, summing shared resources across goals. All checkbox states, quantity inputs, and display-mode preferences persist across sessions. Beside the goal item's price, both modes say whether to buy or farm it: *Buy* when the price is at or below your buy threshold, when the estimated farming time of the parts you still need is over your farming hour cap, or when that time at your platinum per hour is worth more than the price; otherwise *Farm* with the estimated hours. The estimate counts, for each part not yet crafted or held as a blueprint, its quickest source: average runs × typical minutes per run of the mission type (Capture 3; Exterminate and Rescue 4; Spy, Mobile Defense and Assassination 6; rotation missions such as Survival or Defense one 5-minute rotation; any other type 5), or for a relic, one 5-minute Void Fissure run per relic opened (the relic's own farming not counted). A part with no known source makes the time unknown. The dashboard's *Farm next* list uses the same rule over all of an item's parts. Each goal shows its item's market price (asked of warframe.market once when the plan opens, unless the cached one is still fresh), marked *old* with its fetch time when warframe.market could not be asked. A component that comes from void relics (most Prime parts) lists the relics that hold it, each with its Intact drop chance and the missions and bounties where that relic drops (the three needing the fewest runs or bounty stages; a bounty reads as *Bounty — Level 5 - 15 Cetus Bounty — Cetus · Rot. A · Stage 2*); a relic that drops from no mission and no bounty now is marked vaulted.
- **Live Session Advisor** — set a "Currently farming" mission node to surface secondary drop opportunities at that node across all your active goals. Each opportunity shows the drop chance at the current node, the optimal farming node's drop chance, and an efficiency rating (Optimal / Acceptable / Poor). A guaranteed-drop flag highlights when the node's primary drop is near-certain, making even low-probability side drops essentially free. A collapsible harvest summary estimates expected yields per run across all opportunities at the node.
- **Prime Resurgence** — each sync reads Varzia's current stock from the wiki's Prime Resurgence page. Items in it count as available in the collection's Resurgence filter; watched ones get a badge and a highlight, and unowned watched ones appear in the dashboard's "Available Now!" alert. The stock counts until the rotation's end date on the page (18:00 UTC); once that has passed, or before the first successful read, nothing is shown as available and a small notice says the Prime Resurgence data is missing or out of date. In the Farming Planner a vaulted item's components say they can be obtained by trade or through Prime Resurgence, and "in Prime Resurgence now" while it is in the current stock. If the page cannot be read (the wiki unreachable, or its layout changed) the sync still succeeds, keeps the last stock and says so in its result.
- **Market integration** — read-only price data from [warframe.market](https://warframe.market); cached locally with a configurable TTL. Each sync looks every item up in warframe.market's item list (by its name, or its name with " Set" for items traded as sets) to find its listing; items found there get prices and warframe.market links, items not listed have none. If the list cannot be read the sync still succeeds, keeps the listings it found before and says so in its result. The app asks warframe.market's API (version 2) under a client name of its own (`WarframeProgressTracker/<version>`, nothing about you or your instance). A price lookup counts the lowest sell order of a player in game; if warframe.market cannot be asked (no answer, an error, an answer it cannot read), the last price it gave stays as it was and the refresh says the lookup failed. **Old prices are shown, never dropped:** everywhere a price shows (the market page, the dashboard, the Farming Planner, the Collection browser) it is the last cached price of any age, marked *old* with its fetch time once it is older than your market cache duration; `0p` is a price, `—` means none is cached or nobody sells the item in game. A small refresh button (keyboard-reachable) sits beside each old or missing price, none beside a fresh one: it asks warframe.market for that one item only and updates every place that item shows on the page; if the lookup fails, the old price stays, marked *not refreshed*.
- **Wiki sync** — one-click (or scheduled) sync from [wiki.warframe.com](https://wiki.warframe.com) to add new items, update vaulted / Prime Resurgence status, import crafting recipes, populate resource farm data, fetch item portrait images, and look up each item's warframe.market listing — without touching user progress. The sync page shows when the last sync finished (in your local time, how long ago, and whether it succeeded), that none has run yet, or that one started and did not finish. Within an hour after a successful sync, Run Sync Now asks before starting another; after a failed one it runs at once. Only one sync runs at a time: a request while one runs starts nothing, and the page says so.
- **Multi-user** — independent progress records per account, local or OIDC or Azure EasyAuth authentication, role-based access (admin / user). Admins can create, deactivate, and delete accounts, assign roles, and reset passwords for local-auth accounts directly from the admin panel.
- **Preferences** — on the Settings page: market cache duration (hours, default 6), buy threshold (platinum), platinum per hour and farming hour cap (hours; empty switches the hour check off, as 0 does). Each field takes a whole number (0 or more) or stays empty for the default; any other value is refused with a message naming the field, and nothing of the form is saved.
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

# 3 — jQuery 3.7 and Materialize CSS 1.0 are included in app/static/vendor/ (MIT licence);
#     the icons are the app's own drawings: no page loads a font or asset from another server

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

For long-running installations a **systemd service unit** is provided. Scheduled wiki syncs run automatically when running as a service: the app stores when the last sync finished and checks every 15 minutes (first 2 minutes after it starts) whether `SYNC_INTERVAL_HOURS` have passed since then, so a restart neither delays nor repeats the sync.

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
| `SESSION_COOKIE_SAMESITE` | `Lax` | The session cookie's `SameSite` attribute (`Lax`, `Strict`, or `None`, which needs HTTPS), in any letter case; empty means `Lax`; any other value and the app does not start, with a message naming the setting and its three values |
| `MARKET_CACHE_HOURS` | `6` | Market price cache TTL |
| `AUTO_SYNC_ON_STARTUP` | `false` | Run a wiki sync 2 minutes after every server start, whether one is due or not |
| `SYNC_INTERVAL_HOURS` | `24` | Hours after the last finished sync (successful or not) before the scheduled sync runs again; the time is stored, so restarts do not reset it (0 = off) |
| `RESURGENCE_FALLBACK_DAYS` | `7` | Days the Prime Resurgence stock counts after a sync when the wiki page shows no end date |
| `OIDC_CLIENT_ID` | — | Optional sign-in through an OpenID Connect provider: set all four `OIDC_*` (see below) |
| `OIDC_CLIENT_SECRET` | — | The client secret the provider issued; kept out of the database |
| `OIDC_ISSUER_URL` | — | The provider's issuer URL (`https`) |
| `OIDC_REDIRECT_URI` | — | `https://<your host>/auth/oidc/callback` |
| `OIDC_ALLOWED_EMAIL_DOMAINS` | — | Optional: only emails of these domains may sign in through OIDC (comma-separated, for example `example.com, example.org`) |
| `OIDC_ALLOWED_GROUPS` | — | Optional: only members of these provider groups may sign in through OIDC (comma-separated, exact names) |
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
- **Who can sign up:** without the two `OIDC_ALLOWED_*` variables, anyone your provider signs
  in gets a `user` account on their first sign-in, as with self-registration. With a public
  provider (Google, GitHub) that is anyone with an account there. To restrict it, set one or
  both (with both, a user must pass both):
  - `OIDC_ALLOWED_EMAIL_DOMAINS=example.com, example.org`: the email the provider gives must end
    in `@` and one of these domains exactly (case does not matter; `sub.example.com` is not
    `example.com`, list it on its own). The provider must vouch for the email: one it marks as
    not verified is refused; use a provider that only gives addresses it has checked.
  - `OIDC_ALLOWED_GROUPS=tracker-users`: the user must be in one of these groups, spelled
    exactly as the provider sends them (case matters; some send ids or paths such as
    `/tracker-users`). The provider must be set up to put a `groups` claim into the ID token.
  The check runs at every sign-in: tightening a list, or removing someone from the group at the
  provider, refuses their existing account at its next sign-in (the account and its data are
  kept; a session already signed in runs on until it ends). A refused user sees "Your account
  at the identity provider is not allowed to sign in to this app." A variable that is set but
  empty (or only commas and blanks) turns OIDC sign-in off and the startup log names it. An
  admin can also deactivate or delete accounts.
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
dashboard. So does an address longer than 200 characters (counted as written, before any
`%` encoding is undone): it is never shortened, and the sign-in in progress always fits in
the browser's session cookie.

**First-run setup.** The setup page creates the admin only while the database has no account
at all. If another setup or a first OpenID Connect sign-in made the first account in the
meantime, the form is refused and you are sent to the login page.

**Forms and cross-site requests.** Every form and every change the pages send carries a
per-session security token, and the server refuses a change without it (protection against
cross-site request forgery). Logging out is a button in the menu that sends it too. A form left
open across a login, a logout or an expired session is refused once with a page saying
"Reload the page and try again"; after a reload it works, in every tab that reloads. A script or tool outside the browser
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
| Frontend | HTML / CSS / JS · Materialize CSS · jQuery · the app's own SVG icons (every asset served locally, no remote fonts) |
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
