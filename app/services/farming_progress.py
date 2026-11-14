"""
Goal progress: the progress rows of a farming goal and what is computed from them.

The goal list, the goal plan, the aggregate shopping list and the session advisor all read
them. This module imports no other farming module, so any of them can import it.
"""

from models.db import get_db


# ── Goal Progress Tracking ───────────────────────────────────────────────────

def set_goal_progress(user_id: int, goal_id: int, row_type: str,
                      row_key: str, comp_state: str | None = None,
                      acquired: bool = False, quantity_owned: int = 0):
    """Upsert a progress row for a goal (component state or ingredient qty).

    For component rows, comp_state takes precedence over acquired:
      'pending'   → not started (stored as NULL)
      'blueprint' → blueprint obtained, resources still needed
      'crafted'   → done; resources excluded from totals
    For ingredient rows, only quantity_owned matters.

    Writes only into a goal the user owns: returns False and writes nothing for a goal
    id that is another user's or no goal at all; True after the write.
    """
    db = get_db()

    owned = db.execute(
        'SELECT 1 FROM farming_goals WHERE id = ? AND user_id = ?', (goal_id, user_id),
    ).fetchone()
    if owned is None:
        return False

    if row_type == 'component':
        # Normalise comp_state; derive from legacy acquired flag if needed
        if comp_state not in ('pending', 'blueprint', 'crafted'):
            comp_state = 'crafted' if acquired else 'pending'
        acquired_int  = 1 if comp_state == 'crafted' else 0
        stored_state  = None if comp_state == 'pending' else comp_state
    else:
        acquired_int = int(acquired)
        stored_state = None

    try:
        db.execute(
            '''INSERT INTO farming_progress
                   (user_id, goal_id, row_type, row_key, acquired, quantity_owned, comp_state)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(goal_id, row_type, row_key) DO UPDATE SET
                   acquired       = excluded.acquired,
                   quantity_owned = excluded.quantity_owned,
                   comp_state     = excluded.comp_state,
                   updated_at     = datetime('now')''',
            (user_id, goal_id, row_type, row_key, acquired_int, int(quantity_owned), stored_state),
        )
    except Exception:
        # comp_state column not yet present (migration pending on cold start race)
        db.execute(
            '''INSERT INTO farming_progress
                   (user_id, goal_id, row_type, row_key, acquired, quantity_owned)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(goal_id, row_type, row_key) DO UPDATE SET
                   acquired       = excluded.acquired,
                   quantity_owned = excluded.quantity_owned,
                   updated_at     = datetime('now')''',
            (user_id, goal_id, row_type, row_key, acquired_int, int(quantity_owned)),
        )
    db.commit()
    return True


def _get_progress_map(goal_id: int) -> dict:
    """Return {row_type:row_key → {acquired, qty, comp_state}} for a goal.

    comp_state is normalised: None (pending) | 'blueprint' | 'crafted'.
    Pre-v6 rows (comp_state column absent or NULL with acquired=1) are back-filled
    so callers can always rely on comp_state without checking acquired.
    """
    try:
        rows = get_db().execute(
            'SELECT row_type, row_key, acquired, quantity_owned, comp_state '
            'FROM farming_progress WHERE goal_id = ?',
            (goal_id,),
        ).fetchall()
    except Exception:
        # comp_state column not yet migrated — fall back to 4-column query
        rows = get_db().execute(
            'SELECT row_type, row_key, acquired, quantity_owned FROM farming_progress WHERE goal_id = ?',
            (goal_id,),
        ).fetchall()
        return {
            f"{r['row_type']}:{r['row_key']}": {
                'acquired':   bool(r['acquired']),
                'qty':        r['quantity_owned'],
                'comp_state': 'crafted' if bool(r['acquired']) else None,
            }
            for r in rows
        }

    result = {}
    for r in rows:
        cs = r['comp_state']
        # Back-fill comp_state for rows written before v6 migration
        if cs is None and bool(r['acquired']):
            cs = 'crafted'
        result[f"{r['row_type']}:{r['row_key']}"] = {
            'acquired':   bool(r['acquired']),
            'qty':        r['quantity_owned'],
            'comp_state': cs,
        }
    return result


def _comp_state_for(prog_map: dict, cname: str) -> str:
    """Return the normalised comp_state string for a component name."""
    entry = prog_map.get(f'component:{cname}', {})
    cs = entry.get('comp_state')
    if cs is None:
        cs = 'crafted' if entry.get('acquired', False) else 'pending'
    return cs  # 'pending' | 'blueprint' | 'crafted'


def _compute_progress_pct(user_id: int, goal_id: int, item_id: int) -> int:
    """Compute 0-100 progress % for a goal.

    Uses a fixed denominator (all components + all unique ingredients) so that
    crafting a component never shrinks the total and makes previously-satisfied
    ingredient progress disappear.

    Rules:
      - Each component counts as 1 unit; 'crafted' state marks it done.
      - Each unique ingredient counts as 1 unit; it is done when:
          a) every component that uses it is 'crafted' (implicitly consumed), OR
          b) qty_owned >= total qty needed across all non-crafted components.
    """
    db = get_db()
    ings = db.execute(
        'SELECT component_name, ingredient_name, ingredient_qty FROM component_ingredients WHERE item_id = ?',
        (item_id,),
    ).fetchall()
    if not ings:
        return 0

    prog_map   = _get_progress_map(goal_id)
    comp_names = {r['component_name'] for r in ings}

    # Fixed denominator: all unique ingredients regardless of comp state
    all_ing_totals: dict[str, int] = {}
    ing_comps: dict[str, set] = {}
    for r in ings:
        iname = r['ingredient_name']
        cname = r['component_name']
        all_ing_totals[iname] = all_ing_totals.get(iname, 0) + r['ingredient_qty']
        ing_comps.setdefault(iname, set()).add(cname)

    total = len(comp_names) + len(all_ing_totals)
    if total == 0:
        return 0

    done = 0
    for cname in comp_names:
        if _comp_state_for(prog_map, cname) == 'crafted':
            done += 1

    for iname, qty_needed in all_ing_totals.items():
        comps_using = ing_comps.get(iname, set())
        all_crafted = all(
            _comp_state_for(prog_map, c) == 'crafted' for c in comps_using
        )
        if all_crafted:
            # All components consuming this ingredient are done → treat as satisfied
            done += 1
        else:
            # Sum qty needed only across non-crafted components for the ownership check
            qty_still_needed = sum(
                r['ingredient_qty'] for r in ings
                if r['ingredient_name'] == iname
                and _comp_state_for(prog_map, r['component_name']) != 'crafted'
            )
            if prog_map.get(f'ingredient:{iname}', {}).get('qty', 0) >= qty_still_needed:
                done += 1

    return int(done / total * 100)


def _ingredient_drops_for(db, ing_name: str) -> list[dict]:
    res = db.execute(
        "SELECT id FROM items WHERE name = ? AND category = 'resource'", (ing_name,)
    ).fetchone()
    if not res:
        return []
    rows = db.execute(
        'SELECT mission_type, location, drop_chance, avg_runs FROM drop_sources WHERE item_id = ? ORDER BY avg_runs LIMIT 3',
        (res['id'],),
    ).fetchall()
    return [dict(r) for r in rows]
