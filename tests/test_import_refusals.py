"""The import refuses a bad upload whole and writes nothing.

Axis 1 (input spellings) per cell kind: each spelling is either accepted (and the stored
value checked) or refused (message names file, line and column; every table compared
before and after). Corruption: the ZIP and CSV shapes. Repetition: a key repeated in one
file is refused, naming the line it first appeared on.
"""
import io
import zipfile

import pytest

from models.db import get_db
from services.import_service import ImportRefused, import_user_data
from transfer_fixtures import (make_zip, read_user, seeded,  # noqa: F401
                               seeded_template, snapshot)  # noqa: F401

FLAG_OK = [('0', 0), ('1', 1), (' 1 ', 1), ('', 0)]
FLAG_BAD = ['2', '-1', '1.0', 'true', 'x']
INT_BAD = ['-1', '1.5', '9223372036854775808', '100000000000000000000', '1e3', '٣']

# (file, header, row template with {v}, reader of the stored value from read_user(bob))
FLAG_CELLS = {
    'in_possession': ('warframe.csv', 'name,in_possession', 'Excalibur,{v}',
                      lambda u: u['progress'][('Excalibur', 'warframe')][0]),
    'mastered': ('primary.csv', 'name,category,mastered', 'Braton,primary,{v}',
                 lambda u: u['progress'][('Braton', 'primary')][1]),
    'subsumed': ('warframe.csv', 'name,subsumed', 'Excalibur,{v}',
                 lambda u: u['progress'][('Excalibur', 'warframe')][2]),
    'resurgence_watch': ('arcane.csv', 'name,resurgence_watch', 'Arcane Energize,{v}',
                         lambda u: u['progress'][('Arcane Energize', 'arcane')][3]),
}
INT_CELLS = {
    'target_rank': ('warframe.csv', 'name,target_rank', 'Excalibur,{v}'),
    'owned_rank': ('arcane.csv', 'name,owned_rank', 'Arcane Energize,{v}'),
}


@pytest.fixture
def app(seeded):
    return seeded[0]


@pytest.fixture
def bob(seeded):
    return seeded[1]['bob']


def run(app, uid, files):
    """Import; on refusal assert that nothing changed and return the message."""
    with app.app_context():
        before = snapshot()
        try:
            result = import_user_data(uid, make_zip(files) if isinstance(files, dict) else files)
        except ImportRefused as exc:
            assert snapshot() == before
            return str(exc)
        return result


def stored(app, uid, reader):
    with app.app_context():
        return reader(read_user(uid))


def upload(fname, header, row, value):
    return {fname: f'{header}\n{row.format(v=value)}\n'}


# ── Axis 1: flags, integers, categories, names ──────────────────────────────

@pytest.mark.parametrize('cell', FLAG_CELLS)
@pytest.mark.parametrize('value, expected', FLAG_OK)
def test_a_flag_cell_accepts_0_1_blank_padded_and_empty(app, bob, cell, value, expected):
    fname, header, row, reader = FLAG_CELLS[cell]
    assert not isinstance(run(app, bob, upload(fname, header, row, value)), str)
    assert stored(app, bob, reader) == expected


@pytest.mark.parametrize('cell', FLAG_CELLS)
@pytest.mark.parametrize('value', FLAG_BAD)
def test_a_flag_cell_refuses_any_other_spelling(app, bob, cell, value):
    fname, header, row, _ = FLAG_CELLS[cell]
    message = run(app, bob, upload(fname, header, row, value))
    column = header.split(',')[-1]
    assert f'{fname} line 2, column {column}:' in message


@pytest.mark.parametrize('value, expected', [('0', 0), (' 4 ', 4), ('5', 5), ('6', 5),
                                             ('9223372036854775807', 5)])
def test_a_rank_cell_accepts_whole_numbers_and_clamps_to_the_items_max(app, bob, value,
                                                                       expected):
    fname, header, row = INT_CELLS['owned_rank']
    assert not isinstance(run(app, bob, upload(fname, header, row, value)), str)
    assert stored(app, bob, lambda u: u['progress'][('Arcane Energize', 'arcane')][5]) == \
        expected


@pytest.mark.parametrize('cell', INT_CELLS)
@pytest.mark.parametrize('value', INT_BAD)
def test_a_rank_cell_refuses_negatives_fractions_and_overflow(app, bob, cell, value):
    fname, header, row = INT_CELLS[cell]
    message = run(app, bob, upload(fname, header, row, value))
    assert f'{fname} line 2, column {header.split(",")[-1]}:' in message


@pytest.mark.parametrize('cell, reader', [
    ('target_rank', lambda u: u['progress'][('Excalibur', 'warframe')][6]),
    ('owned_rank', lambda u: u['progress'][('Arcane Energize', 'arcane')][5]),
])
def test_an_empty_rank_cell_is_null(app, bob, cell, reader):
    fname, header, row = INT_CELLS[cell]
    assert not isinstance(run(app, bob, upload(fname, header, row, '')), str)
    assert stored(app, bob, reader) is None


@pytest.mark.parametrize('value, ok', [('warframe', True), ('', True), (' warframe ', True),
                                       ('primary', False), ('Warframe', False),
                                       ('weapons', False), ('warframes', False)])
def test_a_category_files_category_cell_must_match_the_file(app, bob, value, ok):
    result = run(app, bob, {'warframe.csv': f'name,category,mastered\nExcalibur,{value},1\n'})
    if ok:
        assert stored(app, bob, lambda u: u['progress'][('Excalibur', 'warframe')][1]) == 1
    else:
        assert 'warframe.csv line 2, column category:' in result


@pytest.mark.parametrize('name, expected', [
    ('Odd, "quoted"\nname Ñoño ☃', 'Odd, "quoted"\nname Ñoño ☃'),
    ('  Lex  ', 'Lex'),
    ('Ünïcödé Frame', 'Ünïcödé Frame'),
])
def test_an_item_name_with_commas_quotes_newlines_or_non_ascii_imports(app, bob, name,
                                                                       expected):
    quoted = '"' + name.replace('"', '""') + '"'
    run(app, bob, {'melee.csv': f'name,in_possession\n{quoted},1\n'})
    assert stored(app, bob, lambda u: u['progress'][(expected, 'melee')][0]) == 1


@pytest.mark.parametrize('fname, text', [
    ('primary.csv', 'name,category\n  ,primary\n'),
    ('warframe.csv', 'name,mastered\n,1\n'),
])
def test_a_row_with_a_blank_name_cell_is_refused(app, bob, fname, text):
    assert f'{fname} line 2, column name:' in run(app, bob, {fname: text})


def test_blank_header_cells_are_ignored(app, bob):
    run(app, bob, {'secondary.csv': 'name,in_possession,,\nLex,1,,\n'})
    assert stored(app, bob, lambda u: u['progress'][('Lex', 'secondary')][0]) == 1


# ── Repetition ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('fname, text, line', [
    ('warframe.csv', 'name,mastered\nExcalibur,1\nLex,1\n Excalibur ,0\n', 4),
    ('primary.csv', 'name,category,mastered\nBraton,primary,1\nBraton,,0\n', 3),
])
def test_a_key_repeated_in_one_file_is_refused(app, bob, fname, text, line):
    message = run(app, bob, {fname: text})
    assert f'{fname} line {line}' in message
    assert 'already appears on line 2' in message


def test_the_same_name_in_two_category_files_is_not_a_repeat(app, bob):
    run(app, bob, {'primary.csv': 'name,in_possession\nLex,1\n',
                   'secondary.csv': 'name,in_possession\nLex,1\n'})
    assert stored(app, bob, lambda u: u['progress'][('Lex', 'secondary')][0]) == 1
    assert stored(app, bob, lambda u: u['progress'][('Lex', 'primary')][0]) == 1


# ── Corruption ──────────────────────────────────────────────────────────────

def zip_bytes(files):
    return make_zip(files).getvalue()


@pytest.mark.parametrize('upload_bytes, words', [
    (b'name,mastered\nExcalibur,1\n', 'not a ZIP archive'),
    (b'', 'not a ZIP archive'),
    (zip_bytes({'warframe.csv': 'name,mastered\n' + 'Excalibur,1\n' * 500})[:150],
     'not a ZIP archive'),
    (zip_bytes({'melee.csv': 'name,mastered\nExcalibur,1\n'.encode('latin-1')
                + 'Ñ,1\n'.encode('latin-1')}), 'melee.csv is not UTF-8'),
    (zip_bytes({'warframe.csv': ''}), 'warframe.csv line 1'),
    (zip_bytes({'warframe.csv': 'category,mastered\nwarframe,1\n'}),
     'warframe.csv line 1, column name: the header lacks this column'),
    (zip_bytes({'warframe.csv': 'name,mastered,name\nExcalibur,1,x\n'}),
     'warframe.csv line 1, column name: the column appears twice'),
    (zip_bytes({'warframe.csv': 'name,mastered\nExcalibur,1,extra\n'}),
     'warframe.csv line 2, column #3'),
    (b'x' * (8 * 1024 * 1024 + 1), 'larger than 8 MiB'),
])
def test_a_damaged_or_oversized_upload_is_refused(app, bob, upload_bytes, words):
    assert words in run(app, bob, io.BytesIO(upload_bytes))


def test_a_refusal_after_valid_files_writes_none_of_them(app, bob):
    message = run(app, bob, {'primary.csv': 'name,mastered\nBraton,1\n',
                             'secondary.csv': 'name,in_possession\nLex,1\n',
                             'warframe.csv': 'name,target_rank\nExcalibur,x\n'})
    assert 'warframe.csv line 2, column target_rank:' in message


def test_a_truncated_member_is_refused(app, bob):
    data = zip_bytes({'warframe.csv': 'name,mastered\n' + 'Excalibur,1\n' * 5000})
    cut = bytearray(data)
    cut[60:200] = b'\0' * 140  # inside the compressed stream; the directory stays intact
    assert 'Import refused, nothing written' in run(app, bob, io.BytesIO(bytes(cut)))


def test_a_zip_that_unpacks_past_the_bound_is_refused(app, bob):
    big = b'name,mastered\n' + b' ' * (33 * 1024 * 1024)
    assert 'unpacks to more than 32 MiB' in run(app, bob, io.BytesIO(zip_bytes(
        {'warframe.csv': big})))


def test_a_file_named_twice_in_the_zip_is_refused(app, bob):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('secondary.csv', 'name,in_possession\nLex,1\n')
        with pytest.warns(UserWarning):
            zf.writestr('secondary.csv', 'name,in_possession\nLex,0\n')
    buf.seek(0)
    assert 'secondary.csv appears twice' in run(app, bob, buf)


def test_a_bom_file_imports(app, bob):
    run(app, bob, {'secondary.csv': '﻿name,in_possession\r\nLex,1\r\n'})
    assert stored(app, bob, lambda u: u['progress'][('Lex', 'secondary')][0]) == 1


def test_unknown_files_directories_extra_columns_and_header_only_files_are_ignored(app, bob):
    with app.app_context():
        get_db().execute('INSERT INTO settings (user_id, "key", value) VALUES (?, ?, ?)',
                         (bob, 'plat_per_hour', '9'))
        get_db().commit()
        before = snapshot()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('notes.txt', 'anything')
        zf.writestr('Secondary.csv', 'name,in_possession\nLex,1\n')
        zf.writestr('sub/', '')
        zf.writestr('sub/secondary.csv', 'name,in_possession\nLex,1\n')
        zf.writestr('watchlist.csv', 'name,category\nLex,secondary\nNew Gun,primary\n')
        zf.writestr('settings.csv', 'key,value\nplat_per_hour,x\n')
        zf.writestr('primary.csv', 'name,mastered,comment\n')
        zf.writestr('warframe.csv', 'name,in_possession,colour\nExcalibur,1,blue\n')
    buf.seek(0)
    result = run(app, bob, buf)
    assert result.counts == {'items': 1}
    with app.app_context():
        after = snapshot()
    assert after['user_market_watchlist'] == before['user_market_watchlist']
    assert after['settings'] == before['settings']
    assert after['items'] == before['items']
    assert stored(app, bob, lambda u: u['progress']) == {
        ('Excalibur', 'warframe'): (1, 0, 0, 0, None, None, None)}
