"""The collection toggle rule: which flags one click writes.

`toggle_flags` is the rule itself, pure: the item's category and source, the field
toggled and the current flags in, the flags to write out. `toggle_progress` validates a
request's item and field, reads the user's row, applies the rule and writes it; the
route only parses the request and turns the result or a `ToggleError` into JSON.
"""
from models.progress import Progress
from services.request_ids import RequestIdError, require_item

TOGGLE_FIELDS = ('in_possession', 'mastered', 'subsumed', 'resurgence_watch')


class ToggleError(Exception):
    """A toggle refused before anything was written; `status` is the HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def can_subsume(category: str, source) -> bool:
    """Only a non-Prime Warframe can be subsumed."""
    return category == 'warframe' and source != 'prime'


def toggle_flags(field: str, current: dict, category: str, source) -> dict:
    """The flags one toggle of `field` writes, given the current flags.

    `current` holds `in_possession`, `mastered`, `subsumed` and `resurgence_watch`
    (a missing key counts as 0). The result names only the flags that are written; every
    other flag stays as it is. Raises `ToggleError(400)` for subsumed ON on an item that
    cannot be subsumed, and for an unknown field.
    """
    if field not in TOGGLE_FIELDS:
        raise ToggleError(400, 'invalid request')
    on = not current.get(field)
    if field == 'subsumed':
        if not on:
            return {'subsumed': 0}
        if not can_subsume(category, source):
            raise ToggleError(400, 'only a non-Prime Warframe can be subsumed')
        return {'subsumed': 1, 'mastered': 1, 'in_possession': 0}
    if field == 'mastered':
        if not on:
            return {'mastered': 0, 'subsumed': 0}
        if current.get('subsumed'):
            return {'mastered': 1}
        return {'mastered': 1, 'in_possession': 1}
    return {field: 1 if on else 0}


def toggle_progress(user_id: int, item_id, field) -> dict:
    """Apply one toggle to the user's row for the item and return the flags written.

    `item_id` must be an integer (a JSON number; never a bool or a string) and `field`
    one of `TOGGLE_FIELDS`, else `ToggleError(400)`; an unknown item, including an id
    outside the 64-bit integer range, is `ToggleError(404)`.
    Nothing is written when an error is raised.
    """
    if not isinstance(field, str) or field not in TOGGLE_FIELDS:
        raise ToggleError(400, 'invalid request')
    try:
        item = require_item(item_id)  # JSON integers only: the toggle takes no string form
    except RequestIdError as err:
        raise ToggleError(err.status, err.message) from err
    item_id = item.id
    current = Progress.get_for_user(user_id, item_id) or {}
    writes = toggle_flags(field, current, item.category, item.source)
    Progress.upsert(user_id, item_id, **writes)
    return writes
