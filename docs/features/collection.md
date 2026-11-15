# Collection browser

The Collection browser lists every item of one category: Warframes, primary, secondary and
melee weapons, companions, archwings, arch-guns, arch-melee, K-Drives / MOAs / Necramechs,
AMPs, mods and arcanes. (Resources are not collected; they appear in the
[Farming Planner](farming-planner.md).)

## Cards and table

A card-based default view per category (portrait image, status badges, vaulted flag), with a
sortable and filterable table view as an alternative; the view you choose is remembered per
category. Portrait images fall back to a placeholder silhouette when no image is available.

## Toggles, notes and links

Inline toggles (mastered, owned, subsumed, Prime Resurgence watch), notes, and wiki links work
in both views; each toggle's icon and its screen-reader name follow its state (*Mark as owned* /
*Owned (click to unmark)*).

Subsuming a Warframe (non-Prime Warframes only) marks it mastered and turns *owned* off, since
the Helminth destroys the copy; mark a re-built copy owned again with one click. Un-mastering a
subsumed Warframe also clears *subsumed*.

An arcane's card has a **rank bar**: your owned rank against your target rank, or against the
arcane's max rank while no target is set; hover it to read which ("Rank 2 of target 4").

## Filters

A persistent **filter bar** lets you combine filters for ownership (owned/unowned), mastery
status (mastered/unmastered), subsume status (Warframes only), source variant (Prime / non-Prime
/ Kuva / Tenet / …), vault status, and Prime Resurgence watch/availability.

## Planning and drop sources

An **Add to Farming Planner** button (*Plan it*) on every item card or table row, mods and
arcanes aside, adds the item directly to the [Farming Planner](farming-planner.md), and a
**Drop sources** button beside it (on mods and arcanes the only one, named *Farm it*) shows
where the item drops and, for an item built from parts, the relics
holding each part and where those relics drop (missions and bounties).

## Market watch and prices

Every item on warframe.market (tradable, or listed there, such as prime warframes sold as
sets), mods and arcanes included, has a **market watch** toggle (a bookmark icon) that adds it
to or removes it from your market watchlist with one click; watched items appear on the
[Market](market.md) page and in the dashboard's Market Watch panel.

The same items show their **market price**, on the right of the card's name (so a card on the
market is no taller than one off it) and in a *Price* column in the table: the last cached price
(or `—` when none is cached yet, or the market had no seller in game), marked *old* with its
fetch time once it is older than your market cache duration, and a link to the item on
warframe.market. Opening a page never fetches prices; the refresh button beside an old or
missing price asks for that one item.
