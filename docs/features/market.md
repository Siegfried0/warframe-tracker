# Market

Market integration is read-only price data from [warframe.market](https://warframe.market),
cached locally for as long as your market cache duration (one of your
[preferences](settings.md), default 6 hours). The app never trades or posts anything there.

## The Market page

The **Market Watch** page lists the items on your market watchlist with their prices. Add or
remove an item with the bookmark toggle in the [Collection browser](collection.md); the
[dashboard](dashboard.md)'s Market Watch panel shows up to ten of them.

## Listings

Each [wiki sync](wiki-sync.md) looks every item up in warframe.market's item list (by its name,
or its name with " Set" for items traded as sets) to find its listing; items found there get
prices and warframe.market links, items not listed have none. The same list gives each part of
an item its listing, when warframe.market names it "<item> <part>" or "<item> <part> Blueprint"
(Ash Prime Neuroptics, Ash Prime Neuroptics Blueprint); the sync result counts the items and the
parts it found. If the list cannot be read the sync still succeeds, keeps the listings it found
before and says so in its result.

## Prices

The app asks warframe.market's API (version 2) under a client name of its own
(`WarframeProgressTracker/<version>`, nothing about you or your instance). A price lookup counts
the lowest sell order of a player in game; if warframe.market cannot be asked (no answer, an
error, an answer it cannot read), the last price it gave stays as it was and the refresh says
the lookup failed.

**Old prices are shown, never dropped:** everywhere a price shows (the market page, the
dashboard, the Farming Planner, the Collection browser) it is the last cached price of any age,
marked *old* with its fetch time once it is older than your market cache duration; `0p` is a
price, `—` means none is cached or nobody sells the item in game.

A small refresh button (keyboard-reachable) sits beside each old or missing price, none beside a
fresh one: it asks warframe.market for that one item only and updates every place that item
shows on the page; if the lookup fails, the old price stays, marked *not refreshed*.

How prices feed the buy-or-farm answers is on the [Farming Planner](farming-planner.md#buy-or-farm)
page.
