# Settings and preferences

The Settings page holds your personal preferences, your password, your data's export and
import, and the danger zone; admins also see the backup section.

## Preferences

- **Market cache duration** (hours, default 6): how long a cached [market](market.md) price
  counts as fresh; an older one is still shown, marked *old*.
- **Buy threshold** (platinum): an item at or below this price is worth buying.
- **Platinum per hour**: what an hour of your farming is worth, for the buy-or-farm answer.
- **Farming hour cap** (hours): farming longer than this answers *Buy*; empty switches the hour
  check off, as 0 does.

Each field takes a whole number (0 or more) or stays empty for the default; any other value is
refused with a message naming the field, and nothing of the form is saved. How the last three
decide between buying and farming is on the
[Farming Planner](farming-planner.md#buy-or-farm) page.

## Change Password

A local account changes its password here (current password, then the new one twice, at least
8 characters). Accounts that sign in through OpenID Connect or Azure EasyAuth have no password
here.

## Data Export & Import

Download your data as a ZIP of CSV files, or import one; see
[Export and import](export-import.md).

## Danger Zone

- **Clear Progress** (type `CLEAR` to confirm) removes your collection progress (owned,
  mastered, subsumed, Prime Resurgence watch, notes, arcane ranks); your account, farming goals,
  market watchlist and preferences are kept.
- **Delete account** (type `DELETE` to confirm) permanently deletes your account and all its
  data, and signs you out.

## Backup (admins only)

The backup targets, their retention and **Back up now**; see
[Backup and restore](../backup.md).

## Account Info

Your username, role, sign-in type and last login.
