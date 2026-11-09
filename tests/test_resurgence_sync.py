"""The Prime Resurgence sync step: the page's parser, the catalogue match, the one-transaction
write, the fetch failures that change nothing, and the freshness of the stored stock.

The page is a hand-written, minimal copy of the wiki page's shape (no wiki content is
committed); every fetch is mocked at ``requests.get``, so no test reaches the network.
"""
from datetime import datetime, timedelta, timezone

import pytest
import requests

from models.db import get_db
from models.item import Item
from services import resurgence_sync as rs
from services import wiki_sync
from services.auth_service import get_global_setting, set_global_setting, set_setting

CHECKED, CROSSED = chr(0x2611), chr(0x2612)
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
ENDS = '2026-11-25T18:00:00Z'


# ── The hand-written page ────────────────────────────────────────────────────

def _dates(first, *past):
    lines = ['{| class="wikitable" style="text-align:center"',
             f'! style="text-align:center;"|{first}', '|-']
    for d in past:
        lines += [f'| {d}', '|-']
    return lines + ['|}']


def _pack(title, entries):
    lines = [f'|-|{title}=',
             '{| border="1" data-tableid="CollectedPrimes" class="article-table sortable"',
             '! style="text-align:center;"|Item Name', '! Item Type', '! {{Resource|Regal Aya}}']
    for rowid, cell, kind in entries:
        lines += [f'|- data-rowid="{rowid}"', f'|{cell}', f'|{kind}', '|2']
    return lines + ['|- class="sortbottom nohighlight"', '| scope="row" colspan="2"| Total',
                    '| 10', '|}', '']


def _row(start, end, packs, mark, relics=('Lith S17',)):
    lines = ['|-', '|'] + start + ['|'] + end
    lines += ['|data-sort-value="Dual" style="display: block;"|', '<tabber>']
    for title, entries in packs:
        lines += _pack(title, entries)
    lines += ['</tabber>', '|', '{| border="1" data-tableid="CollectedRelics" class="article-table"',
              '! Relics']
    for relic in relics:
        lines += [f'|- data-rowid="{relic.replace(" ", "")}"', f'|{{{{Relic|{relic}}}}}']
    lines += ['|-', '|}', f'|<div class="posTextIcon" style="font-size:3em;">{mark}</div>']
    return lines


ASH_ROW = [('Ash & Vauban', [('AshPrime', '{{WF|Ash Prime}}', 'Warframe'),
                             ('VaubanPrime', '{{WF|Vauban Prime}}', 'Warframe'),
                             ('VectisPrime', '{{Weapon|Vectis Prime}}', 'Weapon (Primary)')])]
NIDUS_ROW = [
    ('Nidus & Saryn', [
        ('NidusPrime', '{{WF|Nidus Prime}}', 'Warframe'),
        ('SarynPrime', '{{WF|Saryn Prime}}', 'Warframe'),
        ('MagnusPrime', '{{Weapon|Magnus Prime}}', 'Weapon (Secondary)'),
        ('CrinoidPrimeSyandana', '[[Crinoid Prime Syandana]]', 'Accessory'),
        ('CycutaPrimeSigil', '[[Sigils#Prime_Access_Sigils|Cycuta Prime Sigil]]', 'Accessory'),
        ('MaggotPrime', '<span class="basic-tooltip" title="Maggot Prime (1x) = 1 Regal Aya">'
                        '[[Decorations/Orbiter Decorations#Domestik_Drone|Maggot Prime (2x)]]'
                        '</span>', 'Accessory'),
    ]),
    ('Nidus', [('NidusPrime', '{{WF|Nidus Prime}}', 'Warframe'),
               ('StrunPrime', '{{Weapon|Strun Prime}}', 'Weapon (Primary)')]),
]
HEADER = [
    '{| border="1" class="wikitable sortable" style="text-align:center;"',
    '! Start Date', '! End Date', '!colspan="2"| Packs',
    f'! <span class="basic-tooltip" title="{CHECKED}: Currently Active\\n{CROSSED}: '
    'Currently Inactive">Current Status</span>',
]


def page_wikitext(active='nidus', nidus_end=None, extra_rows=(), section=True):
    """The page: an inactive Ash row and the Nidus row, `active` naming the checked one."""
    ash = _row(_dates('2026-02-19', '2025-02-06'), _dates('2026-03-19', '2025-02-13'),
               ASH_ROW, CHECKED if active == 'ash' else CROSSED)
    nidus = _row(_dates('2026-10-29', '2025-03-13'),
                 nidus_end if nidus_end is not None else _dates('2026-11-25', '2025-04-10'),
                 NIDUS_ROW, CHECKED if active == 'nidus' else CROSSED)
    body = HEADER + ash + nidus + [line for row in extra_rows for line in row] + ['|}']
    table = '\n'.join(body)
    if not section:
        return "'''Prime Resurgence''' is a program.\n" + table
    return ("{{Infobox\n| Box title=Prime Resurgence\n}}\n'''Prime Resurgence''' is run by "
            "[[Varzia]].\n==Current Rewards==\n"
            '<section begin="primeresurgenceoffer" />\n' + table +
            '\n<section end="primeresurgenceoffer" />\n==See Also==\n*[[Prime Vault]]\n')


def api_json(wikitext):
    return {'batchcomplete': '', 'query': {'pages': {'1554139': {
        'pageid': 1554139, 'ns': 0, 'title': 'Prime Resurgence',
        'revisions': [{'slots': {'main': {'contentmodel': 'wikitext', '*': wikitext}}}]}}}}


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code, self._payload, self._text = status, payload, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error', response=self)

    def json(self):
        if self._text is not None:
            import json
            return json.loads(self._text)
        return self._payload


@pytest.fixture
def wiki(monkeypatch):
    """Serve the page from `wiki.response` (a FakeResponse, or an exception to raise)."""
    class Wiki:
        response = FakeResponse(payload=api_json(page_wikitext()))
        calls = []

    def fake_get(url, params=None, timeout=None, **kwargs):
        Wiki.calls.append((url, params))
        if isinstance(Wiki.response, BaseException):
            raise Wiki.response
        return Wiki.response

    monkeypatch.setattr(requests, 'get', fake_get)
    return Wiki


CATALOGUE = [('Ash Prime', 'warframe'), ('Vauban Prime', 'warframe'), ('Vectis Prime', 'primary'),
             ('Nidus Prime', 'warframe'), ('Saryn Prime', 'warframe'),
             ('Magnus Prime', 'secondary'), ('Strun Prime', 'primary'),
             ('Nidus Prime Chassis', 'warframe'), ('Braton', 'primary')]


@pytest.fixture
def ctx(app):
    with app.app_context():
        for name, category in CATALOGUE:
            Item.upsert(name, category, source='prime' if 'Prime' in name else 'standard')
        yield get_db()


def available(db):
    return sorted(r['name'] for r in db.execute(
        'SELECT name FROM items WHERE resurgence_available = 1').fetchall())


def global_rows(db):
    return sorted((r['key'], r['value']) for r in db.execute(
        'SELECT key, value FROM settings WHERE user_id IS NULL').fetchall())


NIDUS_STOCK = ['Magnus Prime', 'Nidus Prime', 'Saryn Prime', 'Strun Prime']


# ── The parser ───────────────────────────────────────────────────────────────

def test_the_active_rotation_gives_its_pack_entries_and_end_date():
    entries, end = rs.parse_stock(page_wikitext())
    assert [names[0] for names in entries] == [
        'Nidus Prime', 'Saryn Prime', 'Magnus Prime', 'Crinoid Prime Syandana',
        'Cycuta Prime Sigil', 'Maggot Prime', 'Strun Prime']
    assert end.isoformat() == '2026-11-25'


def test_the_checked_row_decides_which_rotation_is_read():
    entries, end = rs.parse_stock(page_wikitext(active='ash'))
    assert [names[0] for names in entries] == ['Ash Prime', 'Vauban Prime', 'Vectis Prime']
    assert end.isoformat() == '2026-03-19'


@pytest.mark.parametrize('cell, names', [
    ('{{WF|Ash Prime}}', ['Ash Prime']),
    ('{{Weapon|Akstiletto Prime|Akstiletto}}', ['Akstiletto Prime']),
    ('[[Ash Prime|Ash]]', ['Ash Prime', 'Ash']),
    ('[[Ash Prime]]', ['Ash Prime']),
    ('[[Sigils#Prime_Access_Sigils|Cycuta Prime Sigil]]', ['Cycuta Prime Sigil']),
    ('<span title="x">[[A#b|Maggot Prime (2x)]]</span>', ['Maggot Prime']),
    ('Ash&#32;Prime &amp; more', ['Ash Prime & more']),
    ('  Ash   Prime  ', ['Ash Prime']),
    ('{{WF|Ash&nbsp;Prime}}', ['Ash Prime']),
])
def test_an_entry_cell_gives_its_names_in_every_spelling(cell, names):
    assert rs.entry_names(cell) == names


@pytest.mark.parametrize('case, wikitext', [
    ('no section', page_wikitext(section=False)),
    ('no active row', page_wikitext(active='none')),
    ('two active rows', page_wikitext(extra_rows=[_row(
        _dates('2026-10-29'), _dates('2026-11-25'), ASH_ROW, CHECKED)])),
    ('a row without a status', page_wikitext(extra_rows=[_row(
        _dates('2026-10-29'), _dates('2026-11-25'), ASH_ROW, 'unknown')])),
    ('both marks in one row', page_wikitext(extra_rows=[_row(
        _dates('2026-10-29'), _dates('2026-11-25'), ASH_ROW, CHECKED + CROSSED)])),
    ('an empty stock', page_wikitext().replace('data-tableid="CollectedPrimes"', 'data-x="y"')),
    ('an entry without a name cell', page_wikitext().replace(
        '|- data-rowid="SarynPrime"\n|{{WF|Saryn Prime}}', '|- data-rowid="SarynPrime"\n|-')),
    ('an unclosed table', page_wikitext().replace(
        '\n|}\n<section end', '\n<section end')),
    ('text after the table', page_wikitext().replace(
        '<section end', 'Some new paragraph.\n<section end')),
    ('two tables', page_wikitext().replace(
        '<section end', '{|\n! x\n|}\n<section end')),
    ('empty text', ''),
    ('not text', None),
])
def test_a_page_of_another_shape_is_unreadable(case, wikitext):
    with pytest.raises(rs.PageUnreadable):
        rs.parse_stock(wikitext)


@pytest.mark.parametrize('end_cell', [
    [],                                          # no end-date table
    _dates('soon'),                              # no date in it
    _dates('2026-13-40'),                        # not a date
])
def test_an_absent_or_unparsable_end_date_reads_as_none(end_cell):
    entries, end = rs.parse_stock(page_wikitext(nidus_end=end_cell))
    assert end is None
    assert entries                                  # the stock is still read


# ── Matching ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('names, expected', [
    (['Nidus Prime'], ['Nidus Prime']),
    (['nidus prime'], ['Nidus Prime']),
    (['NIDUS  PRIME '], ['Nidus Prime']),
    (['Nidus'], ['Nidus Prime']),                 # the "Prime" suffix added
    (['Ash Prime', 'Ash'], ['Ash Prime']),        # link target first
    (['Nope', 'Saryn Prime'], ['Saryn Prime']),   # then the label
    (['Nidus Prime Chassis Blueprint'], []),      # a part name names no item
    (['Braton Prime'], []),                       # no "Prime" removed
    (['Crinoid Prime Syandana'], []),             # not in the catalogue
])
def test_entry_names_match_catalogue_names_in_every_spelling(ctx, names, expected):
    ids, unmatched = rs.match_entries(ctx, [names])
    names_by_id = {r['id']: r['name'] for r in ctx.execute('SELECT id, name FROM items')}
    assert sorted(names_by_id[i] for i in ids) == expected
    assert unmatched == ([] if expected else [names[0]])


# ── The write ────────────────────────────────────────────────────────────────

def test_sync_sets_the_stock_clears_every_other_item_and_stores_the_times(ctx, wiki):
    ctx.execute("UPDATE items SET resurgence_available = 1 WHERE name IN ('Ash Prime', 'Braton')")
    ctx.commit()
    result = rs.sync_resurgence(now=NOW)
    assert result['ok'] is True
    assert result['available'] == 4
    assert result['ends_at'] == ENDS and result['end_from_page'] is True
    assert result['unmatched'] == ['Crinoid Prime Syandana', 'Cycuta Prime Sigil', 'Maggot Prime']
    assert available(ctx) == NIDUS_STOCK
    assert global_rows(ctx) == [('resurgence_ends_at', ENDS),
                                ('resurgence_read_at', '2026-11-08T12:00:00Z')]
    assert wiki.calls[0][1]['titles'] == 'Prime Resurgence'


def test_sync_twice_gives_the_same_result_and_one_row_per_setting(ctx, wiki):
    first = rs.sync_resurgence(now=NOW)
    second = rs.sync_resurgence(now=NOW + timedelta(hours=1))
    assert first['available'] == second['available'] == 4
    assert available(ctx) == NIDUS_STOCK
    assert global_rows(ctx) == [('resurgence_ends_at', ENDS),
                                ('resurgence_read_at', '2026-11-08T13:00:00Z')]


def test_a_rotation_change_between_syncs_clears_the_old_stock(ctx, wiki):
    wiki.response = FakeResponse(payload=api_json(page_wikitext(active='ash')))
    rs.sync_resurgence(now=NOW)
    assert available(ctx) == ['Ash Prime', 'Vauban Prime', 'Vectis Prime']
    wiki.response = FakeResponse(payload=api_json(page_wikitext(active='nidus')))
    rs.sync_resurgence(now=NOW)
    assert available(ctx) == NIDUS_STOCK


def test_a_stock_naming_no_catalogue_item_clears_every_item(ctx, wiki):
    ctx.execute("UPDATE items SET resurgence_available = 1 WHERE name = 'Ash Prime'")
    ctx.commit()
    wiki.response = FakeResponse(payload=api_json(page_wikitext().replace(
        'Prime}}', 'Zz}}')))
    result = rs.sync_resurgence(now=NOW)
    assert result['ok'] is True and result['available'] == 0
    assert available(ctx) == []


@pytest.mark.parametrize('days_config, days', [(None, 7), (3, 3), (0, 7), ('x', 7)])
def test_a_page_without_an_end_date_expires_a_set_time_after_the_read(
        app, ctx, wiki, days_config, days):
    if days_config is not None:
        app.config['RESURGENCE_FALLBACK_DAYS'] = days_config
    else:
        app.config.pop('RESURGENCE_FALLBACK_DAYS', None)
    wiki.response = FakeResponse(payload=api_json(page_wikitext(nidus_end=[])))
    result = rs.sync_resurgence(now=NOW)
    assert result['ok'] is True and result['end_from_page'] is False
    assert get_global_setting(rs.SETTING_ENDS_AT) == rs.format_utc(NOW + timedelta(days=days))


FAILURES = [
    ('timeout', requests.Timeout('read timed out')),
    ('unreachable', requests.ConnectionError('no route')),
    ('HTTP 500', FakeResponse(status=500)),
    ('HTTP 404', FakeResponse(status=404)),
    ('rate limited', FakeResponse(status=429)),
    ('invalid JSON', FakeResponse(text='<html>not json')),
    ('JSON of another shape', FakeResponse(payload=['query'])),
    ('missing page', FakeResponse(payload={'query': {'pages': {'-1': {'missing': ''}}}})),
    ('no pages', FakeResponse(payload={'query': {}})),
    ('a page without revisions', FakeResponse(payload={'query': {'pages': {'1': {}}}})),
    ('a page without the stock section', FakeResponse(payload=api_json(
        page_wikitext(section=False)))),
    ('an empty stock', FakeResponse(payload=api_json(
        page_wikitext().replace('data-tableid="CollectedPrimes"', 'data-x="y"')))),
    ('no active rotation', FakeResponse(payload=api_json(page_wikitext(active='none')))),
]


@pytest.mark.parametrize('case, response', FAILURES, ids=[c for c, _ in FAILURES])
def test_a_failed_read_keeps_the_last_stock_and_end_time_and_is_reported(ctx, wiki, case, response):
    rs.sync_resurgence(now=NOW)
    before = (available(ctx), global_rows(ctx))
    wiki.response = response
    result = rs.sync_resurgence(now=NOW + timedelta(days=1))
    assert result['ok'] is False and result['error']
    assert (available(ctx), global_rows(ctx)) == before


def test_a_failed_write_changes_nothing(ctx, wiki, monkeypatch):
    rs.sync_resurgence(now=NOW)
    before = (available(ctx), global_rows(ctx))

    def broken(db, key, value):
        raise RuntimeError('disk full')

    monkeypatch.setattr(rs, 'upsert_global_setting', broken)
    wiki.response = FakeResponse(payload=api_json(page_wikitext(active='ash')))
    result = rs.sync_resurgence(now=NOW)
    assert result == {'ok': False, 'error': 'write failed: disk full'}
    assert (available(ctx), global_rows(ctx)) == before


def test_the_full_sync_reports_a_failed_resurgence_read_and_still_succeeds(app, ctx, wiki,
                                                                         monkeypatch):
    for name in ('_sync_warframes', '_sync_weapons', '_sync_kitgun_chambers', '_sync_companions',
                 '_sync_archwings', '_sync_k_drives', '_sync_amps', '_sync_mods', '_sync_arcanes',
                 '_sync_resources', '_sync_component_ingredients', '_sync_drop_sources'):
        monkeypatch.setattr(wiki_sync, name, lambda *args: 1)
    monkeypatch.setattr(wiki_sync, '_backfill_null_item_images', lambda db, cats: 0)
    monkeypatch.setattr(wiki_sync, '_sync_wiki_images_for_missing', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_item_images', lambda db: (0, 0))
    monkeypatch.setattr(wiki_sync, '_sync_component_wiki_images', lambda db: 0)
    monkeypatch.setattr(wiki_sync, '_sync_component_images', lambda db: (0, 0))
    wiki.response = requests.Timeout('slow')
    result = wiki_sync.run_sync(app)
    assert result['ok'] is True and result['total'] == 12
    assert result['resurgence']['ok'] is False
    wiki.response = FakeResponse(payload=api_json(page_wikitext()))
    assert wiki_sync.run_sync(app)['resurgence']['available'] == 4


# ── The catalogue sync and the settings ──────────────────────────────────────

def test_a_catalogue_upsert_keeps_the_flag_and_a_new_item_starts_clear(ctx):
    ctx.execute("UPDATE items SET resurgence_available = 1 WHERE name = 'Nidus Prime'")
    ctx.commit()
    Item.upsert('Nidus Prime', 'warframe', source='prime', vaulted=True)
    Item.upsert('Gara Prime', 'warframe', source='prime', vaulted=True)
    assert available(ctx) == ['Nidus Prime']


def test_a_global_setting_written_twice_is_one_row_with_the_last_value(ctx):
    set_setting(None, 'other', 'x')                 # not touched by the global write
    set_global_setting('k', 'a')
    set_global_setting('k', 'b')
    set_global_setting('k', None)
    set_global_setting('k', 'c')
    rows = ctx.execute("SELECT value FROM settings WHERE user_id IS NULL AND key = 'k'").fetchall()
    assert [r['value'] for r in rows] == ['c']
    assert get_global_setting('k') == 'c'


# ── Freshness ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('stored, now, state', [
    (None, NOW, 'missing'),
    ('', NOW, 'missing'),
    ('next tuesday', NOW, 'missing'),
    ('2026-11-25 18:00:00', NOW, 'missing'),
    (ENDS, NOW, 'current'),
    (ENDS, datetime(2026, 11, 25, 17, 59, 59, tzinfo=timezone.utc), 'current'),
    (ENDS, datetime(2026, 11, 25, 18, 0, 0, tzinfo=timezone.utc), 'expired'),
    # the same instants in another time zone compare as UTC
    (ENDS, datetime(2026, 11, 25, 19, 59, 59, tzinfo=timezone(timedelta(hours=2))), 'current'),
    (ENDS, datetime(2026, 11, 25, 20, 0, 0, tzinfo=timezone(timedelta(hours=2))), 'expired'),
])
def test_the_stored_stock_counts_only_before_its_end_time(ctx, stored, now, state):
    if stored is not None:
        set_global_setting(rs.SETTING_ENDS_AT, stored)
    status = rs.resurgence_status(now=now)
    assert status['state'] == state
    assert status['current'] is (state == 'current')
    assert (status['notice'] is None) is (state == 'current')
    expected_notice = {'missing': rs.NOTICE_MISSING, 'expired': rs.NOTICE_EXPIRED}.get(state)
    assert status['notice'] == expected_notice
