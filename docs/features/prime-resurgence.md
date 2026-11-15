# Prime Resurgence

Each [wiki sync](wiki-sync.md) reads Varzia's current stock from the wiki's Prime Resurgence
page.

- Items in it count as available in the [Collection browser](collection.md)'s Resurgence
  filter; watched ones (the Prime Resurgence watch toggle) get a badge and a highlight, and
  unowned watched ones appear in the [dashboard](dashboard.md)'s "Available Now!" alert.
- The stock counts until the rotation's end date on the page (18:00 UTC). Once that has passed,
  or before the first successful read, nothing is shown as available and a small notice says
  the Prime Resurgence data is missing or out of date. When the page shows no end date, the
  stock counts for `RESURGENCE_FALLBACK_DAYS` days (default 7) after the sync that read it (see
  [Configuration](../configuration.md#wiki-sync)).
- In the [Farming Planner](farming-planner.md) a vaulted item's components say they can be
  obtained by trade or through Prime Resurgence, and "in Prime Resurgence now" while it is in
  the current stock.
- If the page cannot be read (the wiki unreachable, or its layout changed) the sync still
  succeeds, keeps the last stock and says so in its result.
