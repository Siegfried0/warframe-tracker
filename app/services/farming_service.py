# file-length: exempt -- over the length limit; a split into smaller modules is planned
"""
Farming optimiser — builds efficient multi-target mission routes.
"""

import json
from collections import defaultdict
from datetime import datetime

from models.db import get_db
from models.item import Item
from models.progress import Progress
from services.auth_service import get_setting_int
from services.buy_vs_farm import (expected_hours, hours_label, item_sources,
                                  item_sources_many, part_hours, recommend, user_thresholds,
                                  vaulted_parts)
from services.component_prices import component_price_rows
# The goal modules' public names are imported here so that `services.farming_service` stays
# the one import path of the farming API for routes and tests.
from services.farming_goals import (GOAL_MOVE_DIRECTIONS, add_farming_goal,  # noqa: F401
                                    get_farming_goals, get_pinned_goals, move_farming_goal,
                                    remove_farming_goal, search_goal_items,
                                    update_farming_goal)
from services.farming_progress import (_comp_state_for, _compute_progress_pct,  # noqa: F401
                                       _get_progress_map, _ingredient_drops_for,
                                       set_goal_progress)
from services.market_api import cached_price_rows, get_price, market_url_for, price_view
from services.relic_sources import is_relic_source, relics_by_part
from services.relic_sync import is_relic_name, name_key
from services.resurgence_sync import resurgence_status, vaulted_note


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
    """The item's mission, component and resource drop rows, fewest average runs first.
    Relic sources are left out: a relic is no mission to run."""
    rows = get_db().execute(
        '''SELECT * FROM drop_sources
            WHERE item_id = ? AND (source_type IS NULL OR source_type <> 'relic')
            ORDER BY avg_runs''',
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows if not is_relic_source(r['source_type'], r['location'])]


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
                    plat_per_hour: int | None, limit: int, cache_hours: int = 6,
                    farm_hour_cap: int | None = None) -> list[dict]:
    """The caller's `limit` quickest unowned, unmastered items to farm, priced from the cache.

    The dashboard's "Farm next" list. Every unowned item is ranked by its expected farming
    time, every part counted (`buy_vs_farm.expected_hours()` over `item_sources_many()`):
    fewest hours first, items with an unknown time (a vaulted part included) after all
    the others; equal times keep the category and name order. Each listed item gets its
    `farm_hours` and their label `farm_time` (`~2.0h`, `—` when unknown). Never requests a
    price (see `enrich_with_cached_market`).
    """
    items = Progress.get_unowned_items(user_id)
    sources = item_sources_many([item['id'] for item in items])
    for item in items:
        item['farm_hours'] = expected_hours(sources[item['id']])
    # stable: equal times stay in the category and name order of get_unowned_items()
    items.sort(key=lambda item: (item['farm_hours'] is None, item['farm_hours'] or 0.0))
    listed = items[:limit]
    for item in listed:
        item['farm_time'] = hours_label(item['farm_hours'])
    return enrich_with_cached_market(listed, buy_threshold_plat, plat_per_hour, cache_hours,
                                     farm_hour_cap, sources=sources)


def farm_next_answer(item_id: int, buy_threshold_plat: int | None,
                     plat_per_hour: int | None, cache_hours: int = 6,
                     farm_hour_cap: int | None = None) -> dict | None:
    """One item's Farm next buy-or-farm answer from its cached price, as the list draws it.

    `{'recommend_buy', 'recommend_reason'}` through `enrich_with_cached_market` with the same
    settings `farm_next_items` takes, so the answer is the one the dashboard's load would
    give. None for an unknown item or one without a market slug. Requests and writes nothing.
    """
    found = Item.get_by_id(item_id)
    if found is None or not found.market_url_name:
        return None
    item = {'id': found.id, 'market_url_name': found.market_url_name}
    enrich_with_cached_market([item], buy_threshold_plat, plat_per_hour, cache_hours,
                              farm_hour_cap)
    return {'recommend_buy': item['recommend_buy'], 'recommend_reason': item['recommend_reason']}


def enrich_with_cached_market(items: list[dict], buy_threshold_plat: int | None,
                              plat_per_hour: int | None, cache_hours: int = 6,
                              farm_hour_cap: int | None = None,
                              sources: dict | None = None) -> list[dict]:
    """
    Annotate each item dict with a market slug with its cached market price and the
    buy-or-farm answer of `services.buy_vs_farm` (the slug alone decides: an item the
    market sells only as a set is not tradable, yet priced).
    Mutates items in-place and returns them.

    The price is the cached one of any age, read with its fetch time for all items in one
    query; nothing is requested or written. Each priced item also gets `price`, its
    `price_view` (old mark, fetch time, refresh button) under the user's `cache_hours`. An
    item without a cached price gets `market_price` None; only the hour cap can then
    recommend buying it. The farming time counts every part of the item (no goal says
    which are done): `recommend_buy`, `recommend_reason`, `expected_hours`. `sources`, when
    given, holds the items' `item_sources()` already read ({item id: sources}).
    """
    priced = [item for item in items
              if item.get('market_url_name')]
    rows = cached_price_rows([item['id'] for item in priced])
    for item in priced:
        item['price'] = price_view(rows.get(item['id']), cache_hours, item['market_url_name'])
        plat = item['price']['platinum']
        item['market_price'] = plat
        item['market_url'] = market_url_for(item['market_url_name'])
        found = sources.get(item['id']) if sources is not None else None
        found = found if found is not None else item_sources(item['id'])
        answer = recommend(plat, expected_hours(found),
                           buy_threshold_plat, farm_hour_cap, plat_per_hour,
                           vaulted=vaulted_parts(found))
        item['recommend_buy'] = answer['recommend']
        item['recommend_reason'] = answer['reason']
        item['expected_hours'] = answer['expected_hours']
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
