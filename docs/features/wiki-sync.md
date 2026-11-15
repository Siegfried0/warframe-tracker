# Wiki sync

The wiki sync keeps the app's item catalogue current, with one click on the sync page (admins
only) or on a schedule. It adds new items, updates vaulted and Prime Resurgence status, imports
crafting recipes, populates resource farm data, fetches item portrait images, and looks up each
item's warframe.market listing — without touching user progress: it writes the catalogue only,
never anyone's progress, goals, watchlist or settings.

## What it reads

- The WFCD `warframe-items` data set: items, their components and recipes.
- The Warframe wiki ([wiki.warframe.com](https://wiki.warframe.com)) through its MediaWiki API:
  category lists, blueprint recipes, pictures for items the data set has none for, and the
  [Prime Resurgence](prime-resurgence.md) page.
- The warframestat.us mission reward tables (drop sources: missions, bounties, relics) and its
  image CDN (item portraits).
- warframe.market's item list: each item's and part's [market listing](market.md#listings).

The sync page lists what gets synced: Warframes, primary, secondary and melee weapons,
arch-guns and arch-melee, kitgun chambers, companions, archwings, K-Drives / MOAs / Necramechs,
amps, mods, arcanes, resources, component ingredients, drop sources, item images, Prime
Resurgence's current stock and market listings.

Item images are downloaded once and served from the app's own cache; images already cached are
not fetched again. The sync page's **Image Coverage** shows how many items have a cached image;
run the sync again to fetch the missing ones.

## Running a sync

The sync page shows when the last sync finished (in your local time, how long ago, and whether
it succeeded), that none has run yet, or that one started and did not finish.

- Within an hour after a successful sync, **Run Sync Now** asks before starting another; after a
  failed one it runs at once.
- Only one sync runs at a time: a request while one runs starts nothing, and the page says so.

## Scheduled syncs

While the app runs it stores when the last sync finished and checks every 15 minutes (first 2
minutes after it starts) whether `SYNC_INTERVAL_HOURS` (default 24; 0 switches the schedule off)
have passed since then, so a restart neither delays nor repeats the sync. A sync started from
the page counts too. `AUTO_SYNC_ON_STARTUP=true` also runs one sync 2 minutes after every start.
See [Configuration](../configuration.md#wiki-sync).
