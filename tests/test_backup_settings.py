"""A target's retention read from the global settings: absent keys and NULL values give the
defaults, a stored value that is not a valid whole number within its bounds, or an unknown
strategy, gives the default and a log line (once per stored text), and a failed read is
raised, never replaced by defaults."""
import logging

import pytest

import services.backup_settings as backup_settings
from models.db import get_db
from services.auth_service import upsert_global_setting
from services.backup_retention import Retention
from services.backup_settings import read_retention

DEFAULT = Retention()   # interval_count, min_days 1, keep 7, GFS 7 / 4 / 6


@pytest.fixture(autouse=True)
def fresh_log_memory():
    """Each test sees a process that has logged nothing yet."""
    backup_settings._logged.clear()
    yield
    backup_settings._logged.clear()


def reader(values):
    return lambda key: values.get(key)


def read(target='local', **stored):
    return read_retention(target, reader({f'backup_{target}_{k}': v for k, v in stored.items()}))


def test_absent_keys_give_todays_behaviour(caplog):
    with caplog.at_level(logging.WARNING):
        assert read() == DEFAULT == Retention('interval_count', 1, 7, 7, 4, 6)
    assert caplog.records == []


NUMBERS = [
    ('plain', '30', 30), ('surrounding blanks', ' 30 ', 30), ('leading zeros', '0030', 30),
    ('ideographic space', '　30', 30), ('lower bound', '1', 1), ('upper bound', '1000', 1000),
    ('below the bound', '0', None), ('above the bound', '1001', None),
    ('five digits', '00030', None), ('plus sign', '+7', None), ('minus', '-7', None),
    ('float', '7.0', None), ('underscore', '1_000', None), ('exponent', '1e3', None),
    ('over 64 bits', '18446744073709551617', None), ('empty', '', None),
    ('blanks only', '   ', None), ('NULL is absent', None, 7),
    ('not text: integer', 7, None), ('not text: bytes', b'7', None),
    ('tab and newline', '\t30\n', 30), ('NUL inside', '3\x000', None),
    ('fullwidth digit', chr(0xFF17), None), ('Arabic-Indic digit', chr(0x0667), None),
    ('superscript digit', chr(0x00B2), None), ('lone surrogate', '\udc80', None),
    ('inner blank', '1 0', None), ('hex', '0x10', None),
]


@pytest.mark.parametrize('label,raw,expected', NUMBERS, ids=[r[0] for r in NUMBERS])
def test_keep_is_a_whole_number_of_ascii_digits_within_1_to_1000(label, raw, expected, caplog):
    with caplog.at_level(logging.WARNING):
        got = read(keep=raw).keep
    assert got == (DEFAULT.keep if expected is None else expected)
    assert bool(caplog.records) is (expected is None)


@pytest.mark.parametrize('field,raw,expected', [
    ('min_days', '0', 0), ('min_days', '365', 365), ('min_days', '366', 1),
    ('gfs_daily', '0', 0), ('gfs_weekly', '1000', 1000), ('gfs_monthly', '1001', 6),
    ('gfs_daily', '-1', 7),
], ids=['min_days 0', 'min_days 365', 'min_days 366', 'daily 0', 'weekly 1000',
        'monthly 1001', 'daily negative'])
def test_each_field_has_its_own_bounds(field, raw, expected):
    assert getattr(read(**{field: raw}), field) == expected


STRATEGIES = [
    ('gfs', 'gfs', 'gfs'), ('upper case', 'GFS', 'gfs'), ('blanks', ' gfs ', 'gfs'),
    ('newline', 'gfs\n', 'gfs'), ('mixed case', 'Interval_Count', 'interval_count'),
    ('unknown', 'hourly', None), ('inner blank', 'g fs', None), ('empty', '', None),
    ('NULL is absent', None, 'interval_count'), ('bytes', b'gfs', None),
    ('fullwidth', 'ＧＦＳ', None), ('long s (casefolds to s)', 'gf' + chr(0x017F), None),
    ('dotted capital I', 'İnterval_count', None), ('lone surrogate', '\udc80', None),
]


@pytest.mark.parametrize('label,raw,expected', STRATEGIES, ids=[r[0] for r in STRATEGIES])
def test_the_strategy_is_known_after_blanks_and_ascii_case(label, raw, expected, caplog):
    with caplog.at_level(logging.WARNING):
        got = read(strategy=raw).strategy
    assert got == (expected or 'interval_count')
    assert bool(caplog.records) is (expected is None)


def test_gfs_counts_all_zero_are_read_as_the_defaults_and_logged(caplog):
    with caplog.at_level(logging.WARNING):
        got = read(strategy='gfs', gfs_daily='0', gfs_weekly='0', gfs_monthly='0')
    assert (got.strategy, got.gfs_daily, got.gfs_weekly, got.gfs_monthly) == ('gfs', 7, 4, 6)
    assert caplog.records


def test_one_gfs_count_above_zero_is_kept():
    got = read(strategy='gfs', gfs_daily='0', gfs_weekly='0', gfs_monthly='1')
    assert (got.gfs_daily, got.gfs_weekly, got.gfs_monthly) == (0, 0, 1)


def test_the_targets_are_read_apart():
    values = reader({'backup_s3_keep': '3', 'backup_local_keep': '9'})
    assert read_retention('local', values).keep == 9
    assert read_retention('s3', values).keep == 3


def test_an_invalid_value_is_logged_once_per_stored_text_not_at_every_read(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert read(keep='lots', strategy='hourly').keep == 7
    assert len(caplog.records) == 2
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        read(keep='more')                       # another stored text: logged again
        read(target='s3', keep='lots')          # the same text under another key: logged
    assert len(caplog.records) == 2


def test_all_zero_gfs_counts_are_logged_once(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            read(gfs_daily='0', gfs_weekly='0', gfs_monthly='0')
    assert len(caplog.records) == 1


def test_a_failed_read_is_raised_not_replaced_by_defaults():
    def broken(key):
        raise RuntimeError('database is locked')
    with pytest.raises(RuntimeError):
        read_retention('local', broken)


def test_stored_rows_are_read_from_the_database(app):
    with app.app_context():
        db = get_db()
        upsert_global_setting(db, 'backup_s3_strategy', ' GFS ')
        upsert_global_setting(db, 'backup_s3_gfs_daily', '14')
        upsert_global_setting(db, 'backup_s3_keep', None)
        db.commit()
        got = read_retention('s3')
    assert (got.strategy, got.gfs_daily, got.keep) == ('gfs', 14, 7)


def test_a_database_that_cannot_be_read_raises(app):
    import sqlite3
    with app.app_context():
        db = get_db()
        db.execute('ALTER TABLE settings RENAME TO settings_gone')
        try:
            with pytest.raises(sqlite3.Error):
                read_retention('local')
        finally:
            db.execute('ALTER TABLE settings_gone RENAME TO settings')
