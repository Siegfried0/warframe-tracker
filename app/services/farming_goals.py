"""
Goal management: searching for goal items, a user's goal list, adding, removing, updating
and reordering goals.
"""

from flask import current_app

from models.db import get_db
from services.farming_progress import _compute_progress_pct

# Categories whose items can be farming goals (excludes raw resources)
_GOAL_CATEGORIES = (
    'warframe', 'primary', 'secondary', 'melee',
    'companion', 'archwing', 'arch-gun', 'arch-melee', 'k-drive',
)


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
