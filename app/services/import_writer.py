"""The write phase of the import: one validated plan, written in the caller's one
transaction (``services.import_service`` commits or rolls back). Portable SQL only, the
same statements on SQLite and MSSQL: plain SELECT, UPDATE and INSERT, no SQLite-only upsert
clause, timestamps from Python."""
from datetime import datetime, timezone

_RANKS = ['owned_rank', 'target_rank']


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def write_plan(db, user_id: int, plan) -> dict:
    items = _ItemIds(db)
    now = _now()
    _write_items(db, user_id, plan, items, now)
    goal_ids = _write_goals(db, user_id, plan, items)
    _write_progress(db, user_id, plan, goal_ids, now)
    _write_checklist(db, user_id, plan, items, now)
    return {'items': len(plan.items), 'goals': len(plan.goals),
            'progress': len(plan.progress), 'checklist': len(plan.checklist)}


class _ItemIds:
    """(name, category) -> (id, mod_max_rank); adds an unknown item to the catalogue
    as the spec asks for new items found in an import."""
    def __init__(self, db):
        self._db, self._cache = db, {}

    def get(self, name: str, cat: str):
        key = (name, cat)
        if key not in self._cache:
            row = self._lookup(name, cat)
            if row is None:
                self._db.execute('INSERT INTO items (name, category) VALUES (?, ?)', (name, cat))
                row = self._lookup(name, cat)
            self._cache[key] = (row['id'], row['mod_max_rank'])
        return self._cache[key]

    def _lookup(self, name, cat):
        return self._db.execute('SELECT id, mod_max_rank FROM items WHERE name = ? AND category = ?',
                                (name, cat)).fetchone()


def _upsert(db, table: str, where: dict, values: dict, insert_extra: dict = None):
    """Update ``values`` on the row matching ``where`` or insert it. Table and column
    names come from fixed lists in this module, never from the file."""
    cond = ' AND '.join(f'{c} = ?' for c in where)
    row = db.execute(f'SELECT id FROM {table} WHERE {cond}', list(where.values())).fetchone()
    if row is not None:
        if values:
            sets = ', '.join(f'{c} = ?' for c in values)
            db.execute(f'UPDATE {table} SET {sets} WHERE id = ?', [*values.values(), row['id']])
        return row['id']
    cols = {**where, **(insert_extra or {}), **values}
    marks = ', '.join('?' for _ in cols)
    db.execute(f'INSERT INTO {table} ({", ".join(cols)}) VALUES ({marks})', list(cols.values()))
    return None


def _write_items(db, user_id, plan, items, now):
    for (name, cat), values in plan.items.items():
        item_id, max_rank = items.get(name, cat)
        values = dict(values)
        for col in _RANKS:
            if values.get(col) is not None:
                values[col] = min(values[col], max_rank or 0)  # as the rank routes clamp
        _upsert(db, 'user_progress', {'user_id': user_id, 'item_id': item_id},
                {**values, 'updated_at': now})


def _write_goals(db, user_id, plan, items) -> dict:
    """Write the file's goals; return (name, category) -> goal id for every goal a
    progress row of the file belongs to (the import checked that each one exists)."""
    for (name, cat), values in plan.goals.items():
        item_id, _ = items.get(name, cat)
        _upsert(db, 'farming_goals', {'user_id': user_id, 'item_id': item_id}, values)
    goal_ids = {}
    for name, cat in {(k[0], k[1]) for k in plan.progress}:
        item_id, _ = items.get(name, cat)
        row = db.execute('SELECT id FROM farming_goals WHERE user_id = ? AND item_id = ?',
                         (user_id, item_id)).fetchone()
        goal_ids[(name, cat)] = row['id']
    return goal_ids


def _write_progress(db, user_id, plan, goal_ids, now):
    for (name, cat, row_type, row_key), values in plan.progress.items():
        goal_id = goal_ids[(name, cat)]
        existing = db.execute(
            '''SELECT id, acquired, comp_state FROM farming_progress
                WHERE goal_id = ? AND row_type = ? AND row_key = ?''',
            (goal_id, row_type, row_key)).fetchone()
        fields = _progress_fields(row_type, values, existing)
        _upsert(db, 'farming_progress',
                {'goal_id': goal_id, 'row_type': row_type, 'row_key': row_key},
                {**fields, 'updated_at': now}, insert_extra={'user_id': user_id})


def _progress_fields(row_type: str, values: dict, existing) -> dict:
    """The columns to write for one progress row. On a component row ``comp_state``
    decides when the file carries it and ``acquired`` mirrors it (1 for crafted); without
    it ``acquired`` 1 means crafted and 0 pending, except that an existing blueprint row
    stays blueprint (an import never downgrades a state it cannot see); with neither the
    state is untouched. An ingredient row carries no state."""
    fields = {}
    if 'quantity_owned' in values:
        fields['quantity_owned'] = values['quantity_owned']
    if row_type == 'ingredient':
        if 'acquired' in values:
            fields['acquired'] = values['acquired']
        fields['comp_state'] = None
        return fields
    if 'comp_state' in values:
        state = values['comp_state']
    elif 'acquired' in values:
        state = 'crafted' if values['acquired'] else 'pending'
        if state == 'pending' and existing is not None and existing['comp_state'] == 'blueprint':
            state = 'blueprint'
    else:
        return fields  # state untouched on an existing row, pending (defaults) on a new one
    fields['acquired'] = 1 if state == 'crafted' else 0
    fields['comp_state'] = None if state == 'pending' else state
    return fields


def _write_checklist(db, user_id, plan, items, now):
    for (name, cat, component), values in plan.checklist.items():
        item_id, _ = items.get(name, cat)
        _upsert(db, 'farming_checklist',
                {'user_id': user_id, 'item_id': item_id, 'component_name': component},
                {**values, 'updated_at': now})
