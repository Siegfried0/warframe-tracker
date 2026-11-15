# Dashboard

The dashboard (*Session Focus*) is the first page after sign-in. It answers what to work on
this session.

- **Mastery Rank** — the widget calculates your current Mastery Rank from your tracked mastered
  items and shows a progress bar to the next rank, mirroring the in-game UI so that mismatches
  are immediately obvious.
- **Prime Resurgence — Available Now!** — your unowned watched items that are in Varzia's
  current stock (see [Prime Resurgence](prime-resurgence.md)); when the stock data is missing or
  out of date, a small notice says so instead.
- **Immediate Gains** — items you already own but have not mastered yet.
- **Farm Next** — your pinned farming goals (pin them in the
  [Farming Planner](farming-planner.md)), in the planner's order, each with its progress. When
  no goal is pinned, the list shows the ten unowned, unmastered items quickest to farm, ranked by
  their estimated farming time (all their parts, the estimate the Farming Planner uses) in a
  *Farming time* column (`~2.0h`; `—` when the time is unknown, for instance when a part comes
  only from vaulted relics, such items listed last), each with its last cached market price and
  a buy hint.
- **Market Watch** — the last cached prices of up to ten items on your market watchlist (by
  name, with a link to the full list on the [Market](market.md) page).

## Prices on the dashboard

Opening the dashboard never fetches prices: a price older than your market cache duration
stays shown, marked *old* with its fetch time, and a small refresh button beside an old or
missing price asks warframe.market for that one item and updates it wherever the page shows
it; the farming target's buy hint and the price's colour then follow the new price at once,
without reloading the page. The market cache duration is one of your
[preferences](settings.md).
