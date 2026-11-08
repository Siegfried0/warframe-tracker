"""Format 3: the export neutralises spreadsheet formula cells, the import restores them.

Oracles: the escape rule (a text cell whose first character is `=`, `+`, `-`, `@`, tab,
carriage return or `'` is written with one leading `'`; any other value as is) and its
inverse (in a ZIP marked by `format.csv`, `'` followed by one of those characters loses
the `'` before anything else reads the cell; every other cell is read as written; a ZIP
without the marker is read exactly as before). Axis 1: every trigger, `'` before each,
lone and doubled apostrophes, the empty cell, a blank before a trigger, full-width
look-alikes. Corruption: the marker file's shapes, each refused naming file, line and
column with nothing written. Toolchain: CSV quoting of an escaped cell that holds a comma,
a quote or a line break.
"""
import csv
import io
import zipfile

import pytest

from models.item import Item
from services.export_service import escape_cell, export_user_data
from services.import_reader import unescape_cell
from services.import_service import ImportRefused, import_user_data
from services.farming_service import add_farming_goal, set_checklist, set_goal_progress
from transfer_fixtures import (item_id, make_zip, put_progress, read_user, read_zip,
                               seeded, seeded_template, snapshot, watch)  # noqa: F401

TRIGGERS = ['=', '+', '-', '@', '\t', '\r', "'"]
MARKER = {'format.csv': 'key,value\nformat,3\n'}

# (text, escaped on export)
SPELLINGS = (
    [(t + 'x', True) for t in TRIGGERS]
    + [(t, True) for t in TRIGGERS]                       # a cell that is only the trigger
    + [("'" + t + 'x', True) for t in TRIGGERS]           # an apostrophe before each trigger
    + [("''", True), ('', False), ('x', False), (' =x', False),
       ('a=b', False), ('＝x', False), ('＋x', False), ('\n=x', False),
       ('=SUM(A1:A9)', True), ('-2', True), ('@cmd|x', True)]
)


# ── The escape rule ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, escaped', SPELLINGS)
def test_a_text_cell_is_escaped_exactly_when_it_starts_with_a_trigger(text, escaped):
    out = escape_cell(text)
    assert out == ("'" + text if escaped else text)
    assert unescape_cell(out) == text


@pytest.mark.parametrize('value', [0, 1, 3, -1, 2 ** 63 - 1, None])
def test_a_non_text_cell_is_never_escaped(value):
    assert escape_cell(value) == value


@pytest.mark.parametrize('cell', ["'x", "'", "' =x", "'＝x", "'1", 'x', '', "'\nx"])
def test_a_cell_without_an_apostrophe_before_a_trigger_is_read_as_written(cell):
    assert unescape_cell(cell) == cell


@pytest.mark.parametrize('cell, read', [("'=x", '=x'), ("''", "'"), ("'''", "''"),
                                        ("'\t", '\t'), ("''=x", "'=x")])
def test_an_apostrophe_before_a_trigger_loses_only_that_apostrophe(cell, read):
    assert unescape_cell(cell) == read


# ── Round trip ──────────────────────────────────────────────────────────────

@pytest.fixture
def sapp(seeded):
    return seeded[0]


@pytest.fixture
def users(seeded):
    return seeded[1]


NOTES = [text for text, _ in SPELLINGS if text]
# Names, row keys and component names are stripped on import (as in every format), so the
# round trip of those holds for spellings without surrounding blanks.
NAMES = [text for text in NOTES if text == text.strip()]


def _seed_trigger_rows(alice):
    """Alice gets one item per note spelling, carrying that note, and items, goal rows,
    checklist rows and a watch entry whose key cells start with a trigger."""
    for i, note in enumerate(NOTES):
        Item.upsert(f'Note Item {i:02d}', 'primary', source='standard')
        put_progress(alice, f'Note Item {i:02d}', 'primary', note=note)
    for name in NAMES:
        Item.upsert(name, 'secondary', source='standard')
        put_progress(alice, name, 'secondary', in_possession=1, note=name)
    goal_item = NAMES[0]
    goal = add_farming_goal(alice, item_id(goal_item, 'secondary'))['goal_id']
    for key in NAMES:
        set_goal_progress(alice, goal, 'ingredient', key, quantity_owned=2)
        set_checklist(alice, item_id(goal_item, 'secondary'), key, True)
    watch(alice, NAMES[1], 'secondary')


def test_a_round_trip_restores_every_trigger_spelling_byte_for_byte(sapp, users):
    alice, bob = users['alice'], users['bob']
    with sapp.app_context():
        _seed_trigger_rows(alice)
        before = read_user(alice)
        data = export_user_data(alice)
        import_user_data(bob, io.BytesIO(data))
        after = read_user(bob)
    for i, note in enumerate(NOTES):
        assert after['progress'][(f'Note Item {i:02d}', 'primary')][4] == note, repr(note)
    for name in NAMES:
        assert after['progress'][(name, 'secondary')][0] == 1, repr(name)
        assert after['progress'][(name, 'secondary')][4] == name, repr(name)
    assert after['goal_rows'] == before['goal_rows']
    assert after['checklist'] == before['checklist']
    assert after['watchlist'] == before['watchlist']
    assert {k: v[:5] for k, v in after['progress'].items() if v[4]} == \
        {k: v[:5] for k, v in before['progress'].items() if v[4]}


def test_the_exported_cell_starts_with_an_apostrophe_exactly_for_a_trigger(sapp, users):
    alice = users['alice']
    with sapp.app_context():
        _seed_trigger_rows(alice)
        files = read_zip(export_user_data(alice))
    rows = list(csv.reader(io.StringIO(files['primary.csv'], newline='')))
    header = rows[0]
    notes = {r[header.index('name')]: r[header.index('note')] for r in rows[1:]}
    for i, note in enumerate(NOTES):
        cell = notes[f'Note Item {i:02d}']
        assert cell == (("'" + note) if note[0] in TRIGGERS else note), repr(note)
    progress = list(csv.reader(io.StringIO(files['farming_progress.csv'], newline='')))
    goal_name = "'" + NAMES[0]
    assert {r[3] for r in progress[1:] if r[0] == goal_name} == \
        {("'" + k) if k[0] in TRIGGERS else k for k in NAMES}
    assert files['format.csv'] == 'key,value\r\nformat,3\r\n'


@pytest.mark.parametrize('note, cell', [
    ('=a,b', '"\'=a,b"'),
    ('=say "hi"', '"\'=say ""hi"""'),
    ('-one\ntwo', '"\'-one\ntwo"'),
    ("'=x,\"y\"\r\n", '"\'\'=x,""y""\r\n"'),
])
def test_csv_quoting_wraps_the_escaped_cell_and_the_round_trip_holds(sapp, users, note, cell):
    alice, bob = users['alice'], users['bob']
    with sapp.app_context():
        put_progress(alice, 'Lex', 'secondary', note=note)
        data = export_user_data(alice)
        assert f'Lex,secondary,standard,0,0,0,0,{cell},,' in read_zip(data)['secondary.csv']
        import_user_data(bob, io.BytesIO(data))
        assert read_user(bob)['progress'][('Lex', 'secondary')][4] == note


# ── Hand-made ZIPs: the marker decides ──────────────────────────────────────

def _note_after(sapp, bob, files):
    with sapp.app_context():
        import_user_data(bob, make_zip(files))
        return read_user(bob)['progress'][('Lex', 'secondary')][4]


@pytest.mark.parametrize('cell, marked, stored', [
    ("'x", True, "'x"),
    ("'=x", True, '=x'),
    ("''", True, "'"),
    ("'", True, "'"),
    ("'=x", False, "'=x"),
    ("''", False, "''"),
    ('=x', True, '=x'),
    ('=x', False, '=x'),
])
def test_only_a_marked_zip_is_unescaped(sapp, users, cell, marked, stored):
    files = {'secondary.csv': f'name,note\nLex,"{cell}"\n'}
    if marked:
        files.update(MARKER)
    assert _note_after(sapp, users['bob'], files) == stored


def test_a_marker_beside_format_1_files_unescapes_them(sapp, users):
    """A format-1 category file (no rank columns) in a marked ZIP is read as format 3."""
    header = 'name,category,source,in_possession,mastered,subsumed,resurgence_watch,note'
    files = {'secondary.csv': f"{header}\n'=Lex,secondary,standard,1,0,0,0,'-note\n",
             **MARKER}
    with sapp.app_context():
        import_user_data(users['bob'], make_zip(files))
        assert read_user(users['bob'])['progress'][('=Lex', 'secondary')][:5] == \
            (1, 0, 0, 0, '-note')


@pytest.mark.parametrize('cell, outcome', [
    ("'\t1", 1),                          # unescaped, then stripped: a flag of 1
    ("'-1", "'-1' is not 0 or 1"),        # unescaped before validation: the value shown
    ("' 1", "\"' 1\" is not 0 or 1"),     # not a trigger: read as written
])
def test_the_unescape_runs_before_stripping_and_validation(sapp, users, cell, outcome):
    bob = users['bob']
    files = {'secondary.csv': f'name,in_possession\nLex,"{cell}"\n', **MARKER}
    with sapp.app_context():
        before = snapshot()
        try:
            import_user_data(bob, make_zip(files))
        except ImportRefused as exc:
            assert snapshot() == before
            assert f'secondary.csv line 2, column in_possession: {outcome}' in str(exc)
            return
        assert read_user(bob)['progress'][('Lex', 'secondary')][0] == outcome


# ── Corruption: the marker file ─────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'key,value\nformat, 3 \n',
    ' key , value \n format ,3\n',
    '﻿key,value\r\nformat,3\r\n',
    'key,value\n\nformat,3\n\n',
    'key,value,comment\nformat,3,anything\n',
])
def test_a_marker_spelled_with_blanks_a_bom_blank_rows_or_extra_columns_is_format_3(
        sapp, users, text):
    files = {'secondary.csv': "name,note\nLex,'=x\n", 'format.csv': text}
    assert _note_after(sapp, users['bob'], files) == '=x'


@pytest.mark.parametrize('text, where', [
    ('key,value\n', 'line 2, column key: the file has no format row'),
    ('key,value\n\n', 'line 2, column key: the file has no format row'),
    ('key,value\nformat,2\n', "line 2, column value: '2' is not 3"),
    ('key,value\nformat,03\n', "line 2, column value: '03' is not 3"),
    ('key,value\nformat,3.0\n', "line 2, column value: '3.0' is not 3"),
    ('key,value\nformat,three\n', "line 2, column value: 'three' is not 3"),
    ('key,value\nformat,\n', "line 2, column value: '' is not 3"),
    ("key,value\nformat,'3\n", "line 2, column value: \"'3\" is not 3"),
    ('key,value\nformat\n', "line 2, column value: '' is not 3"),
    ('key,value\nversion,3\n', "line 2, column key: 'version' is not 'format'"),
    ('key,value\nFormat,3\n', "line 2, column key: 'Format' is not 'format'"),
    ('key,value\nformat,3\nformat,3\n', 'line 3, column key: the file holds one row only'),
    ('key,value\nformat,3\nother,1\n', 'line 3, column key: the file holds one row only'),
    ('key,value\nformat,3,x\n', 'line 2, column #3'),
    ('', 'line 1, column -: the file has no header row'),
    ('key\nformat\n', 'line 1, column value: the header lacks this column'),
    ('value\n3\n', 'line 1, column key: the header lacks this column'),
    # an unclosed quote runs to the end of the file: one key cell holding the rest
    ('key,value\n"format,3\n', "line 2, column key: 'format,3\\n' is not 'format'"),
])
def test_a_bad_marker_refuses_the_import_and_writes_nothing(sapp, users, text, where):
    bob = users['bob']
    files = {'secondary.csv': 'name,in_possession\nLex,1\n',
             'watchlist.csv': 'name,category\nNew Gun,primary\n', 'format.csv': text}
    with sapp.app_context():
        before = snapshot()
        with pytest.raises(ImportRefused) as refused:
            import_user_data(bob, make_zip(files))
        assert snapshot() == before
    assert f'Import refused, nothing written: format.csv {where}' in str(refused.value)


def test_a_marker_twice_in_the_zip_is_refused(sapp, users):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('format.csv', 'key,value\nformat,3\n')
        with pytest.warns(UserWarning):
            zf.writestr('format.csv', 'key,value\nformat,3\n')
    buf.seek(0)
    with sapp.app_context():
        before = snapshot()
        with pytest.raises(ImportRefused, match='format.csv appears twice'):
            import_user_data(users['bob'], buf)
        assert snapshot() == before


def test_a_marker_in_a_subfolder_or_another_case_is_not_the_marker(sapp, users):
    files = {'secondary.csv': "name,note\nLex,'=x\n", 'sub/format.csv': 'key,value\nformat,3\n',
             'Format.csv': 'key,value\nformat,3\n'}
    assert _note_after(sapp, users['bob'], files) == "'=x"
