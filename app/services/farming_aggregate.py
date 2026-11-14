"""
The aggregate shopping list: the ingredients every farming goal of a user still needs, summed
across the goals, with what the user holds and where each drops.
"""

from models.db import get_db
from services.farming_progress import (_comp_state_for, _get_progress_map,
                                       _ingredient_drops_for)


# ── Aggregate Shopping List (all goals) ─────────────────────────────────────

def get_aggregate_shopping_list(user_id: int) -> dict:
    """Collapse all active goals into a single summed resource list."""
    db = get_db()
    goals = db.execute(
        'SELECT id AS goal_id, item_id FROM farming_goals WHERE user_id = ?',
        (user_id,),
    ).fetchall()
    if not goals:
        return {'resources': [], 'readiness_pct': 0}

    agg_totals: dict[str, int] = {}
    agg_owned: dict[str, int] = {}
    agg_goal_labels: dict[str, list] = {}

    for goal in goals:
        prog_map = _get_progress_map(goal['goal_id'])
        item_name_row = db.execute('SELECT name FROM items WHERE id = ?', (goal['item_id'],)).fetchone()
        goal_item_name = item_name_row['name'] if item_name_row else '?'

        ings = db.execute(
            'SELECT component_name, ingredient_name, ingredient_qty FROM component_ingredients WHERE item_id = ?',
            (goal['item_id'],),
        ).fetchall()

        for r in ings:
            if _comp_state_for(prog_map, r['component_name']) == 'crafted':
                continue
            iname = r['ingredient_name']
            agg_totals[iname] = agg_totals.get(iname, 0) + r['ingredient_qty']
            qty_owned = prog_map.get(f'ingredient:{iname}', {}).get('qty', 0)
            agg_owned[iname] = agg_owned.get(iname, 0) + qty_owned
            if iname not in agg_goal_labels:
                agg_goal_labels[iname] = []
            if goal_item_name not in agg_goal_labels[iname]:
                agg_goal_labels[iname].append(goal_item_name)

    resources = []
    total_needed = 0
    total_covered = 0
    for iname in sorted(agg_totals):
        qty_needed = agg_totals[iname]
        qty_owned = min(agg_owned.get(iname, 0), qty_needed)
        drops = _ingredient_drops_for(db, iname)
        resources.append({
            'ingredient_name': iname,
            'qty_needed':      qty_needed,
            'qty_owned':       qty_owned,
            'still_needed':    max(0, qty_needed - qty_owned),
            'drops':           drops,
            'shared':          len(agg_goal_labels.get(iname, [])) > 1,
            'goal_labels':     agg_goal_labels.get(iname, []),
        })
        total_needed += qty_needed
        total_covered += qty_owned

    readiness_pct = int(total_covered / total_needed * 100) if total_needed else 100
    return {'resources': resources, 'readiness_pct': readiness_pct}
