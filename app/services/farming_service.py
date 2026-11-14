"""
Farming optimiser — builds efficient multi-target mission routes.
"""

import json
from collections import defaultdict

from models.db import get_db
from models.item import Item
from models.progress import Progress
from services.buy_vs_farm import (expected_hours, hours_label, item_sources,
                                  item_sources_many, recommend, vaulted_parts)
# The farming modules' public names are imported here so that `services.farming_service`
# stays the one import path of the farming API for routes and tests.
from services.farming_advisor import (GUARANTEED_DROP_PERCENT,  # noqa: F401
                                      get_advisor_opportunities, get_all_nodes)
from services.farming_aggregate import get_aggregate_shopping_list  # noqa: F401
from services.farming_goal_plan import get_goal_plan_with_progress  # noqa: F401
from services.farming_goals import (GOAL_MOVE_DIRECTIONS, add_farming_goal,  # noqa: F401
                                    get_farming_goals, get_pinned_goals, move_farming_goal,
                                    remove_farming_goal, search_goal_items,
                                    update_farming_goal)
from services.farming_item_plan import get_item_plan  # noqa: F401
from services.farming_progress import (_comp_state_for, _compute_progress_pct,  # noqa: F401
                                       _get_progress_map, _ingredient_drops_for,
                                       set_goal_progress)
from services.market_api import cached_price_rows, market_url_for, price_view
from services.relic_sources import is_relic_source


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
