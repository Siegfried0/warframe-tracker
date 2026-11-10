"""Reading relic drop sources: which relics hold an item's parts, and where each relic drops.

A part's relics are its ``drop_sources`` rows with ``source_type = 'relic'`` (the relic in
``location``, the part in ``notes``), and its ``component`` rows whose location is a relic
(WFCD names a relic as a part's location). A relic's locations are its ``relic_drops``
rows; relic names are compared apart from case and blanks. A relic without a location
drops from no mission now (it is vaulted).
"""

from collections import defaultdict

from models.db import get_db
from services.relic_sync import clean, is_relic_name, name_key

# Best locations shown per relic (fewest average runs first)
LOCATIONS_PER_RELIC = 3
# Non-relic drop rows the drop modal shows (highest chance first)
MODAL_DROPS = 20

_CHUNK = 500


def is_relic_source(source_type, location) -> bool:
    """A relic row, or a row of another source whose location is a relic ('Lith A1 Relic'):
    a relic is no mission or node to run."""
    return source_type == 'relic' or is_relic_name(location)


def _in_chunks(db, sql: str, names: list[str]) -> list:
    """Rows of ``sql`` (one ``{}`` for the placeholders) for every name, 500 names a query."""
    rows = []
    for start in range(0, len(names), _CHUNK):
        chunk = names[start:start + _CHUNK]
        rows += db.execute(sql.format(','.join('?' for _ in chunk)), chunk).fetchall()
    return rows


def _locations(db, relic_names: list[str]) -> dict[str, list[dict]]:
    """{relic name key: its best locations, fewest average runs first}. A name spelt
    otherwise than the relic_drops rows (case, blanks) still finds them."""
    sql = '''SELECT relic_name, location, mission_type, rotation, drop_chance, avg_runs
               FROM relic_drops
              WHERE relic_name IN ({})'''
    rows = _in_chunks(db, sql, sorted(set(relic_names)))
    missing = {name_key(n) for n in relic_names} - {name_key(r['relic_name']) for r in rows}
    if missing:
        # spelt otherwise than the relic_drops rows: find their spelling there
        spellings = sorted(r['relic_name'] for r in db.execute(
            'SELECT DISTINCT relic_name FROM relic_drops').fetchall()
            if name_key(r['relic_name']) in missing)
        rows += _in_chunks(db, sql, spellings)
    rows.sort(key=lambda r: (r['avg_runs'] is None, r['avg_runs'] or 0, r['location'] or '',
                             r['rotation'] or ''))
    found: dict[str, list[dict]] = defaultdict(list)
    seen: set = set()
    for r in rows:
        key = name_key(r['relic_name'])
        place = (r['location'], r['rotation'])
        listed = found[key]
        if len(listed) < LOCATIONS_PER_RELIC and (key, place) not in seen:
            seen.add((key, place))
            listed.append({
                'location':     r['location'],
                'mission_type': r['mission_type'],
                'rotation':     r['rotation'],
                'drop_chance':  r['drop_chance'],
                'avg_runs':     r['avg_runs'],
            })
    return found


def relics_dropping_nowhere(relic_names, db=None) -> set[str]:
    """The name keys of those of ``relic_names`` that drop from no mission (no relic_drops
    row, names compared apart from case and blanks). Empty while relic_drops holds no row at
    all: drop data not synced yet says nothing about a relic being vaulted."""
    db = db or get_db()
    names = sorted({clean(n) for n in relic_names if clean(n)})
    if not names or db.execute(
            'SELECT 1 WHERE EXISTS (SELECT 1 FROM relic_drops)').fetchone() is None:
        return set()
    return {name_key(n) for n in names} - set(_locations(db, names))


def _sort_key(relic: dict):
    """Relics by their easiest location (a vaulted relic, without one, last), then by
    the higher Intact chance, then by name."""
    best = relic['locations'][0]['avg_runs'] if relic['locations'] else None
    return (best is None, best or 0, -(relic['drop_chance'] or 0), name_key(relic['relic']))


def item_relics(item_id: int, db=None) -> list[dict]:
    """Every relic holding a part of the item, each as
    {part, relic, drop_chance (Intact, %), vaulted, locations: [...]}, sorted by part, then
    relics that drop somewhere first. A part held by one relic is listed once, the relic row
    preferred over a component row naming the relic."""
    db = db or get_db()
    rows = db.execute(
        '''SELECT notes, location, drop_chance, source_type FROM drop_sources
            WHERE item_id = ? AND source_type IN ('relic', 'component')''',
        (item_id,),
    ).fetchall()
    held: dict[tuple, dict] = {}
    for r in rows:
        if not is_relic_source(r['source_type'], r['location']) or not clean(r['location']):
            continue
        key = (name_key(r['notes']), name_key(r['location']))
        if key in held and held[key]['source_type'] == 'relic':
            continue
        held[key] = {'part': clean(r['notes']), 'relic': clean(r['location']),
                     'drop_chance': r['drop_chance'], 'source_type': r['source_type']}
    locations = _locations(db, [h['relic'] for h in held.values()])
    relics = []
    for h in held.values():
        where = locations.get(name_key(h['relic']), [])
        relics.append({
            'part':        h['part'],
            'relic':       h['relic'],
            'drop_chance': h['drop_chance'],
            'vaulted':     not where,
            'locations':   where,
        })
    relics.sort(key=lambda relic: (name_key(relic['part']), _sort_key(relic)))
    return relics


def relics_by_part(item_id: int, db=None) -> dict[str, list[dict]]:
    """{part name: its relics, those that drop somewhere first}."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for relic in item_relics(item_id, db):
        grouped[relic['part']].append(relic)
    return dict(grouped)


def item_drop_sources(item_id: int) -> dict:
    """What the drop modal shows: the item's best non-relic drop rows and its relics."""
    db = get_db()
    rows = db.execute(
        '''SELECT mission_type, location, drop_chance, avg_runs, notes, source_type
             FROM drop_sources
            WHERE item_id = ? AND (source_type IS NULL OR source_type <> 'relic')
            ORDER BY drop_chance DESC''',
        (item_id,),
    ).fetchall()
    drops = [{k: r[k] for k in ('mission_type', 'location', 'drop_chance', 'avg_runs', 'notes')}
             for r in rows if not is_relic_source(r['source_type'], r['location'])]
    return {
        'drops':  drops[:MODAL_DROPS],
        'relics': item_relics(item_id, db),
    }
