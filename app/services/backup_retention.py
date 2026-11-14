"""Which backups a target keeps, which it deletes, and whether a scheduled backup is due.

Pure: no file, no network, no clock (``now`` is always passed in). A target's backups are the
names ``warframe_tracker_YYYYMMDDTHHMMSSZ.db`` (ASCII digits only, case-sensitive), dated by
the UTC time in the name. Any other name, a date that does not exist, or a time more than
10 minutes after ``now`` (the clock was set back) is not a backup: never counted, never
deleted.

Strategies:
- ``interval_count``: due when there is no backup or the newest is at least ``min_days``
  24-hour periods old (``min_days`` 0: always); keeps the ``keep`` newest.
- ``gfs``: keeps the newest backup of each of the ``gfs_daily`` most recent UTC days that have
  one, of each of the ``gfs_weekly`` most recent ISO weeks, and of each of the
  ``gfs_monthly`` most recent months, and always the newest; one backup can stand for a day,
  a week and a month at once. Due when no counted backup is at or after the start of the
  current UTC day (daily count above 0), else of the current ISO week (weekly count above 0),
  else of the current month.

``now`` must be an aware datetime; a naive one raises ``ValueError`` (it has no zone to read).
"""
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

NAME_PATTERN = re.compile(r'warframe_tracker_([0-9]{8}T[0-9]{6}Z)\.db')
PARTIAL_PATTERN = re.compile(r'warframe_tracker_([0-9]{8}T[0-9]{6}Z)\.db\.partial')
PARTIAL_SUFFIX = '.partial'
TIME_FORMAT = '%Y%m%dT%H%M%SZ'
FUTURE_ALLOWANCE = timedelta(minutes=10)   # a name this far ahead of now still counts

INTERVAL_COUNT = 'interval_count'
GFS = 'gfs'
STRATEGIES = (INTERVAL_COUNT, GFS)


class Retention(NamedTuple):
    strategy: str = INTERVAL_COUNT
    min_days: int = 1
    keep: int = 7
    gfs_daily: int = 7
    gfs_weekly: int = 4
    gfs_monthly: int = 6


class Plan(NamedTuple):
    keep: list      # names kept, newest first
    delete: list    # names to delete, newest first
    due: bool       # a scheduled backup is due


def _parse(pattern, name):
    match = pattern.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:       # a date or time that does not exist (20260230, hour 24)
        return None


def parse_name(name):
    """The UTC time of a backup's name, or None when the name is not a backup's."""
    return _parse(NAME_PATTERN, name)


def parse_partial(name):
    """The UTC time of a backup's temporary name, or None."""
    return _parse(PARTIAL_PATTERN, name)


def backup_name(now: datetime) -> str:
    return 'warframe_tracker_' + _utc(now).strftime(TIME_FORMAT) + '.db'


def partial_name(now: datetime) -> str:
    return backup_name(now) + PARTIAL_SUFFIX


def _utc(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be an aware datetime')
    return now.astimezone(timezone.utc)


def _day(moment):
    return moment.date()


def _week(moment):
    year, week, _ = moment.isocalendar()
    return year, week


def _month(moment):
    return moment.year, moment.month


def _day_start(now):
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _week_start(now):          # ISO weeks begin on Monday
    return _day_start(now) - timedelta(days=now.weekday())


def _month_start(now):
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _newest_per_period(backups, period, count):
    """The newest backup of each of the ``count`` most recent periods that have one."""
    chosen, seen = [], set()
    for moment, name in backups:
        if len(seen) >= count:
            break
        key = period(moment)
        if key not in seen:
            seen.add(key)
            chosen.append(name)
    return chosen


def plan(names, retention: Retention, now: datetime) -> Plan:
    """The names a target keeps and deletes, and whether a scheduled backup is due. Names that
    are not backups (see the module) are in neither list."""
    now = _utc(now)
    dated = {}
    for name in names:
        moment = parse_name(name)
        if moment is not None and moment <= now + FUTURE_ALLOWANCE:
            dated[name] = moment
    backups = sorted(((m, n) for n, m in dated.items()), reverse=True)

    if retention.strategy == GFS:
        kept = {backups[0][1]} if backups else set()   # the newest is always kept
        for period, count in ((_day, retention.gfs_daily), (_week, retention.gfs_weekly),
                              (_month, retention.gfs_monthly)):
            kept.update(_newest_per_period(backups, period, count))
        if retention.gfs_daily > 0:
            start = _day_start(now)
        elif retention.gfs_weekly > 0:
            start = _week_start(now)
        else:
            start = _month_start(now)
        # A counted backup at or after the period's start, also one a few minutes ahead of
        # now and so in the next period, means this period has its backup.
        due = not backups or backups[0][0] < start
    else:
        kept = {name for _, name in backups[:retention.keep]}
        due = (retention.min_days == 0 or not backups
               or now - backups[0][0] >= timedelta(days=retention.min_days))

    return Plan(keep=[n for _, n in backups if n in kept],
                delete=[n for _, n in backups if n not in kept],
                due=due)
