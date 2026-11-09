"""Void relic drop sources, read from the drop tables the drop sync already fetches.

These sections of the one drop-table response are read:

- ``relics``: every relic per refinement state with its rewards. Each reward of a relic's
  *Intact* list whose name starts with a catalogue item's name (an item that is built from
  parts) becomes one ``drop_sources`` row of that item: ``source_type = 'relic'``, the
  relic's name as ``location``, the Intact chance in percent, the part in ``notes``.
- ``missionRewards``: every reward entry whose name is a relic becomes a ``relic_drops``
  row (relic, location, mission type, rotation, chance, average runs).
- the bounty reward sections (``BOUNTY_SECTIONS``): every relic reward of a bounty becomes a
  ``relic_drops`` row too, with the bounty and its hub as ``location`` ('Level 5 - 15 Cetus
  Bounty — Cetus'), ``Bounty`` as ``mission_type`` and the rotation and stage as ``rotation``
  ('A · Stage 2'); its average runs count bounty stages completed.

Both tables are catalogue data and are rebuilt, never appended to. When the response is
missing, or ``relics`` or ``missionRewards`` has the wrong shape, no relic row is written at
all: relic contents without locations would show every relic as dropping nowhere. A bounty
section that is missing or has the wrong shape is skipped with a warning: bounty locations
only add to the mission locations.
"""

import logging
import math

log = logging.getLogger(__name__)

# Categories whose items are built from parts a relic can hold. Resources (a relic's
# Forma Blueprint), mods and arcanes are never matched.
RELIC_CATEGORIES = ('warframe', 'primary', 'secondary', 'melee', 'companion',
                    'archwing', 'arch-gun', 'arch-melee', 'k-drive')

RELIC_MISSION_TYPE = 'Relic'

# The bounty reward sections read for relic locations, each with the hub its bounties are
# taken at. A fixed list: no other section of the response is read.
BOUNTY_SECTIONS = (
    ('cetusBountyRewards', 'Cetus'),
    ('solarisBountyRewards', 'Orb Vallis'),
    ('deimosRewards', 'Cambion Drift'),
    ('zarimanRewards', 'Zariman'),
    ('entratiLabRewards', 'Sanctum Anatomica'),
    ('hexRewards', 'Höllvania'),
)
BOUNTY_MISSION_TYPE = 'Bounty'

# Widths of the relic_drops columns a bounty row fills from free text (the MSSQL widths)
LOCATION_WIDTH = 500
ROTATION_WIDTH = 50


class DropTableShapeError(ValueError):
    """A section of the drop tables does not have the shape this module reads."""


def clean(text) -> str:
    """The text with outer blanks removed and every run of blanks made one space;
    '' for anything that is not a string."""
    if not isinstance(text, str):
        return ''
    return ' '.join(text.split())


def name_key(text) -> str:
    """The form two names are compared in: cleaned and case-folded."""
    return clean(text).casefold()


def drop_chance(value) -> float | None:
    """A drop chance in percent, or None unless it is a number above 0 and at most 100."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:       # an integer too large for a float
        return None
    if not math.isfinite(value) or value <= 0 or value > 100:
        return None
    return value


def is_relic_name(name: str) -> bool:
    words = name_key(name).split(' ')
    return len(words) > 1 and words[-1] == 'relic'


def relic_name(tier, name) -> str:
    """The relic's full name as the mission tables spell it ('Lith A1 Relic'), or ''."""
    tier, name = clean(tier), clean(name)
    if not tier or not name:
        return ''
    full = f'{tier} {name}'
    return full if is_relic_name(full) else f'{full} Relic'


def parse_relic_contents(relics) -> dict[str, tuple[str, list[tuple[str, float]]]]:
    """{relic key: (relic name, [(reward name, Intact chance), ...])} from the ``relics``
    section. Entries of another state, without a name, or rewards without a name or a
    valid chance are skipped; a section that is not a list raises DropTableShapeError."""
    if not isinstance(relics, list):
        raise DropTableShapeError('relics is not a list')
    contents: dict[str, tuple[str, list[tuple[str, float]]]] = {}
    for entry in relics:
        if not isinstance(entry, dict) or name_key(entry.get('state')) != 'intact':
            continue
        name = relic_name(entry.get('tier'), entry.get('relicName'))
        rewards = entry.get('rewards')
        if not name or not isinstance(rewards, list):
            continue
        listed = contents.setdefault(name_key(name), (name, []))[1]
        for reward in rewards:
            if not isinstance(reward, dict):
                continue
            reward_name, chance = clean(reward.get('itemName')), drop_chance(reward.get('chance'))
            if reward_name and chance is not None:
                listed.append((reward_name, chance))
    return contents


def _reward_lists(rewards) -> list[tuple[str | None, list]]:
    """[(rotation or None, reward list)] of one node: a flat list, or lists by rotation."""
    if isinstance(rewards, list):
        return [(None, rewards)]
    if isinstance(rewards, dict):
        return [(clean(rot) or None, lst) for rot, lst in rewards.items() if isinstance(lst, list)]
    return []


def parse_relic_locations(mission_rewards) -> list[dict]:
    """Every relic reward of the ``missionRewards`` section, one dict per (relic, location,
    rotation): relic, location ('Node — Planet'), mission_type, rotation, drop_chance.
    A section that is not a mapping raises DropTableShapeError."""
    if not isinstance(mission_rewards, dict):
        raise DropTableShapeError('missionRewards is not a mapping')
    found: dict[tuple[str, str, str], dict] = {}
    for planet, nodes in mission_rewards.items():
        if not isinstance(nodes, dict):
            continue
        for node, node_data in nodes.items():
            if not isinstance(node_data, dict) or not clean(node) or not clean(planet):
                continue
            location = f'{clean(node)} — {clean(planet)}'
            mission_type = clean(node_data.get('gameMode')) or 'Unknown'
            for rotation, rewards in _reward_lists(node_data.get('rewards')):
                for reward in rewards:
                    if not isinstance(reward, dict):
                        continue
                    name = clean(reward.get('itemName'))
                    chance = drop_chance(reward.get('chance'))
                    if not is_relic_name(name) or chance is None:
                        continue
                    key = (name_key(name), name_key(location), rotation or '')
                    if key not in found or chance > found[key]['drop_chance']:
                        found[key] = {'relic': name, 'location': location,
                                      'mission_type': mission_type, 'rotation': rotation,
                                      'drop_chance': chance}
    return list(found.values())


def clip(text: str, width: int) -> str:
    """The text, cut to at most ``width`` characters with '…' at the end when it is longer."""
    return text if len(text) <= width else text[:width - 1].rstrip() + '…'


def bounty_rotation(rotation, stage) -> str | None:
    """A bounty reward's rotation and stage as one ``rotation`` value ('A · Stage 2', 'A',
    'Stage 2'), at most ROTATION_WIDTH characters; None when it has neither."""
    said = [part for part in (clean(rotation), clean(stage)) if part]
    return clip(' · '.join(said), ROTATION_WIDTH) if said else None


def parse_bounty_locations(section, hub: str) -> list[dict]:
    """Every relic reward of one bounty reward section, one dict per (relic, location,
    rotation) with the highest chance: relic, location ('<bounty> — <hub>'), mission_type
    ('Bounty'), rotation (rotation and stage), drop_chance.

    The section is a list of bounties, each with its name in ``bountyLevel`` (or ``name``)
    and its rewards as a list or as lists by rotation; a reward has ``itemName``,
    ``chance`` and an optional ``stage``. Bounties without a name and rewards without a
    relic name or a valid chance are skipped; a section that is not a list raises
    DropTableShapeError."""
    if not isinstance(section, list):
        raise DropTableShapeError('is not a list')
    found: dict[tuple[str, str, str], dict] = {}
    for entry in section:
        if not isinstance(entry, dict):
            continue
        bounty = clean(entry.get('bountyLevel')) or clean(entry.get('name'))
        if not bounty:
            continue
        location = clip(f'{bounty} — {hub}', LOCATION_WIDTH)
        for rotation, rewards in _reward_lists(entry.get('rewards')):
            for reward in rewards:
                if not isinstance(reward, dict):
                    continue
                name = clean(reward.get('itemName'))
                chance = drop_chance(reward.get('chance'))
                if not is_relic_name(name) or chance is None:
                    continue
                where = bounty_rotation(rotation, reward.get('stage'))
                key = (name_key(name), name_key(location), name_key(where))
                if key not in found or chance > found[key]['drop_chance']:
                    found[key] = {'relic': name, 'location': location,
                                  'mission_type': BOUNTY_MISSION_TYPE, 'rotation': where,
                                  'drop_chance': chance}
    return list(found.values())


def bounty_locations(tables: dict) -> list[dict]:
    """The relic locations of every bounty reward section in BOUNTY_SECTIONS. A section that
    is missing or not a list is skipped with a warning; one line logs what each section gave."""
    found: list[dict] = []
    said: list[str] = []
    for section, hub in BOUNTY_SECTIONS:
        try:
            if section not in tables:
                raise DropTableShapeError('is missing')
            locations = parse_bounty_locations(tables[section], hub)
        except DropTableShapeError as exc:
            log.warning('Relic locations from %s not read (non-fatal): %s %s',
                        section, section, exc)
            said.append(f'{section} skipped')
            continue
        found += locations
        said.append(f'{section} {len(locations)}')
    log.info('Relic locations from bounties: %s', ', '.join(said))
    return found


def item_lookup(db) -> list[tuple[list[str], int]]:
    """(name words case-folded, item id) of every item a relic part can belong to, the
    longest name first, so 'Ash Prime ...' resolves to Ash Prime and never to Ash."""
    placeholders = ','.join('?' for _ in RELIC_CATEGORIES)
    rows = db.execute(f'SELECT id, name FROM items WHERE category IN ({placeholders})',
                      RELIC_CATEGORIES).fetchall()
    lookup = [(name_key(r['name']).split(' '), r['id']) for r in rows if clean(r['name'])]
    lookup.sort(key=lambda entry: (-len(entry[0]), entry[1]))
    return lookup


def match_part(reward_name: str, lookup) -> tuple[int, str] | None:
    """(item id, part name) for a relic reward, or None when it is no part of an item.

    The part is what follows the item's name, with a trailing 'Blueprint' dropped unless
    it is the whole part ('Neuroptics Blueprint' -> 'Neuroptics', 'Blueprint' stays): the
    names WFCD gives components. The item itself or its set ('... Set') is no part, nor is
    a Prime of an item whose Prime the catalogue lacks ('Gyre Prime Systems' under Gyre)."""
    words = clean(reward_name).split(' ')
    folded = [w.casefold() for w in words]
    for item_words, item_id in lookup:
        if folded[:len(item_words)] != item_words:
            continue
        part = words[len(item_words):]
        if len(part) > 1 and part[-1].casefold() == 'blueprint':
            part = part[:-1]
        if not part or [w.casefold() for w in part] == ['set']:
            return None
        if part[0].casefold() == 'prime' and item_words[-1] != 'prime':
            return None
        return item_id, ' '.join(part)
    return None


def _part_spellings(db) -> dict[int, dict[str, str]]:
    """{item id: {part key: the part's name as the item's component rows spell it}}."""
    spellings: dict[int, dict[str, str]] = {}
    for sql in ("SELECT item_id, notes AS part FROM drop_sources WHERE source_type = 'component'",
                'SELECT item_id, component_name AS part FROM component_ingredients',
                'SELECT item_id, component_name AS part FROM item_component_meta'):
        for r in db.execute(sql).fetchall():
            if clean(r['part']):
                spellings.setdefault(r['item_id'], {}).setdefault(name_key(r['part']), r['part'])
    return spellings


def _write_locations(db, locations: list[dict], names: dict[str, str]) -> None:
    for loc in locations:
        chance = loc['drop_chance']
        db.execute(
            '''INSERT INTO relic_drops
                   (relic_name, location, mission_type, rotation, drop_chance, avg_runs)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (names.get(name_key(loc['relic']), loc['relic']), loc['location'],
             loc['mission_type'], loc['rotation'], round(chance, 4), round(100.0 / chance, 1)),
        )


def _relic_rows(db, contents) -> dict[tuple[int, str, str], tuple[str, str, float]]:
    """{(item id, part key, relic key): (part name, relic name, best Intact chance)}."""
    lookup, spellings = item_lookup(db), _part_spellings(db)
    rows: dict[tuple[int, str, str], tuple[str, str, float]] = {}
    for relic_key, (relic, rewards) in contents.items():
        for reward_name, chance in rewards:
            match = match_part(reward_name, lookup)
            if match is None:
                continue
            item_id, part = match
            part = spellings.get(item_id, {}).get(name_key(part), part)
            key = (item_id, name_key(part), relic_key)
            if key not in rows or chance > rows[key][2]:
                rows[key] = (part, relic, chance)
    return rows


def _drop_listed_twice(db, rows) -> None:
    """Remove the WFCD component rows that name the same item, part and relic as a relic
    row: one row per (item, part, relic), whichever source also lists it."""
    item_ids = sorted({item_id for item_id, _, _ in rows})
    for start in range(0, len(item_ids), 500):
        chunk = item_ids[start:start + 500]
        placeholders = ','.join('?' for _ in chunk)
        for r in db.execute(
                f'''SELECT id, item_id, notes, location FROM drop_sources
                     WHERE source_type = 'component' AND item_id IN ({placeholders})''',
                chunk).fetchall():
            if (r['item_id'], name_key(r['notes']), name_key(r['location'])) in rows:
                db.execute('DELETE FROM drop_sources WHERE id = ?', (r['id'],))


def sync_relic_drops(db, tables) -> int:
    """Rebuild relic_drops and add the relic rows of drop_sources from the drop tables.

    The caller has emptied drop_sources and commits (or rolls back) the whole rebuild.
    ``tables`` is the parsed drop-table response, or None when it could not be read.
    Returns the number of relic rows written to drop_sources."""
    db.execute('DELETE FROM relic_drops')
    if not isinstance(tables, dict):
        return 0
    try:
        contents = parse_relic_contents(tables.get('relics'))
        locations = parse_relic_locations(tables.get('missionRewards'))
    except DropTableShapeError as exc:
        log.warning('Relic drop tables not read (non-fatal): %s', exc)
        return 0
    bounties = bounty_locations(tables)

    _write_locations(db, locations + bounties,
                     {key: name for key, (name, _) in contents.items()})
    rows = _relic_rows(db, contents)
    _drop_listed_twice(db, rows)
    for (item_id, _, _), (part, relic, chance) in rows.items():
        db.execute(
            '''INSERT INTO drop_sources
                   (item_id, mission_type, location, drop_chance,
                    avg_runs, source_type, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (item_id, RELIC_MISSION_TYPE, relic, round(chance, 4), None, 'relic', part),
        )
    log.info('Relic drops: %d relic rows, %d mission locations, %d bounty locations',
             len(rows), len(locations), len(bounties))
    return len(rows)
