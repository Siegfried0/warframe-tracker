"""
The live session advisor: the nodes a user can farm, and the drops at one node that the
user's farming goals still need, each rated against the best node for it.
"""

from models.db import get_db
from services.farming_progress import _comp_state_for, _get_progress_map
from services.relic_sync import is_relic_name


# ── Live Session Advisor ─────────────────────────────────────────────────────

def get_all_nodes() -> list[str]:
    """Return sorted distinct node locations from drop_sources (a relic is no node)."""
    rows = get_db().execute(
        '''SELECT DISTINCT location FROM drop_sources
            WHERE location IS NOT NULL AND (source_type IS NULL OR source_type <> 'relic')
            ORDER BY location'''
    ).fetchall()
    return [r['location'] for r in rows if r['location'] and not is_relic_name(r['location'])]


# drop_sources.drop_chance is a percentage: a node whose highest drop chance is at least
# this many percent counts as a guaranteed drop, and its opportunities as essentially free.
GUARANTEED_DROP_PERCENT = 95.0


def get_advisor_opportunities(user_id: int, node_location: str) -> dict:
    """Cross-reference user's still-needed items against drops at the selected node.
    A relic is no node: its name finds no opportunities."""
    db = get_db()

    node_drops = db.execute(
        '''SELECT ds.item_id, ds.drop_chance, ds.source_type,
                  ds.notes AS drop_notes, i.name AS item_name
             FROM drop_sources ds
             JOIN items i ON ds.item_id = i.id
            WHERE ds.location = ?
              AND (ds.source_type IS NULL OR ds.source_type <> 'relic')
            ORDER BY ds.drop_chance DESC''',
        (node_location,),
    ).fetchall()
    if is_relic_name(node_location):
        node_drops = []

    if not node_drops:
        return {
            'opportunities': [], 'harvest_summary': [],
            'guaranteed_free': False, 'node': node_location,
        }

    max_chance = node_drops[0]['drop_chance'] or 0
    guaranteed_free = bool(max_chance >= GUARANTEED_DROP_PERCENT)

    # Build needed sets from active goals
    goals = db.execute(
        'SELECT id AS goal_id, item_id FROM farming_goals WHERE user_id = ?',
        (user_id,),
    ).fetchall()

    needed_resources: dict[int, list] = {}     # resource item_id → [goal item names]
    needed_components: dict[tuple, list] = {}  # (parent_item_id, comp_name) → [goal item names]

    for goal in goals:
        prog_map = _get_progress_map(goal['goal_id'])
        gname_row = db.execute('SELECT name FROM items WHERE id = ?', (goal['item_id'],)).fetchone()
        goal_name = gname_row['name'] if gname_row else '?'

        ings = db.execute(
            'SELECT DISTINCT component_name, ingredient_name FROM component_ingredients WHERE item_id = ?',
            (goal['item_id'],),
        ).fetchall()

        for r in ings:
            comp_done = _comp_state_for(prog_map, r['component_name']) == 'crafted'
            if comp_done:
                continue

            # Track needed component blueprint
            key = (goal['item_id'], r['component_name'])
            if key not in needed_components:
                needed_components[key] = []
            if goal_name not in needed_components[key]:
                needed_components[key].append(goal_name)

            # Track needed resource
            res_row = db.execute(
                "SELECT id FROM items WHERE name = ? AND category = 'resource'",
                (r['ingredient_name'],),
            ).fetchone()
            if res_row:
                qty_needed_row = db.execute(
                    'SELECT SUM(ingredient_qty) AS total FROM component_ingredients WHERE item_id = ? AND ingredient_name = ?',
                    (goal['item_id'], r['ingredient_name']),
                ).fetchone()
                qty_needed = (qty_needed_row['total'] or 0) if qty_needed_row else 0
                qty_owned = prog_map.get(f'ingredient:{r["ingredient_name"]}', {}).get('qty', 0)
                if qty_owned < qty_needed:
                    rid = res_row['id']
                    if rid not in needed_resources:
                        needed_resources[rid] = []
                    if goal_name not in needed_resources[rid]:
                        needed_resources[rid].append(goal_name)

    # Best drop chance per (item, source_type, notes) for efficiency rating
    best_rows = db.execute(
        '''SELECT item_id, source_type, notes, MAX(drop_chance) AS best FROM drop_sources
            WHERE source_type IS NULL OR source_type <> 'relic'
            GROUP BY item_id, source_type, notes'''
    ).fetchall()
    best_chances: dict[str, float] = {}
    for r in best_rows:
        k = f"{r['item_id']}:{r['source_type']}:{r['notes'] or ''}"
        best_chances[k] = r['best'] or 0

    opportunities = []
    seen_opp_keys: set = set()
    harvest_summary = []
    seen_harvest_keys: set = set()

    for drop in node_drops:
        item_id = drop['item_id']
        source_type = drop['source_type']
        drop_notes = drop['drop_notes'] or ''
        drop_chance = drop['drop_chance'] or 0

        # Build harvest summary for ALL drops at this node
        harvest_key = f"{item_id}:{drop_notes}"
        if harvest_key not in seen_harvest_keys:
            seen_harvest_keys.add(harvest_key)
            display = drop['item_name']
            if drop_notes and source_type == 'component':
                display = f"{drop['item_name']} — {drop_notes}"
            harvest_summary.append({
                'name':              display,
                'drop_chance':       drop_chance,
                'drop_pct':          f'{drop_chance:.2f}%' if drop_chance else '?%',
                'expected_per_10':   round(drop_chance / 100 * 10, 2) if drop_chance else 0,
            })

        # Match drop to needed items
        goal_labels = []
        if source_type == 'resource':
            goal_labels = needed_resources.get(item_id, [])
        elif source_type == 'component':
            goal_labels = needed_components.get((item_id, drop_notes), [])
        elif source_type == 'mission':
            # Try to match by item_id directly (mission drops are associated with goal items)
            for (pid, cname), glabels in needed_components.items():
                if pid == item_id:
                    # Check if drop_notes contains this component name
                    if cname in drop_notes or not drop_notes:
                        goal_labels = glabels
                        break

        if not goal_labels:
            continue

        opp_key = f"{item_id}:{drop_notes}"
        if opp_key in seen_opp_keys:
            continue
        seen_opp_keys.add(opp_key)

        best_key = f"{item_id}:{source_type}:{drop_notes}"
        best_chance = best_chances.get(best_key, drop_chance) or drop_chance
        efficiency_ratio = (drop_chance / best_chance * 100) if best_chance > 0 else 0

        if efficiency_ratio >= 80:
            rating, rating_class = 'Optimal', 'teal-text'
        elif efficiency_ratio >= 40:
            rating, rating_class = 'Acceptable', 'orange-text'
        else:
            rating, rating_class = 'Poor', 'red-text'

        display = drop['item_name']
        if drop_notes and source_type == 'component':
            display = f"{drop['item_name']} — {drop_notes}"

        opportunities.append({
            'name':              display,
            'drop_pct':         f'{drop_chance:.2f}%' if drop_chance else '?%',
            'best_pct':         f'{best_chance:.2f}%' if best_chance else '?%',
            'efficiency_pct':   round(efficiency_ratio, 1),
            'rating':           rating,
            'rating_class':     rating_class,
            'goal_labels':      goal_labels,
            'essentially_free': guaranteed_free,
        })

    return {
        'opportunities':   opportunities,
        'harvest_summary': harvest_summary,
        'guaranteed_free': guaranteed_free,
        'node':            node_location,
    }
