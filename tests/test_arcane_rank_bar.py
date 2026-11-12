"""The arcane card's rank bar: owned rank against the target rank, or the max rank without one.

The bar is `min(owned / target, 1)` wide when a target rank is set (above 0), and
`min(owned / max, 1)` when none is (0 or NULL); it has no fill when it measures against 0.
Its title and accessible label say what it measures: `Rank N of target T` or `Rank N of max M`.
The `owned/max` label beside the rank buttons stays as it is, and the table view has no bar.

The server render and the page's script compute the bar the same way: the page repaints it
after a rank button's success and after the target input's success, without a reload (checked
in a headless Chrome or Edge with the page's AJAX answered locally; skipped without one).
"""
import re

import pytest

from models.db import get_db
from models.item import Item
from test_collection_card_height_in_browser import BROWSER, head_heights  # noqa: F401
from test_collection_prices import page, view_chunks

# name: (owned_rank, target_rank, mod_max_rank, expected width %, expected title)
CASES = {
    'Below Target':     (2, 4, 5, 50, 'Rank 2 of target 4'),
    'At Target':        (4, 4, 5, 100, 'Rank 4 of target 4'),
    'Above Target':     (5, 3, 5, 100, 'Rank 5 of target 3'),
    'Target Is Max':    (1, 5, 5, 20, 'Rank 1 of target 5'),
    'Rounds Down':      (1, 3, 5, 33, 'Rank 1 of target 3'),
    'Target Zero':      (3, 0, 5, 60, 'Rank 3 of max 5'),
    'Target Null':      (2, None, 5, 40, 'Rank 2 of max 5'),
    'Nothing Owned':    (None, None, 5, 0, 'Rank 0 of max 5'),
    'Owned At Max':     (5, None, 5, 100, 'Rank 5 of max 5'),
    'Max Zero':         (0, 0, 0, 0, 'Rank 0 of max 0'),
    'Max Null':         (0, None, None, 0, 'Rank 0 of max 0'),
    'Target, Max Null': (1, 2, None, 50, 'Rank 1 of target 2'),
}


def add_arcane(name, owned, target, max_rank):
    Item.upsert(name, 'arcane', source='standard', mod_max_rank=max_rank)
    db = get_db()
    iid = db.execute("SELECT id FROM items WHERE name = ? AND category = 'arcane'",
                     (name,)).fetchone()['id']
    uid = db.execute('SELECT id FROM users').fetchone()['id']
    db.execute('INSERT INTO user_progress (user_id, item_id, owned_rank, target_rank) '
               'VALUES (?, ?, ?, ?)', (uid, iid, owned, target))
    db.commit()
    return iid


def bar_of(chunk):
    """(width %, title, aria-label, aria-valuenow) of one card's rank bar."""
    track = re.search(r'<div class="arc-rank-track"[^>]*>', chunk)
    assert track is not None, chunk[:400]
    attr = lambda n: re.search(rf'\b{n}="([^"]*)"', track.group(0)).group(1)
    width = re.search(r'<div class="arc-rank-bar" style="width:(\d+)%', chunk)
    assert width is not None
    return int(width.group(1)), attr('title'), attr('aria-label'), int(attr('aria-valuenow'))


@pytest.fixture
def arcanes(app, admin_client):
    with app.app_context():
        return {name: add_arcane(f'Arcane {name}', *case[:3]) for name, case in CASES.items()}


def test_each_card_bar_measures_owned_against_target_or_max(admin_client, arcanes):
    chunks = view_chunks(page(admin_client, 'arcane'), 'cards')
    for name, (owned, _target, max_rank, pct, title) in CASES.items():
        chunk = chunks[arcanes[name]]
        assert bar_of(chunk) == (pct, title, title, pct), name
        # The label beside the buttons still says owned against max.
        assert f'>{owned or 0}/{max_rank or 0}</span>' in chunk, name


def test_the_table_view_has_no_rank_bar(admin_client, arcanes):
    chunks = view_chunks(page(admin_client, 'arcane'), 'table')
    for name in CASES:
        assert 'arc-rank-bar' not in chunks[arcanes[name]], name


# ── The page's script: both handlers repaint the bar through one helper ────────

PROBE = """<script>
window.addEventListener('load', function () { setTimeout(function () {
  // jQuery runs the page's ready handlers after load's turn: bind first, then act.
  // Answer the page's AJAX locally, as the server does for a rank or a target in range.
  $.ajax = function (o) {
    var d = JSON.parse(o.data);
    o.success('owned_rank' in d ? {ok: true, owned_rank: d.owned_rank}
                                : {ok: true, target_rank: d.target_rank});
  };
  var card = function (name) {
    return $('#collection-cards .coll-col').filter(function () {
      return $(this).find('.coll-card-name').text() === name;
    });
  };
  card('Arcane Rank Up').find('.arc-rank-btn[data-delta="1"]').trigger('click');
  card('Arcane Rank Down').find('.arc-rank-btn[data-delta="-1"]').trigger('click');
  card('Arcane Set Target').find('.arc-target-input').val(4).trigger('change');
  setTimeout(function () {   // the target input's debounce is 600 ms
    card('Arcane Clear Target').find('.arc-target-input').val(0).trigger('change');
    setTimeout(function () {
      var rows = [];
      $('#collection-cards .coll-col').each(function () {
        var $t = $(this).find('.arc-rank-track');
        rows.push($(this).find('.coll-card-name').text() + '=' +
                  $(this).find('.arc-rank-bar')[0].style.width + '|' + $t.attr('title') + '|' +
                  $t.attr('aria-label') + '|' + $t.attr('aria-valuenow'));
      });
      var pre = document.createElement('pre');
      pre.id = 'heads';
      pre.textContent = rows.join('\\n');
      document.body.appendChild(pre);
    }, 1000);
  }, 1000);
}, 200); });
</script>"""

# name: (owned, target, max) before, and the bar after the page's change
SCRIPT_CASES = {
    'Arcane Rank Up':      ((2, 4, 5), ('75%', 'Rank 3 of target 4')),
    'Arcane Rank Down':    ((3, None, 5), ('40%', 'Rank 2 of max 5')),
    'Arcane Set Target':   ((2, 0, 5), ('50%', 'Rank 2 of target 4')),
    'Arcane Clear Target': ((3, 4, 5), ('60%', 'Rank 3 of max 5')),
}


@pytest.mark.skipif(BROWSER is None, reason='no headless Chrome or Edge installed')
def test_a_rank_or_target_change_repaints_the_bar_without_a_reload(
        app, admin_client, tmp_path, monkeypatch):
    import test_collection_card_height_in_browser as browser
    with app.app_context():
        for name, (before, _after) in SCRIPT_CASES.items():
            add_arcane(name, *before)
    monkeypatch.setattr(browser, 'PROBE', PROBE)
    shown = head_heights(app, page(admin_client, 'arcane'), tmp_path, 1366)
    expected = {name: f'{width}|{title}|{title}|{width[:-1]}'
                for name, (_before, (width, title)) in SCRIPT_CASES.items()}
    assert shown == expected


def test_the_two_handlers_paint_the_bar_through_one_helper(app, admin_client):
    with app.app_context():
        add_arcane('Arcane One', 1, 2, 5)
    script = page(admin_client, 'arcane')
    script = script[script.index('function arcaneBar('):]
    # Only the helper writes the bar's width.
    assert script.count(".arc-rank-bar').css('width'") == 1
    rank = script[script.index('function updateArcaneRank('):]
    rank = rank[:rank.index('\n}\n')]
    target = script[script.index("'.arc-target-input', function"):]
    target = target[:target.index('}, 600);')]
    assert 'paintArcaneBar(' in rank
    assert 'paintArcaneBar(' in target
