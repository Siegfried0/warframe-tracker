"""Buy or farm: whether buying an item on warframe.market beats farming it.

One rule for the dashboard's Farm next list and the Farming Planner. Four checks, in
order; the first that fires wins:

1. the price threshold: the price is at most the user's ``buy_threshold_plat``;
2. vaulted: a part still needed drops only from relics no mission drops (whatever the
   settings, no price needed: buying is the only way);
3. the hour cap: the expected farming time is over the user's ``farm_hour_cap`` (needs no
   price, only that the item is on the market);
4. the time value: expected hours x the user's ``plat_per_hour`` is over the price.

An unset setting switches its check off: no value for the threshold, no value or 0 for the
cap and the rate. A check that needs a price or a known farming time does not fire without
one.

The farming time is an estimate: each part still needed counts its best (fewest minutes)
source, runs x the typical minutes of a run of that mission type (the row's type, or the game
mode in brackets in its location), and the parts are added up. A relic counts one Void
Fissure run per opening (``100 / chance`` openings); the relic's own farming is not counted. A
relic that drops from no mission gives no time; a part held only by such relics is vaulted.
That needs drop data: while ``relic_drops`` is empty no relic counts as dropping nowhere. A
needed part without any source makes the time unknown.
"""

import math
import re
from collections import defaultdict

from models.db import get_db
from services.auth_service import get_setting_int
from services.relic_sources import is_relic_source, relics_dropping_nowhere
from services.relic_sync import clean, name_key

# Typical minutes for one run of a mission type, as the drop tables name the game mode
# (compared apart from case and surrounding blanks). A rotation mission counts one rotation
# of 5 minutes, since the stored drop rows carry no rotation.
MINUTES_PER_RUN = {
    'capture':              3,
    'exterminate':          4,
    'rescue':               4,
    'sabotage':             5,
    'hijack':               5,
    'spy':                  6,
    'mobile defense':       6,
    'assassination':        6,
    'survival':             5,
    'defense':              5,
    'interception':         5,
    'excavation':           5,
    'disruption':           5,
    'defection':            5,
    'infested salvage':     5,
    'void flood':           5,
    'void cascade':         5,
    'alchemy':              5,
    'mirror defense':       5,
    'conjunction survival': 5,
}
# A mission type not in the table
DEFAULT_MINUTES = 5
# One relic opening: one Void Fissure run
RELIC_OPENING_MINUTES = 5


# A game mode in brackets in a drop location: 'Venus/Fossa (Assassination)'
_BRACKETED = re.compile(r'\(([^()]*)\)')


def minutes_per_run(mission_type, location=None) -> int:
    """The typical minutes of one run: of ``mission_type`` when the table lists it, else of
    the game mode named in brackets in ``location`` (a part's drop row holds the reward name
    as its type and the mode only in its location); DEFAULT_MINUTES when neither is listed."""
    candidates = [mission_type]
    if isinstance(location, str):
        candidates += reversed(_BRACKETED.findall(location))
    for candidate in candidates:
        minutes = MINUTES_PER_RUN.get(name_key(candidate))
        if minutes is not None:
            return minutes
    return DEFAULT_MINUTES


def _positive(value):
    """``value`` as a float when it is a finite number above 0, else None (text, NULL, 0,
    a negative, NaN or an infinity in a hand-edited row)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def source_minutes(row, nowhere=frozenset()) -> float | None:
    """Minutes to get one drop from a drop source row (``mission_type``, ``location``,
    ``drop_chance``, ``avg_runs``, ``source_type``), or None when the row gives no number or
    is a relic whose name key is in ``nowhere`` (it drops from no mission)."""
    if is_relic_source(row['source_type'], row['location']):
        if name_key(row['location']) in nowhere:
            return None
        chance = _positive(row['drop_chance'])
        return 100.0 / chance * RELIC_OPENING_MINUTES if chance else None
    runs = _positive(row['avg_runs'])
    return runs * minutes_per_run(row['mission_type'], row['location']) if runs else None


def _best(current, minutes):
    if minutes is None:
        return current
    return minutes if current is None else min(current, minutes)


def item_sources(item_id: int, db=None) -> dict:
    """The farming time of an item's parts and of the item itself, from its drop rows.

    ``parts``: {part name key: best minutes, None when the part has no source}, over the
    parts the Farming Planner lists (those with a drop row, a relic or a recipe);
    ``own``: the best minutes of the item's own sources (none of a part), or None;
    ``vaulted``: {part name key: part name} of the parts whose every source is a relic that
    drops from no mission (the name as its first drop row spells it, blanks cleaned)."""
    db = db or get_db()
    rows = db.execute(
        '''SELECT mission_type, location, drop_chance, avg_runs, source_type, notes
             FROM drop_sources WHERE item_id = ?''',
        (item_id,),
    ).fetchall()
    nowhere = relics_dropping_nowhere(
        [r['location'] for r in rows if is_relic_source(r['source_type'], r['location'])], db)
    parts: dict = defaultdict(lambda: None)
    names: dict = {}
    farmable: set = set()
    own = None
    for r in rows:
        minutes = source_minutes(r, nowhere)
        if r['source_type'] in ('component', 'relic'):
            key = name_key(r['notes'])
            if key:
                parts[key] = _best(parts[key], minutes)
                names.setdefault(key, clean(r['notes']))
                if not (is_relic_source(r['source_type'], r['location'])
                        and name_key(r['location']) in nowhere):
                    farmable.add(key)
        else:
            own = _best(own, minutes)
    for r in db.execute(
            'SELECT DISTINCT component_name FROM component_ingredients WHERE item_id = ?',
            (item_id,)).fetchall():
        key = name_key(r['component_name'])
        if key and key not in parts:
            parts[key] = None
    vaulted = {key: name for key, name in names.items() if key not in farmable}
    return {'parts': dict(parts), 'own': own, 'vaulted': vaulted}


def expected_hours(sources: dict, needed=None) -> float | None:
    """Expected hours of farming from ``item_sources()``: the sum over the needed parts of
    each part's best minutes, or the item's own best source when it has no parts. ``needed``
    names the parts still needed (any spelling of case and blanks); None means all of them.
    None when a needed part (or a part-less item) has no source."""
    parts = sources['parts']
    if not parts:
        return None if sources['own'] is None else sources['own'] / 60
    keys = parts.keys() if needed is None else {name_key(n) for n in needed}
    total = 0.0
    for key in keys:
        minutes = parts.get(key)
        if minutes is None:
            return None
        total += minutes
    return total / 60


def part_hours(sources: dict, part) -> float | None:
    """Expected hours of farming one part from ``item_sources()``: its best source (``part``
    in any spelling of case and blanks), or None when it has no source."""
    minutes = sources['parts'].get(name_key(part))
    return None if minutes is None else minutes / 60


def vaulted_parts(sources: dict, needed=None) -> list[str]:
    """The needed parts (``needed`` as in ``expected_hours()``) that are vaulted, by their
    names from ``item_sources()``, in the order of their name keys."""
    vaulted = sources.get('vaulted', {})
    keys = vaulted.keys() if needed is None else {name_key(n) for n in needed}
    return [vaulted[key] for key in sorted(keys) if key in vaulted]


def recommend(price, hours, buy_threshold_plat=None, farm_hour_cap=None,
              plat_per_hour=None, vaulted=()) -> dict:
    """The buy-or-farm answer for an item on the market.

    ``price``: its platinum price or None; ``hours``: its expected farming hours or None;
    ``vaulted``: the names of its needed parts no mission drops (``vaulted_parts()``).
    Returns ``recommend`` (bool), ``check`` (``'threshold'``, ``'vaulted'``, ``'hour_cap'``,
    ``'time_value'`` or None), ``reason`` (text) and ``expected_hours`` (None when vaulted)."""
    def answer(check, reason):
        return {'recommend': check is not None, 'check': check, 'reason': reason,
                'expected_hours': None if check == 'vaulted' else hours}

    if price is not None and buy_threshold_plat is not None and price <= buy_threshold_plat:
        return answer('threshold', f'{price}p ≤ threshold {buy_threshold_plat}p')
    if vaulted:
        return answer('vaulted', f'{", ".join(vaulted)} vaulted: no mission drops it')
    if hours is None:
        return answer(None, 'farming time unknown')
    if farm_hour_cap and hours > farm_hour_cap:
        return answer('hour_cap', f'~{hours:.1f}h farming > hour cap {farm_hour_cap}h')
    if price is not None and plat_per_hour and hours * plat_per_hour > price:
        return answer('time_value',
                      f'~{hours:.1f}h farming × {plat_per_hour}p/h > market {price}p')
    return answer(None, f'~{hours:.1f}h farming')


def user_thresholds(user_id) -> dict:
    """The user's three buy-or-farm settings, as ``recommend()`` takes them."""
    return {key: get_setting_int(user_id, key)
            for key in ('buy_threshold_plat', 'farm_hour_cap', 'plat_per_hour')}
