# Warframe Progress Tracker — documentation

The [project README](../README.md) says what the app is and gives the quick start. These pages
say how to run it and what each part of it does.

## How-to

- [Install and run](install.md) — the quick start, the secret key, the development server and
  its two settings, the first-run setup, running under Gunicorn, running the tests.
- [Configuration](configuration.md) — every environment variable the app reads, with its
  default and meaning.
- [Sign-in and accounts](sign-in.md) — local accounts and roles, OpenID Connect, Azure
  EasyAuth, the return page after login, first-run setup, forms and the security token.
- [Backup and restore](backup.md) — backup targets, retention, Back up now, MSSQL, and how to
  restore a backup by hand.
- [Deploy to Azure App Service](azure.md) — the three settings, EasyAuth and the zip deploy.

## Features

- [Dashboard](features/dashboard.md) — Session Focus: Mastery Rank, Prime Resurgence alerts,
  what to master and farm next, and your watched prices.
- [Collection browser](features/collection.md) — every item by category, its toggles, filters,
  drop sources and market price.
- [Farming Planner](features/farming-planner.md) — goals, the tree and shopping-list views, buy
  or farm, relics and vaulted parts.
- [Live Session Advisor](features/live-session-advisor.md) — what else your goals need from the
  mission node you are farming.
- [Prime Resurgence](features/prime-resurgence.md) — Varzia's current stock, the watch toggle
  and the alert.
- [Market](features/market.md) — warframe.market prices, the watchlist, listings and refreshing
  an old price.
- [Wiki sync](features/wiki-sync.md) — what the catalogue sync reads, running it and its
  schedule.
- [Settings and preferences](features/settings.md) — your preferences, password, danger zone
  and, for admins, backups.
- [Export and import](features/export-import.md) — your data as a ZIP of CSV files, and
  bringing it back.
- [Accounts and administration](features/accounts.md) — roles and the admin panel.
