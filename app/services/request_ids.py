"""The one check for an id or integer a request passes to SQL.

SQLite's INTEGER and MSSQL's BIGINT hold a signed 64-bit integer; the drivers refuse a
larger Python int with an error (a 500), so every request value is checked here first.

The spellings an item id may take:

* a JSON integer (never a bool, never a float, not even 1.0), and
* where `strings=True`, also a string of ASCII digits `[0-9]+` (no sign, no blanks, no
  other script's digits): the note dialog and the farming checkboxes send strings.

Anything else (null, a list, an object, "", "abc", "-1", " 1", a float) is malformed: 400.
An integer that is well formed but names no item (0, a negative, a value outside the
64-bit range) is unknown: 404. The check runs before any SQL, so a refused request
writes nothing.
"""
import re

from werkzeug.routing import BaseConverter, ValidationError

from models.item import Item

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

_ASCII_DIGITS = re.compile(r'[0-9]+')
# More significant digits than INT64_MAX has can only be out of range; checked before
# int() so a very long string never reaches Python's int-string length limit.
_INT64_MAX_DIGITS = 19


class RequestIdError(Exception):
    """A request value refused before any SQL; `status` is the HTTP status (400 or 404)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def in_int64(value: int) -> bool:
    """True when `value` fits SQL's signed 64-bit INTEGER / BIGINT."""
    return INT64_MIN <= value <= INT64_MAX


def _digits_to_int(text: str):
    """The value of a string of ASCII digits, or None when it lies outside the 64-bit range."""
    significant = text.lstrip('0')
    if len(significant) > _INT64_MAX_DIGITS:
        return None
    value = int(significant or '0')
    return value if value <= INT64_MAX else None


def parse_item_id(value, *, strings: bool = False) -> int:
    """The item id a request value spells, checked for form and range, not for existence.

    Raises `RequestIdError(400)` for a malformed value and `RequestIdError(404)` for a
    well-formed one outside the signed 64-bit range (it cannot name a row).
    """
    if isinstance(value, bool):
        raise RequestIdError(400, 'invalid item_id')
    if isinstance(value, int):
        if not in_int64(value):
            raise RequestIdError(404, 'item not found')
        return value
    if strings and isinstance(value, str) and _ASCII_DIGITS.fullmatch(value):
        number = _digits_to_int(value)
        if number is None:
            raise RequestIdError(404, 'item not found')
        return number
    raise RequestIdError(400, 'invalid item_id')


def require_item(value, *, strings: bool = False) -> Item:
    """The existing item a request value names; `RequestIdError` 400 or 404 otherwise."""
    item = Item.get_by_id(parse_item_id(value, strings=strings))
    if not item:
        raise RequestIdError(404, 'item not found')
    return item


def json_object(data) -> dict:
    """The parsed JSON body when it is an object; `RequestIdError(400)` otherwise."""
    if not isinstance(data, dict):
        raise RequestIdError(400, 'invalid request')
    return data


def item_ids_in(values) -> list:
    """The well-formed, in-range, non-negative item ids of a list, in order.

    For the farming plan's `item_ids`: an entry that is not a JSON integer or a string of
    ASCII digits, a negative one, or one outside the 64-bit range is dropped, not refused,
    so one stale checkbox cannot fail the whole plan. A value that is not a list gives none.
    """
    if not isinstance(values, list):
        return []
    ids = []
    for value in values:
        try:
            item_id = parse_item_id(value, strings=True)
        except RequestIdError:
            continue
        if item_id >= 0:
            ids.append(item_id)
    return ids


class Id64Converter(BaseConverter):
    """The URL converter `<id64:name>` for a row id in a path.

    Matches ASCII digits only and a value up to the signed 64-bit maximum; a longer or
    larger value does not match, so the URL answers 404 instead of reaching SQL.
    """

    regex = r'[0-9]+'
    weight = 50  # as the built-in int converter

    def to_python(self, value: str) -> int:
        number = _digits_to_int(value)
        if number is None:
            raise ValidationError()
        return number

    def to_url(self, value) -> str:
        return str(int(value))


def int64(value) -> int:
    """`int(value)` as the farming routes take it, refused outside the 64-bit range.

    Accepts what `int()` accepts (so a bool, a numeric string or a float truncates as
    before); raises `ValueError` for anything `int()` refuses, for an infinity and for a
    result outside the signed 64-bit range.
    """
    try:
        number = int(value)
    except OverflowError as err:  # int(float('inf')): JSON's Infinity parses as a float
        raise ValueError('not a finite integer') from err
    if not in_int64(number):
        raise ValueError('outside the 64-bit integer range')
    return number
