"""Which backups a target keeps and deletes, and when a scheduled backup is due, on a synthetic
calendar: several backups in a day, days without one, month and year ends, ISO week 53, a gap
of months, GFS counts of 0 and 1, the promotion, and names that are not backups."""
from datetime import datetime, timedelta, timezone

import pytest

from services.backup_retention import (GFS, INTERVAL_COUNT, Retention, backup_name,
                                       parse_name, parse_partial, partial_name, plan)

UTC = timezone.utc
FOREIGN = ['notes.txt', 'warframe_tracker_20260230T120000Z.db',
           'Warframe_Tracker_20261101T120000Z.db', 'warframe_tracker_20261101T120000Z.DB',
           'warframe_tracker_20261101T120000Z.db.partial', 'warframe_tracker_20261101T240000Z.db']


def B(stamp):
    return f'warframe_tracker_{stamp}Z.db'


def at(stamp):
    return datetime.strptime(stamp, '%Y%m%dT%H%M%S').replace(tzinfo=UTC)


def gfs(daily, weekly, monthly):
    return Retention(strategy=GFS, gfs_daily=daily, gfs_weekly=weekly, gfs_monthly=monthly)


def run(stamps, retention, now):
    """The plan of these backups plus foreign names; checks that the foreign names are in
    neither list and that kept and deleted split the backups."""
    result = plan([B(s) for s in stamps] + FOREIGN, retention, at(now))
    assert not set(FOREIGN) & set(result.keep + result.delete)
    assert sorted(result.keep + result.delete) == sorted(B(s) for s in stamps)
    return {n[17:32] for n in result.keep}, result.due


# ── Names ────────────────────────────────────────────────────────────────────

# strptime reads fullwidth digits in %Y (its other fields take ASCII ranges): only the
# pattern's [0-9] keeps this name out.
FULLWIDTH = ''.join(chr(0xFF10 + int(d)) for d in '2026') + '1114'
NAMES = [
    ('valid', B('20261114T120000'), at('20261114T120000')),
    ('leap day', B('20240229T235959'), at('20240229T235959')),
    ('upper-case prefix', 'Warframe_Tracker_20261114T120000Z.db', None),
    ('upper-case suffix', 'warframe_tracker_20261114T120000Z.DB', None),
    ('suffix after .db', 'warframe_tracker_20261114T120000Z.db.bak', None),
    ('temporary name', 'warframe_tracker_20261114T120000Z.db.partial', None),
    ('no Z', 'warframe_tracker_20261114T120000.db', None),
    ('lower-case z', 'warframe_tracker_20261114T120000z.db', None),
    ('trailing newline', B('20261114T120000') + '\n', None),
    ('leading space', ' ' + B('20261114T120000'), None),
    ('February 30', B('20260230T120000'), None),
    ('February 29 of a common year', B('20260229T120000'), None),
    ('hour 24', B('20261114T240000'), None),
    ('second 60', B('20261114T235960'), None),
    ('fullwidth digits', f'warframe_tracker_{FULLWIDTH}T120000Z.db', None),
    ('a prefix of a name', 'warframe_tracker_20261114T120000Z.d', None),
    ('seven-digit date', 'warframe_tracker_2026111T120000Z.db', None),
    ('lone surrogate', 'warframe_tracker_\udc80.db', None),
    ('empty', '', None),
    ('not text', None, None),
    ('bytes', B('20261114T120000').encode(), None),
    ('Arabic-Indic digit', 'warframe_tracker_2026111' + chr(0x0664) + 'T120000Z.db', None),
    ('a backup name inside a longer one', 'x' + B('20261114T120000'), None),
]


@pytest.mark.parametrize('label,name,expected', NAMES, ids=[r[0] for r in NAMES])
def test_only_names_with_a_real_utc_time_are_backups(label, name, expected):
    assert parse_name(name) == expected


def test_new_names_are_lower_case_utc_and_the_temporary_name_is_not_a_backup():
    moment = datetime(2026, 11, 15, 1, 30, 5, tzinfo=timezone(timedelta(hours=2)))
    assert backup_name(moment) == 'warframe_tracker_20261114T233005Z.db'
    assert partial_name(moment) == 'warframe_tracker_20261114T233005Z.db.partial'
    assert parse_name(partial_name(moment)) is None
    assert parse_partial(partial_name(moment)) == at('20261114T233005')
    assert parse_partial(backup_name(moment)) is None


# ── Interval + count ─────────────────────────────────────────────────────────

TEN = [f'202611{d:02d}T120000' for d in range(1, 11)]
IC = Retention(strategy=INTERVAL_COUNT, min_days=1, keep=7)
INTERVAL = [
    ('keeps the 7 newest', TEN, IC, '20261110T130000', set(TEN[3:]), False),
    ('keep 1', TEN, IC._replace(keep=1), '20261110T130000', {TEN[-1]}, False),
    ('keep above the count', TEN[:3], IC, '20261110T130000', set(TEN[:3]), True),
    ('no backup: due', [], IC, '20261110T130000', set(), True),
    ('newest 1 s short of a day', ['20261109T130001'], IC, '20261110T130000',
     {'20261109T130001'}, False),
    ('newest exactly a day old', ['20261109T130000'], IC, '20261110T130000',
     {'20261109T130000'}, True),
    ('min_days 3, 2 days', ['20261108T130000'], IC._replace(min_days=3), '20261110T130000',
     {'20261108T130000'}, False),
    ('min_days 0 with a backup this second', ['20261110T130000'], IC._replace(min_days=0),
     '20261110T130000', {'20261110T130000'}, True),
    ('newest 10 min ahead counts', ['20261110T131000'], IC, '20261110T130000',
     {'20261110T131000'}, False),
    ('several in one day keep by count', ['20261110T010000', '20261110T020000',
                                          '20261110T030000'], IC._replace(keep=2),
     '20261110T040000', {'20261110T020000', '20261110T030000'}, False),
]


@pytest.mark.parametrize('label,stamps,retention,now,kept,due', INTERVAL,
                         ids=[r[0] for r in INTERVAL])
def test_interval_count_keeps_the_newest_and_is_due_after_min_days(label, stamps, retention,
                                                                   now, kept, due):
    assert run(stamps, retention, now) == (kept, due)


def test_a_name_more_than_10_minutes_ahead_is_neither_counted_nor_deleted():
    ahead = B('20261110T131001')
    result = plan([ahead, B('20261101T120000')], IC._replace(keep=1), at('20261110T130000'))
    assert result.keep == [B('20261101T120000')] and result.delete == []
    assert result.due is True


# ── GFS ──────────────────────────────────────────────────────────────────────

GFS_CASES = [
    ('several in one day: the day keeps its newest',
     ['20261114T010000', '20261114T120000', '20261114T230000'], gfs(7, 0, 0),
     '20261114T235900', {'20261114T230000'}, False),
    ('days without a backup are not counted',
     ['20261110T120000', '20261112T120000', '20261113T120000'], gfs(2, 0, 0),
     '20261114T120000', {'20261113T120000', '20261112T120000'}, True),
    ('month ends of 31, 28, 30 days',
     ['20260131T235959', '20260201T000000', '20260228T235959', '20260301T000000',
      '20260401T000000', '20260630T235959', '20260701T000000'], gfs(0, 0, 6),
     '20260701T010000',
     {'20260701T000000', '20260630T235959', '20260401T000000', '20260301T000000',
      '20260228T235959', '20260131T235959'}, False),
    ('month end of 29 days (leap year)',
     ['20240228T120000', '20240229T235959', '20240301T000000'], gfs(0, 0, 2),
     '20240301T010000', {'20240301T000000', '20240229T235959'}, False),
    ('year end by month',
     ['20261231T235959', '20261215T120000', '20270101T000000'], gfs(0, 0, 2),
     '20270101T010000', {'20270101T000000', '20261231T235959'}, False),
    ('ISO week 53 (2026-12-28 to 2027-01-03) is one week',
     ['20261227T120000', '20261228T000000', '20261231T235959', '20270103T235959',
      '20270104T000000'], gfs(0, 3, 0), '20270104T010000',
     {'20270104T000000', '20270103T235959', '20261227T120000'}, False),
    ('year end by day', ['20261231T120000', '20270101T120000'], gfs(1, 0, 0),
     '20270101T130000', {'20270101T120000'}, False),
    ('a gap of months keeps the last days that have a backup',
     ['20260115T120000', '20260120T120000', '20260601T120000'], gfs(2, 0, 0),
     '20260602T120000', {'20260601T120000', '20260120T120000'}, True),
    ('daily 1', TEN, gfs(1, 0, 0), '20261110T130000', {TEN[-1]}, False),
    ('weekly 1', TEN, gfs(0, 1, 0), '20261110T130000', {TEN[-1]}, False),
    ('monthly 1', TEN, gfs(0, 0, 1), '20261110T130000', {TEN[-1]}, False),
    ('weekly 2 across a Sunday', TEN, gfs(0, 2, 0), '20261110T130000',
     {'20261110T120000', '20261108T120000'}, False),
    ('one backup stands for day, week and month', ['20261114T120000'], gfs(7, 4, 6),
     '20261114T130000', {'20261114T120000'}, False),
    ('promotion: day, week and month slots together',
     ['20261031T120000', '20261101T120000', '20261102T120000', '20261103T120000',
      '20261104T120000'], gfs(2, 2, 2), '20261104T130000',
     {'20261104T120000', '20261103T120000', '20261101T120000', '20261031T120000'}, False),
]


@pytest.mark.parametrize('label,stamps,retention,now,kept,due', GFS_CASES,
                         ids=[r[0] for r in GFS_CASES])
def test_gfs_keeps_the_newest_of_the_most_recent_periods(label, stamps, retention, now,
                                                         kept, due):
    assert run(stamps, retention, now) == (kept, due)


def test_a_days_newest_stays_as_its_weeks_newest_after_it_leaves_the_daily_set():
    week44 = ['20261029T120000', '20261101T120000']           # Thursday and Sunday of W44
    later = ['20261102T120000', '20261103T120000', '20261104T120000']
    kept, _ = run(week44, gfs(2, 2, 0), '20261101T130000')
    assert '20261101T120000' in kept                           # as a day's newest
    kept, _ = run(week44 + later, gfs(2, 2, 0), '20261104T130000')
    assert kept == {'20261104T120000', '20261103T120000', '20261101T120000'}


DUE = [
    ('daily: one today', ['20261114T000000'], gfs(7, 4, 6), '20261114T235959', False),
    ('daily: last yesterday', ['20261113T235959'], gfs(7, 4, 6), '20261114T000000', True),
    ('weekly: Monday, now Sunday', ['20261109T000000'], gfs(0, 4, 6), '20261115T235959',
     False),
    ('weekly: last week', ['20261108T235959'], gfs(0, 4, 6), '20261109T000000', True),
    ('weekly: week 53 across the new year', ['20261228T000000'], gfs(0, 4, 0),
     '20270103T235959', False),
    ('weekly: week 1 after week 53', ['20270103T235959'], gfs(0, 4, 0), '20270104T000000',
     True),
    ('monthly: the 1st, now the 30th', ['20261101T000000'], gfs(0, 0, 6), '20261130T235959',
     False),
    ('monthly: last month', ['20261130T235959'], gfs(0, 0, 6), '20261201T000000', True),
    ('none at all', [], gfs(7, 4, 6), '20261114T120000', True),
    # A counted backup a few minutes ahead of now, in the next period, still fills this one.
    ('daily: one 7 min into tomorrow', ['20261115T000500'], gfs(7, 4, 6), '20261114T235800',
     False),
    ('weekly: one 7 min into next week', ['20261116T000500'], gfs(0, 4, 6), '20261115T235800',
     False),
    ('monthly: one 7 min into next month', ['20261201T000500'], gfs(0, 0, 6),
     '20261130T235800', False),
]


@pytest.mark.parametrize('label,stamps,retention,now,due', DUE, ids=[r[0] for r in DUE])
def test_gfs_is_due_when_the_smallest_counted_period_has_no_backup(label, stamps, retention,
                                                                   now, due):
    assert run(stamps, retention, now)[1] is due


def test_the_utc_day_decides_whatever_the_zone_of_now():
    now = datetime(2026, 11, 15, 0, 30, tzinfo=timezone(timedelta(hours=1)))  # 23:30 UTC
    assert plan([B('20261114T120000')], gfs(7, 0, 0), now).due is False


@pytest.mark.parametrize('retention', [gfs(2, 2, 2), gfs(7, 4, 6), IC._replace(keep=3)],
                         ids=['gfs 2/2/2', 'gfs defaults', 'interval_count keep 3'])
def test_retention_run_twice_on_its_own_result_deletes_nothing_more(retention):
    now = at('20261110T130000')
    first = plan([B(s) for s in TEN], retention, now)
    second = plan(first.keep, retention, now)
    assert second.keep == first.keep and second.delete == []


def test_a_name_more_than_10_minutes_ahead_does_not_make_gfs_not_due():
    result = plan([B('20261115T000900')], gfs(7, 4, 6), at('20261114T235800'))
    assert result == ([], [], True)


def test_a_name_up_to_10_minutes_ahead_fills_a_gfs_slot_and_is_kept():
    result = plan([B('20261110T130500'), B('20261110T120000')], gfs(1, 0, 0),
                  at('20261110T130000'))
    assert result.keep == [B('20261110T130500')]
    assert result.delete == [B('20261110T120000')]


def test_the_newest_is_kept_even_when_no_slot_would_keep_it():
    # Counts are never all 0 after the settings reader, but the rule holds on its own.
    result = plan([B('20261110T120000'), B('20261109T120000')], gfs(0, 0, 0),
                  at('20261110T130000'))
    assert result.keep == [B('20261110T120000')]


@pytest.mark.parametrize('now', [datetime(2026, 11, 10, 13), '20261110T130000', None],
                         ids=['naive datetime', 'text', 'None'])
def test_now_without_a_zone_is_refused(now):
    with pytest.raises(ValueError):
        plan([B('20261110T120000')], IC, now)
    with pytest.raises(ValueError):
        backup_name(now)
