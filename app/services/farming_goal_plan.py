"""
The goal plan: a farming goal's component tree merged with its progress, the market prices
of the item and its components with a buy-or-farm answer each, and the goal's shopping list.
"""

from datetime import datetime

from models.db import get_db
from services.auth_service import get_setting_int
from services.buy_vs_farm import (expected_hours, item_sources, part_hours, recommend,
                                  user_thresholds, vaulted_parts)
from services.component_prices import component_price_rows
from services.farming_item_plan import get_item_plan
from services.farming_progress import (_comp_state_for, _compute_progress_pct,
                                       _get_progress_map, _ingredient_drops_for)
from services.market_api import cached_price_rows, get_price, market_url_for, price_view
from services.relic_sync import name_key
from services.resurgence_sync import resurgence_status, vaulted_note


# ── Goal Plan with Progress ──────────────────────────────────────────────────

def get_goal_plan_with_progress(goal_id: int, user_id: int, fetch_price: bool = True) -> dict:
    """Return full crafting plan for a goal, merged with goal-scoped progress.

    The goal item's price is `get_price()`'s (asked of the market when the cached one is not
    fresh) when `fetch_price` is true, as when a plan is opened; with `fetch_price` false it is
    the cached one of any age and nothing is requested, as when the planner reads the plan
    again after a price refresh: the page keeps the goal price it shows, so the answer must
    follow that price, not one fetched behind the user's back."""
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
        'SELECT market_url_name FROM items WHERE id = ?', (item_id,)
    ).fetchone()
    cache_hours = get_setting_int(user_id, 'market_cache_hours')
    thresholds = user_thresholds(user_id)
    sources = item_sources(item_id, db)
    _add_component_prices(plan, item_id, cache_hours, thresholds, sources, db)
    if item_row and item_row['market_url_name']:
        if fetch_price:
            price_data = get_price(item_id, item_row['market_url_name'], cache_hours)
        else:
            price_data = cached_price_rows([item_id]).get(item_id) or                 {'platinum': None, 'fetched_at': None}
        plan['market_price'] = price_data.get('platinum')
        plan['market_url'] = market_url_for(item_row['market_url_name'])
        # How the page shows it: a fresh answer or row is fresh; a kept row after a failed
        # request is old; no row is a missing price. All with a refresh button but the fresh.
        row = ({'platinum': price_data['platinum'], 'fetched_at': price_data['fetched_at']}
               if price_data.get('fetched_at') is not None else None)
        plan['market'] = price_view(row, cache_hours, item_row['market_url_name'],
                                    failed=bool(price_data.get('failed')))
        plan['market_item_id'] = item_id
        if price_data.get('failed'):
            plan['market']['error'] = price_data['error']
        # Buy or farm: the parts not yet crafted and not held as a blueprint still need farming
        needed = [c['component_name'] for c in plan.get('components', [])
                  if c['goal_comp_state'] == 'pending']
        answer = recommend(plan['market_price'], expected_hours(sources, needed),
                           **thresholds, vaulted=vaulted_parts(sources, needed))
        plan['recommend_buy'] = answer['recommend']
        plan['recommend_reason'] = answer['reason']
        plan['expected_hours'] = answer['expected_hours']
    else:
        plan['market_price'] = None
        plan['market_url'] = None
        plan['market'] = None
        plan['recommend_buy'] = False
        plan['recommend_reason'] = None
        plan['expected_hours'] = None

    return plan


def _add_component_prices(plan: dict, item_id: int, cache_hours: int, thresholds: dict,
                          sources: dict, db) -> None:
    """Give each component of the plan that has a market slug its cached price and its own
    buy-or-farm answer; a component without a slug gets `market` None.

    One query reads the prices of the plan's components (any age, old ones marked): opening a
    plan never requests a component price. Each priced component gets `market` (its
    `price_view`, with a refresh button beside an old or missing price), `market_slug`,
    `recommend_buy`, `recommend_reason` and `expected_hours`: the rule of
    `services.buy_vs_farm` with the cached price and the part's own best source.
    """
    rows = component_price_rows(item_id, db)
    now = datetime.utcnow()
    for comp in plan.get('components', []):
        cname = comp['component_name']
        found = rows.get(name_key(cname))
        if found is None:
            comp['market'] = None
            comp['market_slug'] = None
            comp['recommend_buy'] = False
            comp['recommend_reason'] = None
            comp['expected_hours'] = None
            continue
        slug = found['market_url_name']
        comp['market'] = price_view(found['cache'], cache_hours, slug, now)
        comp['market_slug'] = slug
        answer = recommend(comp['market']['platinum'], part_hours(sources, cname),
                           **thresholds, vaulted=vaulted_parts(sources, [cname]))
        comp['recommend_buy'] = answer['recommend']
        comp['recommend_reason'] = answer['reason']
        comp['expected_hours'] = answer['expected_hours']


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
