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
    return {'items': len(plan.items)}


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


def _upsert(db, table: str, where: dict, values: dict):
    """Update ``values`` on the row matching ``where`` or insert it. Table and column
    names come from fixed lists in this module, never from the file."""
    cond = ' AND '.join(f'{c} = ?' for c in where)
    row = db.execute(f'SELECT id FROM {table} WHERE {cond}', list(where.values())).fetchone()
    if row is not None:
        if values:
            sets = ', '.join(f'{c} = ?' for c in values)
            db.execute(f'UPDATE {table} SET {sets} WHERE id = ?', [*values.values(), row['id']])
        return row['id']
    cols = {**where, **values}
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

