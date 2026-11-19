# Settings and preferences

The Settings page holds your personal preferences, your password, your data's export and
import, and the danger zone; admins also see the sync and session settings and the backup
section.

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

## Sync & Sessions (admins only)

App-wide settings for every user:

- **Sync on startup** (default off): run a [wiki sync](wiki-sync.md) 2 minutes after every
  start of the app. Applies from the next start.
- **Scheduled sync interval** (hours, 0 to 8760, default 24; 0 switches the schedule off): how
  long after the last finished sync the next one runs. Applies from the next scheduled check,
  within 15 minutes. When a save switches the schedule on, this server process checks shortly
  (about 2 minutes later); other worker processes of a multi-process server start their check
  at their next start, and the page says so.
- **Session lifetime** (days, 1 to 365, default 30): a session unused for longer ends, and
  every sign-in ends three lifetimes after it was made. Applies within a minute, to every
  session.

Each shows the value that applies and where it comes from: saved here, the default, or set by
its environment variable. A setting whose variable is set is shown read-only with the variable
to remove; it cannot be saved here (see [Configuration](../configuration.md)). A saved value
that is not valid is shown as the default it counts as, with a note, until you save.

One **Save** stores every field together, or nothing: a number must be whole digits within its
range, and a refused save names every field it refused. To go back to a default, tick its **Use
the default** box (shown for a saved value) and empty the field, or untick the switch; the
saved value is removed. A value sent together with **Use the default** is refused.

## Backup (admins only)

The backup targets, their retention and **Back up now**; see
[Backup and restore](../backup.md).

## Account Info

Your username, role, sign-in type and last login.
