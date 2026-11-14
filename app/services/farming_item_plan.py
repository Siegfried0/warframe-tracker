"""
The item plan: one item's crafting plan, with its components' drop locations, the relics
holding them, their ingredients and the user's checklist state.

It imports no farming module, so the farming facade and the goal plan both import it.
"""

from collections import defaultdict

from models.db import get_db
from services.relic_sources import is_relic_source, relics_by_part


def get_item_plan(item_id: int, user_id: int) -> dict:
    """Return full crafting plan for one item: components, their ingredients,
    drop locations, and the user's current checklist state.

    Returns:
      {
        item_id, item_name,
        components: [
          {
            component_name,
            acquired,            # bool — user ticked this off
            drops: [...],        # best drop locations for this component
            relics: [            # relics holding it, those dropping somewhere first
              {part, relic, drop_chance, vaulted, locations: [...]}
            ],
            ingredients: [
              {ingredient_name, ingredient_qty, drops: [...]}
            ]
          }
        ]
      }
    """
    db = get_db()

    item_row = db.execute('SELECT name, image_url FROM items WHERE id = ?', (item_id,)).fetchone()
    if not item_row:
        return {}

    # All component drop entries for this item, grouped by component name. A relic is no
    # place to run: the relics holding a part are listed apart, below.
    drop_rows = db.execute(
        '''SELECT notes AS component_name, mission_type, location,
                  drop_chance, avg_runs, source_type
             FROM drop_sources
            WHERE item_id = ? AND source_type = 'component'
            ORDER BY notes, avg_runs''',
        (item_id,),
    ).fetchall()
    drop_rows = [r for r in drop_rows if not is_relic_source(r['source_type'], r['location'])]

    # Build component → drops map (keep best 3 per component)
    comp_drops: dict[str, list] = defaultdict(list)
    for r in drop_rows:
        cname = r['component_name'] or ''
        if len(comp_drops[cname]) < 3:
            comp_drops[cname].append({
                'mission_type': r['mission_type'],
                'location':     r['location'],
                'drop_chance':  r['drop_chance'],
                'avg_runs':     r['avg_runs'],
            })

    # Crafting ingredients for this item's components
    ing_rows = db.execute(
        '''SELECT component_name, ingredient_name, ingredient_qty
             FROM component_ingredients
            WHERE item_id = ?
            ORDER BY component_name, ingredient_name''',
        (item_id,),
    ).fetchall()

    # For each ingredient, look up its best drop locations
    def _ingredient_drops(ing_name: str) -> list[dict]:
        res_row = db.execute(
            "SELECT id FROM items WHERE name = ? AND category = 'resource'",
            (ing_name,),
        ).fetchone()
        if not res_row:
            return []
        rows = db.execute(
            '''SELECT mission_type, location, drop_chance, avg_runs
                 FROM drop_sources WHERE item_id = ?
                ORDER BY avg_runs LIMIT 3''',
            (res_row['id'],),
        ).fetchall()
        return [dict(r) for r in rows]

    # Checklist state for this item (component_name → acquired bool)
    cl_rows = db.execute(
        '''SELECT component_name, acquired FROM farming_checklist
            WHERE user_id = ? AND item_id = ?''',
        (user_id, item_id),
    ).fetchall()
    checklist = {r['component_name']: bool(r['acquired']) for r in cl_rows}

    # Group ingredients by component
    comp_ingredients: dict[str, list] = defaultdict(list)
    for r in ing_rows:
        comp_ingredients[r['component_name']].append({
            'ingredient_name': r['ingredient_name'],
            'ingredient_qty':  r['ingredient_qty'],
            'drops':           _ingredient_drops(r['ingredient_name']),
        })

    # Relics holding each part, with each relic's best locations
    comp_relics = relics_by_part(item_id, db)

    # Assemble component list — union of those with drops, relics or ingredients
    all_comp_names = sorted(set(comp_drops) | set(comp_relics) | set(comp_ingredients))
    # Remove empty-string key that can appear if notes was NULL
    all_comp_names = [c for c in all_comp_names if c]

    components = []
    for cname in all_comp_names:
        components.append({
            'component_name': cname,
            'acquired':       checklist.get(cname, False),
            'drops':          comp_drops.get(cname, []),
            'relics':         comp_relics.get(cname, []),
            'ingredients':    comp_ingredients.get(cname, []),
        })

    return {
        'item_id':    item_id,
        'item_name':  item_row['name'],
        'image_url':  item_row['image_url'],
        'components': components,
    }
