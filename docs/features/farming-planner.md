# Farming Planner

The Farming Planner is a goal-card system: each target item is shown as a card with its portrait
image (with a placeholder fallback), category, and an overall completion progress bar.

## Goals

Goals can be added (by searching, or with the **Add to Farming Planner** button in the
[Collection browser](collection.md)), removed, pinned (surfaced on the
[dashboard](dashboard.md)), and reordered: the arrow buttons on a card move it one place up or
down, pinned goals always staying above the others, and the dashboard lists pinned goals in the
same order.

## Two display modes

Each goal supports two display modes:

- **Tree View** — the full crafting hierarchy with expandable/collapsible nodes and
  per-ingredient checkboxes.
- **Shopping List View** — a flat list with numeric quantity inputs and a per-resource progress
  bar.

An **All Goals aggregate** collapses all active goals into a single shopping list, summing shared
resources across goals. All checkbox states, quantity inputs, and display-mode preferences
persist across sessions.

## Buy or farm

Beside the goal item's price, both modes say whether to buy or farm it: *Buy* when the price is
at or below your buy threshold, when a part you still need comes only from vaulted relics (no
mission drops it, so buying is the only way; this holds whatever your settings, and needs the
drop data of a sync), when the estimated farming time of the parts you still need is over your
farming hour cap, or when that time at your platinum per hour is worth more than the price;
otherwise *Farm* with the estimated hours. The thresholds are your
[preferences](settings.md).

The estimate counts, for each part not yet crafted or held as a blueprint, its quickest source:
average runs × typical minutes per run of the mission type (Capture 3; Exterminate and Rescue 4;
Spy, Mobile Defense and Assassination 6; rotation missions such as Survival or Defense one
5-minute rotation; any other type 5), or for a relic, one 5-minute Void Fissure run per relic
opened (the relic's own farming not counted); a relic that drops from no mission is no source.
A part with no known source makes the time unknown. The dashboard's *Farm next* list uses the
same rule over all of an item's parts.

## Prices

Each goal shows its item's market price (asked of warframe.market once when the plan opens,
unless the cached one is still fresh), marked *old* with its fetch time when warframe.market
could not be asked.

Each part with a warframe.market listing shows its own price and its own buy-or-farm answer
(from that price and the part's quickest source), on its row in the tree view and under its
circle in the visual plan: opening a plan asks warframe.market for no part price, it shows the
last one fetched (marked *old*, or `—` when none), and the refresh button beside an old or
missing part price asks for that one part. After a refresh, of the goal's price or of a part's,
the buy-or-farm answers beside the prices follow the new price at once, without reopening the
plan, and warframe.market is asked only for the price you clicked.

## Relics and vaulted parts

A component that comes from void relics (most Prime parts) lists the relics that hold it, each
with its Intact drop chance and the missions and bounties where that relic drops (the three
needing the fewest runs or bounty stages; a bounty reads as *Bounty — Level 5 - 15 Cetus Bounty
— Cetus · Rot. A · Stage 2*); a relic that drops from no mission and no bounty now is marked
vaulted.

A vaulted item's components say they can be obtained by trade or through Prime Resurgence, and
"in Prime Resurgence now" while the item is in the current stock (see
[Prime Resurgence](prime-resurgence.md)).

The [Live Session Advisor](live-session-advisor.md) sits on the same page.
