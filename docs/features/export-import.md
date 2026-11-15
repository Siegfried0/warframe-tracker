# Export and import

On the [Settings](settings.md) page, under **Data Export & Import**.

## Export

**Export ZIP** downloads all your data as a ZIP of CSV files you can open in a spreadsheet:
per-category progress (owned, mastered, subsumed, resurgence watch, notes, arcane ranks),
farming goals with their component and resource progress (every component's state: pending,
blueprint or crafted), the farming checklist, the market watchlist and your personal settings.

A cell that starts with `=`, `+`, `-`, `@`, a tab, a carriage return or `'` is written with a
leading `'` so that a spreadsheet shows it as text instead of running it as a formula; importing
the ZIP removes that `'` again, so your notes and names come back exactly as they were.

## Import

Importing a ZIP (older exports too) checks every cell first, then merges it into your data in
one step: rows in the file overwrite yours for the same item, new items are added, everything
else is kept; one invalid cell refuses the whole file and the message names the file, line and
column.

You can also import one CSV file of an export on its own (after editing it in a spreadsheet,
say), without zipping it again: keep its export name (`warframe.csv`, `farming_goals.csv`,
`settings.csv` and so on; a copy suffix such as `warframe (1).csv` is fine) and only that
file's data changes.

An export moves one user's data. To keep the whole database (every account and the catalogue),
use [Backup and restore](../backup.md).
