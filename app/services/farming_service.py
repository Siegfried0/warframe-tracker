# file-length: exempt -- over the length limit; a split into smaller modules is planned
"""
Farming optimiser — builds efficient multi-target mission routes.
"""

import json
from collections import defaultdict

from flask import current_app

from models.db import get_db
from models.progress import Progress
from services.auth_service import get_setting_int
from services.market_api import cached_prices, get_price, market_url_for
from services.resurgence_sync import resurgence_status, vaulted_note

# Categories whose items can be farming goals (excludes raw resources)
_GOAL_CATEGORIES = (
    'warframe', 'primary', 'secondary', 'melee',
    'companion', 'archwing', 'arch-gun', 'arch-melee', 'k-drive',
)


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

    # All component drop entries for this item, grouped by component name
    drop_rows = db.execute(
        '''SELECT notes AS component_name, mission_type, location,
                  drop_chance, avg_runs, source_type
             FROM drop_sources
            WHERE item_id = ? AND source_type = 'component'
            ORDER BY notes, avg_runs''',
        (item_id,),
    ).fetchall()

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

    # Assemble component list — union of those with drops and those with ingredients
    all_comp_names = sorted(set(list(comp_drops.keys()) + list(comp_ingredients.keys())))
    # Remove empty-string key that can appear if notes was NULL
    all_comp_names = [c for c in all_comp_names if c]

    components = []
    for cname in all_comp_names:
        components.append({
            'component_name': cname,
            'acquired':       checklist.get(cname, False),
            'drops':          comp_drops.get(cname, []),
            'ingredients':    comp_ingredients.get(cname, []),
        })

    return {
        'item_id':    item_id,
        'item_name':  item_row['name'],
        'image_url':  item_row['image_url'],
        'components': components,
    }


def get_checklist_state(user_id: int, item_ids: list[int]) -> dict[str, bool]:
    """Return {'{item_id}:{component_name}': acquired} for all checklist rows."""
    if not item_ids:
        return {}
    placeholders = ','.join('?' for _ in item_ids)
    rows = get_db().execute(
        f'''SELECT item_id, component_name, acquired FROM farming_checklist
            WHERE user_id = ? AND item_id IN ({placeholders})''',
        [user_id, *item_ids],
    ).fetchall()
    return {f"{r['item_id']}:{r['component_name']}": bool(r['acquired']) for r in rows}


def set_checklist(user_id: int, item_id: int, component_name: str, acquired: bool):
    db = get_db()
    db.execute(
        '''INSERT INTO farming_checklist (user_id, item_id, component_name, acquired)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, item_id, component_name)
           DO UPDATE SET acquired = excluded.acquired, updated_at = datetime('now')''',
        (user_id, item_id, component_name, int(acquired)),
    )
    db.commit()


def get_drop_sources(item_id: int) -> list[dict]:
    rows = get_db().execute(
        'SELECT * FROM drop_sources WHERE item_id = ? ORDER BY avg_runs',
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def compute_farming_route(user_id: int, target_item_ids: list[int]) -> list[dict]:
    """
    Given a list of target item IDs (unowned), return a sorted list of missions
    that cover the most targets per run.

    Returns list of:
      {mission_type, location, drop_chance, avg_runs, covers: [item names]}
    """
    # Map each target to its best drop source
    db = get_db()
    mission_coverage: dict[tuple, list] = defaultdict(list)

    for item_id in target_item_ids:
        sources = get_drop_sources(item_id)
        if not sources:
            continue
        best = sources[0]  # already sorted by avg_runs ASC
        key = (best['mission_type'], best['location'])
        mission_coverage[key].append({
            'item_id': item_id,
            'name': db.execute('SELECT name FROM items WHERE id = ?', (item_id,)).fetchone()['name'],
            'avg_runs': best['avg_runs'],
            'drop_chance': best['drop_chance'],
        })

    route = []
    for (mission_type, location), covered in mission_coverage.items():
        route.append({
            'mission_type': mission_type,
            'location': location,
            'covers': covered,
            'target_count': len(covered),
            'avg_runs_best': min(c['avg_runs'] for c in covered if c['avg_runs']),
        })

    # Sort: most targets covered first, then fewest avg runs
    route.sort(key=lambda m: (-m['target_count'], m.get('avg_runs_best') or 999))
    return route


def farm_next_items(user_id: int, buy_threshold_plat: int | None,
                    plat_per_hour: int | None, limit: int) -> list[dict]:
    """The caller's first `limit` unowned, unmastered items, priced from the cache.

    The dashboard's "Farm next" list. Never requests a price (see `enrich_with_cached_market`).
    """
    items = Progress.get_unowned_items(user_id)[:limit]
    return enrich_with_cached_market(items, buy_threshold_plat, plat_per_hour)


def enrich_with_cached_market(items: list[dict], buy_threshold_plat: int | None,
                              plat_per_hour: int | None) -> list[dict]:
    """
    Annotate each tradable item dict with its cached market price and a buy recommendation.
    Mutates items in-place and returns them.

    The price is the cached one of any age, read for all items in one query; nothing is
    requested or written. An item without a cached price gets `market_price` None and no
    recommendation.
    """
    priced = [item for item in items
              if item.get('tradable') and item.get('market_url_name')]
    prices = cached_prices([item['id'] for item in priced])
    for item in priced:
        plat = prices.get(item['id'])
        item['market_price'] = plat
        item['market_url'] = market_url_for(item['market_url_name'])
        item['recommend_buy'] = False

        if plat is None:
            continue

        if buy_threshold_plat and plat <= buy_threshold_plat:
            item['recommend_buy'] = True
            item['recommend_reason'] = f'{plat}p ≤ threshold {buy_threshold_plat}p'
            continue

        # Time-value check: if expected farming time × rate > price, suggest buying
        sources = get_drop_sources(item['id'])
        if sources and plat_per_hour:
            avg_runs = sources[0].get('avg_runs') or 0
            # Assume ~5 min/run → runs × 5 min → hours
            expected_hours = (avg_runs * 5) / 60
            if expected_hours * plat_per_hour > plat:
                item['recommend_buy'] = True
                item['recommend_reason'] = (
                    f'~{expected_hours:.1f}h farming × {plat_per_hour}p/h '
                    f'> market {plat}p'
                )
    return items


def get_pinned_route(user_id: int) -> dict | None:
    row = get_db().execute(
        'SELECT route_data FROM pinned_routes WHERE user_id = ? ORDER BY pinned_at DESC LIMIT 1',
        (user_id,),
    ).fetchone()
    return json.loads(row['route_data']) if row else None


def pin_route(user_id: int, route: list[dict]):
    db = get_db()
    db.execute('DELETE FROM pinned_routes WHERE user_id = ?', (user_id,))
    db.execute(
        'INSERT INTO pinned_routes (user_id, route_data) VALUES (?, ?)',
        (user_id, json.dumps(route)),
    )
    db.commit()


def clear_pinned_route(user_id: int):
    db = get_db()
    db.execute('DELETE FROM pinned_routes WHERE user_id = ?', (user_id,))
    db.commit()


# ── Goal Management ──────────────────────────────────────────────────────────

def search_goal_items(query: str, limit: int = 20) -> list[dict]:
    """Search craftable items for the goal picker (excludes resources)."""
    db = get_db()
    q = f'%{query}%'
    cats = ','.join('?' * len(_GOAL_CATEGORIES))
    rows = db.execute(
        f'SELECT id, name, category, vaulted, image_url FROM items '
        f'WHERE category IN ({cats}) AND name LIKE ? ORDER BY name LIMIT ?',
        (*_GOAL_CATEGORIES, q, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_farming_goals(user_id: int) -> list[dict]:
    """Return all farming goals for a user with item info and progress %."""
    db = get_db()
    rows = db.execute(
        '''SELECT fg.id AS goal_id, fg.item_id, fg.pinned, fg.display_mode, fg.sort_order,
                  i.name AS item_name, i.category, i.vaulted, i.image_url
             FROM farming_goals fg
             JOIN items i ON i.id = fg.item_id
            WHERE fg.user_id = ?
            ORDER BY fg.pinned DESC, fg.sort_order, fg.created_at, fg.id''',
        (user_id,),
    ).fetchall()
    goals = []
    for r in rows:
        g = dict(r)
        g['progress_pct'] = _compute_progress_pct(user_id, g['goal_id'], g['item_id'])
        goals.append(g)
    return goals


def get_pinned_goals(user_id: int) -> list[dict]:
    """Return only pinned goals with progress for the dashboard."""
    db = get_db()
    rows = db.execute(
        '''SELECT fg.id AS goal_id, fg.item_id,
                  i.name AS item_name, i.category, i.vaulted, i.image_url
             FROM farming_goals fg
             JOIN items i ON i.id = fg.item_id
            WHERE fg.user_id = ? AND fg.pinned = 1
            ORDER BY fg.sort_order, fg.created_at, fg.id''',
        (user_id,),
    ).fetchall()
    goals = []
    for r in rows:
        g = dict(r)
        g['progress_pct'] = _compute_progress_pct(user_id, g['goal_id'], g['item_id'])
        goals.append(g)
    return goals


def add_farming_goal(user_id: int, item_id: int) -> dict:
    """Add an item as a farming goal; returns goal dict (or existing if duplicate)."""
    db = get_db()
    item_row = db.execute(
        'SELECT id, name, category, vaulted, image_url FROM items WHERE id = ?',
        (item_id,),
    ).fetchone()
    if not item_row:
        return {}

    existing = db.execute(
        '''SELECT id, pinned, display_mode, sort_order FROM farming_goals
            WHERE user_id = ? AND item_id = ?''',
        (user_id, item_id),
    ).fetchone()

    if existing:
        goal_id = existing['id']
        pinned, display_mode, sort_order = (
            existing['pinned'], existing['display_mode'], existing['sort_order'])
        already_exists = True
    else:
        next_order = _insert_goal_last(db, user_id, item_id)
        goal_id = db.execute(
            'SELECT id FROM farming_goals WHERE user_id = ? AND item_id = ?',
            (user_id, item_id),
        ).fetchone()['id']
        pinned, display_mode, sort_order = 0, 'tree', next_order
        already_exists = False

    return {
        'goal_id':       goal_id,
        'item_id':       item_id,
        'item_name':     item_row['name'],
        'category':      item_row['category'],
        'vaulted':       item_row['vaulted'],
        'image_url':     item_row['image_url'],
        'pinned':        pinned,
        'display_mode':  display_mode,
        'sort_order':    sort_order,
        'progress_pct':  _compute_progress_pct(user_id, goal_id, item_id),
        'already_exists': already_exists,
    }


# The largest value `farming_goals.sort_order` holds, per backend: SQLite's INTEGER is
# 64-bit, the MSSQL schema's INT is 32-bit.
_SORT_ORDER_MAX = {'sqlite': 2**63 - 1, 'mssql': 2**31 - 1}


def _sort_order_max() -> int:
    return _SORT_ORDER_MAX[current_app.config.get('DB_BACKEND', 'sqlite')]


def _lock_user_goals(db, user_id: int):
    """Open the transaction with a no-op write on the user's goals, so a concurrent move or
    add of the same user waits until this one commits (SQLite: the write lock; MSSQL: the
    rows' locks) and then reads what this one wrote."""
    db.execute('UPDATE farming_goals SET sort_order = sort_order WHERE user_id = ?', (user_id,))


def _goals_in_page_order(db, user_id: int) -> list[tuple]:
    """The user's goals as (id, pinned, sort_order), in the planner's order."""
    rows = db.execute(
        '''SELECT id, pinned, sort_order FROM farming_goals
            WHERE user_id = ?
            ORDER BY pinned DESC, sort_order, created_at, id''',
        (user_id,),
    ).fetchall()
    return [(r['id'], r['pinned'], r['sort_order']) for r in rows]


def _renumber_goals(db, user_id: int, order: list[tuple]):
    """Write `sort_order` 0..n-1 in the given order; a row already at its place is not
    written. Runs inside the caller's transaction."""
    for position, (row_id, _pinned, current) in enumerate(order):
        if current != position:
            db.execute(
                'UPDATE farming_goals SET sort_order = ? WHERE id = ? AND user_id = ?',
                (position, row_id, user_id),
            )


def _insert_goal_last(db, user_id: int, item_id: int) -> int:
    """Insert a new goal after the user's largest `sort_order`; returns the value written.

    When that value would pass the column's maximum for the backend, the user's goals are
    first renumbered 0..n-1 in the planner's order (as a move does) and the new goal gets n.
    One transaction, opened by the same lock as a move.
    """
    try:
        _lock_user_goals(db, user_id)
        largest = db.execute(
            'SELECT MAX(sort_order) AS m FROM farming_goals WHERE user_id = ?',
            (user_id,),
        ).fetchone()['m']
        next_order = 0 if largest is None else largest + 1
        if next_order > _sort_order_max():
            order = _goals_in_page_order(db, user_id)
            _renumber_goals(db, user_id, order)
            next_order = len(order)
        db.execute(
            'INSERT INTO farming_goals (user_id, item_id, sort_order) VALUES (?, ?, ?)',
            (user_id, item_id, next_order),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return next_order


def remove_farming_goal(user_id: int, goal_id: int):
    db = get_db()
    db.execute('DELETE FROM farming_goals WHERE id = ? AND user_id = ?', (goal_id, user_id))
    db.commit()


def update_farming_goal(user_id: int, goal_id: int, **kwargs):
    """Update pinned / display_mode / sort_order for a goal."""
    allowed = {'pinned', 'display_mode', 'sort_order'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    db = get_db()
    set_parts = ', '.join(f'{k} = ?' for k in fields)
    db.execute(
        f'UPDATE farming_goals SET {set_parts} WHERE id = ? AND user_id = ?',
        list(fields.values()) + [goal_id, user_id],
    )
    db.commit()


GOAL_MOVE_DIRECTIONS = ('up', 'down')


def move_farming_goal(user_id: int, goal_id: int, direction: str) -> bool:
    """Move one of the user's goals one place up or down within its pin group.

    The goals are taken in the planner's order (pinned first, then `sort_order`,
    `created_at`, `id`); the goal swaps places with its neighbour in that order when the
    neighbour has the same pin state. Then every goal of the user gets `sort_order` 0..n-1
    in the resulting order, so goals that shared a value are told apart by the first move.
    At the edge of its group the goal stays put and nothing is written. Pinning or
    unpinning never touches `sort_order`.

    One transaction. Its first statement is a write on the user's rows, so a concurrent
    move of the same user waits for this one to commit (SQLite: the write lock; MSSQL: the
    rows' locks) and then reads the order this one wrote: whatever the interleaving, the
    result is a valid order with every goal once.

    Returns False, writing nothing, when the user has no goal `goal_id` or `direction` is
    not `up` or `down`; True otherwise, also when the goal is at the edge of its group.
    """
    if direction not in GOAL_MOVE_DIRECTIONS:
        return False
    db = get_db()
    try:
        _lock_user_goals(db, user_id)
        order = _goals_in_page_order(db, user_id)
        index = next((i for i, row in enumerate(order) if row[0] == goal_id), None)
        if index is None:
            db.rollback()
            return False
        other = index - 1 if direction == 'up' else index + 1
        if not (0 <= other < len(order) and order[other][1] == order[index][1]):
            db.rollback()  # at the edge of its group: nothing changes
            return True
        order[index], order[other] = order[other], order[index]
        _renumber_goals(db, user_id, order)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True


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


# ── Goal Plan with Progress ──────────────────────────────────────────────────

def get_goal_plan_with_progress(goal_id: int, user_id: int) -> dict:
    """Return full crafting plan for a goal, merged with goal-scoped progress."""
    db = get_db()
    goal_row = db.execute(
        'SELECT item_id, display_mode FROM farming_goals WHERE id = ? AND user_id = ?',
        (goal_id, user_id),
    ).fetchone()
    if not goal_row:
        return {}

    item_id = goal_row['item_id']
    plan = get_item_plan(item_id, user_id)
    if not plan:
        return {}

    prog_map = _get_progress_map(goal_id)

    # Enrich tree-view components with goal-scoped progress + vaulted badge. A vaulted
    # item's note names trade and Prime Resurgence, and says "in Prime Resurgence now" only
    # while the stored stock is current and lists the item.
    flags = db.execute(
        'SELECT vaulted, resurgence_available FROM items WHERE id = ?', (item_id,)
    ).fetchone()
    item_vaulted = bool(flags['vaulted'])
    resurgence = resurgence_status()
    in_resurgence = bool(resurgence['current'] and flags['resurgence_available'])
    note = vaulted_note(in_resurgence) if item_vaulted else None
    plan['vaulted'] = item_vaulted
    plan['vaulted_note'] = note
    plan['in_resurgence'] = in_resurgence
    plan['resurgence_notice'] = resurgence['notice'] if item_vaulted else None

    # Pre-fetch component and ingredient images for tree-view thumbnails
    all_comp_names_list = [c['component_name'] for c in plan.get('components', [])]
    comp_img_map: dict[str, str | None] = {}
    if all_comp_names_list:
        ph = ','.join('?' for _ in all_comp_names_list)
        try:
            for row in db.execute(
                f'SELECT component_name, image_url FROM item_component_meta '
                f'WHERE item_id = ? AND component_name IN ({ph})',
                [item_id, *all_comp_names_list],
            ).fetchall():
                comp_img_map[row['component_name']] = row['image_url']
        except Exception:
            pass

    all_ing_names = list({
        ing['ingredient_name']
        for comp in plan.get('components', [])
        for ing in comp.get('ingredients', [])
    })
    ing_img_map: dict[str, str | None] = {}
    if all_ing_names:
        ph2 = ','.join('?' for _ in all_ing_names)
        for row in db.execute(
            f"SELECT name, image_url FROM items WHERE category='resource' AND name IN ({ph2})",
            all_ing_names,
        ).fetchall():
            ing_img_map[row['name']] = row['image_url']

    for comp in plan.get('components', []):
        cname = comp['component_name']
        cs = _comp_state_for(prog_map, cname)
        comp['goal_comp_state'] = cs
        comp['goal_acquired']   = (cs == 'crafted')   # tree-view checkbox compat
        comp['vaulted'] = item_vaulted  # component blueprint vaults with the item
        comp['vaulted_note'] = note
        comp['image_url'] = comp_img_map.get(cname)
        for ing in comp.get('ingredients', []):
            iname = ing['ingredient_name']
            ing['quantity_owned'] = prog_map.get(f'ingredient:{iname}', {}).get('qty', 0)
            ing['image_url'] = ing_img_map.get(iname)

    all_comp_names = [c['component_name'] for c in plan.get('components', [])]
    plan['shopping_list'] = _build_shopping_list(item_id, prog_map, all_comp_names)
    plan['goal_id']       = goal_id
    plan['display_mode']  = goal_row['display_mode']
    plan['progress_pct']  = _compute_progress_pct(user_id, goal_id, item_id)

    # Market price for the goal item itself
    item_row = db.execute(
        'SELECT tradable, market_url_name FROM items WHERE id = ?', (item_id,)
    ).fetchone()
    if item_row and item_row['tradable'] and item_row['market_url_name']:
        cache_hours = get_setting_int(user_id, 'market_cache_hours')
        price_data = get_price(item_id, item_row['market_url_name'], cache_hours)
        plan['market_price'] = price_data.get('platinum')
        plan['market_url'] = market_url_for(item_row['market_url_name'])
    else:
        plan['market_price'] = None
        plan['market_url'] = None

    return plan


def _build_shopping_list(item_id: int, prog_map: dict, all_comp_names: list = None) -> dict:
    """Flat shopping-list view for a single goal.

    Component states:
      'pending'   → nothing done; all resources required
      'blueprint' → blueprint in hand; resources still required
      'crafted'   → done; resources excluded from required totals

    Resource dots carry `from_components_states` so the frontend can render
    an amber ring on dots for components in 'blueprint' state.
    """
    db = get_db()
    ings = db.execute(
        '''SELECT component_name, ingredient_name, ingredient_qty
             FROM component_ingredients WHERE item_id = ?
             ORDER BY component_name, ingredient_name''',
        (item_id,),
    ).fetchall()

    # Build comp_state_map: component → 'pending' | 'blueprint' | 'crafted'
    comp_state_map: dict[str, str] = {}
    for r in ings:
        cname = r['component_name']
        if cname not in comp_state_map:
            comp_state_map[cname] = _comp_state_for(prog_map, cname)

    # Include components that have drops but no ingredient rows (e.g. boss blueprints)
    if all_comp_names:
        for cname in all_comp_names:
            if cname and cname not in comp_state_map:
                comp_state_map[cname] = _comp_state_for(prog_map, cname)

    # Batch-fetch component image URLs from item_component_meta
    if comp_state_map:
        cnames = list(comp_state_map.keys())
        ph = ','.join('?' for _ in cnames)
        try:
            comp_img_rows = db.execute(
                f'SELECT component_name, image_url FROM item_component_meta '
                f'WHERE item_id = ? AND component_name IN ({ph})',
                [item_id, *cnames],
            ).fetchall()
            component_images: dict[str, str | None] = {
                r['component_name']: r['image_url'] for r in comp_img_rows
            }
        except Exception:
            component_images = {}
    else:
        component_images = {}

    # Aggregate resources.
    # resource_comps  — ALL components using this resource (dots + graying)
    # resource_totals — only pending/blueprint components (crafted excluded)
    resource_totals: dict[str, int] = {}
    resource_comps:  dict[str, list] = {}
    for r in ings:
        cname = r['component_name']
        iname = r['ingredient_name']
        if iname not in resource_comps:
            resource_comps[iname] = []
        if cname not in resource_comps[iname]:
            resource_comps[iname].append(cname)
        # Blueprint state still requires resources; only crafted removes them
        if comp_state_map.get(cname, 'pending') != 'crafted':
            resource_totals[iname] = resource_totals.get(iname, 0) + r['ingredient_qty']

    # Batch-fetch resource images
    all_resource_names = sorted(resource_comps.keys())
    if all_resource_names:
        ph = ','.join('?' for _ in all_resource_names)
        img_rows = db.execute(
            f"SELECT name, image_url FROM items WHERE category='resource' AND name IN ({ph})",
            all_resource_names,
        ).fetchall()
        resource_images = {r['name']: r['image_url'] for r in img_rows}
    else:
        resource_images = {}

    resources = []
    total_qty_needed  = 0
    total_qty_covered = 0
    for iname in all_resource_names:
        qty_needed   = resource_totals.get(iname, 0)
        qty_owned    = prog_map.get(f'ingredient:{iname}', {}).get('qty', 0)
        still_needed = max(0, qty_needed - qty_owned)
        # Grayed when every component using this resource is crafted
        all_comps_crafted = all(
            comp_state_map.get(c, 'pending') == 'crafted'
            for c in resource_comps.get(iname, [])
        )
        drops = _ingredient_drops_for(db, iname)
        # Per-component states for dot tint rendering on the frontend
        from_comp_states = {
            c: comp_state_map.get(c, 'pending')
            for c in resource_comps.get(iname, [])
        }

        resources.append({
            'ingredient_name':        iname,
            'qty_needed':             qty_needed,
            'qty_owned':              qty_owned,
            'still_needed':           still_needed,
            'drops':                  drops,
            'grayed':                 all_comps_crafted,
            'from_components':        resource_comps.get(iname, []),
            'from_components_states': from_comp_states,
            'image_url':              resource_images.get(iname),
        })
        if not all_comps_crafted and qty_needed > 0:
            total_qty_needed  += qty_needed
            total_qty_covered += min(qty_owned, qty_needed)

    readiness_pct = int(total_qty_covered / total_qty_needed * 100) if total_qty_needed else 100

    return {
        'components': [
            {
                'component_name': c,
                'comp_state':     state,
                'acquired':       state == 'crafted',   # kept for tree-view compat
                'image_url':      component_images.get(c),
            }
            for c, state in comp_state_map.items()
        ],
        'resources':     resources,
        'readiness_pct': readiness_pct,
    }


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


# ── Live Session Advisor ─────────────────────────────────────────────────────

def get_all_nodes() -> list[str]:
    """Return sorted distinct node locations from drop_sources."""
    rows = get_db().execute(
        'SELECT DISTINCT location FROM drop_sources WHERE location IS NOT NULL ORDER BY location'
    ).fetchall()
    return [r['location'] for r in rows if r['location']]


def get_advisor_opportunities(user_id: int, node_location: str) -> dict:
    """Cross-reference user's still-needed items against drops at the selected node."""
    db = get_db()

    node_drops = db.execute(
        '''SELECT ds.item_id, ds.drop_chance, ds.source_type,
                  ds.notes AS drop_notes, i.name AS item_name
             FROM drop_sources ds
             JOIN items i ON ds.item_id = i.id
            WHERE ds.location = ?
            ORDER BY ds.drop_chance DESC''',
        (node_location,),
    ).fetchall()

    if not node_drops:
        return {
            'opportunities': [], 'harvest_summary': [],
            'guaranteed_free': False, 'node': node_location,
        }

    max_chance = node_drops[0]['drop_chance'] or 0
    guaranteed_free = bool(max_chance >= 0.95)

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
        'SELECT item_id, source_type, notes, MAX(drop_chance) AS best FROM drop_sources GROUP BY item_id, source_type, notes'
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
